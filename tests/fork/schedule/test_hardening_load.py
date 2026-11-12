"""Many tasks at once.

Task sends run on the plugin's own thread pool, so a slow `ask` can never hold up the host's data load or
tasks_sync. At most DATA_SLOTS data tasks (report / query / ask: each starts a child process) render at once, so a
burst can't run the container out of memory; reminders don't wait for a slot. Tasks released after a load (or after
waiting HOLD_MAX) are booked as runs instead of being sent one by one inside the load's or the sync's thread, and a
released task counts as busy until its run starts. The same task never runs twice at once."""
import threading
from datetime import datetime, timedelta, timezone

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from pico_plugin_schedule import runner, slack_api, tasks

from .test_runner import FakeGate
from .test_slack_api import FakeSlack

RAVI = "U0RAVI0001"
NOW = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
ME = {"type": "user", "id": RAVI}


@pytest.fixture
def ld(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    for k in ("FOCUS_REPORT_CHANNELS", "FOCUS_ASK_MAX_PER_DAY", "FOCUS_REPORT_CREATORS", "FOCUS_REPORT_ADMINS"):
        monkeypatch.delenv(k, raising=False)
    fake = FakeSlack(conversations_open=lambda p: {"ok": True, "channel": {"id": "D0" + p["users"][2:]}},
                     chat_postMessage={"ok": True, "ts": "1.1"})
    monkeypatch.setattr(runner, "_utcnow", lambda: NOW)
    monkeypatch.setattr(runner, "_HELD", {})
    monkeypatch.setattr(runner.kinds, "render", lambda t, *a, **k: (f"TEXT {t['id']}", "2026-11-09"))
    d = str(tmp_path)
    sched = BackgroundScheduler()
    gate = FakeGate()
    runner.install(sched, data_dir=d, gate=gate,
                   slack_client_factory=lambda: slack_api.Client("xoxb-test-token", transport=fake))

    def add(kind="report", schedule=None, payload=None):
        return tasks.add(d, owner=RAVI, where="D0RAVI0001", kind=kind, name="n", target=ME,
                         schedule=schedule or {"cron": "0 9 * * *", "tz": "UTC"},
                         payload=payload or ({"text": "x"} if kind == "reminder" else {"template": "t"}),
                         now=NOW - timedelta(hours=2), settings=tasks.Settings())

    def posts():
        return [p["text"] for m, p in fake.calls if m == "chat.postMessage"]

    return {"d": d, "sched": sched, "gate": gate, "add": add, "posts": posts}


def test_task_jobs_run_on_their_own_pool_with_an_hour_of_grace(ld):
    t = ld["add"]()
    runner.tasks_sync()
    job = ld["sched"].get_job(f"task_{t['id']}")
    assert job.executor == runner.DATA_EXECUTOR and job.misfire_grace_time == runner.TASK_GRACE_S == 3600
    assert ld["sched"].get_job("tasks_sync").executor == "default"   # the sync stays with the host's jobs


def _blocking_render(monkeypatch):
    """render that waits until released; records how many render at once."""
    gate, state = threading.Event(), {"now": 0, "max": 0}
    lock = threading.Lock()

    def render(task, *a, **k):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        if task["kind"] != "reminder":
            gate.wait(5)
        with lock:
            state["now"] -= 1
        return f"TEXT {task['id']}", None

    monkeypatch.setattr(runner.kinds, "render", render)
    return gate, state


def _start(task_ids):
    threads = [threading.Thread(target=runner.run_task, args=(i,), kwargs={"hold": False}) for i in task_ids]
    for th in threads:
        th.start()
    return threads


def test_at_most_two_data_tasks_render_at_once(ld, monkeypatch):
    gate, state = _blocking_render(monkeypatch)
    ids = [ld["add"]()["id"] for _ in range(4)]
    threads = _start(ids)
    threading.Event().wait(0.5)
    assert state["max"] == runner.DATA_SLOTS == 2
    gate.set()
    for th in threads:
        th.join(5)
    assert sorted(ld["posts"]()) == sorted(f"TEXT {i}" for i in ids)


def test_a_reminder_does_not_wait_for_a_data_slot(ld, monkeypatch):
    gate, _ = _blocking_render(monkeypatch)
    threads = _start([ld["add"]()["id"] for _ in range(2)])         # both data slots taken
    threading.Event().wait(0.3)
    r = ld["add"](kind="reminder")
    runner.run_task(r["id"], hold=False)
    assert ld["posts"]() == [f"TEXT {r['id']}"]
    gate.set()
    for th in threads:
        th.join(5)


def test_the_same_task_never_runs_twice_at_once(ld, monkeypatch):
    gate, _ = _blocking_render(monkeypatch)
    t = ld["add"]()
    threads = _start([t["id"]])
    threading.Event().wait(0.3)
    runner.run_task(t["id"], hold=False)                             # e.g. its retry while the first is still on
    gate.set()
    for th in threads:
        th.join(5)
    assert ld["posts"]() == [f"TEXT {t['id']}"]


def test_load_done_books_the_held_tasks_instead_of_sending_them_in_its_thread(ld):
    ld["gate"].is_running = True
    ids = [ld["add"]()["id"] for _ in range(3)]
    for i in ids:
        runner.run_task(i)                                           # held for the load
    assert ld["posts"]() == [] and sorted(runner._HELD) == sorted(ids)
    runner.on_load_done(0)                                           # returns at once
    assert ld["posts"]() == []
    booked = [runner._booking(ld["sched"], i) for i in ids]
    assert all(j is not None and j.executor == runner.DATA_EXECUTOR for j in booked)


def test_held_too_long_is_booked_too(ld, monkeypatch):
    ld["gate"].is_running = True
    t = ld["add"]()
    runner.run_task(t["id"])
    monkeypatch.setattr(runner, "_utcnow", lambda: NOW + runner.HOLD_MAX + timedelta(minutes=1))
    runner.tasks_sync()
    assert ld["posts"]() == [] and runner._booking(ld["sched"], t['id']) is not None


def test_a_released_one_time_task_stays_busy_until_its_run(ld, monkeypatch):
    ld["gate"].is_running = True
    t = ld["add"](schedule={"at": "2026-11-10 09:00", "tz": "UTC"})
    runner.run_task(t["id"])
    runner.on_load_done(0)
    monkeypatch.setattr(runner, "_utcnow", lambda: NOW + timedelta(hours=2))  # well past its time and the grace
    runner.tasks_sync()
    assert tasks.get(ld["d"], t["id"])["status"] == "active"          # not closed as missed: its run is booked


def test_taking_held_tasks_marks_them_busy_before_they_are_booked(ld):
    ld["gate"].is_running = True
    t = ld["add"]()
    runner.run_task(t["id"])
    assert runner._take_held() == [t["id"]]
    assert runner._busy(t["id"])                                     # no window where it is neither held nor booked
    runner.run_task(t["id"], hold=False)
    assert not runner._busy(t["id"])


def test_without_a_scheduler_a_release_still_sends(tmp_path, monkeypatch):
    """configure() alone (no scheduler to book on): released tasks are sent in place, as before."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    fake = FakeSlack(conversations_open=lambda p: {"ok": True, "channel": {"id": "D0" + p["users"][2:]}},
                     chat_postMessage={"ok": True, "ts": "1.1"})
    runner.install(BackgroundScheduler(), data_dir=str(tmp_path))     # an earlier host ...
    gate = FakeGate()
    runner.configure(data_dir=str(tmp_path), gate=gate,             # ... replaced by one without a scheduler
                     slack_client_factory=lambda: slack_api.Client("xoxb-test-token", transport=fake))
    monkeypatch.setattr(runner, "_utcnow", lambda: NOW)
    monkeypatch.setattr(runner, "_HELD", {})
    monkeypatch.setattr(runner.kinds, "render", lambda t, *a, **k: (f"TEXT {t['id']}", None))
    gate.is_running = True
    t = tasks.add(str(tmp_path), owner=RAVI, where="D0RAVI0001", kind="report", name="n", target=ME,
                  schedule={"cron": "0 9 * * *", "tz": "UTC"}, payload={"template": "t"},
                  now=NOW - timedelta(hours=1), settings=tasks.Settings())
    runner.run_task(t["id"])
    runner.on_load_done(0)
    assert [p["text"] for m, p in fake.calls if m == "chat.postMessage"] == [f"TEXT {t['id']}"]
