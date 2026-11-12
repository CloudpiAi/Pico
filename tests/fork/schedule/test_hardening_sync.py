"""tasks_sync, missed runs and restarts.

- One broken task (hand-edited file, bad cron) is paused and its owner told once; every other task still runs.
- A broken line in tasks-history.jsonl or tasks-held.json is skipped, not fatal.
- A one-time task up to TASK_GRACE_S late (made a few seconds before its time, a busy minute, a short restart) is sent
  late, not closed as "missed".
- The first sync after a start catches up a repeating task's run missed while the agent was down (within the grace),
  and tells the owner about an older one; never for a run that was sent or came before the task existed.
- A send that was posting when the agent stopped (sending_at set, never marked sent) is never sent again."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from pico_plugin_schedule import runner, slack_api, tasks

from .test_slack_api import FakeSlack

RAVI = "U0RAVI0001"
NOW = datetime(2026, 11, 10, 9, 20, tzinfo=timezone.utc)
ME = {"type": "user", "id": RAVI}


@pytest.fixture
def sy(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    for k in ("FOCUS_REPORT_CHANNELS", "FOCUS_ASK_MAX_PER_DAY", "FOCUS_REPORT_CREATORS", "FOCUS_REPORT_ADMINS"):
        monkeypatch.delenv(k, raising=False)
    fake = FakeSlack(conversations_open=lambda p: {"ok": True, "channel": {"id": "D0" + p["users"][2:]}},
                     chat_postMessage={"ok": True, "ts": "1.1"})
    clock = {"now": NOW}
    monkeypatch.setattr(runner, "_utcnow", lambda: clock["now"])
    monkeypatch.setattr(runner, "_HELD", {})
    monkeypatch.setattr(runner.kinds, "render", lambda t, *a, **k: (f"TEXT {t['id']}", None))
    d = str(tmp_path)
    sched = BackgroundScheduler()

    def start():
        """The agent starts: a fresh install (the first sync after it catches up)."""
        runner.install(sched, data_dir=d, slack_client_factory=lambda: slack_api.Client("xoxb-test-token",
                                                                                        transport=fake))

    def add(schedule=None, created=NOW - timedelta(days=2), kind="reminder", payload=None):
        return tasks.add(d, owner=RAVI, where="D0RAVI0001", kind=kind, name="Stand-up", target=ME,
                         schedule=schedule or {"cron": "0 9 * * *", "tz": "UTC"},
                         payload=payload or {"text": "x"}, now=created, settings=tasks.Settings())

    def edit_file(fn):
        p = f"{d}/tasks.json"
        data = json.load(open(p, encoding="utf-8"))
        fn(data)
        json.dump(data, open(p, "w", encoding="utf-8"))

    def dms():
        return [p["text"] for m, p in fake.calls if m == "chat.postMessage"]

    start()
    return {"d": d, "sched": sched, "start": start, "add": add, "edit": edit_file, "dms": dms, "clock": clock}


def _sent(sy, task_id, at):
    tasks.mark_sent(sy["d"], task_id, at=at, data_through=None)


# ---------- one bad task never stops the others ----------

def test_a_broken_task_is_paused_and_the_others_still_run(sy):
    bad, good = sy["add"](), sy["add"]()
    sy["edit"](lambda data: data["tasks"][bad["id"]]["schedule"].update(cron="every monday please"))
    runner.tasks_sync()
    assert sy["sched"].get_job(f"task_{good['id']}") is not None
    assert tasks.get(sy["d"], bad["id"])["status"] == "paused"
    runner.tasks_sync()
    told = [t for t in sy["dms"]() if bad["id"] in t]
    assert len(told) == 1 and "can't be read" in told[0]


def test_a_task_missing_fields_does_not_stop_the_sync(sy):
    bad, good = sy["add"](), sy["add"]()
    sy["edit"](lambda data: data["tasks"][bad["id"]].pop("target"))
    runner.tasks_sync()
    assert sy["sched"].get_job(f"task_{good['id']}") is not None


def test_a_broken_history_line_is_skipped(sy):
    t = sy["add"]()
    with open(f"{sy['d']}/tasks-history.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"at": "2026-11-10T09:0')                               # cut short (disk full / power loss)
    tasks.change(sy["d"], t["id"], "pause", by=RAVI, where="D0RAVI0001", now=NOW, settings=tasks.Settings())
    assert [e["action"] for e in tasks.history(sy["d"], t["id"])] == ["created", "paused"]


def test_a_broken_held_entry_is_skipped(sy):
    t = sy["add"]()
    with open(f"{sy['d']}/tasks-held.json", "w", encoding="utf-8") as fh:
        json.dump({"t99": "not a time", t["id"]: NOW.isoformat()}, fh)
    runner.tasks_sync()
    assert sy["sched"].get_job(f"task_{t['id']}") is not None


# ---------- one-time tasks: late, not missed ----------

def test_a_one_time_task_made_just_before_its_time_is_sent_late_not_missed(sy):
    at = NOW.replace(minute=21)
    t = sy["add"](schedule={"at": at.strftime("%Y-%m-%d %H:%M"), "tz": "UTC"}, created=NOW + timedelta(seconds=40))
    sy["clock"]["now"] = at + timedelta(seconds=30)                       # the sync runs after its time
    runner.tasks_sync()
    assert tasks.get(sy["d"], t["id"])["status"] == "active"
    assert runner._booking(sy["sched"], t['id']) is not None
    assert not any("missed" in x for x in sy["dms"]())


def test_a_one_time_task_more_than_the_grace_late_is_missed(sy):
    t = sy["add"](schedule={"at": "2026-11-10 07:00", "tz": "UTC"})
    runner.tasks_sync()                                                    # 2 h 20 min late
    assert tasks.get(sy["d"], t["id"])["status"] == "done"
    assert any("missed" in x for x in sy["dms"]())


def test_a_one_time_task_that_was_posting_when_the_agent_stopped_is_not_sent_again(sy):
    t = sy["add"](schedule={"at": "2026-11-10 09:15", "tz": "UTC"})
    tasks.mark_sending(sy["d"], t["id"], at=NOW - timedelta(minutes=5))    # posting at 09:15 ... then the stop
    sy["start"]()
    runner.tasks_sync()
    assert tasks.get(sy["d"], t["id"])["status"] == "done"
    assert runner._booking(sy["sched"], t['id']) is None and sy["dms"]() == []


def test_a_send_marks_sending_before_posting_and_clears_it_after(sy, monkeypatch):
    t = sy["add"]()
    seen = {}
    real = slack_api.Client.post

    def post(self, target, text):
        seen["sending_at"] = tasks.get(sy["d"], t["id"]).get("sending_at")
        return real(self, target, text)

    monkeypatch.setattr(slack_api.Client, "post", post)
    runner.run_task(t["id"])
    assert seen["sending_at"] and tasks.get(sy["d"], t["id"]).get("sending_at") is None


def test_a_one_time_ask_over_the_daily_limit_is_closed_not_retried_every_minute(sy, monkeypatch):
    monkeypatch.setenv("FOCUS_ASK_MAX_PER_DAY", "1")
    tasks.take_ask_slot(sy["d"], NOW, tasks.Settings(ask_max_per_day=1))  # today's one question is used
    t = sy["add"](kind="ask", payload={"question": "q?"}, schedule={"at": "2026-11-10 09:20", "tz": "UTC"})
    runner.run_task(t["id"])
    assert tasks.get(sy["d"], t["id"])["status"] == "done"


# ---------- repeating tasks: catch up after a restart ----------

def test_restart_catches_up_a_run_missed_within_the_grace(sy):
    t = sy["add"]()
    _sent(sy, t["id"], NOW - timedelta(days=1, minutes=20))               # yesterday's 09:00 went out
    sy["start"]()                                                          # agent back at 09:20; 09:00 was missed
    runner.tasks_sync()
    job = runner._booking(sy["sched"], t['id'])
    assert job is not None and job.name == "late"


def test_restart_tells_the_owner_about_an_older_missed_run(sy):
    t = sy["add"]()
    sy["clock"]["now"] = NOW.replace(hour=12)                               # back 3 h after 09:00
    sy["start"]()
    runner.tasks_sync()
    assert runner._booking(sy["sched"], t['id']) is None
    told = [x for x in sy["dms"]() if t["id"] in x]
    assert len(told) == 1 and "missed" in told[0] and "09:00" in told[0]


def test_no_catch_up_when_the_run_was_sent(sy):
    t = sy["add"]()
    _sent(sy, t["id"], NOW - timedelta(minutes=20))
    sy["start"]()
    runner.tasks_sync()
    assert runner._booking(sy["sched"], t['id']) is None and sy["dms"]() == []


def test_no_catch_up_for_a_run_before_the_task_existed(sy):
    t = sy["add"](created=NOW - timedelta(minutes=10))                    # made at 09:10, after today's 09:00
    sy["start"]()
    runner.tasks_sync()
    assert runner._booking(sy["sched"], t['id']) is None and sy["dms"]() == []


def test_no_catch_up_when_that_run_was_posting_at_the_stop(sy):
    t = sy["add"]()
    tasks.mark_sending(sy["d"], t["id"], at=NOW - timedelta(minutes=20))
    sy["start"]()
    runner.tasks_sync()
    assert runner._booking(sy["sched"], t['id']) is None


def test_catch_up_happens_only_on_the_first_sync(sy):
    runner.tasks_sync()                                                    # the first sync: nothing to catch up
    t = sy["add"]()                                                        # a task that would look "missed"
    runner.tasks_sync()
    assert runner._booking(sy["sched"], t['id']) is None
