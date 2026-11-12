"""Scheduled tasks: the store, the owner rules and the history (PRD R5, R11-R17, R19).

Files in the data dir (/data, next to the cache):
  tasks.json            {"next_id", "next_request", "tasks": {id: task}, "requests": {id: request}, "ask_slots": {"day", "used"}}
  tasks-history.jsonl   one line per change: at, task_id, action, by, where, old, new
  tasks.lock            flock around every read-modify-write (the Slack side's bash and the scheduler both write)
  tasks.json.bak        a second copy of tasks.json, written after it: a torn or broken tasks.json is read from here

A task: id, kind (reminder | report | query | ask), name, owner (Slack user ID), created_in (channel), created_at,
target {"type": "user"|"channel", "id"}, schedule {"cron": "m h dom mon dow"} or {"at": "YYYY-MM-DD HH:MM"} plus
"tz", payload (per kind), status (active | paused | done | removed), last_sent_at, last_data_through.

Owner rules: the owner, or a report admin, changes a task at once. Anyone else gets a request; the owner says
yes or no (`resolve`); an unanswered request is accepted automatically after 7 days (the owner is reminded on day 6),
so an owner who has left doesn't block it. Only people in FOCUS_REPORT_CREATORS (or admins) may make a task post to
a channel; anyone may schedule for their own DM. History keeps the last `history_max` changes
per task, plus its `created` line and its latest `removed` line. Nothing here talks to Slack.
"""
import fcntl
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger

KINDS = ("reminder", "report", "query", "ask")
MAX_ACTIVE_PER_OWNER = 10
MAX_TEXT = 1000
MIN_GAP = timedelta(minutes=59)
MAX_AHEAD = timedelta(days=366)    # a one-time task at most a year ahead
KEEP_CLOSED = timedelta(days=30)   # stopped / finished tasks (and their history) are purged after this
REQUEST_TTL = timedelta(days=7)
REMIND_BEFORE = timedelta(days=1)
AUTO_ACCEPT_NEVER = {"target", "payload"}  # where it posts / what it says: only the owner or an admin
_EDIT_ACTION = {"schedule": "time_changed", "target": "target_changed", "name": "name_changed",
                "payload": "text_changed"}
_REQUEST_ACTION = {"remove": "stop_requested"}  # pause / resume / edit -> change_requested
_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")


class TaskError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _ids(value, pattern):
    return frozenset(x.strip() for x in (value or "").split(",") if re.match(pattern, x.strip()))


def _whole(name, default):
    try:
        n = int(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return n if n >= 1 else default


@dataclass(frozen=True)
class Settings:
    history_max: int = 5
    ask_max_per_day: int = 1000
    allowed_channels: frozenset = field(default_factory=frozenset)  # empty: any channel the bot is in
    admins: frozenset = field(default_factory=frozenset)
    creators: frozenset = field(default_factory=frozenset)  # who may post tasks to a channel; empty: nobody (admins can)
    allow_mentions: bool = False

    @classmethod
    def from_env(cls):
        """Unset (or invalid: the entrypoint refuses those at start) -> the defaults above."""
        return cls(history_max=_whole("FOCUS_REPORT_HISTORY_MAX", 5),
                   ask_max_per_day=_whole("FOCUS_ASK_MAX_PER_DAY", 1000),
                   allowed_channels=_ids(os.environ.get("FOCUS_REPORT_CHANNELS"), r"^[CG][A-Z0-9]+$"),
                   admins=_ids(os.environ.get("FOCUS_REPORT_ADMINS"), r"^[UW][A-Z0-9]+$"),
                   creators=_ids(os.environ.get("FOCUS_REPORT_CREATORS"), r"^[UW][A-Z0-9]+$"),
                   allow_mentions=os.environ.get("FOCUS_REPORT_ALLOW_MENTIONS", "").strip() == "true")


# ---------------- files ----------------

def _path(d, name):
    return os.path.join(d, name)


@contextmanager
def _locked(d):
    os.makedirs(d, exist_ok=True)
    with open(_path(d, "tasks.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _read_store(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not isinstance(data.get("tasks", {}), dict):
        raise ValueError("not a task list")
    return data


def _load(d):
    """The store. A broken tasks.json is read from tasks.json.bak; both broken is store_unreadable (never an
    empty list: saving that would wipe every task)."""
    try:
        data = _read_store(_path(d, "tasks.json"))
    except FileNotFoundError:  # never made yet -> empty; but if a backup is there, the file was lost: use it
        try:
            data = _read_store(_path(d, "tasks.json.bak"))
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError):
            raise TaskError("store_unreadable", "The saved task list (tasks.json) can't be read, so I can't change "
                                                "tasks now. An admin needs to look at it.") from None
    except (OSError, ValueError) as e:
        try:
            data = _read_store(_path(d, "tasks.json.bak"))
        except (OSError, ValueError):
            raise TaskError("store_unreadable", "The saved task list (tasks.json) can't be read, so I can't change "
                                                "tasks now. An admin needs to look at it.") from None
        print(json.dumps({"event": "tasks_store_from_backup", "error": f"{type(e).__name__}: {str(e)[:200]}"}),
              flush=True)
    return {"next_id": 1, "next_request": 1, "tasks": {}, "requests": {}, "ask_slots": {"day": None, "used": 0}, **data}


def _write_synced(path, text):
    """Write `path` so that a crash or power loss leaves the old file or the new one, never a torn one."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _sync_dir(d):
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _save(d, data):
    text = json.dumps(data, indent=2)
    _write_synced(_path(d, "tasks.json"), text)
    _write_synced(_path(d, "tasks.json.bak"), text)
    _sync_dir(d)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _read_history(d):
    """The history lines; a line that isn't a whole entry (cut short by a full disk or a power loss) is skipped."""
    try:
        with open(_path(d, "tasks-history.jsonl"), encoding="utf-8") as fh:
            lines = [line for line in fh if line.strip()]
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and "task_id" in entry and "action" in entry:
            out.append(entry)
    return out


def _record(d, task_id, action, by, where, now, old=None, new=None, history_max=5):
    """Append one change, then keep only the last `history_max` changes of this task (+ created, latest removed)."""
    entry = {"at": _iso(now), "task_id": task_id, "action": action, "by": by, "where": where, "old": old, "new": new}
    with open(_path(d, "tasks-history.jsonl"), "a+b") as fh:
        if fh.tell():
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":  # the last line was cut short: never glue this entry onto it
                fh.write(b"\n")
        fh.write((json.dumps(entry) + "\n").encode("utf-8"))
    lines = _read_history(d)
    mine = [i for i, e in enumerate(lines) if e["task_id"] == task_id]
    removed = [i for i in mine if lines[i]["action"] == "removed"]
    keep_always = {i for i in mine if lines[i]["action"] == "created"} | set(removed[-1:])
    changes = [i for i in mine if i not in keep_always]
    drop = set(changes[:-history_max]) if len(changes) > history_max else set()
    if drop:
        _write_synced(_path(d, "tasks-history.jsonl"),
                      "".join(json.dumps(e) + "\n" for i, e in enumerate(lines) if i not in drop))


def history(d, task_id):
    return [e for e in _read_history(d) if e["task_id"] == task_id]


# ---------------- schedule ----------------

_DOW = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")


def _dow_name(token):
    return _DOW[int(token) % 7] if token.isdigit() else token


def _dow_item(item):
    """One comma item of a cron day-of-week field in cron numbering (0/7 = Sunday) -> APScheduler names."""
    base, _, step = item.partition("/")
    step = f"/{step}" if step else ""
    if base in ("*", "?"):
        return "*" + step
    if "-" not in base:
        return _dow_name(base)
    a, b = base.split("-", 1)
    if a.isdigit() and b.isdigit():
        lo, hi = int(a), int(b)
        if lo == 0 and hi >= 6 and not step:
            return "*"
        if lo == 0:  # Sunday first: APScheduler weeks start on Monday
            return "sun" + (f",mon-{_DOW[hi % 7]}" if hi % 7 >= 1 else "")
        if hi == 7:
            return f"{_DOW[lo]}-sat,sun"
    return f"{_dow_name(a)}-{_dow_name(b)}{step}"


def cron_trigger(cron, zone):
    """APScheduler's from_crontab counts weekdays from Monday = 0; cron counts from Sunday = 0 (or 7). Translate
    the day-of-week numbers to names first, so "0 17 * * 5" is Friday, as people and the skill text mean it."""
    fields = cron.split()
    if len(fields) == 5:
        fields[4] = ",".join(_dow_item(x) for x in fields[4].split(","))
    return CronTrigger.from_crontab(" ".join(fields), timezone=zone)


def _zone(tz):
    try:
        return ZoneInfo(tz or "UTC")
    except (ZoneInfoNotFoundError, ValueError, TypeError, OSError) as e:
        raise TaskError("bad_timezone", f"I don't know the time zone {tz!r}.") from e


def scheduled_at(schedule):
    """A one-time task's time in UTC."""
    at = datetime.strptime(schedule["at"], "%Y-%m-%d %H:%M").replace(tzinfo=_zone(schedule.get("tz")))
    return at.astimezone(timezone.utc)


def next_run(schedule, now):
    """Next run (UTC) or None when a one-time task's time has passed."""
    zone = _zone(schedule.get("tz"))
    if "at" in schedule:
        at = datetime.strptime(schedule["at"], "%Y-%m-%d %H:%M").replace(tzinfo=zone)
        return at.astimezone(timezone.utc) if at > now else None
    nxt = cron_trigger(schedule["cron"], zone).get_next_fire_time(None, now)
    return nxt.astimezone(timezone.utc) if nxt else None


def check_schedule(schedule, now):
    if not isinstance(schedule, dict) or ("cron" in schedule) == ("at" in schedule):
        raise TaskError("bad_schedule", "Give either a repeating time (cron) or a one-time date and time.")
    zone = _zone(schedule.get("tz"))
    if "at" in schedule:
        if not _AT_RE.match(str(schedule["at"])):
            raise TaskError("bad_schedule", "A one-time task needs a date and time like 2026-11-06 10:00.")
        try:
            first = next_run(schedule, now)
        except (ValueError, OverflowError) as e:
            raise TaskError("bad_schedule", "That date or time doesn't exist.") from e
        if first is None:
            raise TaskError("time_passed", "That time has already passed. Did you mean tomorrow?")
        local = datetime.strptime(schedule["at"], "%Y-%m-%d %H:%M")
        if first.astimezone(zone).replace(tzinfo=None) != local:  # e.g. 02:30 on the night the clocks go forward
            raise TaskError("bad_schedule", f"{schedule['at']} doesn't exist in {zone.key}: the clocks change "
                                            "then. Pick another time.")
        if first - now > MAX_AHEAD:
            raise TaskError("time_too_far", "That's more than a year ahead. Pick a date within the next year.")
        return first
    cron = str(schedule["cron"])
    if len(cron.split()) != 5:
        raise TaskError("bad_schedule", "A repeating time needs 5 cron fields, e.g. \"0 15 * * *\".")
    try:
        trigger = cron_trigger(cron, zone)
    except ValueError as e:
        raise TaskError("bad_schedule", f"I can't read the time {cron!r}.") from e
    first = trigger.get_next_fire_time(None, now)
    if first is None:
        raise TaskError("bad_schedule", f"The time {cron!r} never happens.")
    prev, t = first, trigger.get_next_fire_time(first, first + timedelta(seconds=1))
    while t is not None and t - first <= timedelta(days=8):  # one whole week: every gap, not just the first
        if t - prev < MIN_GAP:
            raise TaskError("too_often", "A task can run at most once an hour.")
        prev, t = t, trigger.get_next_fire_time(t, t + timedelta(seconds=1))
    return first.astimezone(timezone.utc)


# ---------------- checks ----------------

def check_payload(kind, payload):
    if kind not in KINDS:
        raise TaskError("bad_kind", f"Unknown kind {kind!r}: use reminder, report, query or ask.")
    payload = payload or {}
    key = {"reminder": "text", "query": "sql", "ask": "question"}.get(kind)
    if key:
        value = str(payload.get(key) or "").strip()
        if not value:
            raise TaskError("bad_payload", f"A {kind} needs its {key}.")
        if len(value) > MAX_TEXT and kind != "query":
            raise TaskError("too_long", f"Keep the {key} under {MAX_TEXT} characters.")
        return {key: value}
    if not isinstance(payload.get("template"), str) or not payload["template"].strip():
        raise TaskError("bad_payload", "A report needs a template.")  # which ones: the data source checks (CLI)
    flt = {k: v for k, v in (payload.get("filter") or {}).items() if k in ("service", "account", "region") and v}
    return {"template": payload["template"], **({"filter": flt} if flt else {})}


def check_target(target, settings, user=None):
    """The checked target. `user` (when given) is the person making the task post there: a channel needs them in
    FOCUS_REPORT_CREATORS or FOCUS_REPORT_ADMINS (decided 2026-11-06)."""
    if not isinstance(target, dict) or target.get("type") not in ("user", "channel") or not target.get("id"):
        raise TaskError("bad_target", "Send it to a person or a channel.")
    if target["type"] == "channel" and settings.allowed_channels and target["id"] not in settings.allowed_channels:
        raise TaskError("channel_not_allowed", "Tasks aren't allowed to post in that channel here.")
    if (target["type"] == "channel" and user is not None
            and user not in settings.creators and user not in settings.admins):
        raise TaskError("not_a_creator", "Only the people your admin has listed can set up tasks that post in a "
                                         "channel. I can send it to your DM instead.")
    return {"type": target["type"], "id": target["id"]}


# ---------------- store API ----------------

def add(d, *, owner, where, kind, name, target, schedule, payload, now, settings):
    payload = check_payload(kind, payload)
    target = check_target(target, settings, owner)
    check_schedule(schedule, now)
    with _locked(d):
        data = _load(d)
        active = [t for t in data["tasks"].values() if t["owner"] == owner and t["status"] in ("active", "paused")]
        if len(active) >= MAX_ACTIVE_PER_OWNER:
            raise TaskError("too_many_tasks", f"You already have {MAX_ACTIVE_PER_OWNER} tasks; stop one first.")
        tid = f"t{data['next_id']}"
        task = {"id": tid, "kind": kind, "name": (name or kind).strip()[:120], "owner": owner, "created_in": where,
                "created_at": _iso(now), "target": target, "schedule": dict(schedule), "payload": payload,
                "status": "active", "last_sent_at": None, "last_data_through": None}
        data["next_id"] += 1
        data["tasks"][tid] = task
        _save(d, data)
        _record(d, tid, "created", owner, where, now, new={"name": task["name"], "kind": kind, "target": target,
                                                           "schedule": task["schedule"]},
                history_max=settings.history_max)
    return task


def get(d, task_id):
    task = _load(d)["tasks"].get(task_id)
    if task is None:
        raise TaskError("no_such_task", f"There's no task {task_id}.")
    return task


def list_for(d, *, user, channel):
    """Tasks this person owns plus tasks that post to this channel (active or paused), oldest first."""
    out = [t for t in _load(d)["tasks"].values() if t["status"] in ("active", "paused")
           and (t["owner"] == user or (t["target"]["type"] == "channel" and t["target"]["id"] == channel))]
    return sorted(out, key=lambda t: int(t["id"][1:]))


def active_tasks(d):
    return [t for t in _load(d)["tasks"].values() if t["status"] == "active"]


def _apply(d, data, task, action, by, where, changes, now, settings, acting_for=None):
    """Do the change and write its history line(s). Caller holds the lock. `acting_for` is whose rights count for a
    channel target when `by` is "system" (an auto-accepted request)."""
    if action == "remove":
        task["status"] = "removed"
        task["closed_at"] = _iso(now)
        for rid in [r for r, req in data["requests"].items() if req["task_id"] == task["id"]]:
            del data["requests"][rid]  # nothing left to confirm
        _record(d, task["id"], "removed", by, where, now, history_max=settings.history_max)
    elif action == "pause":
        task["status"] = "paused"
        _record(d, task["id"], "paused", by, where, now, history_max=settings.history_max)
    elif action == "resume":
        check_schedule(task["schedule"], now)  # a one-time task whose time passed can't come back
        check_target(task["target"], settings, acting_for or by)  # still allowed to post there?
        task["status"] = "active"
        task.pop("paused_reason", None)
        task.pop("fail_count", None)
        _record(d, task["id"], "resumed", by, where, now, history_max=settings.history_max)
    elif action == "edit":
        checked = dict(changes or {})
        if "schedule" in checked:
            check_schedule(checked["schedule"], now)
        if "target" in checked:
            checked["target"] = check_target(checked["target"], settings, acting_for or by)
        if "payload" in checked:
            checked["payload"] = check_payload(task["kind"], checked["payload"])
        for key in ("schedule", "target", "name", "payload"):
            if key in checked and checked[key] != task.get(key):
                old = task.get(key)
                task[key] = checked[key]
                _record(d, task["id"], _EDIT_ACTION[key], by, where, now, old={key: old} if key == "name" else old,
                        new={key: checked[key]} if key == "name" else checked[key],
                        history_max=settings.history_max)
    else:
        raise TaskError("bad_action", f"Unknown action {action!r}.")
    _save(d, data)


def _check_change(task, action, changes, now, settings, by=None):
    """Raise TaskError when this change can't be applied now (bad time, place or text, a passed one-time time)."""
    changes = changes or {}
    if action == "resume":
        check_schedule(task["schedule"], now)
        check_target(task["target"], settings, by)
    if action == "edit":
        if "schedule" in changes:
            check_schedule(changes["schedule"], now)
        if "target" in changes:
            check_target(changes["target"], settings, by)
        if "payload" in changes:
            check_payload(task["kind"], changes["payload"])


def change(d, task_id, action, *, by, where, changes=None, now, settings):
    """Owner or admin: done at once. Anyone else: a request the owner must confirm."""
    with _locked(d):
        data = _load(d)
        task = data["tasks"].get(task_id)
        if task is None:
            raise TaskError("no_such_task", f"There's no task {task_id}.")
        if task["status"] in ("removed", "done"):
            raise TaskError(f"task_{task['status']}", f"Task {task_id} is {task['status']} and can't be changed.")
        if by == task["owner"] or by in settings.admins:
            _apply(d, data, task, action, by, where, changes, now, settings)
            return {"result": "done", "task": task}
        _check_change(task, action, changes, now, settings, by)  # the owner is never asked to approve a bad change
        rid = f"r{data['next_request']}"
        data["next_request"] += 1
        req = {"id": rid, "task_id": task_id, "action": action, "changes": changes, "by": by, "where": where,
               "owner": task["owner"], "at": _iso(now)}
        data["requests"][rid] = req
        _save(d, data)
        _record(d, task_id, _REQUEST_ACTION.get(action, "change_requested"), by, where, now,
                new={"request": rid, "action": action, **({"changes": changes} if changes else {})},
                history_max=settings.history_max)
        return {"result": "requested", "task": task, "request": req}


def resolve(d, request_id, *, by, accept, now, settings):
    """Only the task's owner (or an admin) answers a request."""
    with _locked(d):
        data = _load(d)
        req = data["requests"].get(request_id)
        if req is None:
            raise TaskError("no_such_request", f"There's no open request {request_id}.")
        if by != req["owner"] and by not in settings.admins:
            raise TaskError("not_owner", "Only the person who set up this task can answer that.")
        task = data["tasks"].get(req["task_id"])
        del data["requests"][request_id]
        if task is None or task["status"] in ("removed", "done"):
            _save(d, data)
            return {"result": "gone", "request": req, "task": task}
        if not accept:
            _save(d, data)
            _record(d, task["id"], "declined", by, req["where"], now,
                    new={"request": request_id, "action": req["action"]}, history_max=settings.history_max)
            return {"result": "declined", "request": req, "task": task}
        try:
            _check_change(task, req["action"], req.get("changes"), now, settings, by)
        except TaskError as e:  # e.g. the requested one-time time has passed meanwhile: close it cleanly
            _save(d, data)
            _record(d, task["id"], "request_failed", by, req["where"], now,
                    new={"request": request_id, "reason": e.code}, history_max=settings.history_max)
            raise
        _record(d, task["id"], "confirmed", by, req["where"], now,
                new={"request": request_id, "action": req["action"]}, history_max=settings.history_max)
        _apply(d, data, task, req["action"], by, req["where"], req.get("changes"), now, settings)
        return {"result": "confirmed", "request": req, "task": task}


def process_requests(d, now, settings):
    """Unanswered requests: remind the owner once on day 6, accept automatically after 7 days (decided 2026-11-06).

    Only stop / pause / resume and time or name changes go through unanswered; a change to where a task posts or
    what it says (target, payload) expires instead, so nobody can redirect someone else's data by waiting.
    Returns {"reminders", "accepted", "failed", "expired"} (lists of requests) so the caller can tell the people.
    A request that can no longer apply (e.g. its one-time time has passed) is closed as `request_failed`."""
    out = {"reminders": [], "accepted": [], "failed": [], "expired": []}
    with _locked(d):
        data = _load(d)
        changed = False
        for rid, req in list(data["requests"].items()):
            age = now - datetime.fromisoformat(req["at"])
            if age <= REQUEST_TTL - REMIND_BEFORE:
                continue
            if age <= REQUEST_TTL:
                if not req.get("reminded"):
                    req["reminded"] = True
                    changed = True
                    out["reminders"].append(dict(req))
                continue
            if data["requests"].pop(rid, None) is None:  # closed by an earlier request on the same task
                continue
            changed = True
            task = data["tasks"].get(req["task_id"])
            if task is None or task["status"] in ("removed", "done"):
                continue
            if req["action"] == "edit" and set(req.get("changes") or {}) & AUTO_ACCEPT_NEVER:
                _record(d, task["id"], "request_expired", "system", req["where"], now,
                        new={"request": rid, "reason": "needs the owner or an admin"},
                        history_max=settings.history_max)
                out["expired"].append(dict(req))
                continue
            try:
                # nobody answered: the change must be allowed for the owner AND for the person who asked
                _check_change(task, req["action"], req.get("changes"), now, settings, req["owner"])
                _check_change(task, req["action"], req.get("changes"), now, settings, req["by"])
            except TaskError as e:
                _record(d, task["id"], "request_failed", "system", req["where"], now,
                        new={"request": rid, "reason": e.code}, history_max=settings.history_max)
                out["failed"].append({**req, "reason": e.message})
                continue
            _record(d, task["id"], "auto_accepted", "system", req["where"], now,
                    new={"request": rid, "action": req["action"]}, history_max=settings.history_max)
            _apply(d, data, task, req["action"], "system", req["where"], req.get("changes"), now, settings,
                   acting_for=req["owner"])
            out["accepted"].append(dict(req))
        if changed:
            _save(d, data)
    return out


def mark_sent(d, task_id, *, at, data_through, settings=None):
    with _locked(d):
        data = _load(d)
        task = data["tasks"][task_id]
        if task["status"] in ("removed", "done"):  # stopped while it was being sent
            return
        task["last_sent_at"] = _iso(at)
        task.pop("fail_count", None)
        task.pop("sending_at", None)
        if data_through:
            task["last_data_through"] = data_through
        if "at" in task["schedule"]:
            task["status"] = "done"
            task["closed_at"] = _iso(at)
            _record(d, task_id, "done", "system", task["target"]["id"], at,
                    history_max=(settings or Settings()).history_max)
        _save(d, data)


def mark_sending(d, task_id, *, at):
    """Set just before the post: if the agent stops before mark_sent, the next start knows it may have gone out."""
    with _locked(d):
        data = _load(d)
        data["tasks"][task_id]["sending_at"] = _iso(at)
        _save(d, data)


def clear_sending(d, task_id):
    """The post failed: it did not go out."""
    with _locked(d):
        data = _load(d)
        task = data["tasks"].get(task_id)
        if task is not None and task.pop("sending_at", None) is not None:
            _save(d, data)


def note_ran(d, task_id, *, at):
    """A run that ended without a failure but sent nothing (condition not met, ask limit): the failure count
    starts again."""
    with _locked(d):
        data = _load(d)
        task = data["tasks"].get(task_id)
        if task is None:
            return
        task["last_run_at"] = _iso(at)
        task.pop("fail_count", None)
        _save(d, data)


def note_failure(d, task_id):
    """One more run in a row that failed for a temporary reason; returns how many now (a send resets it)."""
    with _locked(d):
        data = _load(d)
        task = data["tasks"][task_id]
        task["fail_count"] = int(task.get("fail_count") or 0) + 1
        _save(d, data)
        return task["fail_count"]


def auto_pause(d, task_id, *, reason, now, settings):
    with _locked(d):
        data = _load(d)
        task = data["tasks"][task_id]
        if task["status"] != "active":  # removed or paused meanwhile: keep that
            return
        task["status"] = "paused"
        task["paused_reason"] = reason
        _save(d, data)
        _record(d, task_id, "auto_paused", "system", (task.get("target") or {}).get("id", ""), now,
                new={"reason": reason}, history_max=settings.history_max)


def mark_missed(d, task_id, *, now, settings, action="missed"):
    """Close a one-time task that won't be sent (status done; history `missed`, or `action`: `skipped` over the
    daily question limit, `send_unconfirmed` when it was posting as the agent stopped)."""
    with _locked(d):
        data = _load(d)
        task = data["tasks"][task_id]
        if task["status"] != "active":
            return
        task["status"] = "done"
        task["closed_at"] = _iso(now)
        _save(d, data)
        _record(d, task_id, action, "system", (task.get("target") or {}).get("id", ""), now,
                history_max=settings.history_max)


def _closed_time(task, now):
    for key in ("closed_at", "last_sent_at", "created_at"):
        try:
            return datetime.fromisoformat(task[key])
        except (KeyError, TypeError, ValueError):
            continue
    return now


def purge_closed(d, now, keep=KEEP_CLOSED):
    """Forget stopped / finished tasks closed more than `keep` ago, with their requests and history lines; returns
    their ids."""
    with _locked(d):
        data = _load(d)
        stamped = False
        for t in data["tasks"].values():  # closed before closed_at existed: its 30 days start now
            if t.get("status") in ("removed", "done") and not t.get("closed_at"):
                t["closed_at"] = _iso(now)
                stamped = True
        gone = {tid for tid, t in data["tasks"].items()
                if t.get("status") in ("removed", "done") and now - _closed_time(t, now) > keep}
        if not gone:
            if stamped:
                _save(d, data)
            return []
        for tid in gone:
            del data["tasks"][tid]
        data["requests"] = {r: q for r, q in data["requests"].items() if q.get("task_id") not in gone}
        _save(d, data)
        lines = _read_history(d)
        _write_synced(_path(d, "tasks-history.jsonl"),
                      "".join(json.dumps(e) + "\n" for e in lines if e["task_id"] not in gone))
    return sorted(gone)


def give_back_ask_slot(d, now):
    """A question that couldn't finish doesn't count against today's limit."""
    day = now.astimezone(timezone.utc).date().isoformat()
    with _locked(d):
        data = _load(d)
        slots = data["ask_slots"]
        if slots.get("day") == day and slots.get("used", 0) > 0:
            slots["used"] -= 1
            _save(d, data)


def take_ask_slot(d, now, settings):
    """Count one `ask` run for today (UTC); False when today's limit is used up."""
    day = now.astimezone(timezone.utc).date().isoformat()
    with _locked(d):
        data = _load(d)
        slots = data["ask_slots"] if data["ask_slots"].get("day") == day else {"day": day, "used": 0}
        if slots["used"] >= settings.ask_max_per_day:
            return False
        slots["used"] += 1
        data["ask_slots"] = slots
        _save(d, data)
    return True
