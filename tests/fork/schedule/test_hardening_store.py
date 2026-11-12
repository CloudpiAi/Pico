"""Store, growth, health, child processes and time edge cases.

- tasks.json is fsync'd and kept twice (tasks.json.bak); a torn or hand-broken tasks.json is read from the backup,
  and when both are bad the store says so (store_unreadable) instead of looking empty (a save would wipe the tasks).
- Stopped / finished tasks and their history are purged after 30 days, once a day.
- runner.health() says when tasks_sync last worked; report admins are told once when it keeps failing, and again
  when it works again.
- A child process that runs too long is killed with everything it started; its output is capped.
- Times that don't exist (clock change), more than a year ahead, or cron times that run more than once an hour
  anywhere in their cycle are refused with a plain message; a clock that jumped back doesn't block sends."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from pico_plugin_schedule import child, kinds, runner, slack_api, tasks

from .test_slack_api import FakeSlack

RAVI, ADMIN = "U0RAVI0001", "U0ADMIN001"
NOW = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
ME = {"type": "user", "id": RAVI}
S = tasks.Settings()


def _add(d, schedule=None, created=NOW - timedelta(hours=1)):
    return tasks.add(d, owner=RAVI, where="D0RAVI0001", kind="reminder", name="n", target=ME,
                     schedule=schedule or {"cron": "0 9 * * *", "tz": "UTC"}, payload={"text": "x"}, now=created,
                     settings=S)


# ---------- #13 the store survives a torn file ----------

def test_every_save_is_fsynced_and_kept_twice(tmp_path, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(tasks.os, "fsync", lambda fd: (synced.append(fd), real(fd)))
    t = _add(str(tmp_path))
    assert synced
    bak = json.loads((tmp_path / "tasks.json.bak").read_text(encoding="utf-8"))
    assert bak["tasks"][t["id"]]["name"] == "n"


@pytest.mark.parametrize("broken", ['{"next_id": 2, "tasks": {"t1": {"id"', "[]", ""])
def test_a_broken_tasks_file_is_read_from_the_backup(tmp_path, broken):
    t = _add(str(tmp_path))
    (tmp_path / "tasks.json").write_text(broken, encoding="utf-8")
    assert tasks.get(str(tmp_path), t["id"])["name"] == "n"


def test_both_broken_is_an_error_not_an_empty_list(tmp_path):
    _add(str(tmp_path))
    (tmp_path / "tasks.json").write_text("{", encoding="utf-8")
    (tmp_path / "tasks.json.bak").write_text("{", encoding="utf-8")
    with pytest.raises(tasks.TaskError) as e:
        tasks.active_tasks(str(tmp_path))
    assert e.value.code == "store_unreadable"
    with pytest.raises(tasks.TaskError):                     # and nothing is saved over it
        _add(str(tmp_path))
    assert (tmp_path / "tasks.json").read_text(encoding="utf-8") == "{"


def test_an_unreadable_store_keeps_the_jobs(tmp_path, monkeypatch):
    sched = BackgroundScheduler()
    runner.install(sched, data_dir=str(tmp_path), slack_client_factory=lambda: None)
    monkeypatch.setattr(runner, "_utcnow", lambda: NOW)
    t = _add(str(tmp_path))
    runner.tasks_sync()
    (tmp_path / "tasks.json").write_text("{", encoding="utf-8")
    (tmp_path / "tasks.json.bak").write_text("{", encoding="utf-8")
    runner.tasks_sync()
    assert sched.get_job(f"task_{t['id']}") is not None


# ---------- #14 closed tasks are purged ----------

def test_closed_tasks_and_their_history_are_purged_after_30_days(tmp_path):
    d = str(tmp_path)
    old_done, old_removed, recent_done, active = (_add(d)["id"] for _ in range(4))
    tasks.mark_missed(d, old_done, now=NOW - timedelta(days=31), settings=S)
    tasks.change(d, old_removed, "remove", by=RAVI, where="D0RAVI0001", now=NOW - timedelta(days=40), settings=S)
    tasks.mark_missed(d, recent_done, now=NOW - timedelta(days=5), settings=S)
    assert sorted(tasks.purge_closed(d, NOW)) == sorted([old_done, old_removed])
    left = tasks._load(d)["tasks"]
    assert set(left) == {recent_done, active}
    assert {e["task_id"] for e in tasks._read_history(d)} == {recent_done, active}


def test_sync_purges_once_a_day(tmp_path, monkeypatch):
    d = str(tmp_path)
    runner.install(BackgroundScheduler(), data_dir=d, slack_client_factory=lambda: None)
    clock = {"now": NOW}
    monkeypatch.setattr(runner, "_utcnow", lambda: clock["now"])
    calls = []
    monkeypatch.setattr(tasks, "purge_closed", lambda d, now, **k: calls.append(now) or [])
    runner.tasks_sync()
    runner.tasks_sync()
    clock["now"] = NOW + timedelta(days=1)
    runner.tasks_sync()
    assert len(calls) == 2


# ---------- #15 health and the admins ----------

def _install_with_slack(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    monkeypatch.setenv("FOCUS_REPORT_ADMINS", ADMIN)
    fake = FakeSlack(conversations_open=lambda p: {"ok": True, "channel": {"id": "D0" + p["users"][2:]}},
                     chat_postMessage={"ok": True, "ts": "1"})
    runner.install(BackgroundScheduler(), data_dir=str(tmp_path),
                   slack_client_factory=lambda: slack_api.Client("xoxb-test-token", transport=fake))
    monkeypatch.setattr(runner, "_utcnow", lambda: NOW)
    return fake


def test_health_says_when_the_sync_last_worked(tmp_path, monkeypatch):
    _install_with_slack(tmp_path, monkeypatch)
    wall = {"t": 1000.0}
    monkeypatch.setattr(runner, "_wall", lambda: wall["t"])
    runner.tasks_sync()
    assert runner.health()["last_ok"] == 1000.0
    (tmp_path / "tasks.json").write_text("{", encoding="utf-8")
    (tmp_path / "tasks.json.bak").write_text("{", encoding="utf-8")
    wall["t"] = 1060.0
    runner.tasks_sync()
    assert runner.health()["last_ok"] == 1000.0 and runner.health()["fails"] == 1


def test_admins_are_told_once_when_the_sync_keeps_failing_and_when_it_works_again(tmp_path, monkeypatch):
    fake = _install_with_slack(tmp_path, monkeypatch)
    _add(str(tmp_path))
    good = (tmp_path / "tasks.json").read_text(encoding="utf-8")
    (tmp_path / "tasks.json").write_text("{", encoding="utf-8")
    (tmp_path / "tasks.json.bak").write_text("{", encoding="utf-8")
    for _ in range(runner.SYNC_ALERT_AFTER + 2):
        runner.tasks_sync()
    to_admin = [p["text"] for m, p in fake.calls if m == "chat.postMessage" and p["channel"] == "D0ADMIN001"]
    assert len(to_admin) == 1 and "tasks.json" in to_admin[0]
    (tmp_path / "tasks.json").write_text(good, encoding="utf-8")
    runner.tasks_sync()
    to_admin = [p["text"] for m, p in fake.calls if m == "chat.postMessage" and p["channel"] == "D0ADMIN001"]
    assert len(to_admin) == 2 and "working again" in to_admin[1]


def test_health_check_is_unhealthy_when_tasks_are_not_checked(monkeypatch):
    from pico_finops_agent import health

    snap = {"state": "connected", "sampled_at": 5000.0}
    ok = health.evaluate(5000.0, snap, 5000.0, tasks_ok_at=4990.0)
    stale = health.evaluate(5000.0, snap, 5000.0, tasks_ok_at=5000.0 - health.down_limit())
    assert ok.status == "healthy"
    assert stale.status == "unhealthy" and "scheduled tasks" in stale.reason and stale.exit_code is None
    assert health.evaluate(5000.0, snap, 5000.0).status == "healthy"   # no tasks plugin: not judged


def test_the_agent_reports_the_task_sync_to_the_health_check():
    from pico_finops_agent import scheduler

    assert callable(scheduler.tasks_ok_at)


# ---------- #17 child processes ----------

def test_a_timed_out_child_is_killed_with_what_it_started(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    code = ("import subprocess, sys, time; "
            f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            f"open({str(pidfile)!r}, 'w').write(str(p.pid)); time.sleep(60)")
    with pytest.raises(subprocess.TimeoutExpired):
        child.run([sys.executable, "-c", code], timeout=2, env=dict(os.environ))
    pid = int(pidfile.read_text())
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_child_output_is_capped_to_its_tail():
    r = child.run([sys.executable, "-c", "print('x' * 3_000_000); print('LAST')"], timeout=30,
                  env=dict(os.environ), max_output=1000)
    assert r.returncode == 0 and len(r.stdout) <= 1000 and r.stdout.rstrip().endswith("LAST")


def test_child_passes_input_and_returns_text():
    r = child.run([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], input="hi",
                  timeout=30, env=dict(os.environ))
    assert r.stdout.strip() == "HI" and isinstance(r.stderr, str)


def test_scheduled_questions_and_data_reads_use_the_guarded_child():
    import inspect

    from pico_plugin_focus_data import schedule_source

    assert inspect.signature(kinds.render).parameters["runner"].default is child.run
    assert "child.run(" in inspect.getsource(schedule_source._read_cache)


# ---------- #18 time edge cases ----------

@pytest.mark.parametrize("schedule,code", [
    ({"at": "2027-03-14 02:30", "tz": "America/New_York"}, "bad_schedule"),     # clocks go 02:00 -> 03:00
    ({"at": "2028-01-01 09:00", "tz": "UTC"}, "time_too_far"),
    ({"at": "9999-12-31 23:59", "tz": "Pacific/Kiritimati"}, "time_too_far"),
    ({"at": "2026-11-11 09:00", "tz": "../../etc/passwd"}, "bad_timezone"),
    ({"cron": "0,30 9 * * *", "tz": "UTC"}, "too_often"),                      # 09:00 and 09:30
    ({"cron": "0 9,10 * * *", "tz": "UTC"}, None),                              # an hour apart: fine
])
def test_time_edge_cases(schedule, code):
    now = datetime(2026, 11, 10, 9, 10, tzinfo=timezone.utc)
    if code is None:
        tasks.check_schedule(schedule, now)
        return
    with pytest.raises(tasks.TaskError) as e:
        tasks.check_schedule(schedule, now)
    assert e.value.code == code


def test_a_clock_that_jumped_back_does_not_block_sends(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    fake = FakeSlack(conversations_open=lambda p: {"ok": True, "channel": {"id": "D0" + p["users"][2:]}},
                     chat_postMessage={"ok": True, "ts": "1"})
    runner.configure(data_dir=str(tmp_path),
                     slack_client_factory=lambda: slack_api.Client("xoxb-test-token", transport=fake))
    monkeypatch.setattr(runner, "_utcnow", lambda: NOW)
    t = _add(str(tmp_path))
    tasks.mark_sent(str(tmp_path), t["id"], at=NOW + timedelta(days=1), data_through=None)  # sent "tomorrow"
    runner.run_task(t["id"])
    assert [m for m, _ in fake.calls].count("chat.postMessage") == 1
