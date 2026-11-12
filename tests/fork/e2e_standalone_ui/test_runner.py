"""The runner drives each case through the page model and reports (tests/e2e/standalone_ui/runner.py).

FakePage implements the SlackPage methods of CONTRACTS.md over in-memory channels; Bot plays the agent, with a task
store that answers the task list like the real bot ("• *t14*: ... • Status: Active").
"""

import os
import re
from collections import defaultdict
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from tests.e2e.standalone_ui import catalog, runner
from tests.e2e.standalone_ui.report import RunLog
from tests.e2e.standalone_ui.runner import LIST_TEXT, SETTLE_S, Runner


@dataclass(frozen=True)
class M:
    ts: str
    sender: str
    sender_id: str
    is_bot: bool
    text: str
    reactions: tuple = ()
    task_id: str = None


@dataclass
class Resp:
    texts: tuple = ("Your AWS cost was $10.00.",)
    states: tuple = ("done",)
    late: tuple = ()  # posted when the late answer arrives (after the settle wait, or at the next message)
    posts: tuple = ()  # (channel, text, task id): scheduled posts


class Bot:
    """The agent: rules (regex -> Resp or callable(bot, text) -> Resp), a task store, the list and remove texts."""

    def __init__(self, tasks=None, rules=(), honours_remove=True):
        self.tasks = dict({"t2": "paused"} if tasks is None else tasks)
        self.rules = list(rules)
        self.n = 10
        self.honours_remove = honours_remove

    def create(self, status="active"):
        self.n += 1
        tid = f"t{self.n}"
        self.tasks[tid] = status
        return tid

    def list_text(self):
        live = [(t, s) for t, s in self.tasks.items() if s != "done"]
        if not live:
            return "No tasks scheduled."
        return "Here are your scheduled tasks: " + " ".join(
            f"• *{t}*: Something (Reminder) • Status: {s.capitalize()} • Schedule: Daily •" for t, s in live)

    def __call__(self, text, place):
        if text == LIST_TEXT:
            return Resp(texts=(self.list_text(),))
        m = re.match(r"Please remove task (t\d+)\.", text)
        if m:
            tid = m.group(1)
            if self.tasks.get(tid) == "done":
                return Resp(texts=(f"Task {tid} has already run (completed).",))
            if self.honours_remove:
                self.tasks.pop(tid, None)
            return Resp(texts=("(No response generated)",))  # the real bot does this; the list decides
        for pat, resp in self.rules:
            if re.search(pat, text):
                return resp(self, text) if callable(resp) else resp
        return Resp()


class FakePage:
    def __init__(self, bot, dm="D1"):
        self.bot, self.dm = bot, dm
        self.chans = defaultdict(list)
        self.threads = defaultdict(list)
        self.states, self.late = {}, {}
        self.n = 100
        self.at, self.thread = None, None
        self.sent, self.opened, self.shots, self.waits = [], [], [], []
        self.fail_on = {}  # text pattern -> exception raised by send
        self.names = {"C1": "test-channel"}  # channel id -> name, as the sidebar shows them

    def channel_names(self):
        return dict(self.names)

    def _ts(self):
        self.n += 1
        return f"{self.n}.000100"

    def _post(self, lst, text, task_id=None):
        lst.append(M(self._ts(), "Pico FinOps Agent", "UBOT", True, text, (), task_id))

    def _flush(self, ts):
        target, texts = self.late.get(ts, (None, []))
        for t in texts:
            self._post(target, t)
        texts.clear()

    def open(self, channel_id):
        self.at, self.thread = channel_id, None
        self.opened.append(channel_id)

    def open_bot_dm(self):
        self.open(self.dm)
        return self.dm

    def messages(self):
        return list(self.chans[self.at])

    def _send(self, text, lst, place, in_thread):
        for pat, exc in self.fail_on.items():
            if re.search(pat, text):
                raise exc
        ts = self._ts()
        lst.append(M(ts, "me", "UME", False, text))
        for pending in list(self.late):  # a late answer lands after my next message
            self._flush(pending)
        self.sent.append((place, text))
        r = self.bot(text, place)
        target = self.threads[(self.at, ts)] if (self.at.startswith("C") and not in_thread) else lst
        for t in r.texts:
            self._post(target, t)
        for ch, t, tid in r.posts:
            self._post(self.chans[ch], t, tid)
        self.states[ts] = list(r.states)
        self.late[ts] = (target, list(r.late))
        return ts

    def send(self, text):
        return self._send(text, self.chans[self.at], self.at, False)

    def wait_done(self, my_ts, timeout_s, *, want_reaction=True, in_thread=False):
        self.waits.append((my_ts, timeout_s))
        states = self.states[my_ts]
        st = states.pop(0) if len(states) > 1 else states[0]
        if st != "timeout":
            self._flush(my_ts)
        return st

    @staticmethod
    def _after(lst, ts, until_ts=None, exclude=frozenset(), posts=True):
        return [m for m in lst if m.is_bot and float(m.ts) > float(ts) and m.ts not in exclude
                and (until_ts is None or float(m.ts) < float(until_ts)) and (posts or m.task_id is None)]

    def replies_after(self, my_ts, *, until_ts=None, exclude=frozenset()):
        return self._after(self.chans[self.at], my_ts, until_ts, exclude, posts=False)

    def posts_after(self, ts):
        return self._after(self.chans[self.at], ts)

    def open_thread(self, root_ts):
        self.thread = (self.at, root_ts)

    def send_in_thread(self, text):
        return self._send(text, self.threads[self.thread], f"thread:{self.at}", True)

    def thread_replies_after(self, my_ts, exclude=frozenset()):
        return self._after(self.threads[self.thread], my_ts, None, exclude, posts=False)

    def screenshot(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"png")
        self.shots.append(path)


class FakeTruth:
    def __init__(self, values=None, texts=None):
        self.values = dict(values or {})
        self.texts = dict(texts or {})

    def text(self, sql):
        for key, v in self.texts.items():
            if key in sql:
                return v
        return None

    def value(self, sql):
        for key, v in self.values.items():
            if key in sql:
                if isinstance(v, Exception):
                    raise v
                return v
        return None

    def vars(self):
        return {"prev_month_name": "October 2026", "top_service": "Amazon EC2"}

    def close(self):
        pass


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def _cfg(**kw):
    base = dict(bot_name="Pico FinOps Agent", bot_dm=None, bot_user="UBOT", me="UME", test_channels=("C1",),
                outside_channel=None, notify_channel="CN", answer_timeout_s=5.0, task_timeout_s=7.0,
                deliver_timeout_s=30.0)
    base.update(kw)
    return SimpleNamespace(**base)


def _cases(tmp_path, body):
    p = tmp_path / "c.yaml"
    p.write_text("cases:\n" + body, encoding="utf-8")
    return catalog.load(str(p))


@pytest.fixture
def world(tmp_path):
    def make(body, bot=None, truth=None, cfg=None, vars_=None):
        bot = bot or Bot()
        page = FakePage(bot)
        clock = Clock()
        log = RunLog(str(tmp_path / "run"))
        r = Runner(page, cfg or _cfg(), truth or FakeTruth(), vars_ or {"prev_month_name": "October 2026"}, log,
                   sleep=clock.sleep, clock=clock)
        return SimpleNamespace(runner=r, page=page, bot=bot, log=log, clock=clock,
                               cases=_cases(tmp_path, body))
    return make


def _names(log, cid):
    return {c.name: c for c in log.cases[cid].checks}


# --- questions ---------------------------------------------------------------------------------------------------

def test_a_dm_question_passes_with_figures_and_is_logged(world):
    w = world('  - {id: qa_a, group: qa, where: dm, text: "Spend in {{ prev_month_name }}?",'
              ' truth: {total: "SELECT total"}}\n', truth=FakeTruth({"total": 10.0}))
    w.runner.run(w.cases)
    assert ("D1", "Spend in October 2026?") in w.page.sent
    rec = w.log.cases["qa_a"]
    assert rec.status == "PASS", rec.checks
    assert "qa_a figures" in _names(w.log, "qa_a")
    assert rec.text == "Spend in October 2026?"
    assert "$10.00" in rec.reply
    assert not w.page.shots  # no screenshot when nothing failed


def test_a_channel_case_mentions_the_bot_and_reads_the_thread_and_a_follow_up_goes_in_it(world):
    bot = Bot(rules=[("And S3", Resp(texts=("S3 was $3.00",)))])
    w = world('  - {id: ch, group: qa, where: channel, text: "EC2 cost?"}\n'
              '  - {id: th, group: qa, where: thread, after: ch, text: "And S3?", expect: {contains: ["S3"]}}\n',
              bot=bot)
    w.runner.run(w.cases)
    assert ("C1", "@Pico FinOps Agent EC2 cost?") in w.page.sent
    assert ("thread:C1", "And S3?") in w.page.sent
    assert w.log.cases["ch"].status == "PASS", w.log.cases["ch"].checks
    assert w.log.cases["th"].status == "PASS", w.log.cases["th"].checks


def test_channel_names_are_typed_like_a_person_does(world):
    w = world('  - {id: post, group: schedule, where: dm, text: "Post a report in #{{ test_channel_name }} at 9am"}\n'
              '  - {id: out, group: refusal, where: dm, text: "Post in #{{ outside_channel_name }}"}\n',
              cfg=_cfg(outside_channel="C9"))
    w.page.names = {"C1": "cost-reports", "C9": "no-bot"}
    w.runner.run(w.cases)
    assert ("D1", "Post a report in #cost-reports at 9am") in w.page.sent
    assert ("D1", "Post in #no-bot") in w.page.sent


def test_a_channel_without_a_known_name_skips(world):
    w = world('  - {id: post, group: schedule, where: dm, text: "Post in #{{ test_channel_name }}"}\n')
    w.page.names = {}
    w.runner.run(w.cases)
    assert w.log.cases["post"].status == "SKIP"


def test_channel_cases_skip_without_test_channels(world):
    w = world('  - {id: ch, group: qa, where: channel, text: "EC2 cost?"}\n', cfg=_cfg(test_channels=()))
    w.runner.run(w.cases)
    assert w.log.cases["ch"].status == "SKIP"
    assert not w.page.sent


def test_a_missing_value_is_skip_naming_it(world):
    w = world('  - {id: mtd, group: qa, where: dm, text: "So far in {{ mtd_month_name }}?"}\n')
    w.runner.run(w.cases)
    rec = w.log.cases["mtd"]
    assert rec.status == "SKIP" and "mtd_month_name" in rec.checks[0].detail
    assert not w.page.sent


def test_an_empty_answer_fails_and_takes_a_screenshot(world):
    bot = Bot(rules=[("hi", Resp(texts=("(No response generated)",)))])
    w = world('  - {id: qa_hi, group: qa, where: dm, text: "hi"}\n', bot=bot)
    w.runner.run(w.cases)
    rec = w.log.cases["qa_hi"]
    assert rec.status == "FAIL"
    assert len(w.page.shots) == 1 and rec.screenshot == w.page.shots[0]
    assert os.path.isfile(w.page.shots[0])


def test_after_a_timeout_the_late_answer_is_waited_for_and_not_counted_in_the_next_case(world):
    bot = Bot(rules=[("slow", Resp(texts=(), states=("timeout", "done"), late=("late answer about argentina",))),
                     ("next", Resp(texts=("Your AWS cost was $1.00",)))])
    w = world('  - {id: slow, group: qa, where: dm, text: "slow"}\n'
              '  - {id: nxt, group: qa, where: dm, text: "next", expect: {not_contains: ["argentina"]}}\n', bot=bot)
    w.runner.run(w.cases)
    assert w.log.cases["slow"].status == "FAIL"  # it did time out
    assert any(t == SETTLE_S for _, t in w.page.waits)
    assert w.log.cases["nxt"].status == "PASS", w.log.cases["nxt"].checks


def test_case_timeout_is_the_longer_of_catalog_and_config(world):
    w = world('  - {id: a, group: qa, where: dm, text: "x", timeout_s: 2}\n', cfg=_cfg(answer_timeout_s=50.0))
    w.runner.run(w.cases)
    assert w.page.waits[0][1] == 50.0


def test_a_page_error_fails_only_that_case(world):
    w = world('  - {id: a, group: qa, where: dm, text: "boom"}\n'
              '  - {id: b, group: qa, where: dm, text: "fine"}\n')
    w.page.fail_on["boom"] = RuntimeError("selector [data-qa=message_input] not found")
    w.runner.run(w.cases)
    assert w.log.cases["a"].status == "FAIL"
    assert "message_input" in " ".join(c.detail for c in w.log.cases["a"].checks)
    assert w.log.cases["b"].status == "PASS"


def test_a_signed_out_session_stops_the_run(world):
    w = world('  - {id: a, group: qa, where: dm, text: "x"}\n  - {id: b, group: qa, where: dm, text: "y"}\n')
    w.page.fail_on["x"] = runner.SignedOut("the sign-in page is shown")
    with pytest.raises(runner.SignedOut):
        w.runner.run(w.cases)
    assert w.log.cases["a"].status == "FAIL"
    assert "b" not in w.log.cases


def test_read_latest_checks_the_newest_notice(world):
    w = world('  - {id: st, group: status, action: read_latest, where: notify,'
              ' expect: {contains_any: ["Fresh cost data is in", "No new AWS cost data"]}}\n')
    w.page._post(w.page.chans["CN"], "old: couldn't refresh")
    w.page._post(w.page.chans["CN"], "Fresh cost data is in: $2.73")
    w.runner.run(w.cases)
    assert w.log.cases["st"].status == "PASS", w.log.cases["st"].checks
    assert not w.page.sent


def test_read_latest_skips_without_a_notify_channel(world):
    w = world('  - {id: st, group: status, action: read_latest, where: notify}\n', cfg=_cfg(notify_channel=None))
    w.runner.run(w.cases)
    assert w.log.cases["st"].status == "SKIP"


# --- tasks -------------------------------------------------------------------------------------------------------

def _creates(bot, text):
    bot.create()
    return Resp(texts=("All set! Next run: Monday 9:00 AM.",))


TASKS = ('  - {id: add, group: schedule, where: dm, text: "Remind me", expect: {task: created}}\n'
         '  - {id: pause, group: schedule, where: dm, text: "Pause task {{ tasks.add }}.",'
         ' expect: {task: changed, task_of: add}}\n')


def test_a_created_task_is_found_in_the_list_and_removed_at_the_end(world):
    bot = Bot(rules=[("Remind me", _creates), ("Pause", Resp(texts=("Paused.",)))])
    w = world(TASKS, bot=bot)
    w.runner.run(w.cases)
    assert _names(w.log, "add")["add task"].status == "PASS"
    assert "t11" in _names(w.log, "add")["add task"].detail
    assert ("D1", "Pause task t11.") in w.page.sent
    assert ("D1", "Please remove task t11.") in w.page.sent
    assert not any("t2" in t for _, t in w.page.sent if t.startswith("Please remove"))  # not ours
    assert _names(w.log, "_cleanup")["clean-up t11"].status == "PASS"  # list confirms, despite the empty reply
    assert w.log.exit_code() == 0, {k: v.checks for k, v in w.log.cases.items()}


def test_a_created_task_that_is_not_in_the_list_fails(world):
    bot = Bot(rules=[("Remind me", Resp(texts=("All set!",)))])
    w = world('  - {id: add, group: schedule, where: dm, text: "Remind me", expect: {task: created}}\n', bot=bot)
    w.runner.run(w.cases)
    assert _names(w.log, "add")["add task"].status == "FAIL"


def test_a_task_still_active_after_removal_is_a_fail(world):
    bot = Bot(rules=[("Remind me", _creates)], honours_remove=False)
    w = world('  - {id: add, group: schedule, where: dm, text: "Remind me", expect: {task: created}}\n', bot=bot)
    w.runner.run(w.cases)
    chk = _names(w.log, "_cleanup")["clean-up t11"]
    assert chk.status == "FAIL" and "by hand" in chk.detail
    assert w.log.exit_code() == 1


def test_a_one_time_task_that_already_ran_is_not_removed(world):
    def once(bot, text):
        bot.create(status="done")
        return Resp(texts=("All set!",))
    bot = Bot(rules=[("In 2 minutes", once)])
    w = world('  - {id: once, group: schedule, where: dm, text: "In 2 minutes", expect: {task: created}}\n', bot=bot)
    w.runner.run(w.cases)  # t11 is closed already: the list doesn't show it, so there is nothing to remove
    assert not any(t.startswith("Please remove") for _, t in w.page.sent)


def test_removed_is_confirmed_with_the_list(world):
    def remove(bot, text):
        bot.tasks.pop(re.search(r"t\d+", text).group(0))
        return Resp(texts=("Done.",))
    bot = Bot(rules=[("Remind me", _creates), ("Remove task", remove)])
    w = world('  - {id: add, group: schedule, where: dm, text: "Remind me", expect: {task: created}}\n'
              '  - {id: rm, group: schedule, where: dm, text: "Remove task {{ tasks.add }}.",'
              ' expect: {task: removed, task_of: add}}\n', bot=bot)
    w.runner.run(w.cases)
    assert _names(w.log, "rm")["rm task removed"].status == "PASS"
    assert not any(t.startswith("Please remove") for _, t in w.page.sent)


def test_a_task_saved_by_a_refusal_case_fails_and_is_cleaned_up(world):
    bot = Bot(rules=[("outside", _creates)])
    w = world('  - {id: ref, group: refusal, where: dm, text: "Post in outside",'
              ' expect: {reaction: any, task: none}}\n', bot=bot)
    w.runner.run(w.cases)
    assert _names(w.log, "ref")["ref no task"].status == "FAIL"
    assert ("D1", "Please remove task t11.") in w.page.sent


def test_an_unreadable_task_list_before_still_removes_the_ids_it_found(world):
    calls = {"n": 0}
    real = Bot.list_text

    def flaky(self):
        calls["n"] += 1
        return "Sorry, something went wrong." if calls["n"] == 1 else real(self)
    bot = Bot(rules=[("Remind me", _creates)])
    bot.list_text = flaky.__get__(bot)
    w = world('  - {id: add, group: schedule, where: dm, text: "Remind me", expect: {task: created}}\n', bot=bot)
    w.runner.run(w.cases)
    assert ("D1", "Please remove task t11.") in w.page.sent
    assert not any("t2." in t for _, t in w.page.sent if t.startswith("Please remove"))


def test_an_empty_task_list_answer_is_asked_again(world):
    # Live 2026-11-11: the bot answered the task-list question with "(No response generated)"; the next ask worked.
    calls = {"n": 0}
    real = Bot.list_text

    def flaky(self):
        calls["n"] += 1
        return "(No response generated)" if calls["n"] == 1 else real(self)
    bot = Bot(rules=[("Remind me", _creates)])
    bot.list_text = flaky.__get__(bot)
    w = world('  - {id: add, group: schedule, where: dm, text: "Remind me", expect: {task: created}}\n', bot=bot)
    w.runner.run(w.cases)
    assert _names(w.log, "_tasks_before")["tasks before"].status == "PASS"


def test_ctrl_c_during_a_case_still_cleans_up(world):
    bot = Bot(rules=[("Remind me", _creates)])
    w = world('  - {id: add, group: schedule, where: dm, text: "Remind me", expect: {task: created}}\n'
              '  - {id: b, group: qa, where: dm, text: "stop here"}\n', bot=bot)
    w.page.fail_on["stop here"] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        w.runner.run(w.cases)
    assert ("D1", "Please remove task t11.") in w.page.sent


def test_ctrl_c_during_clean_up_names_the_tasks_left(world):
    bot = Bot(rules=[("Remind me", _creates)])
    w = world('  - {id: add, group: schedule, where: dm, text: "Remind me", expect: {task: created}}\n', bot=bot)
    w.page.fail_on["Please remove"] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        w.runner.run(w.cases)
    stopped = _names(w.log, "_cleanup")["clean-up stopped"]
    assert stopped.status == "FAIL" and "t11" in stopped.detail


# --- deliveries and silence --------------------------------------------------------------------------------------

def test_a_delivery_with_the_task_footer_passes(world):
    def once(bot, text):
        tid = bot.create()
        return Resp(texts=("All set!",), posts=(("D1", f"Reminder: check the e2e test · task {tid} · "
                                                      f"say 'stop task {tid}' to stop it", tid),))
    bot = Bot(rules=[("In 2 minutes", once)])
    w = world('  - {id: once, group: schedule, where: dm, text: "In 2 minutes",'
              ' expect: {task: created, deliver: {within_s: 60, contains: ["(?i)e2e test"]}}}\n', bot=bot)
    w.runner.run(w.cases)
    assert _names(w.log, "once")["once delivered"].status == "PASS"


def test_a_channel_delivery_checks_the_figures_on_the_post(world):
    def post(bot, text):
        tid = bot.create()
        return Resp(texts=("All set!",), posts=(("C1", f"October total $161.63, top Amazon EC2 · task {tid} · "
                                                      f"say 'stop task {tid}'", tid),))
    bot = Bot(rules=[("post", post)])
    w = world('  - {id: chp, group: schedule, where: dm, text: "post in <#{{ test_channel }}>",'
              ' truth: {total: "SELECT total"},'
              ' expect: {task: created, deliver: {within_s: 60, channel: test_channel, figures: true}}}\n',
              bot=bot, truth=FakeTruth({"total": 161.63}))
    w.runner.run(w.cases)
    assert ("D1", "post in <#C1>") in w.page.sent
    names = _names(w.log, "chp")
    assert names["chp delivered"].status == "PASS"
    assert names["chp figures"].status == "PASS"
    assert "C1" in w.page.opened


def test_no_delivery_in_time_is_a_fail_after_polling(world):
    bot = Bot(rules=[("In 2 minutes", _creates)])
    w = world('  - {id: once, group: schedule, where: dm, text: "In 2 minutes",'
              ' expect: {task: created, deliver: {within_s: 60}}}\n', bot=bot, cfg=_cfg(deliver_timeout_s=40.0))
    w.runner.run(w.cases)
    assert _names(w.log, "once")["once delivered"].status == "FAIL"
    assert sum(w.clock.slept) >= 60


def test_silent_passes_when_nothing_is_posted(world):
    bot = Bot(rules=[("only tell me", _creates)])
    w = world('  - {id: quiet, group: schedule, where: dm, text: "only tell me",'
              ' expect: {task: created, silent: {for_s: 300}}}\n', bot=bot)
    w.runner.run(w.cases)
    assert _names(w.log, "quiet")["quiet silent"].status == "PASS"
    assert sum(w.clock.slept) >= 300


def test_silent_fails_when_the_task_posts(world):
    def noisy(bot, text):
        tid = bot.create()
        return Resp(texts=("All set!",), posts=(("D1", f"It was more! · task {tid} · say 'stop task {tid}'", tid),))
    bot = Bot(rules=[("only tell me", noisy)])
    w = world('  - {id: quiet, group: schedule, where: dm, text: "only tell me",'
              ' expect: {task: created, silent: {for_s: 300}}}\n', bot=bot)
    w.runner.run(w.cases)
    assert _names(w.log, "quiet")["quiet silent"].status == "FAIL"


def test_a_truth_text_case_checks_the_names_in_the_reply(world):
    bot = Bot(rules=[(r"Biggest bucket", Resp(texts=("The biggest bucket was logs-bucket, at $0.04.",)))])
    w = world('  - {id: col_a, group: columns, where: dm, text: "Biggest bucket?",'
              ' truth: {cost: "SELECT cost"}, truth_text: {bucket: "SELECT bucket"}, covers: [resourceid]}\n',
              bot=bot, truth=FakeTruth({"cost": 0.04}, {"bucket": "logs-bucket"}))
    w.runner.run(w.cases)
    names = _names(w.log, "col_a")
    assert names["col_a names"].status == "PASS" and names["col_a figures"].status == "PASS"


def test_a_bad_truth_sql_fails_the_figures_check(world):
    w = world('  - {id: qa_a, group: qa, where: dm, text: "x", truth: {total: "SELECT broken"}}\n',
              truth=FakeTruth({"broken": RuntimeError("Binder Error")}))
    w.runner.run(w.cases)
    chk = _names(w.log, "qa_a")["qa_a figures"]
    assert chk.status == "FAIL" and "Binder Error" in chk.detail
