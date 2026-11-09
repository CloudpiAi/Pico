"""The Pico FinOps Agent's scheduler: the AWS cost data load, its retry, and the Slack notices and alerts.

It runs in APScheduler's own threads inside the one agent process (``pico_finops_agent.agent`` starts and stops it).
The 1-minute watch job also stamps a tick that the agent's health check reads (``last_tick``).

Env:    FOCUS_SCHEDULE="0 6,18 * * *"            when the job runs: cron-style string read by APScheduler
                                                 inside this process (5 fields, UTC); no OS cron is used
        FOCUS_JOB_CMD="pico focus-data load"     command the job runs (no shell)
        FOCUS_DATA_DIR=/data                     where the cache and the local state live
        SLACK_NOTIFY_CHANNEL=#channel           optional: post a note there after a load that brought new months
At start: a load runs at once when the cache is missing or the last success is older than 12 h.
A run that exits 1 (error / conflict) is retried once 30 minutes later; exit 2 needs a person.
"""
import calendar
import json
import os
import shlex
import subprocess
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import SchedulerNotRunningError
from apscheduler.triggers.cron import CronTrigger

from pico_finops_agent.log import describe
from pico_finops_agent.log import log_event as _log

DEFAULT_CRON = "0 6,18 * * *"
DEFAULT_CMD = "pico focus-data load"
JOB_ID = "pico_finops_agent_load"
WATCH_JOB_ID = "pico_finops_agent_watch"  # announces loads started from Slack
JOB_TIMEOUT_S = 3600  # a load may invoke the Lambda up to 4 times (partial) plus the cache rebuild
STALE_AFTER = timedelta(hours=12)
RETRY_AFTER = timedelta(minutes=30)
RC_RETRY = 1  # the plugin's "error / conflict" exit code; 2 (a person must act) is not retried
_SCHEDULER = None
# The load job and the watch run in different threads: every alerts.json read-modify-write holds this.
_ANNOUNCE_LOCK = threading.RLock()
_LAST_ANNOUNCED = None  # backup for alerts.json `announced` if it can't be saved
_LAST_TICK = None  # when the 1-minute watch last ran (time.time()); the health check reads it
_LOADS_RUNNING = 0  # data loads in progress (the load job and its retry); the health check reads it
_LOADS_LOCK = threading.Lock()


def job_cron():
    """The customer's FOCUS_SCHEDULE (.env), else twice a day. Parsed by APScheduler, not the OS."""
    return os.environ.get("FOCUS_SCHEDULE") or DEFAULT_CRON


def job_command():
    return os.environ.get("FOCUS_JOB_CMD", DEFAULT_CMD)


def data_dir():
    return os.environ.get("FOCUS_DATA_DIR", "/data")


def startup_due(directory=None, now=None):
    """Load at start when there is no cache yet or the last success is older than 12 h."""
    directory = directory or data_dir()
    now = now or datetime.now(timezone.utc)
    if not os.path.exists(os.path.join(directory, "focus.duckdb")):
        return True
    try:
        with open(os.path.join(directory, "state.json"), encoding="utf-8") as fh:
            state = json.load(fh)
        last = datetime.fromisoformat(state["last_success"])
    except (OSError, ValueError, KeyError, TypeError):
        return True
    if state.get("load_in_progress"):
        return True  # the last load was cut short (a stop or restart): finish the job now
    return now - last > STALE_AFTER


def _money(x):
    return f"${x:,.2f}"


def _compare(value, base):
    """'about the same as' within 10%, else 'N% above' / 'N% below'."""
    change = (value - base) / base * 100
    if abs(change) < 10:
        return "about the same as"
    return f"{abs(round(change))}% {'above' if change > 0 else 'below'}"


def notice_text(months, insights=None):
    """The post-load Slack message: what's new, how the last full day and the month compare, and what's unusual.

    All numbers come from the plugin's SQL insights (state.json); without them, a one-line notice."""
    if not insights:
        names = [f"{calendar.month_name[int(m[5:7])]} {m[:4]}" for m in sorted(months)]
        joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        verb = "was" if len(names) == 1 else "were"
        return f":bar_chart: Fresh cost data is in: {joined} {verb} updated from the latest AWS export. Ask me anything about it."
    day = date.fromisoformat(insights["last_full_day"])
    lines = [f":bar_chart: Fresh cost data is in, through *{day.strftime('%a')} {day.day} {day.strftime('%b')}*."]
    latest, avg = insights.get("latest_day_cost", 0), insights.get("avg7", 0)
    if avg > 0:
        lines.append(f"• That day came to *{_money(latest)}*, {_compare(latest, avg)} your usual *{_money(avg)}/day*.")
    else:
        lines.append(f"• That day came to *{_money(latest)}*.")
    mtd = insights.get("mtd") or {}
    month = calendar.month_name[day.month]
    if mtd.get("prev_same_days", 0) > 0:
        change = (mtd["cost"] - mtd["prev_same_days"]) / mtd["prev_same_days"] * 100
        prev_month = calendar.month_name[(day.month - 2) % 12 + 1]
        how = "about the same as" if abs(change) < 10 else f"{abs(round(change))}% {'higher' if change > 0 else 'lower'} than"
        lines.append(f"• {month} so far: *{_money(mtd['cost'])}*, {how} the same days in {prev_month}.")
    elif mtd:
        lines.append(f"• {month} so far: *{_money(mtd.get('cost', 0))}*.")
    spikes, stopped = insights.get("spikes") or [], insights.get("stopped") or []
    if spikes:
        lines.append(":warning: *Sudden increases worth a look:*")
        for s in spikes:
            label = "Service" if s["kind"] == "service" else "Resource"
            lines.append(f"   • {label} `{s['name']}`: *{_money(s['today'])}* vs {_money(s['avg7'])}/day before")
    for s in stopped:
        lines.append(f"• {s['name']} had no charges that day (usually {_money(s['avg7'])}/day); "
                     "worth checking if that's not expected.")
    new = insights.get("new_resources") or {}
    if new.get("count"):
        n = new["count"]
        lines.append(f"• {n} new resource{'s' if n != 1 else ''} showed up, together {_money(new['cost'])}.")
    if not spikes and not stopped:
        lines.append("• Nothing unusual stood out.")
    lines.append("Ask me about any of these.")
    return "\n".join(lines)


def _post_slack(token, channel, text):
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps({"channel": channel, "text": text}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        body = json.loads(r.read() or b"{}")
    if not body.get("ok"):
        raise RuntimeError(f"Slack said: {body.get('error', 'unknown error')}")


STALE_DAYS_DEFAULT = 3


def _today():
    return datetime.now(timezone.utc).date()


def _stale_days():
    try:
        n = int(os.environ.get("FOCUS_ALERT_STALE_DAYS", "").strip())
    except ValueError:
        return STALE_DAYS_DEFAULT
    return n if n > 0 else STALE_DAYS_DEFAULT


def _day_label(iso):
    d = date.fromisoformat(iso)
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def _alerts_path():
    return os.path.join(data_dir(), "alerts.json")


def _read_alerts():
    try:
        with open(_alerts_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"failure": None, "stale": False}


def _write_alerts(alerts):
    tmp = _alerts_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(alerts, fh)
    os.replace(tmp, _alerts_path())


def _post(token, channel, text, event, **kw):
    try:
        _post_slack(token, channel, text)
        _log(event, channel=channel, **kw)
        return True
    except Exception as e:  # a failed post must never take the API down; the token is never logged
        _log("slack_notice_failed", channel=channel, error=describe(e))
        return False


def failure_text(problem, data_through):
    text = f":warning: I couldn't refresh the cost data: {problem['summary']}. {problem['action']}"
    if data_through:
        text += f" Until then, answers use data through {_day_label(data_through)}."
    return text


def _notify_after_load(rc, final=True):
    """After a load, post in SLACK_NOTIFY_CHANNEL (optional): fresh data with insights, a failure
    once per problem (after the retry, or at once when a retry can't help), a recovery, stale data."""
    with _ANNOUNCE_LOCK:
        _after_load(rc, final)


def _after_load(rc, final):
    channel, token = os.environ.get("SLACK_NOTIFY_CHANNEL"), os.environ.get("SLACK_BOT_TOKEN")
    if not channel or not token:
        return
    try:
        with open(os.path.join(data_dir(), "state.json"), encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return
    alerts = _read_alerts()
    last = state.get("last_result") or {}
    through = state.get("data_through")
    if rc != 0:
        problem = state.get("problem")
        if final and problem and alerts.get("failure") != problem.get("code"):
            if _post(token, channel, failure_text(problem, through), "slack_alert_sent", problem=problem.get("code")):
                alerts["failure"] = problem.get("code")
                _write_alerts(alerts)
        return
    if alerts.get("failure"):
        if _post(token, channel, ":white_check_mark: Cost data is refreshing again; the earlier problem is fixed.",
                 "slack_recovery_sent"):
            alerts["failure"] = None
            _write_alerts(alerts)
    alerts = _announce(token, channel, state, alerts)
    if through:
        age = (_today() - date.fromisoformat(through)).days
        if age > _stale_days() and not alerts.get("stale"):
            text = (f":warning: No new AWS cost data for {age} days: the newest is from {_day_label(through)}. "
                    "The AWS FOCUS export may have stopped; check Billing and Cost Management -> Data Exports.")
            if _post(token, channel, text, "slack_stale_sent", days=age):
                alerts["stale"] = True
                _write_alerts(alerts)
        elif age <= _stale_days() and alerts.get("stale"):
            alerts["stale"] = False
            _write_alerts(alerts)


def _read_state():
    try:
        with open(os.path.join(data_dir(), "state.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _announce(token, channel, state, alerts):
    """Post the fresh-data notice for the last load once, whoever started it (schedule or Slack).
    alerts.json `announced` holds the `last_result.at` of the load already posted."""
    global _LAST_ANNOUNCED
    with _ANNOUNCE_LOCK:
        alerts = _read_alerts()  # the other thread may have just posted it
        last = state.get("last_result") or {}
        months = last.get("months_loaded") or []
        at = last.get("at")
        if not months or (at is not None and at in (alerts.get("announced"), _LAST_ANNOUNCED)):
            return alerts
        if _post(token, channel, notice_text(months, last.get("insights")), "slack_notice_sent", months=sorted(months)):
            _LAST_ANNOUNCED = alerts["announced"] = at
            try:
                _write_alerts(alerts)
            except OSError as e:  # the in-memory copy stops a repeat every minute
                _log("alerts_save_failed", error=describe(e))
        return alerts


def announce_new_data():
    """A load asked for in Slack runs as its own `pico focus-data load` process, so its notice is posted from here."""
    state = _read_state()
    if state and not state.get("problem"):
        _notify_after_load(0)  # recovery, notice and stale check, each posted once


def mark_current_load_announced():
    """At start, treat a load from before this version as already posted, so an upgrade doesn't repost it."""
    alerts, state = _read_alerts(), _read_state()
    if "announced" in alerts or not state:
        return  # already tracked, or a new install with nothing loaded yet
    alerts["announced"] = (state.get("last_result") or {}).get("at")
    try:
        _write_alerts(alerts)
    except OSError as e:  # never stop the agent over this
        _log("announce_mark_failed", error=describe(e))


def load_running():
    with _LOADS_LOCK:
        return _LOADS_RUNNING > 0


def _run_job(retry=True):
    global _LOADS_RUNNING
    with _LOADS_LOCK:
        _LOADS_RUNNING += 1
    try:
        _load(retry)
    finally:
        with _LOADS_LOCK:
            _LOADS_RUNNING -= 1


def _load(retry):
    cmd = job_command()
    t0 = time.perf_counter()
    rc = None
    try:
        rc = subprocess.run(shlex.split(cmd), timeout=JOB_TIMEOUT_S).returncode
        _log("scheduled_load_done", cmd=cmd, rc=rc, seconds=round(time.perf_counter() - t0, 2))
        _notify_after_load(rc, final=not (rc == RC_RETRY and retry and _SCHEDULER is not None))
    except Exception as e:  # a failed run must never take the agent down
        _log("scheduled_load_failed", cmd=cmd, error=describe(e, 300), seconds=round(time.perf_counter() - t0, 2))
        rc = RC_RETRY
    if rc == RC_RETRY and retry and _SCHEDULER is not None:
        at = datetime.now(timezone.utc) + RETRY_AFTER
        _SCHEDULER.add_job(_run_job, "date", run_date=at, args=(False,), id="pico_finops_agent_retry", replace_existing=True)
        _log("retry_scheduled", at=at.isoformat(timespec="seconds"))


def build_scheduler():
    global _SCHEDULER
    sched = BackgroundScheduler(job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300})
    sched.add_job(_run_job, CronTrigger.from_crontab(job_cron(), timezone="UTC"), id=JOB_ID, replace_existing=True)
    sched.add_job(watch, "interval", minutes=1, id=WATCH_JOB_ID, replace_existing=True)
    _SCHEDULER = sched
    return sched


def watch():
    """The 1-minute job: post what a load started from Slack brought, and show the scheduler is alive."""
    global _LAST_TICK
    try:
        announce_new_data()
    finally:
        _LAST_TICK = time.time()  # stamped even when the post fails: a Slack problem isn't a stuck scheduler


def last_tick():
    return _LAST_TICK


def start_scheduler(scheduler=None):
    """Start the jobs; also the load at once when the data is missing or old (see startup_due)."""
    global _SCHEDULER, _LAST_TICK
    scheduler = scheduler or build_scheduler()
    _SCHEDULER = scheduler
    mark_current_load_announced()
    _LAST_TICK = time.time()  # the first watch runs a minute from now
    scheduler.start()
    if startup_due():
        scheduler.add_job(_run_job, id="pico_finops_agent_startup", replace_existing=True)  # runs now
    _log("scheduler_started", jobs=[j.id for j in scheduler.get_jobs()], schedule=job_cron(),
         next_runs={j.id: (j.next_run_time.isoformat() if j.next_run_time else None) for j in scheduler.get_jobs()})
    return scheduler


def stop_scheduler(scheduler):
    """Stop the jobs. A load still running is cut short when the agent exits (its files are written atomically,
    and the next load notices the `load_in_progress` mark and rebuilds the local copy)."""
    global _SCHEDULER
    if load_running():
        _log("load_interrupted", reason="the agent is stopping; the next load finishes the job")
    try:
        scheduler.shutdown(wait=False)
    except SchedulerNotRunningError:
        return
    if _SCHEDULER is scheduler:
        _SCHEDULER = None
    _log("scheduler_stopped")
