"""The task runner: keeps one APScheduler job per active task, sends each at its time, holds data tasks for the
host's data load (when it has one) and releases them after it.

The host calls install(sched, data_dir=..., source=..., gate=...) once, and on_load_done(rc) after each data load.
Without a gate (NoLoads: a host without a data load) data tasks are never held. Nothing here imports the FOCUS plugin or the agent.

The tasks live in FOCUS_DATA_DIR/tasks.json, written by `pico schedule ...` (run by the agent in the
gateway). tasks_sync re-reads it every minute and keeps one APScheduler job per active task, in the task's
own time zone. Each send re-checks that the bot can still post there. A send that fails for a temporary reason
(Slack trouble, a render that couldn't finish) is tried again in RETRY_IN and the task paused only after RETRY_MAX
such runs; a problem a retry can't fix pauses it at once; either way its creator is told once (the report admins when
the creator can't be reached). Sends are messages only: an `ask` child runs with PICO_FINOPS_AGENT_UNATTENDED=1 and no
Slack tokens, so it can't load data, post, or manage tasks.

Sends run on the plugin's own pool (EXECUTOR), at most DATA_SLOTS data tasks render at once, and a task never runs
twice at the same time. A one-time task up to TASK_GRACE_S late is sent late; later, it is closed and its creator
told. The first sync after a start catches up a repeating task's run missed while the agent was down (or tells the
creator when that is older than the grace). A send interrupted by a stop (sending_at set) is never sent again.
health() says when tasks_sync last worked; the report admins are told when it keeps failing.
"""
import itertools
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from . import kinds, secret_guard, slack_api, tasks
from .source import NoData

# first: the first sync after install catches up runs missed while the agent was down; ok_at (wall clock) and
# fails: when tasks_sync last worked and how many runs failed since (health); purged: the day closed tasks were purged
_SYNC: dict = {"first": True, "ok_at": None, "fails": 0, "alerted": False, "purged": None}
SYNC_ALERT_AFTER = 3  # failed syncs in a row before the report admins are told (once)
_BAD: set = set()  # (task id, what was wrong) already paused/told, so a broken task is reported once
_CFG: dict = {"data_dir": None, "source": None, "gate": None, "client": None, "sched": None, "executor": None}
EXECUTOR = "schedule_tasks"  # the plugin's own thread pool: a slow send never holds up the host's jobs
DATA_EXECUTOR = "schedule_data"  # data tasks' own pool, so they never hold up reminders
TASK_WORKERS = 4
DATA_SLOTS = 2               # data tasks (each starts a child process) rendering at once; reminders don't wait
_BOOKING_IDS = itertools.count(1)
_DATA_SLOTS = threading.BoundedSemaphore(DATA_SLOTS)


class LoadGate(Protocol):
    """What the host tells the runner about its data load (the standalone agent: its FOCUS load)."""

    def running(self) -> bool: ...

    def next_load_at(self) -> "datetime | None": ...

    def fresh_since_last_slot(self, now: datetime) -> bool: ...


class NoLoads:
    """A host without a data load: data tasks are sent at their time, never held."""

    def running(self):
        return False

    def next_load_at(self):
        return None

    def fresh_since_last_slot(self, now):
        return True


def configure(*, data_dir, source=None, gate=None, slack_client_factory=None):
    """Settings without a scheduler: sends happen in the caller's thread (tests, or a host that only sends)."""
    _CFG.update(data_dir=data_dir, source=source or NoData(), gate=gate or NoLoads(), client=slack_client_factory,
                sched=None, executor=None)


def install(sched, *, data_dir, source=None, gate=None, slack_client_factory=None):
    """Add the plugin's thread pool, the 1-minute tasks_sync job and the missed-run listener to the host's scheduler."""
    from apscheduler.events import EVENT_JOB_MISSED
    from apscheduler.executors.pool import ThreadPoolExecutor

    configure(data_dir=data_dir, source=source, gate=gate, slack_client_factory=slack_client_factory)
    for alias, workers in ((EXECUTOR, TASK_WORKERS), (DATA_EXECUTOR, DATA_SLOTS)):
        try:
            sched.add_executor(ThreadPoolExecutor(workers), alias=alias)
        except ValueError:  # installed on this scheduler before
            pass
    _CFG.update(sched=sched, executor=EXECUTOR)
    _SYNC.update(first=True, ok_at=_wall(), fails=0, alerted=False, purged=None)
    sched.add_job(tasks_sync, "interval", seconds=60, id="tasks_sync", replace_existing=True,
                  next_run_time=datetime.now(timezone.utc) + timedelta(seconds=5))
    sched.add_listener(_on_job_event, EVENT_JOB_MISSED)


def on_load_done(rc=0, note=None):
    """The host's data load ended (rc 0 = ok): send the data tasks that waited for it."""
    _release_held(rc, note)


def _log(event, **fields):
    rec = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
    print(json.dumps(rec), flush=True)


def data_dir():
    return _CFG["data_dir"] or os.environ.get("FOCUS_DATA_DIR", "/data")


def _source():
    return _CFG["source"] or NoData()


def _gate():
    return _CFG["gate"] or NoLoads()


def _utcnow():
    return datetime.now(timezone.utc)


def _wall():
    return time.time()


def health():
    """For the host's health check: when tasks_sync last worked (wall clock seconds) and failures since."""
    return {"last_ok": _SYNC["ok_at"], "fails": _SYNC["fails"]}


def _sync_failed(error):
    _SYNC["fails"] += 1
    _log("tasks_sync_failed", error=error[:300], in_a_row=_SYNC["fails"])
    if _SYNC["fails"] >= SYNC_ALERT_AFTER and not _SYNC["alerted"]:
        _SYNC["alerted"] = True
        _tell_admins(None, f":warning: Scheduled tasks are stuck: I can't read the task list ({error[:200]}). No "
                           f"task is sent until it's fixed. Check FOCUS_DATA_DIR/tasks.json and the agent log.")


def _sync_ok():
    if _SYNC["alerted"]:
        _tell_admins(None, "Scheduled tasks are working again.")
    _SYNC.update(ok_at=_wall(), fails=0, alerted=False)


def _slack_client():
    return _CFG["client"]() if _CFG["client"] else slack_api.Client.from_env()


def _should_hold(task, now):
    if task["kind"] not in DATA_KINDS:
        return False
    gate = _gate()
    if gate.running():
        return True
    nxt = gate.next_load_at()
    if nxt is None or nxt - now > HOLD_MAX:
        return False
    return not gate.fresh_since_last_slot(now)


TASK_PREFIX = "task_"
RUN_PREFIX = "taskrun_"     # a one-off run of a task: a retry after a temporary failure, a release, a late send
TASK_GRACE_S = 3600         # a send up to an hour late (busy pool, restart) still goes out; later is task_missed
# A run that fails for a temporary reason (Slack trouble that outlasted the in-call retries, a render that couldn't
# finish) is tried again RETRY_IN later, and the task is paused only after RETRY_MAX such runs in a row.
RETRY_IN = timedelta(minutes=5)
RETRY_MAX = 3
TASK_MIN_GAP = timedelta(minutes=55)
# Data tasks (report / query / ask) are sent after the data load (decided 2026-11-06): they wait while a scheduled
# load runs, or when one is due within HOLD_MAX; never longer than HOLD_MAX. Reminders never wait. Loads started from
# Slack (in the gateway process) are not seen here; they only change the data a few minutes earlier.
DATA_KINDS = ("report", "query", "ask")
HOLD_MAX = timedelta(hours=4)
NOTE_LOAD_FAILED = "_The data refresh failed this time, so this uses the last data._"
NOTE_LOAD_SLOW = "_The data refresh is taking longer than usual, so this uses the last data._"
_HELD = {}  # task id -> when it started waiting; also in FOCUS_DATA_DIR/tasks-held.json (survives a restart)
_HELD_LOCK = threading.Lock()
_SENDING = set()  # task ids whose send is running now (a slow `ask` can outlast the next tasks_sync)
_RESERVED = set()  # task ids taken off the held list whose run isn't booked or started yet
_SENDING_LOCK = threading.Lock()


def _held_path():
    return os.path.join(data_dir(), "tasks-held.json")


def _held_merge_from_disk():
    try:
        with open(_held_path(), encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        return
    if not isinstance(saved, dict):
        return
    for task_id, since in saved.items():
        try:
            _HELD.setdefault(task_id, datetime.fromisoformat(since))
        except (TypeError, ValueError):  # a broken entry: skip it
            _log("task_held_entry_skipped", task=str(task_id)[:40])


def _held_save():
    try:
        tmp = _held_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({k: v.isoformat() for k, v in _HELD.items()}, fh)
        os.replace(tmp, _held_path())
    except OSError as e:
        _log("task_held_save_failed", error=f"{type(e).__name__}: {str(e)[:200]}")


def _hold(task_id, now):
    with _HELD_LOCK:
        _held_merge_from_disk()
        _HELD.setdefault(task_id, now)
        _held_save()


def _take_held(older_than=None, now=None):
    """Remove and return the held task ids (all, or those waiting longer than `older_than`). Each id is returned
    once, so a task is never released twice."""
    with _HELD_LOCK:
        _held_merge_from_disk()
        ids = [t for t, since in _HELD.items() if older_than is None or now - since >= older_than]
        with _SENDING_LOCK:
            _RESERVED.update(ids)  # busy from now until its run is booked or starts
        for t in ids:
            _HELD.pop(t, None)
        if ids:
            _held_save()
    return ids


def _release(task_ids, note=None):
    """Send released tasks: booked as runs on the plugin's pool, or in this thread without a scheduler."""
    for task_id in task_ids:
        try:
            booked = _run_later(task_id, _utcnow(), note=note, why="release")
        except Exception as e:  # noqa: BLE001
            booked = False
            _log("task_book_failed", task=task_id, error=f"{type(e).__name__}: {str(e)[:200]}")
        if booked:
            with _SENDING_LOCK:
                _RESERVED.discard(task_id)  # the booked job keeps it busy now
        else:
            run_task(task_id, note=note, hold=False)


def _busy(task_id):
    """Held for a load, being sent now, or a run of it is booked: its one-time job has fired, so tasks_sync must not
    close it as missed."""
    with _SENDING_LOCK:
        if task_id in _SENDING or task_id in _RESERVED:
            return True
    if _booking(_CFG["sched"], task_id) is not None:
        return True
    with _HELD_LOCK:
        _held_merge_from_disk()
        return task_id in _HELD


def _task_trigger(task, now):

    from apscheduler.triggers.date import DateTrigger

    sched = task["schedule"]
    if "at" in sched:
        run_at = tasks.next_run(sched, now)
        return DateTrigger(run_date=run_at) if run_at else None
    return tasks.cron_trigger(sched["cron"], ZoneInfo(sched.get("tz") or "UTC"))  # cron weekday numbering


def _tell_owner(client, task, text):
    """DM the task's owner; when that fails (e.g. they have left), tell the report admins instead."""
    try:
        (client or _slack_client()).post({"type": "user", "id": task["owner"]}, text, tries=1)
        return
    except Exception as e:  # noqa: BLE001 - never take the API down; the token is never logged
        _log("task_notice_failed", task=task["id"], error=f"{type(e).__name__}: {str(e)[:200]}")
    _tell_admins(client, f"I couldn't reach <@{task['owner']}> about their task {task['id']}: {text}")


def _tell_admins(client, text):
    for admin in sorted(tasks.Settings.from_env().admins):
        try:
            (client or _slack_client()).post({"type": "user", "id": admin}, text, tries=1)
        except Exception as e:  # noqa: BLE001
            _log("admin_notice_failed", admin=admin, error=f"{type(e).__name__}: {str(e)[:200]}")


def _executor_for(kind):
    if not _CFG.get("executor"):
        return "default"
    return DATA_EXECUTOR if kind in DATA_KINDS else EXECUTOR


def _booking(sched, task_id):
    """A booked run of this task (job taskrun_<id>@<n>), or None."""
    if sched is None:
        return None
    mark = f"{RUN_PREFIX}{task_id}@"
    return next((j for j in sched.get_jobs() if j.id.startswith(mark)), None)


def _run_later(task_id, at, note=None, why="retry", kind=None):
    """Book one run of the task at `at`; False when there is no scheduler to book it on. Each booking has its own
    job id: APScheduler removes a finished date job by id right after starting it, which would drop a new booking
    made under the same id by that run."""
    sched = _CFG["sched"]
    if sched is None:
        return False
    if kind is None:
        try:
            kind = tasks.get(data_dir(), task_id)["kind"]
        except Exception:  # noqa: BLE001 - unknown: the general pool
            kind = None
    sched.add_job(run_task, "date", run_date=at, args=(task_id,), kwargs={"note": note, "hold": False},
                  id=f"{RUN_PREFIX}{task_id}@{next(_BOOKING_IDS)}", name=why, misfire_grace_time=TASK_GRACE_S,
                  executor=_executor_for(kind))
    return True


def _temporary(client, task, reason):
    """This run failed for a reason that may pass: try again in RETRY_IN; pause after RETRY_MAX runs in a row."""
    count = tasks.note_failure(data_dir(), task["id"])
    if count >= RETRY_MAX:
        return _pause_and_tell(client, task, f"it failed {count} times in a row ({reason.rstrip('.')}).")
    at = _utcnow() + RETRY_IN
    booked = _run_later(task["id"], at, kind=task.get("kind"))
    _log("task_retry", task=task["id"], kind=task["kind"], attempt=count, reason=reason[:300],
         at=at.isoformat(timespec="seconds") if booked else None)


def _pause_and_tell(client, task, reason):
    tk = tasks
    tk.auto_pause(data_dir(), task["id"], reason=reason, now=_utcnow(), settings=tk.Settings.from_env())
    _log("task_failed", task=task["id"], kind=task["kind"], reason=reason[:300])
    _tell_owner(client, task, f":warning: I paused your task *{task['name']}* ({task['id']}): {reason} "
                              f"Say \"resume task {task['id']}\" when it's fixed.")


def _release_held(rc=0, note=None):
    """Send the data tasks that waited for a load (with a note when it failed)."""
    _release(_take_held(), note=note or (None if rc == 0 else NOTE_LOAD_FAILED))


@contextmanager
def _data_slot(task):
    """At most DATA_SLOTS data tasks render at once (each starts a child process); reminders never wait."""
    if task["kind"] not in DATA_KINDS:
        yield
        return
    with _DATA_SLOTS:
        yield


def run_task(task_id, note=None, hold=True):
    """One send of one task. Never raises; a second run of the same task while one is going is skipped."""
    with _SENDING_LOCK:
        _RESERVED.discard(task_id)
        if task_id in _SENDING:
            _log("task_skipped", task=task_id, reason="already being sent")
            return
        _SENDING.add(task_id)
    try:
        _run_task(task_id, note=note, hold=hold)
    except Exception as e:  # noqa: BLE001 - a scheduled send must never take the agent down
        reason = f"{type(e).__name__}: {str(e)[:300]}"
        _log("task_failed", task=task_id, reason=reason)
        try:  # e.g. no Slack client: counts as a temporary failure (try again, pause after RETRY_MAX)
            _temporary(None, tasks.get(data_dir(), task_id), "it stopped with an error (details in the agent log)")
        except Exception as e2:  # noqa: BLE001
            _log("task_failed", task=task_id, reason=f"{type(e2).__name__}: {str(e2)[:300]}")
    finally:
        with _SENDING_LOCK:
            _SENDING.discard(task_id)


def _run_task(task_id, note=None, hold=True):
    tk = tasks
    d, now, settings = data_dir(), _utcnow(), tk.Settings.from_env()
    try:
        task = tk.get(d, task_id)
    except tk.TaskError:
        return
    if task["status"] != "active":
        return
    since_sent = now - datetime.fromisoformat(task["last_sent_at"]) if task.get("last_sent_at") else None
    if since_sent is not None and timedelta(0) <= since_sent < TASK_MIN_GAP:
        _log("task_skipped", task=task_id, reason="sent less than an hour ago")
        return
    if since_sent is not None and since_sent < timedelta(0):  # the clock jumped back: don't block sends on it
        _log("task_clock_skew", task=task_id, last_sent_at=task["last_sent_at"])
    if hold and _should_hold(task, now):
        _hold(task_id, now)
        _log("task_held", task=task_id, reason="waiting for the data load")
        return
    sending = False
    client = _slack_client()
    tgt = task["target"]
    if tgt["type"] == "channel" and task["owner"] not in settings.creators | settings.admins:
        return _pause_and_tell(client, task, "only people your admin has listed can have tasks post in a channel, "
                                             "and you're not on that list now. Ask your admin, or move it to your DM.")
    try:
        code = client.check_target(task["target"])
        if code in slack_api.TEMPORARY:
            return _temporary(client, task, slack_api.MESSAGES[code])
        if code != "ok":
            return _pause_and_tell(client, task, slack_api.MESSAGES[code])
        if task["kind"] == "ask" and not tk.take_ask_slot(d, now, settings):
            _log("task_skipped", task=task_id, reason="ask limit reached")
            if "at" in task["schedule"]:  # a one-time task: closed, not tried again every minute
                tk.mark_missed(d, task_id, now=now, settings=settings, action="skipped")
            else:
                tk.note_ran(d, task_id, at=now)
            return _tell_owner(client, task, f"I skipped your task *{task['name']}* ({task['id']}) today: the daily "
                                             f"limit of {settings.ask_max_per_day} questions is used up. "
                                             "It runs again next time.")
        try:
            with _data_slot(task):
                text, through = kinds.render(task, _source(), settings, now)
        except kinds.RenderError as e:  # it couldn't finish (no answer in time, the data couldn't be read): try again
            if task["kind"] == "ask" and not e.spent:  # the agent never ran (e.g. the data couldn't be read)
                tk.give_back_ask_slot(d, now)
            return _temporary(client, task, str(e))
        except Exception as e:  # noqa: BLE001 - e.g. a saved query no longer fits the data
            return _pause_and_tell(client, task, f"it couldn't run ({type(e).__name__}: {str(e)[:200]}).")
        if text is None:  # a conditional task whose condition isn't met: nothing to send today
            _log("task_skipped", task=task_id, kind=task["kind"], reason="condition not met")
            if "at" in task["schedule"]:
                tk.mark_sent(d, task_id, at=now, data_through=None, settings=settings)  # one-time: done
            else:
                tk.note_ran(d, task_id, at=now)
            return
        text = f"{text}\n{note}" if note else text
        if secret_guard.contains_secret(text):  # e.g. an `ask` answer: never post a secret
            return _pause_and_tell(client, task, "its text contained a password or key from this system, so I didn't "
                                                 "post it.")
        tk.mark_sending(d, task_id, at=now)
        sending = True
        client.post(task["target"], text)
    except Exception as e:  # noqa: BLE001 - a Slack error while posting
        code = getattr(e, "code", "")
        if code == "slack_unsure":  # it may have gone out: count it as sent, never post it twice
            _log("task_send_unconfirmed", task=task_id, kind=task["kind"])
            return _record_sent(d, task, now, through, settings)
        if sending:
            tk.clear_sending(d, task_id)
        if code in slack_api.TEMPORARY:
            return _temporary(client, task, slack_api.MESSAGES[code])
        msg = slack_api.MESSAGES.get(code, f"Slack refused the post ({str(e)[:100]}).")
        return _pause_and_tell(client, task, msg)
    if _record_sent(d, task, now, through, settings):
        _log("task_sent", task=task_id, kind=task["kind"], target=task["target"]["type"])


def _record_sent(d, task, now, through, settings):
    """mark_sent after a post, tried a few times. If it still fails the post stands (sending_at stays set, so a
    restart won't send it again) and nothing is retried: a retry would post a second time."""
    for attempt in range(3):
        try:
            tasks.mark_sent(d, task["id"], at=now, data_through=through, settings=settings)
            return True
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {str(e)[:200]}"
            if attempt < 2:
                time.sleep(0.2)
    _log("task_mark_failed", task=task["id"], error=err)
    return False


_VERB = {"remove": "stop", "pause": "pause", "resume": "resume", "edit": "change"}


def _notify(client, target, text, task_id):
    try:
        client.post(target, text)
    except Exception as e:  # noqa: BLE001
        _log("task_notice_failed", task=task_id, error=f"{type(e).__name__}: {str(e)[:200]}")


def _announce_requests(client, result):
    """Day-6 reminders to owners; results of auto-accepted (or failed) requests to both people."""
    for req in result["reminders"]:
        _notify(client, {"type": "user", "id": req["owner"]},
                f"Reminder: <@{req['by']}> asked to {_VERB.get(req['action'], 'change')} your task {req['task_id']}. "
                f"It will happen tomorrow unless you reply \"no {req['id']}\".", req["task_id"])
    for req in result.get("expired", []):
        where = req["where"]
        place = {"type": "channel", "id": where} if where[:1] in ("C", "G") else {"type": "user", "id": req["by"]}
        _notify(client, place, f"<@{req['by']}>, your request to change task {req['task_id']} expired: "
                               f"<@{req['owner']}> didn't answer within 7 days. A change to where it posts or what "
                               "it says needs the person who set it up or an admin.", req["task_id"])
    for key in ("accepted", "failed"):
        for req in result[key]:
            verb = _VERB.get(req["action"], "change")
            text = (f"<@{req['by']}>, your request to {verb} task {req['task_id']} went through: <@{req['owner']}> "
                    "gave no answer within 7 days." if key == "accepted" else
                    f"<@{req['by']}>, your request to {verb} task {req['task_id']} couldn't be applied: "
                    f"{req.get('reason', '')}")
            where = req["where"]
            place = {"type": "channel", "id": where} if where[:1] in ("C", "G") else {"type": "user", "id": req["by"]}
            _notify(client, place, text, req["task_id"])
            if key == "accepted":
                _notify(client, {"type": "user", "id": req["owner"]},
                        f"Your task {req['task_id']} was set to {verb} as <@{req['by']}> asked (no answer in 7 days).",
                        req["task_id"])


def tasks_sync(scheduler=None):
    """Make the scheduler's task_<id> jobs match tasks.json. Runs every minute. Never raises.

    Each part is on its own: a failing request, a broken held entry or one broken task never stops the rest."""
    sched = scheduler or _CFG["sched"]
    if sched is None:
        return
    tk = tasks
    d, now = data_dir(), _utcnow()
    try:
        client = _slack_client()
    except Exception as e:  # noqa: BLE001 - without a client, leave requests for the next minute
        client = None
        _log("tasks_sync_no_slack", error=f"{type(e).__name__}: {str(e)[:200]}")
    if client is not None:
        try:
            result = tk.process_requests(d, now, tk.Settings.from_env())
            if any(result.values()):
                _announce_requests(client, result)
        except Exception as e:  # noqa: BLE001
            _log("tasks_requests_failed", error=f"{type(e).__name__}: {str(e)[:300]}")
    try:
        _release(_take_held(older_than=HOLD_MAX, now=now), note=NOTE_LOAD_SLOW)  # never wait longer than HOLD_MAX
    except Exception as e:  # noqa: BLE001
        _log("tasks_held_failed", error=f"{type(e).__name__}: {str(e)[:300]}")
    try:
        active = tk.active_tasks(d)
    except Exception as e:  # noqa: BLE001 - the store can't be read: keep the jobs as they are
        return _sync_failed(f"{type(e).__name__}: {e}")
    first = _SYNC["first"]
    wanted = set()
    for task in active:
        try:
            _sync_task(sched, task, now, first, wanted)
        except (KeyError, ValueError, TypeError, AttributeError, tasks.TaskError) as e:  # its data is broken
            wanted.discard(TASK_PREFIX + str(task.get("id")))
            _bad_task(task, e)
        except Exception as e:  # noqa: BLE001 - not the task's fault (scheduler, disk): keep it as it is
            job_id = TASK_PREFIX + str(task.get("id"))
            if sched.get_job(job_id) is not None:
                wanted.add(job_id)
            _log("task_sync_failed", task=str(task.get("id")), error=f"{type(e).__name__}: {str(e)[:300]}")
    try:
        for job in sched.get_jobs():
            if job.id.startswith(TASK_PREFIX) and job.id not in wanted:
                job.remove()
    except Exception as e:  # noqa: BLE001
        return _sync_failed(f"{type(e).__name__}: {e}")
    _SYNC["first"] = False
    _sync_ok()
    if _SYNC["purged"] != now.date():
        _SYNC["purged"] = now.date()
        try:
            gone = tk.purge_closed(d, now)
            if gone:
                _log("tasks_purged", tasks=gone)
        except Exception as e:  # noqa: BLE001
            _log("tasks_purge_failed", error=f"{type(e).__name__}: {str(e)[:300]}")


def _sync_task(sched, task, now, first, wanted):
    job_id = TASK_PREFIX + task["id"]
    sig = json.dumps(task["schedule"], sort_keys=True)
    job = sched.get_job(job_id)
    if job is not None and job.name == sig:
        wanted.add(job_id)
        return
    if job is not None:  # the schedule changed: replace the job
        job.remove()
    trigger = _task_trigger(task, now)
    if trigger is None:  # a one-time task whose time has passed
        return _one_time_passed(task, now)
    if first and "cron" in task["schedule"]:
        _catch_up(task, trigger, now)
    sched.add_job(run_task, trigger, args=(task["id"],), id=job_id, name=sig, replace_existing=True,
                  misfire_grace_time=TASK_GRACE_S, coalesce=True, max_instances=1,
                  executor=_executor_for(task["kind"]))
    wanted.add(job_id)


def _bad_task(task, error):
    """A task tasks_sync can't schedule: pause it and tell its owner, once."""
    key = (str(task.get("id")), f"{type(error).__name__}: {str(error)[:200]}")
    told = key in _BAD
    _BAD.add(key)
    _log("task_broken", task=key[0], error=key[1])
    reason = f"its saved settings can't be read ({type(error).__name__}). Ask me to set it up again."
    try:
        tasks.auto_pause(data_dir(), task["id"], reason=reason, now=_utcnow(), settings=tasks.Settings.from_env())
        if task.get("owner") and not told:
            _tell_owner(None, task, f":warning: I paused your task *{task.get('name', task['id'])}* "
                                               f"({task['id']}): {reason}")
    except Exception as e:  # noqa: BLE001
        _log("task_broken_notice_failed", task=key[0], error=f"{type(e).__name__}: {str(e)[:200]}")


def _one_time_passed(task, now):
    """A one-time task with no job and its time passed: it fired (busy), went out as the agent stopped (closed), is a
    little late (sent now), or was missed while the agent was down (closed, owner told)."""
    tk, d = tasks, data_dir()
    if _busy(task["id"]):  # held for the load, being sent, or a run of it is booked
        return
    settings = tk.Settings.from_env()
    if task.get("sending_at"):  # it was posting when the agent stopped: never send it twice
        tk.mark_missed(d, task["id"], now=now, settings=settings, action="send_unconfirmed")
        _log("task_send_unconfirmed", task=task["id"], sending_at=task["sending_at"])
        return
    late = now - tk.scheduled_at(task["schedule"])
    if late <= timedelta(seconds=TASK_GRACE_S):  # e.g. made seconds before its time, or a short restart
        _run_later(task["id"], now, why="late", kind=task["kind"])
        _log("task_late", task=task["id"], minutes=int(late.total_seconds() // 60))
        return
    tk.mark_missed(d, task["id"], now=now, settings=settings)
    _log("task_missed", task=task["id"], scheduled=task["schedule"].get("at"))
    _tell_owner(None, task, f"I missed your one-time task *{task['name']}* ({task['id']}): "
                                       "the agent was not running at that time. Ask me again to set it "
                                       "for a new time.")


CATCH_UP_LOOKBACK = timedelta(days=1)


def _last_fire_before(trigger, now, since):
    last, t = None, trigger.get_next_fire_time(None, since)
    while t is not None and t < now:
        last = t
        t = trigger.get_next_fire_time(t, t + timedelta(seconds=1))
    return last


def _when(iso):
    return datetime.fromisoformat(iso) if iso else None


def _catch_up(task, trigger, now):
    """First sync after a start: the run missed while the agent was down goes out now when it is at most
    TASK_GRACE_S late; an older one (within CATCH_UP_LOOKBACK) is only told to the owner."""
    if _busy(task["id"]):  # held, being sent or booked: that run is under way
        return
    prev = _last_fire_before(trigger, now, now - CATCH_UP_LOOKBACK)
    if prev is None:
        return
    done = [t for t in (_when(task.get("last_sent_at")), _when(task.get("sending_at")),
                        _when(task.get("created_at"))) if t is not None]
    if done and max(done) >= prev:  # sent (or posting) since, or made after that run
        return
    if now - prev <= timedelta(seconds=TASK_GRACE_S):
        _run_later(task["id"], now, why="late", kind=task["kind"])
        _log("task_late", task=task["id"], scheduled=prev.isoformat(timespec="minutes"))
        return
    _log("task_missed", task=task["id"], scheduled=prev.isoformat(timespec="minutes"))
    zone = ZoneInfo(task["schedule"].get("tz") or "UTC")
    nxt = trigger.get_next_fire_time(None, now)
    _tell_owner(None, task,
                f"I missed your task *{task['name']}* ({task['id']}) at {prev.astimezone(zone):%Y-%m-%d %H:%M}: the "
                "agent was not running then." + (f" It runs again at {nxt.astimezone(zone):%Y-%m-%d %H:%M}."
                                                 if nxt else ""))


def _on_job_event(event):
    if getattr(event, "job_id", "").startswith(TASK_PREFIX):
        when = event.scheduled_run_time
        _log("task_missed", task=event.job_id[len(TASK_PREFIX):],
             scheduled=when.isoformat(timespec="seconds") if when else None)
