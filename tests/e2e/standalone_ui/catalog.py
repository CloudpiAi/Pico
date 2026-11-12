"""The question catalog (catalog.yaml): load, validate, select, expand per channel and render with live values.

A case is one message to the bot (action `ask`) or one read of the newest notice (action `read_latest`). `text`, the
`truth` SQL and the `expect` patterns are Jinja templates (sandboxed) over the ground-truth vars (`prev_month_name`,
`top_service`, ...), the run's vars (`me`, `test_channel`, `outside_channel`) and `tasks.<case id>` (the task id a
case created: the runner finds it as the id that is new in the task list after the case, since the bot does not
reliably repeat it). A template that needs a value the run doesn't have raises MissingVar, and the runner marks the case
SKIP.

Channel cases (where: channel, a thread after one, or a text naming `test_channel`) run once per `--channel`: the
first channel keeps the case id, each further channel gets a copy `<id>@<channel>`.
"""

import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Optional

import jinja2
import jinja2.sandbox
import yaml

GROUPS = ("qa", "schedule", "refusal", "status", "columns")
WHERE = ("dm", "channel", "thread", "notify")
ACTIONS = ("ask", "read_latest")
REACTIONS = ("done", "error", "any", "none")
TASK = (None, "created", "changed", "removed", "none")
KINDS = ("money", "percent", "number")
DELIVER_TO = ("here", "dm", "test_channel")
_CASE_KEYS = {"id", "group", "where", "after", "action", "text", "truth", "truth_text", "kinds", "expect", "timeout_s",
              "tol_pct", "ignore_sign", "note", "covers"}
_EXPECT_KEYS = {"reaction", "contains", "not_contains", "contains_any", "task", "task_of", "deliver", "silent",
                "no_reply"}
_LIST_KEYS = ("contains", "not_contains", "contains_any")
_DELIVER_KEYS = {"within_s", "contains", "channel", "figures"}
_SILENT_KEYS = {"for_s"}

_env = jinja2.sandbox.SandboxedEnvironment(undefined=jinja2.StrictUndefined, autoescape=False)
_env.filters["re"] = re.escape  # `{{ top_service | re }}` inside a pattern: a service name is text, not a regex
_env.filters["sql"] = lambda v: str(v).replace("'", "''")  # `'{{ top_service | sql }}'` inside a SQL string


class CatalogError(Exception):
    pass


class MissingVar(Exception):
    pass


@dataclass(frozen=True)
class Expect:
    reaction: str = "done"
    contains: list = field(default_factory=list)
    not_contains: list = field(default_factory=list)
    contains_any: list = field(default_factory=list)
    task: Optional[str] = None
    task_of: Optional[str] = None
    deliver: Optional[dict] = None  # {within_s, contains?, channel: here|dm|test_channel, figures: bool}
    silent: Optional[dict] = None  # {for_s}: nothing new may arrive in the DM for that long
    no_reply: bool = False


@dataclass(frozen=True)
class Case:
    id: str
    group: str
    where: str
    text: str = ""
    action: str = "ask"
    after: Optional[str] = None
    truth: dict = field(default_factory=dict)
    truth_text: dict = field(default_factory=dict)  # name -> SQL whose text value must appear in the reply
    kinds: dict = field(default_factory=dict)
    expect: Expect = field(default_factory=Expect)
    timeout_s: float = 180.0
    tol_pct: float = 1.0
    ignore_sign: bool = False
    note: str = ""
    channel: Optional[str] = None  # set by expand_channels for a channel case
    covers: tuple = ()  # the focus columns the case exercises (documentation + the coverage test)


def _fail(case_id: str, why: str):
    raise CatalogError(f"case {case_id}: {why}")


def _number(cid: str, name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(cid, f"{name} must be a number, not {value!r}")
    return float(value)


def _patterns(cid: str, name: str, value: Any) -> list:
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        _fail(cid, f"{name} must be a list of strings (regexes)")
    for p in value:
        if "{{" not in p:  # a template is checked after rendering
            _compile(cid, p)
    return list(value)


def _compile(cid: str, pattern: str) -> None:
    try:
        re.compile(pattern)
    except re.error as e:
        _fail(cid, f"bad regex {pattern!r}: {e}")


def _sub(cid: str, name: str, raw: Any, keys: set) -> dict:
    if not isinstance(raw, dict):
        _fail(cid, f"{name} must be a mapping")
    extra = set(raw) - keys
    if extra:
        _fail(cid, f"unknown {name} key(s): {', '.join(sorted(extra))}")
    return dict(raw)


def _expect(cid: str, raw: Any) -> Expect:
    raw = _sub(cid, "expect", raw or {}, _EXPECT_KEYS)
    for key in _LIST_KEYS:
        if key in raw:
            raw[key] = _patterns(cid, key, raw[key])
    if raw.get("deliver") is not None:
        d = _sub(cid, "deliver", raw["deliver"], _DELIVER_KEYS)
        if "within_s" not in d:
            _fail(cid, "deliver needs within_s")
        d["within_s"] = _number(cid, "deliver within_s", d["within_s"])
        if "contains" in d:
            d["contains"] = _patterns(cid, "deliver contains", d["contains"])
        if d.get("channel", "here") not in DELIVER_TO:
            _fail(cid, f"deliver channel must be one of {DELIVER_TO}")
        if "figures" in d and not isinstance(d["figures"], bool):
            _fail(cid, "deliver figures must be true or false")
        raw["deliver"] = d
    if raw.get("silent") is not None:
        sl = _sub(cid, "silent", raw["silent"], _SILENT_KEYS)
        sl["for_s"] = _number(cid, "silent for_s", sl.get("for_s"))
        raw["silent"] = sl
    exp = Expect(**raw)
    if exp.reaction not in REACTIONS:
        _fail(cid, f"reaction must be one of {REACTIONS}")
    if exp.task not in TASK:
        _fail(cid, f"task must be one of {TASK[1:]}")
    if exp.task in ("changed", "removed") and not exp.task_of:
        _fail(cid, "task changed/removed needs task_of (the case that created it)")
    if (exp.deliver or exp.silent) and exp.task != "created":
        _fail(cid, "deliver and silent watch a task the case creates: set task: created")
    return exp


def load(path: str) -> list[Case]:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
    except yaml.YAMLError as e:
        raise CatalogError(f"the catalog is not valid YAML: {e}") from None
    if not isinstance(doc, dict):
        raise CatalogError("the catalog must be a mapping with a `cases:` list")
    defaults = doc.get("defaults") or {}
    raw_cases = doc.get("cases") or []
    if not isinstance(raw_cases, list) or not raw_cases:
        raise CatalogError("the catalog has no cases")
    cases: list[Case] = []
    seen: set[str] = set()
    for raw in raw_cases:
        if not isinstance(raw, dict):
            raise CatalogError(f"each case must be a mapping, not {raw!r}")
        cid = str(raw.get("id", "?"))
        extra = set(raw) - _CASE_KEYS
        if extra:
            _fail(cid, f"unknown key(s): {', '.join(sorted(extra))}")
        if cid in seen:
            _fail(cid, "id used twice")
        if raw.get("group") not in GROUPS:
            _fail(cid, f"group must be one of {GROUPS}")
        if raw.get("where") not in WHERE:
            _fail(cid, f"where must be one of {WHERE}")
        action = raw.get("action", "ask")
        if action not in ACTIONS:
            _fail(cid, f"action must be one of {ACTIONS}")
        if action == "read_latest" and raw["where"] != "notify":
            _fail(cid, "read_latest reads the notify channel: set where: notify")
        if action == "ask" and raw["where"] == "notify":
            _fail(cid, "the bot is never asked in the notify channel")
        if action == "ask" and not raw.get("text"):
            _fail(cid, "an ask case needs text")
        if raw["where"] == "thread" and raw.get("after") not in seen:
            _fail(cid, "a thread case needs after: <an earlier case id>")
        truth = _sub(cid, "truth", raw.get("truth") or {}, set(raw.get("truth") or {}))
        if not all(isinstance(v, str) for v in truth.values()):
            _fail(cid, "each truth value must be a SQL string")
        truth_text = _sub(cid, "truth_text", raw.get("truth_text") or {}, set(raw.get("truth_text") or {}))
        if not all(isinstance(v, str) for v in truth_text.values()):
            _fail(cid, "each truth_text value must be a SQL string")
        covers = raw.get("covers") or []
        if not isinstance(covers, list) or not all(isinstance(c, str) for c in covers):
            _fail(cid, "covers must be a list of column names")
        kinds = _sub(cid, "kinds", raw.get("kinds") or {}, set(raw.get("kinds") or {}))
        if set(kinds) - set(truth) or any(k not in KINDS for k in kinds.values()):
            _fail(cid, f"kinds must name truth values, each one of {KINDS}")
        exp = _expect(cid, raw.get("expect"))
        if exp.task_of and exp.task_of not in seen:
            _fail(cid, "task_of must name an earlier case")
        cases.append(Case(
            id=cid, group=raw["group"], where=raw["where"], text=str(raw.get("text") or ""), action=action,
            after=raw.get("after"), truth=truth, truth_text=truth_text, kinds=kinds, expect=exp,
            covers=tuple(covers),
            timeout_s=_number(cid, "timeout_s", raw.get("timeout_s", defaults.get("timeout_s", 180))),
            tol_pct=_number(cid, "tol_pct", raw.get("tol_pct", defaults.get("tol_pct", 1.0))),
            ignore_sign=bool(raw.get("ignore_sign", False)), note=str(raw.get("note") or ""),
        ))
        seen.add(cid)
    return cases


def select(cases: list[Case], groups: Iterable[str], ids: Iterable[str]) -> list[Case]:
    """The cases in the given groups or with the given ids (both empty = all), plus the cases they depend on."""
    groups, ids = set(groups), set(ids)
    if not groups and not ids:
        return list(cases)
    by_id = {c.id: c for c in cases}
    wanted = {c.id for c in cases if c.group in groups or c.id in ids}
    changed = True
    while changed:
        changed = False
        for cid in list(wanted):
            for dep in (by_id[cid].after, by_id[cid].expect.task_of):
                if dep and dep not in wanted:
                    wanted.add(dep)
                    changed = True
    return [c for c in cases if c.id in wanted]


def _channel_bound(cases: list[Case]) -> set[str]:
    """Ids of the cases that depend on the test channel, directly or through `after` / `task_of`."""
    bound = set()
    for c in cases:
        if (c.where == "channel" or "test_channel" in c.text
                or (c.expect.deliver or {}).get("channel") == "test_channel"
                or c.after in bound or c.expect.task_of in bound):
            bound.add(c.id)
    return bound


def expand_channels(cases: list[Case], channels: list[str]) -> list[Case]:
    """Bind channel cases to channels[0] in place, then append one copy of them per further channel."""
    if not channels:
        return list(cases)
    bound = _channel_bound(cases)
    out = [replace(c, channel=channels[0]) if c.id in bound else c for c in cases]
    for ch in channels[1:]:
        def rename(cid):
            return f"{cid}@{ch}" if cid in bound else cid
        for c in cases:
            if c.id in bound:
                out.append(replace(c, id=rename(c.id), channel=ch, after=rename(c.after) if c.after else None,
                                   expect=replace(c.expect, task_of=rename(c.expect.task_of)
                                                  if c.expect.task_of else None)))
    return out


def _r(template: str, vars_: dict) -> str:
    try:
        return _env.from_string(template).render(**vars_)
    except jinja2.UndefinedError as e:
        raise MissingVar(str(e)) from None
    except jinja2.exceptions.SecurityError as e:
        raise CatalogError(f"unsafe template {template!r}: {e}") from None


def render(case: Case, vars_: dict) -> Case:
    """The case with every template filled in; MissingVar names a value the run doesn't have, CatalogError a pattern
    that is no regex once filled in."""
    exp = case.expect

    def pats(items):
        filled = [_r(p, vars_) for p in items]
        for p in filled:
            _compile(case.id, p)
        return filled

    deliver = {**exp.deliver, "contains": pats(exp.deliver.get("contains", []))} if exp.deliver else None
    rendered = replace(exp, contains=pats(exp.contains), not_contains=pats(exp.not_contains),
                       contains_any=pats(exp.contains_any), deliver=deliver)
    return replace(case, text=_r(case.text, vars_), truth={k: _r(s, vars_) for k, s in case.truth.items()},
                   truth_text={k: _r(s, vars_) for k, s in case.truth_text.items()}, expect=rendered)
