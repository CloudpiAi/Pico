"""Run the catalog cases through the Slack web app (a SlackPage) and record every check in a RunLog.

One case: render it (a missing value is SKIP), open its place (the DM, a test channel with an @mention, or the thread
of an earlier case), send, wait for the done/error mark, read the bot's replies, judge them (checks.judge_reply) and,
for a task, confirm it with the task list and watch its delivery (`deliver`) or its silence (`silent`). A screenshot
of the message pane is taken when a case fails.

The bot does not reliably repeat a new task's id, so ids come from the task list: it is read before the run, around
each case that creates a task, and after the run. Clean-up removes the tasks that are new since the start and still
open (a one-time task closes itself) and then reads the list again: a task still open is a FAIL. Tasks you had before
the run are never touched. Clean-up also runs after a failure or Ctrl-C.

Replies are never counted twice: every bot message read for a case is remembered (`consumed`). After a timeout the
runner waits once more (SETTLE_S) so a late answer can't land in the next case.
"""

from __future__ import annotations

import os
import re
import time
from typing import Callable, Optional

from tests.e2e.standalone_ui import catalog
from tests.e2e.standalone_ui.answer_check import match_figures
from tests.e2e.standalone_ui.catalog import CatalogError, MissingVar
from tests.e2e.standalone_ui.checks import (
    EMPTY_ANSWERS,
    FAIL_PHRASES,
    Check,
    already_closed,
    judge_reply,
    new_tasks,
    parse_task_list,
    task_footer,
    to_remove,
)
from tests.e2e.standalone_ui.page import SignedOut

LIST_TEXT = "List all my scheduled tasks with their ids."
REMOVE_TEXT = "Please remove task {task}."
SETTLE_S = 120.0  # after a timeout, how long to wait for the late answer before the next message
POLL_S = 10.0  # delivery / silence polling
BEFORE_ID = "_tasks_before"
LIST_TRIES = 3  # asks for the task list at most this many times when the answer is empty or unreadable
CLEANUP_ID = "_cleanup"

__all__ = ["Runner", "SignedOut", "LIST_TEXT", "REMOVE_TEXT", "SETTLE_S"]


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.@-]", "_", name)


class _Asked:
    def __init__(self, ts: str, state: str, msgs: list):
        self.ts, self.state, self.msgs = ts, state, msgs

    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.msgs]

    @property
    def text(self) -> str:
        return "\n".join(self.texts)


class Runner:
    def __init__(self, page, cfg, truth, vars_: dict, log, *, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.page, self.cfg, self.truth, self.log = page, cfg, truth, log
        self.vars = dict(vars_)
        self.sleep, self.clock = sleep, clock
        self.dm: Optional[str] = cfg.bot_dm or None
        self.at: Optional[str] = None  # the channel open in the page
        self.threads: dict[str, tuple[str, str]] = {}  # case id -> (channel, thread root ts)
        self.tasks: dict[str, str] = {}  # case id -> the task id it created
        self.consumed: set[str] = set()  # bot message ts already counted
        self.before: Optional[dict] = None  # the task list before the run (None: unreadable)
        self.known: Optional[dict] = None  # the newest task list read
        self._failed = False

    # -- the run --

    def run(self, cases) -> None:
        cases = catalog.expand_channels(list(cases), list(self.cfg.test_channels or ()))
        makes_tasks = any(c.expect.task in ("created", "none") for c in cases)
        self.names = self._channel_names()
        try:
            if makes_tasks:
                self._tasks_before()
            for case in cases:
                self._guarded(case)
        finally:
            if makes_tasks:
                self._cleanup()

    def _add(self, status: str, name: str, detail: str = "") -> None:
        self._check(Check(status, name, detail))

    def _check(self, check) -> None:
        if check.status == "FAIL":
            self._failed = True
        self.log.add(check)

    def _guarded(self, case) -> None:
        start = self.clock()
        self._failed = False
        r, problem = None, None
        try:
            r = self._render(case)
        except MissingVar as e:
            problem = ("SKIP", f"needs a value this run doesn't have: {e}")
        except CatalogError as e:
            problem = ("FAIL", f"catalog: {e}")
        self.log.case_start(case.id, r.text if r else case.text, group=case.group, where=case.where)
        try:
            if problem:
                self._add(problem[0], case.id, problem[1])
            else:
                self._case(r)
        except KeyboardInterrupt:
            self._add("FAIL", f"{case.id} stopped", "Ctrl-C")
            raise
        except SignedOut as e:
            self._add("FAIL", f"{case.id} session", f"signed out: {e} - run the login script again")
            raise
        except CatalogError as e:
            self._add("FAIL", case.id, f"catalog: {e}")
        except Exception as e:  # a page error fails this case only
            self._add("FAIL", case.id, f"{type(e).__name__}: {e}")
        finally:
            if self._failed:
                self._screenshot(case.id)
            self.log.case_end(case.id, self.clock() - start)

    def _channel_names(self) -> dict:
        """Channel id -> name from the sidebar, so cases can type '#cost-reports' like a person (a '<#C…>' id made the
        agent's shell guard refuse the command, live 2026-11-11). Unknown names make those cases SKIP."""
        try:
            self._open_dm()
            names = dict(self.page.channel_names())
        except (KeyboardInterrupt, SignedOut):
            raise
        except Exception as e:
            self.log.note(f"channel names not read from the sidebar: {type(e).__name__}: {e}")
            return {}
        self.log.note("channels: " + ", ".join(f"{c}=#{names[c]}" for c in names
                                               if c in (*self.cfg.test_channels, self.cfg.outside_channel)))
        return names

    def _render(self, case):
        vars_ = {**self.vars, "tasks": dict(self.tasks)}
        names = getattr(self, "names", {}) or {}
        if case.channel:
            vars_["test_channel"] = case.channel
            if case.channel in names:
                vars_["test_channel_name"] = names[case.channel]
        if self.cfg.outside_channel and self.cfg.outside_channel in names:
            vars_["outside_channel_name"] = names[self.cfg.outside_channel]
        return catalog.render(case, vars_)

    def _screenshot(self, case_id: str) -> None:
        path = os.path.join(self.log.dir, "shots", f"{_safe(case_id)}.png")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self.page.screenshot(path)
            self.log.screenshot(case_id, path)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            self.log.note(f"no screenshot for {case_id}: {type(e).__name__}: {e}")

    # -- the page --

    def _open(self, channel: str) -> None:
        if self.at != channel:
            self.page.open(channel)
            self.at = channel

    def _open_dm(self) -> str:
        if self.dm:
            self._open(self.dm)
        else:
            self.dm = self.page.open_bot_dm()
            self.at = self.dm
        return self.dm

    def _ask(self, text: str, timeout_s: float, *, want_reaction: bool = True, in_thread: bool = False,
             reply_in_thread: bool = False) -> _Asked:
        """Send, wait, settle after a timeout, and read the bot's replies (each counted once)."""
        ts = self.page.send_in_thread(text) if in_thread else self.page.send(text)
        state = self.page.wait_done(ts, timeout_s, want_reaction=want_reaction, in_thread=in_thread)
        if state == "timeout" and want_reaction:
            self.log.note(f"no done mark after {timeout_s:.0f}s: waiting up to {SETTLE_S:.0f}s for a late answer")
            late = self.page.wait_done(ts, SETTLE_S, want_reaction=True, in_thread=in_thread)
            self.log.note(f"after the extra wait: {late}")
        exclude = frozenset(self.consumed)
        if in_thread:
            msgs = list(self.page.thread_replies_after(ts, exclude=exclude))
        else:
            msgs = list(self.page.replies_after(ts, exclude=exclude))
            if reply_in_thread:  # a channel answer goes in the thread of my message
                try:
                    self.page.open_thread(ts)
                    msgs += [m for m in self.page.thread_replies_after(ts, exclude=exclude)
                             if m.ts not in {x.ts for x in msgs}]
                except (KeyboardInterrupt, SignedOut):
                    raise
                except Exception as e:
                    self.log.note(f"no thread under {ts}: {type(e).__name__}: {e}")
        self.consumed.update(m.ts for m in msgs)
        return _Asked(ts, state, msgs)

    # -- one case --

    def _case(self, r) -> None:
        exp = r.expect
        if r.action == "read_latest":
            self._read_latest(r)
            return
        timeout = max(float(r.timeout_s), float(self.cfg.answer_timeout_s))
        want = exp.reaction != "none"
        prev = None
        if exp.task in ("created", "none"):
            prev = self.known if self.known is not None else self._list()
        if r.where == "dm":
            ch = self._open_dm()
            asked = self._ask(r.text, timeout, want_reaction=want)
            self.threads[r.id] = (ch, asked.ts)
        elif r.where == "channel":
            if not r.channel:
                self._add("SKIP", r.id, "no test channel (E2E_TEST_CHANNELS)")
                return
            ch = r.channel
            self._open(ch)
            asked = self._ask(f"@{self.cfg.bot_name} {r.text}", timeout, want_reaction=want, reply_in_thread=True)
            self.threads[r.id] = (ch, asked.ts)
        else:  # thread
            parent = self.threads.get(r.after)
            if not parent:
                self._add("SKIP", r.id, f"{r.after} did not run")
                return
            ch, root = parent
            self._open(ch)
            self.page.open_thread(root)
            asked = self._ask(r.text, timeout, want_reaction=want, in_thread=True)
            self.threads[r.id] = (ch, root)
        self.log.reply(r.id, asked.texts)

        figures_on_post = bool(exp.deliver and exp.deliver.get("figures"))
        truth_values = None
        if r.truth and not figures_on_post:
            truth_values = self._truth(r)
        truth_texts = self._truth_texts(r) if r.truth_text else None
        for check in judge_reply(r.id, exp, asked.state, asked.texts, truth_values=truth_values,
                                 truth_texts=truth_texts, kinds=r.kinds,
                                 tol_pct=r.tol_pct, ignore_sign=r.ignore_sign):
            self._check(check)
        if exp.task:
            tid = self._task(r, prev)
            if tid and exp.deliver:
                self._deliver(r, ch, asked.ts, tid)
            if tid and exp.silent:
                self._silent(r, asked.ts, tid)

    def _truth_texts(self, r) -> Optional[dict]:
        values = {}
        for name, sql in r.truth_text.items():
            try:
                values[name] = self.truth.text(sql)
            except Exception as e:  # a bad SQL in the catalog fails this case's names
                self._add("FAIL", f"{r.id} names", f"truth_text SQL for {name} failed: {type(e).__name__}: {e}")
                return None
        return values

    def _truth(self, r) -> Optional[dict]:
        values = {}
        for name, sql in r.truth.items():
            try:
                values[name] = self.truth.value(sql)
            except Exception as e:  # a bad SQL in the catalog fails this case's figures
                self._add("FAIL", f"{r.id} figures", f"truth SQL for {name} failed: {type(e).__name__}: {e}")
                return None
        return values

    # -- tasks --

    def _list(self, tries: int = LIST_TRIES) -> Optional[dict]:
        """The task list (id -> status), or None when it can't be read. The bot sometimes answers
        "(No response generated)" (seen live 2026-11-11), so an empty or unreadable answer is asked again."""
        self._open_dm()
        for attempt in range(1, tries + 1):
            asked = self._ask(LIST_TEXT, float(self.cfg.task_timeout_s))
            self.log.note(f"task list (try {attempt}): {asked.state}: {' '.join(asked.text.split())[:300]}")
            if asked.state != "done" or any(t.strip() in EMPTY_ANSWERS for t in asked.texts):
                continue
            try:
                tasks = parse_task_list(asked.text)
            except ValueError:
                continue
            self.known = tasks
            return tasks
        return None

    def _tasks_before(self) -> None:
        start = self.clock()
        self._failed = False
        self.log.case_start(BEFORE_ID, LIST_TEXT, group="tasks", where="dm")
        try:
            self.before = self._list()
            if self.before is None:
                self._add("SKIP", "tasks before", "couldn't read your task list: only the tasks this run finds "
                                                  "around its own cases are removed at the end")
            else:
                self._add("PASS", "tasks before", ", ".join(sorted(self.before)) or "none")
        except (KeyboardInterrupt, SignedOut):
            raise
        except Exception as e:
            self._add("SKIP", "tasks before", f"{type(e).__name__}: {e}")
        finally:
            self.log.case_end(BEFORE_ID, self.clock() - start)

    def _task(self, r, prev: Optional[dict]) -> Optional[str]:
        """Confirm the task change with the list; returns the id a `created` case made."""
        exp, cid = r.expect, r.id
        after = self._list()
        if exp.task in ("created", "none"):
            if prev is None or after is None:
                self._add("FAIL", f"{cid} task" if exp.task == "created" else f"{cid} no task",
                          "couldn't read the task list before and after this case")
                return None
            new = new_tasks(prev, after)
            if exp.task == "none":
                self._add("FAIL" if new else "PASS", f"{cid} no task",
                          f"a task was saved: {', '.join(new)}" if new else "no new task")
                return None
            if not new:
                self._add("FAIL", f"{cid} task", "no new task in your task list after this case")
                return None
            tid = new[-1]
            self.tasks[cid] = tid
            more = f" (also new: {', '.join(new[:-1])})" if len(new) > 1 else ""
            self._add("PASS", f"{cid} task", f"{tid} ({after.get(tid)}){more}")
            return tid
        tid = self.tasks.get(exp.task_of)
        name = f"{cid} task {exp.task}"
        if not tid:
            self._add("SKIP", name, f"{exp.task_of} created no task")
        elif after is None:
            self._add("FAIL", name, "couldn't read the task list after this case")
        elif exp.task == "removed":
            gone = after.get(tid, "done") == "done"
            self._add("PASS" if gone else "FAIL", name, f"{tid} {'gone' if gone else 'still ' + after[tid]}")
        else:
            here = tid in after
            self._add("PASS" if here else "FAIL", name, f"{tid} {after[tid]}" if here else f"{tid} not in the list")
        return None

    def _target(self, r, ch: str) -> Optional[str]:
        where = r.expect.deliver.get("channel", "here")
        if where == "dm":
            return self.dm
        if where == "test_channel":
            return r.channel or (self.cfg.test_channels[0] if self.cfg.test_channels else None)
        return ch

    def _deliver(self, r, ch: str, ts: str, tid: str) -> None:
        name = f"{r.id} delivered"
        target = self._target(r, ch)
        if not target:
            self._add("SKIP", name, "no test channel (E2E_TEST_CHANNELS)")
            return
        patterns = r.expect.deliver.get("contains", [])
        within = max(float(r.expect.deliver["within_s"]), float(self.cfg.deliver_timeout_s))
        deadline = self.clock() + within
        self.log.note(f"waiting up to {within:.0f}s for {tid} to post in {target}")
        while True:
            self.page.open(target)  # reload: a post can land while the page shows another channel
            self.at = target
            for m in self.page.posts_after(ts):
                if m.ts in self.consumed:
                    continue
                if (m.task_id or task_footer(m.text)) != tid:
                    continue
                if all(re.search(p, m.text) for p in patterns):
                    self.consumed.add(m.ts)
                    self.log.reply(r.id, [m.text])
                    self._add("PASS", name, f"{tid} in {target}: {' '.join(m.text.split())[:160]}")
                    if r.expect.deliver.get("figures") and r.truth:
                        self._post_figures(r, m.text)
                    return
            if self.clock() >= deadline:
                self._add("FAIL", name, f"no post from {tid} matching {patterns} in {target} within {within:.0f}s")
                return
            self.sleep(POLL_S)

    def _post_figures(self, r, text: str) -> None:
        values = self._truth(r)
        if values is None:
            return
        known = {k: v for k, v in values.items() if v is not None}
        if not known:
            self._add("SKIP", f"{r.id} figures", "no expected value in the data copy")
            return
        missing = match_figures(text, known, tol_pct=r.tol_pct, kinds=r.kinds, ignore_sign=r.ignore_sign)
        self._add("FAIL" if missing else "PASS", f"{r.id} figures",
                  ("missing: " + ", ".join(f"{k}={known[k]:g}" for k in missing)) if missing else
                  ", ".join(f"{k}={v:g}" for k, v in known.items()))

    def _silent(self, r, ts: str, tid: str) -> None:
        name = f"{r.id} silent"
        for_s = float(r.expect.silent["for_s"])
        deadline = self.clock() + for_s
        self.log.note(f"watching the DM for {for_s:.0f}s: {tid} must not post")
        while True:
            self._open_dm()
            for m in self.page.posts_after(ts):
                if m.ts in self.consumed:
                    continue
                owner = m.task_id or task_footer(m.text)
                if owner in (None, tid):
                    self.consumed.add(m.ts)
                    self.log.reply(r.id, [m.text])
                    self._add("FAIL", name, f"the bot posted: {' '.join(m.text.split())[:200]}")
                    return
            if self.clock() >= deadline:
                self._add("PASS", name, f"nothing posted in {for_s:.0f}s")
                return
            self.sleep(POLL_S)

    def _read_latest(self, r) -> None:
        name = f"{r.id} notice"
        if not self.cfg.notify_channel:
            self._add("SKIP", name, "no notify channel (E2E_NOTIFY_CHANNEL)")
            return
        self._open(self.cfg.notify_channel)
        bots = [m for m in self.page.messages() if m.is_bot]
        if not bots:
            self._add("FAIL", name, "no message from the bot in the notify channel")
            return
        text, exp = bots[-1].text, r.expect
        self.log.reply(r.id, [text])
        ok = all(re.search(p, text) for p in exp.contains) and (
            not exp.contains_any or any(re.search(p, text) for p in exp.contains_any))
        self._add("PASS" if ok else "FAIL", name, " ".join(text.split())[:200])

    # -- clean-up --

    def _cleanup(self) -> None:
        start = self.clock()
        self._failed = False
        self.log.case_start(CLEANUP_ID, "remove the tasks this run created", group="tasks", where="dm")
        todo: list[str] = []
        replies: dict[str, tuple[str, str]] = {}
        try:
            after = self._list()
            if self.before is not None and after is not None:
                todo = to_remove(self.before, after)
            else:
                todo = [t for t in dict.fromkeys(self.tasks.values())
                        if after is None or after.get(t, "done") != "done"]
                self.log.note("the task list before or after could not be read: removing only the ids found "
                              f"around this run's cases: {', '.join(todo) or 'none'}")
            for i, tid in enumerate(todo):
                try:
                    self._open_dm()
                    asked = self._ask(REMOVE_TEXT.format(task=tid), float(self.cfg.task_timeout_s))
                    replies[tid] = (asked.state, asked.text)
                    self.log.reply(CLEANUP_ID, asked.texts)
                except KeyboardInterrupt:
                    self._add("FAIL", "clean-up stopped", f"remove these by hand: {', '.join(todo[i:])}")
                    raise
                except SignedOut:
                    raise
                except Exception as e:
                    replies[tid] = ("error", f"{type(e).__name__}: {e}")
            final = self._list() if todo else after
            for tid in todo:
                state, text = replies.get(tid, ("error", ""))
                if final is not None:
                    ok = final.get(tid, "done") == "done"
                    why = "gone from the list" if ok else f"still {final[tid]} - remove it by hand"
                else:
                    ok = already_closed(text) or (state == "done" and bool(text.strip())
                                                  and text.strip() not in EMPTY_ANSWERS
                                                  and not FAIL_PHRASES.search(text))
                    why = f"{state}: {' '.join(text.split())[:160]}" + ("" if ok else " - remove it by hand")
                self._add("PASS" if ok else "FAIL", f"clean-up {tid}", why)
        except KeyboardInterrupt:
            if not any(c.name == "clean-up stopped" for c in self.log.cases[CLEANUP_ID].checks):
                left = todo or list(self.tasks.values())
                self._add("FAIL", "clean-up stopped", f"remove these by hand: {', '.join(left) or 'none known'}"
                                                      " - and check your task list")
            raise
        except Exception as e:  # incl. SignedOut: nothing more can be removed from here
            left = [t for t in (todo or list(self.tasks.values()))]
            self._add("FAIL", "clean-up", f"{type(e).__name__}: {e} - remove these by hand: "
                                          f"{', '.join(left) or 'none known'}")
        finally:
            self.log.case_end(CLEANUP_ID, self.clock() - start)
