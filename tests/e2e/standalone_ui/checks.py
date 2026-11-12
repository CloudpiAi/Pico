"""Judge one case's bot reply, and read the bot's task list (ids and their status).

Pure functions over text: no Slack, no browser. `judge_reply` turns a case's outcome into PASS/FAIL/SKIP checks;
the task-list helpers let the runner find the tasks a run created and remove them afterwards.
"""

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from tests.e2e.standalone_ui.answer_check import match_figures

EMPTY_ANSWERS = ("(No response generated)",)

FAIL_PHRASES = re.compile(
    r"\b(?:couldn['’]t|could not|can['’]t|cannot|not found|no such|doesn['’]t exist|unable|failed)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Check:
    status: str  # "PASS" | "FAIL" | "SKIP"
    name: str
    detail: str = ""


# --- judging a reply ---------------------------------------------------------------------------------------------

_FINISHED_OK = {
    "done": ("done",),
    "error": ("error",),
    "any": ("done", "error", "replied"),
}


def _finished(case_id: str, reaction: str, state: str) -> Check:
    name = f"{case_id} finished"
    if reaction == "none":
        return Check("PASS", name, f"no reaction expected (state {state})")
    ok = _FINISHED_OK.get(reaction, (reaction,))
    if state in ok:
        return Check("PASS", name, f"state {state}")
    return Check("FAIL", name, f"expected {reaction}, got state {state}")


def _real_texts(texts: Sequence[str]) -> list[str]:
    return [t for t in (x.strip() for x in texts) if t and t not in EMPTY_ANSWERS]


def _reply(case_id: str, expect: Any, texts: Sequence[str]) -> Check:
    name = f"{case_id} reply"
    real = _real_texts(texts)
    if getattr(expect, "no_reply", False):
        if real:
            return Check("FAIL", name, f"expected no reply, got: {real[0][:120]!r}")
        return Check("PASS", name, "no reply, as expected")
    if real:
        return Check("PASS", name, f"{len(real)} message(s)")
    if any(t.strip() in EMPTY_ANSWERS for t in texts):
        return Check("FAIL", name, "the bot answered " + repr(EMPTY_ANSWERS[0]))
    return Check("FAIL", name, "no reply text")


def _search(pattern: str, text: str) -> bool:
    return re.search(pattern, text) is not None


def judge_reply(
    case_id: str,
    expect: Any,
    state: str,
    texts: Sequence[str],
    *,
    truth_values: Optional[Mapping[str, Optional[float]]] = None,
    truth_texts: Optional[Mapping[str, Optional[str]]] = None,
    kinds: Optional[Mapping[str, str]] = None,
    tol_pct: float = 1.0,
    ignore_sign: bool = False,
) -> list[Check]:
    """The checks for one case. `state`: "done" | "error" | "replied" | "timeout"; `texts`: the bot's replies."""
    text = "\n".join(texts)
    checks = [_finished(case_id, expect.reaction, state), _reply(case_id, expect, texts)]

    if expect.contains:
        missing = [p for p in expect.contains if not _search(p, text)]
        checks.append(Check("FAIL" if missing else "PASS", f"{case_id} contains",
                            ("missing: " + ", ".join(missing)) if missing else ""))
    if expect.not_contains:
        found = [p for p in expect.not_contains if _search(p, text)]
        checks.append(Check("FAIL" if found else "PASS", f"{case_id} not_contains",
                            ("found: " + ", ".join(found)) if found else ""))
    if expect.contains_any:
        hit = [p for p in expect.contains_any if _search(p, text)]
        checks.append(Check("PASS" if hit else "FAIL", f"{case_id} contains_any",
                            ("matched: " + ", ".join(hit)) if hit else "none of: " + ", ".join(expect.contains_any)))

    if truth_values:
        checks.append(_figures(case_id, text, truth_values, kinds, tol_pct, ignore_sign))
    if truth_texts:
        checks.append(_names(case_id, text, truth_texts))

    if expect.task in ("changed", "removed"):
        name = f"{case_id} refused"
        if already_closed(text):
            checks.append(Check("PASS", name, "the task had already run or stopped"))
        else:
            m = FAIL_PHRASES.search(text)
            checks.append(Check("FAIL", name, f"the bot said {m.group(0)!r}") if m else Check("PASS", name))
    return checks


def _names(case_id, text, truth_texts) -> Check:
    """Each expected name (a bucket, an account, a region) appears in the reply, ignoring case."""
    name = f"{case_id} names"
    known = {k: v for k, v in truth_texts.items() if v}
    if not known:
        return Check("SKIP", name, "no expected value in the data copy: " + ", ".join(truth_texts))
    low = text.casefold()
    missing = [f"{k}={v!r}" for k, v in known.items() if v.casefold() not in low]
    return Check("FAIL" if missing else "PASS", name, ("missing: " + ", ".join(missing)) if missing else "")


def _figures(case_id, text, truth_values, kinds, tol_pct, ignore_sign) -> Check:
    name = f"{case_id} figures"
    known = {k: v for k, v in truth_values.items() if v is not None}
    unknown = [k for k, v in truth_values.items() if v is None]
    if not known:
        return Check("SKIP", name, "no expected value in the data copy: " + ", ".join(unknown))
    missing = match_figures(text, known, tol_pct=tol_pct, kinds=kinds, ignore_sign=ignore_sign)
    parts = []
    if missing:
        parts.append("missing: " + ", ".join(f"{k}={known[k]:g}" for k in missing))
    if unknown:
        parts.append("not checked (no expected value): " + ", ".join(unknown))
    return Check("FAIL" if missing else "PASS", name, "; ".join(parts))


# --- the task list -----------------------------------------------------------------------------------------------

# "*t14*", "`t14`" or "t14:" (never "t3" inside "t3.micro")
_TASK_ID = re.compile(
    r"\*t(\d+)\b(?!\.\w)"            # *t7* and, since 2026-11-11 15:21, *t7 — Cost by service daily* (bold over the name)
    r"|`t(\d+)`|\bt(\d+):"
    r"|\btask\s+(?:id\s*:?\s*)?t(\d+)\b(?!\.\w)"  # "task t15", "(task ID: t22)"
    r"|(?<![\w.*`(])t(\d+)\s*[•·]\s*\**\s*Name\b",  # "t2 • Name: EC2 cost report" (live 2026-11-12), also on its own line
    re.IGNORECASE)
_NO_TASKS = re.compile(r"\bno (?:scheduled )?tasks\b|\bdon['’]t have any (?:scheduled )?tasks\b", re.IGNORECASE)
_STATUS_FIELD = re.compile(r"\bstatus\s*[:=]?\s*\**\s*([a-z]+)", re.IGNORECASE)
_STATUS_WORDS = {
    "active": "active", "enabled": "active", "running": "active", "scheduled": "active",
    "paused": "paused", "disabled": "paused",
    "completed": "done", "complete": "done", "done": "done", "finished": "done", "expired": "done",
    "stopped": "done", "cancelled": "done", "canceled": "done", "removed": "done", "deleted": "done",
}
_DONE_PROSE = re.compile(
    r"\bhas already run\b|\bcompleted\b|\balready stopped\b|\bfinished\b"
    r"|\b(?:has been|was|is now)\s+(?:removed|deleted|stopped|cancell?ed)\b",  # live: "Task *t15* has been removed."
    re.IGNORECASE)
_PAUSED_PROSE = re.compile(r"\bpaused\b", re.IGNORECASE)
_ACTIVE_PROSE = re.compile(r"\bactive\b", re.IGNORECASE)


def _status_of(segment: str) -> str:
    field = _STATUS_FIELD.search(segment)
    if field and field.group(1).lower() in _STATUS_WORDS:
        return _STATUS_WORDS[field.group(1).lower()]
    if _DONE_PROSE.search(segment):
        return "done"
    if _PAUSED_PROSE.search(segment):
        return "paused"
    if _ACTIVE_PROSE.search(segment):
        return "active"
    return "unknown"


def parse_task_list(text: str) -> dict[str, str]:
    """Task id -> "active" | "paused" | "done" | "unknown" from the bot's task-list reply.

    A "no tasks" reply gives {}; a reply with neither task ids nor a "no tasks" phrase raises ValueError.
    """
    matches = list(_TASK_ID.finditer(text or ""))
    if not matches:
        if _NO_TASKS.search(text or ""):
            return {}
        raise ValueError(f"no task list in the reply: {(text or '')[:200]!r}")
    tasks: dict[str, str] = {}
    for i, m in enumerate(matches):
        tid = "t" + next(g for g in m.groups() if g)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        status = _status_of(text[m.end():end])
        if tasks.get(tid, "unknown") == "unknown":
            tasks[tid] = status
    # "I've removed tasks *t28*, *t29*, and *t30*. Here is your updated list…" (live 2026-11-11): ids named only
    # before the list header, in a sentence that says they were removed, are closed.
    header = _LIST_HEADER.search(text or "")
    if header:
        before, after = text[:header.start()], text[header.end():]
        if _REMOVED_WORD.search(before):
            listed = {"t" + next(g for g in m.groups() if g) for m in _TASK_ID.finditer(after)}
            for m in _TASK_ID.finditer(before):
                tid = "t" + next(g for g in m.groups() if g)
                if tid not in listed:
                    tasks[tid] = "done"
    return tasks


_LIST_HEADER = re.compile(r"\bhere (?:is|are) (?:your|all your|the)\b[^•*`]{0,60}?\btasks?\b", re.IGNORECASE)
_REMOVED_WORD = re.compile(r"\b(?:removed|deleted|stopped|cancell?ed)\b", re.IGNORECASE)


_FOOTER = re.compile(r"\btask\s+(t\d+)\b.{0,40}?\bstop task\s+\1\b", re.IGNORECASE | re.DOTALL)


def task_footer(text: str) -> Optional[str]:
    """The task id in a scheduled post's footer ("task t14 · say 'stop task t14' to stop it"), else None."""
    m = _FOOTER.search(text or "")
    return m.group(1).lower() if m else None


def _num(tid: str) -> int:
    return int(tid[1:]) if tid[1:].isdigit() else 0


def new_tasks(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    """Ids in `after` that were not in `before`, in numeric order."""
    return sorted((t for t in after if t not in before), key=_num)


def to_remove(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    """The new tasks still to clean up (anything not already "done")."""
    return [t for t in new_tasks(before, after) if after[t] != "done"]


_CLOSED = re.compile(r"\bhas already run\b|\bcompleted\b|\balready stopped\b", re.IGNORECASE)


def already_closed(text: str) -> bool:
    """True when the bot says the task already ran, completed or stopped (removing it then is not a failure)."""
    return _CLOSED.search(text or "") is not None
