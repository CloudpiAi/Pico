"""Scheduled tasks in the standalone agent: pico_plugin_schedule.runner with the agent's FOCUS load as its LoadGate (PRD R6-R8, R14, R17, R19): tasks_sync keeps one job per active task, each send
re-checks Slack, a failed send pauses the task and DMs the creator once, one-time tasks end as `done`."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("apscheduler", reason="Needs the pico-finops-agent extra")
from apscheduler.schedulers.background import BackgroundScheduler  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "schedule"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "focus"))
from pico_finops_agent import scheduler as agent_api  # noqa: E402
from pico_plugin_schedule import (  # noqa: E402
    runner,  # noqa: E402
    slack_api,
    tasks,
)

from ..schedule.test_slack_api import FakeSlack  # noqa: E402

RAVI, MEENA = "U0RAVI0001", "U0MEENA001"
NOW = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)
TEAM = {"type": "channel", "id": "C0TEAM0001"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("FOCUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    monkeypatch.setenv("FOCUS_REPORT_CREATORS", RAVI)  # Ravi may post tasks to channels
    for k in ("FOCUS_REPORT_CHANNELS", "FOCUS_ASK_MAX_PER_DAY"):
        monkeypatch.delenv(k, raising=False)
    state = {"member": True}
    fake = FakeSlack(
        conversations_info=lambda p: {"ok": True, "channel": {"id": p["channel"], "is_member": state["member"]}},
        conversations_open=lambda p: {"ok": True, "channel": {"id": "D0" + p["users"][2:]}},
        chat_postMessage={"ok": True, "ts": "1.1"},
    )
    monkeypatch.setattr(runner, "_slack_client", lambda: slack_api.Client("xoxb-test-token", transport=fake))
    monkeypatch.setattr(runner, "_utcnow", lambda: NOW)
    runner.configure(data_dir=str(tmp_path), gate=agent_api._FocusLoadGate())  # the standalone's wiring
    monkeypatch.setattr(agent_api, "_TASKS", runner)
    monkeypatch.setattr(agent_api, "_next_load_at", lambda: None)  # no load due unless a test says so
    monkeypatch.setattr(agent_api, "_LOAD", {"running": False})
    monkeypatch.setattr(runner, "_HELD", {})
    monkeypatch.setattr(runner.kinds, "render", lambda t, *a, **k: (f"TEXT {t['id']}", "2026-11-04"))
    d = str(tmp_path)

    def add(**kw):
        args = dict(owner=RAVI, where="C0TEAM0001", kind="reminder", name="n", target=TEAM,
                    schedule={"cron": "0 15 * * *", "tz": "Asia/Kolkata"}, payload={"text": "x"}, now=NOW,
                    settings=tasks.Settings(creators=frozenset({RAVI})))
        args.update(kw)
        return tasks.add(d, **args)

    def posts():
        return [p for m, p in fake.calls if m == "chat.postMessage"]

    return d, add, fake, state, posts


def test_sync_adds_changes_and_removes_jobs(world):
    d, add, *_ = world
    sched = BackgroundScheduler()
    t = add()
    runner.tasks_sync(sched)
    job = sched.get_job(f"task_{t['id']}")
    assert job is not None and str(job.trigger.timezone) == "Asia/Kolkata"
    tasks.change(d, t["id"], "edit", by=RAVI, where="x", changes={"schedule": {"cron": "0 9 * * *", "tz": "UTC"}},
                 now=NOW, settings=tasks.Settings())
    runner.tasks_sync(sched)
    assert str(sched.get_job(f"task_{t['id']}").trigger.timezone) == "UTC"
    tasks.change(d, t["id"], "remove", by=RAVI, where="x", now=NOW, settings=tasks.Settings())
    runner.tasks_sync(sched)
    assert sched.get_job(f"task_{t['id']}") is None


def test_one_time_uses_a_date_trigger(world):
    _, add, *_ = world
    sched = BackgroundScheduler()
    t = add(schedule={"at": "2026-11-06 10:00", "tz": "Asia/Kolkata"})
    runner.tasks_sync(sched)
    assert "date" in str(sched.get_job(f"task_{t['id']}").trigger)


def test_run_posts_and_records(world):
    d, add, _, _, posts = world
    t = add()
    runner.run_task(t["id"])
    assert posts()[-1] == {"channel": "C0TEAM0001", "text": f"TEXT {t['id']}"}
    saved = tasks.get(d, t["id"])
    assert saved["last_sent_at"] and saved["last_data_through"] == "2026-11-04"


def test_one_time_ends_as_done(world):
    d, add, *_ = world
    t = add(schedule={"at": "2026-11-06 10:00", "tz": "Asia/Kolkata"})
    runner.run_task(t["id"])
    assert tasks.get(d, t["id"])["status"] == "done"


def test_bot_removed_from_channel_pauses_and_dms_creator_once(world):
    d, add, _, state, posts = world
    t = add()
    state["member"] = False
    runner.run_task(t["id"])
    runner.run_task(t["id"])  # paused now: nothing more
    sent = posts()
    assert len(sent) == 1 and sent[0]["channel"] == "D0RAVI0001" and "/invite" in sent[0]["text"]
    assert tasks.get(d, t["id"])["status"] == "paused"
    assert tasks.history(d, t["id"])[-1]["action"] == "auto_paused"


def test_render_failure_pauses_too(world, monkeypatch):
    d, add, _, _, posts = world
    t = add(kind="query", payload={"sql": "SELECT 1"})

    def boom(*a, **k):
        raise RuntimeError("Binder Error: column x not found")

    monkeypatch.setattr(runner.kinds, "render", boom)
    runner.run_task(t["id"])
    assert tasks.get(d, t["id"])["status"] == "paused"
    assert "column x not found" in posts()[-1]["text"]


def test_not_sent_twice_within_an_hour(world):
    _, add, _, _, posts = world
    t = add()
    runner.run_task(t["id"])
    runner.run_task(t["id"])
    assert len(posts()) == 1


def test_paused_or_removed_tasks_do_not_run(world):
    d, add, _, _, posts = world
    t = add()
    tasks.change(d, t["id"], "pause", by=RAVI, where="x", now=NOW, settings=tasks.Settings())
    runner.run_task(t["id"])
    assert posts() == []


def test_ask_daily_limit(world, monkeypatch):
    d, add, _, _, posts = world
    monkeypatch.setenv("FOCUS_ASK_MAX_PER_DAY", "1")
    t1 = add(kind="ask", payload={"question": "q1"})
    t2 = add(kind="ask", payload={"question": "q2"})
    runner.run_task(t1["id"])
    runner.run_task(t2["id"])
    sent = posts()
    assert sent[0]["text"] == f"TEXT {t1['id']}"
    assert sent[1]["channel"] == "D0RAVI0001" and "limit" in sent[1]["text"]
    assert tasks.get(d, t2["id"])["status"] == "active"  # skipped today, not paused


def test_unanswered_requests_remind_then_go_through(world, monkeypatch):
    d, add, _, _, posts = world
    t = add()
    tasks.change(d, t["id"], "remove", by=MEENA, where="C0TEAM0001", now=NOW, settings=tasks.Settings())
    monkeypatch.setattr(runner, "_utcnow", lambda: NOW + timedelta(days=6, hours=1))
    runner.tasks_sync(BackgroundScheduler())
    assert any(p["channel"] == "D0RAVI0001" and "tomorrow" in p["text"] for p in posts())  # reminder to Ravi
    monkeypatch.setattr(runner, "_utcnow", lambda: NOW + timedelta(days=7, hours=1))
    runner.tasks_sync(BackgroundScheduler())
    assert tasks.get(d, t["id"])["status"] == "removed"
    assert any(p["channel"] == "C0TEAM0001" and "no answer" in p["text"] for p in posts())


def test_scheduler_has_the_sync_job(monkeypatch):
    sched = agent_api.build_scheduler()
    assert sched.get_job("tasks_sync") is not None and sched.get_job(agent_api.JOB_ID) is not None


def test_missed_send_is_logged(world, capsys):
    from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent

    runner._on_job_event(JobExecutionEvent(EVENT_JOB_MISSED, "task_t7", "default", NOW))
    assert '"event": "task_missed"' in capsys.readouterr().out



def test_one_time_missed_while_down_is_closed_and_owner_told(world, monkeypatch):
    d, add, _, _, posts = world
    t = add(schedule={"at": "2026-11-05 10:00", "tz": "UTC"})
    monkeypatch.setattr(runner, "_utcnow", lambda: NOW + timedelta(hours=3))  # the agent was down at 10:00
    runner.tasks_sync(BackgroundScheduler())
    assert tasks.get(d, t["id"])["status"] == "done"
    assert any(p["channel"] == "D0RAVI0001" and "missed" in p["text"] for p in posts())


def test_run_task_never_raises(world, monkeypatch):
    _, add, *_ = world
    t = add()

    def broken():
        raise RuntimeError("no client")

    monkeypatch.setattr(runner, "_slack_client", broken)
    runner.run_task(t["id"])  # logged, not raised



# ---------- D4: data tasks wait for the data load ----------

def _report(add):
    return add(kind="report", payload={"template": "month_to_date"})


def test_data_task_waits_while_a_load_runs_then_sends(world, monkeypatch):
    _, add, _, _, posts = world
    t = _report(add)
    agent_api._LOAD["running"] = True
    runner.run_task(t["id"])
    assert posts() == [] and t["id"] in runner._HELD
    agent_api._LOAD["running"] = False
    runner._release_held(rc=0)
    assert posts()[-1]["text"] == f"TEXT {t['id']}" and runner._HELD == {}


def test_data_task_waits_for_a_load_due_within_4_hours(world, monkeypatch):
    _, add, _, _, posts = world
    t = _report(add)
    monkeypatch.setattr(agent_api, "_next_load_at", lambda: NOW + timedelta(minutes=30))
    runner.run_task(t["id"])
    assert posts() == []


def test_load_more_than_4_hours_away_sends_now(world, monkeypatch):
    _, add, _, _, posts = world
    t = _report(add)
    monkeypatch.setattr(agent_api, "_next_load_at", lambda: NOW + timedelta(hours=5))
    runner.run_task(t["id"])
    assert posts()[-1]["text"] == f"TEXT {t['id']}"


def test_reminders_never_wait(world, monkeypatch):
    _, add, _, _, posts = world
    t = add()
    agent_api._LOAD["running"] = True
    runner.run_task(t["id"])
    assert posts()[-1]["text"] == f"TEXT {t['id']}"


def test_failed_load_sends_with_a_note(world):
    _, add, _, _, posts = world
    t = _report(add)
    agent_api._LOAD["running"] = True
    runner.run_task(t["id"])
    agent_api._LOAD["running"] = False
    runner._release_held(rc=2)
    assert "refresh failed" in posts()[-1]["text"]


def test_held_more_than_4_hours_is_sent_with_a_note(world, monkeypatch):
    _, add, _, _, posts = world
    t = _report(add)
    agent_api._LOAD["running"] = True
    runner.run_task(t["id"])
    monkeypatch.setattr(runner, "_utcnow", lambda: NOW + timedelta(hours=4, minutes=1))
    runner.tasks_sync(BackgroundScheduler())
    assert "taking longer" in posts()[-1]["text"]


def test_scheduled_load_releases_held_tasks(world, monkeypatch):
    _, add, _, _, posts = world
    t = _report(add)
    agent_api._LOAD["running"] = True
    runner.run_task(t["id"])
    agent_api._LOAD["running"] = False
    monkeypatch.setattr(agent_api.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(agent_api, "_notify_after_load", lambda *a, **k: None)
    agent_api._run_job(retry=False)
    assert posts()[-1]["text"] == f"TEXT {t['id']}" and agent_api._LOAD["running"] is False



# ---------- review 2026-11-06 ----------

def test_a_held_task_is_released_only_once(world):
    _, add, _, _, posts = world
    t = _report(add)
    agent_api._LOAD["running"] = True
    runner.run_task(t["id"])
    agent_api._LOAD["running"] = False
    runner._release_held(rc=0)
    runner._release_held(rc=0)
    assert len(posts()) == 1


def test_held_tasks_survive_a_restart(world, monkeypatch):
    _, add, _, _, posts = world
    t = _report(add)
    agent_api._LOAD["running"] = True
    runner.run_task(t["id"])
    monkeypatch.setattr(runner, "_HELD", {})        # the agent restarted: memory is empty
    agent_api._LOAD["running"] = False
    runner._release_held(rc=0)                       # the next load ends
    assert posts()[-1]["text"] == f"TEXT {t['id']}"


def test_a_failing_notice_does_not_turn_a_good_load_into_a_failed_one(world, monkeypatch):
    _, add, _, _, posts = world
    t = _report(add)
    agent_api._LOAD["running"] = True
    runner.run_task(t["id"])
    agent_api._LOAD["running"] = False
    monkeypatch.setattr(agent_api.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())

    def boom(*a, **k):
        raise RuntimeError("slack down")

    monkeypatch.setattr(agent_api, "_notify_after_load", boom)
    agent_api._run_job(retry=False)
    assert "refresh failed" not in posts()[-1]["text"]


def test_channel_task_of_someone_no_longer_listed_is_paused(world, monkeypatch):
    d, add, _, _, posts = world
    t = add()                                           # Ravi was listed when he made it
    monkeypatch.setenv("FOCUS_REPORT_CREATORS", "U0OTHER001")
    runner.run_task(t["id"])
    assert tasks.get(d, t["id"])["status"] == "paused"
    assert posts()[-1]["channel"] == "D0RAVI0001" and "listed" in posts()[-1]["text"]


def test_reminder_survives_a_slack_client_failure(world, monkeypatch):
    d, add, _, _, posts = world
    t = add()
    tasks.change(d, t["id"], "remove", by=MEENA, where="C0TEAM0001", now=NOW, settings=tasks.Settings())
    good = runner._slack_client

    def broken():
        raise RuntimeError("no client")

    monkeypatch.setattr(runner, "_slack_client", broken)
    monkeypatch.setattr(runner, "_utcnow", lambda: NOW + timedelta(days=6, hours=1))
    runner.tasks_sync(BackgroundScheduler())         # can't send: must try again next minute
    monkeypatch.setattr(runner, "_slack_client", good)
    runner.tasks_sync(BackgroundScheduler())
    assert any(p["channel"] == "D0RAVI0001" and "tomorrow" in p["text"] for p in posts())



def test_unanswered_content_change_expires_and_asker_is_told(world, monkeypatch):
    d, add, _, _, posts = world
    t = add()
    tasks.change(d, t["id"], "edit", by=MEENA, where="C0TEAM0001", changes={"payload": {"text": "go home"}},
                 now=NOW, settings=tasks.Settings())
    monkeypatch.setattr(runner, "_utcnow", lambda: NOW + timedelta(days=7, hours=1))
    runner.tasks_sync(BackgroundScheduler())
    assert tasks.get(d, t["id"])["payload"] == {"text": "x"}
    assert any(p["channel"] == "C0TEAM0001" and "admin" in p["text"] for p in posts())



def test_condition_not_met_sends_nothing_and_keeps_the_task(world, monkeypatch):
    d, add, _, _, posts = world
    t = add(kind="ask", payload={"question": "if EC2 > $200 list resources"})
    monkeypatch.setattr(runner.kinds, "render", lambda *a, **k: (None, "2026-11-04"))
    runner.run_task(t["id"])
    assert posts() == []
    saved = tasks.get(d, t["id"])
    assert saved["status"] == "active" and saved["last_sent_at"] is None


def test_one_time_task_with_nothing_to_send_is_done(world, monkeypatch):
    d, add, _, _, posts = world
    t = add(kind="ask", payload={"question": "q"}, schedule={"at": "2026-11-06 10:00", "tz": "UTC"})
    monkeypatch.setattr(runner.kinds, "render", lambda *a, **k: (None, None))
    runner.run_task(t["id"])
    assert posts() == [] and tasks.get(d, t["id"])["status"] == "done"


# ---------- live check 2026-11-10: a held one-time task was closed as "missed" ----------

def _one_time_report(add, at="2026-11-05 09:00"):
    return add(kind="report", payload={"template": "month_to_date"}, target={"type": "user", "id": RAVI},
               schedule={"at": at, "tz": "UTC"}, now=NOW - timedelta(hours=1))  # set up an hour before it is due


def test_held_one_time_task_is_not_closed_as_missed(world, monkeypatch):
    """11:15 IST: t1 fired on time and was held for the 11:30 load; a minute later tasks_sync saw an active
    one-time task with no job and closed it as missed ("the agent was not running"). It must wait instead."""
    d, add, _, _, posts = world
    t = _one_time_report(add)
    monkeypatch.setattr(agent_api, "_next_load_at", lambda: NOW + timedelta(minutes=15))
    runner.run_task(t["id"])                                   # fired at its time, held for the load
    assert posts() == [] and t["id"] in runner._HELD
    monkeypatch.setattr(runner, "_utcnow", lambda: NOW + timedelta(minutes=1))
    runner.tasks_sync(BackgroundScheduler())                   # the 1-minute sync after the fire
    assert tasks.get(d, t["id"])["status"] == "active"
    assert not any("missed" in p["text"] for p in posts())        # no "agent was not running" DM
    runner._release_held(rc=0)                                  # the load ends
    assert posts()[-1]["text"] == f"TEXT {t['id']}"
    assert tasks.get(d, t["id"])["status"] == "done"


def test_one_time_task_being_sent_is_not_closed_as_missed(world, monkeypatch):
    """A slow send (e.g. an `ask`) still running when the next tasks_sync comes must not be closed as missed."""
    d, add, _, _, posts = world
    t = _one_time_report(add)
    seen = {}

    def slow_render(task, *a, **k):
        monkeypatch.setattr(runner, "_utcnow", lambda: NOW + timedelta(minutes=2))
        runner.tasks_sync(BackgroundScheduler())               # the sync runs while this send is in progress
        seen["status"] = tasks.get(d, task["id"])["status"]
        return f"TEXT {task['id']}", "2026-11-04"

    monkeypatch.setattr(runner.kinds, "render", slow_render)
    runner.run_task(t["id"])
    assert seen["status"] == "active"
    assert [p["text"] for p in posts()] == [f"TEXT {t['id']}"]   # the report, no "missed" DM
    assert tasks.get(d, t["id"])["status"] == "done"


def _state(d, last_success):
    import json
    import os

    with open(os.path.join(d, "state.json"), "w", encoding="utf-8") as fh:
        json.dump({"last_success": last_success}, fh)


def test_no_hold_when_a_load_already_finished_since_the_last_scheduled_slot(world, monkeypatch):
    """10:56 IST startup load, then a report at 11:15 IST with the 11:30 load due: the data is already as fresh as
    the schedule gives, so send at the set time (FOCUS_SCHEDULE 06:00/18:00 UTC; NOW is 09:00 UTC)."""
    d, add, _, _, posts = world
    monkeypatch.setenv("FOCUS_SCHEDULE", "0 6,18 * * *")
    _state(d, (NOW - timedelta(minutes=30)).isoformat())           # 08:30, after the 06:00 slot
    monkeypatch.setattr(agent_api, "_next_load_at", lambda: NOW + timedelta(minutes=15))
    t = _report(add)
    runner.run_task(t["id"])
    assert posts()[-1]["text"] == f"TEXT {t['id']}" and runner._HELD == {}


def test_still_holds_when_the_last_success_is_older_than_the_last_slot(world, monkeypatch):
    d, add, _, _, posts = world
    monkeypatch.setenv("FOCUS_SCHEDULE", "0 6,18 * * *")
    _state(d, (NOW - timedelta(hours=4)).isoformat())              # 05:00, before the 06:00 slot (it failed)
    monkeypatch.setattr(agent_api, "_next_load_at", lambda: NOW + timedelta(minutes=15))
    t = _report(add)
    runner.run_task(t["id"])
    assert posts() == [] and t["id"] in runner._HELD


def test_still_holds_while_a_load_runs_even_after_a_recent_success(world, monkeypatch):
    d, add, _, _, posts = world
    monkeypatch.setenv("FOCUS_SCHEDULE", "0 6,18 * * *")
    _state(d, (NOW - timedelta(minutes=30)).isoformat())
    agent_api._LOAD["running"] = True
    t = _report(add)
    runner.run_task(t["id"])
    assert posts() == [] and t["id"] in runner._HELD
