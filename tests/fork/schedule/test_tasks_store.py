"""Task store (PRD R5, R11-R17): /data/tasks.json + /data/tasks-history.jsonl.

Owner rules: the creator (or a report admin) changes a task at once; anyone else only asks, and the creator
confirms. History keeps the last FOCUS_REPORT_HISTORY_MAX changes per task (default 5) plus `created` and the
latest `removed`."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("apscheduler", reason="Needs the pico-finops-agent extra")

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "schedule"))
from pico_plugin_schedule import tasks  # noqa: E402

NOW = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)
RAVI, MEENA, ADMIN = "U0RAVI0001", "U0MEENA001", "U0ADMIN001"
DM, TEAM = {"type": "user", "id": RAVI}, {"type": "channel", "id": "C0TEAM0001"}
DAILY = {"cron": "0 15 * * *", "tz": "Asia/Kolkata"}


@pytest.fixture
def d(tmp_path):
    return str(tmp_path)


def settings(**kw):
    kw.setdefault("creators", frozenset({RAVI, MEENA}))  # D1: only listed people create channel tasks
    return tasks.Settings(**kw)


def add(d, **kw):
    args = dict(owner=RAVI, where="D0RAVIDM01", kind="reminder", name="Stop dev instances", target=DM,
                schedule=DAILY, payload={"text": "stop the dev instances"}, now=NOW, settings=settings())
    args.update(kw)
    return tasks.add(d, **args)


# ---------- add ----------

def test_add_saves_and_records_created(d):
    t = add(d)
    assert t["id"] == "t1" and t["status"] == "active" and t["owner"] == RAVI and t["created_in"] == "D0RAVIDM01"
    assert json.loads(Path(d, "tasks.json").read_text())["tasks"]["t1"]["kind"] == "reminder"
    h = tasks.history(d, "t1")
    assert [e["action"] for e in h] == ["created"] and h[0]["by"] == RAVI
    assert add(d)["id"] == "t2"


def test_next_run_in_the_task_time_zone(d):
    t = add(d)
    # 3 PM IST = 09:30 UTC; it is 09:00 UTC now, so the first run is today
    assert tasks.next_run(t["schedule"], NOW) == datetime(2026, 11, 5, 9, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("schedule,code", [
    ({"cron": "0 15 * *", "tz": "UTC"}, "bad_schedule"),          # 4 fields
    ({"cron": "*/10 * * * *", "tz": "UTC"}, "too_often"),         # more than once an hour
    ({"cron": "0 15 * * *", "tz": "Mars/Base"}, "bad_timezone"),
    ({"at": "2026-11-05 08:00", "tz": "UTC"}, "time_passed"),      # one-time in the past
    ({"at": "tomorrow", "tz": "UTC"}, "bad_schedule"),
    ({}, "bad_schedule"),
])
def test_bad_schedules_refused(d, schedule, code):
    with pytest.raises(tasks.TaskError) as e:
        add(d, schedule=schedule)
    assert e.value.code == code
    assert not Path(d, "tasks.json").exists() or json.loads(Path(d, "tasks.json").read_text())["tasks"] == {}


def test_one_time_task(d):
    t = add(d, schedule={"at": "2026-11-06 10:00", "tz": "Asia/Kolkata"})
    assert tasks.next_run(t["schedule"], NOW) == datetime(2026, 11, 6, 4, 30, tzinfo=timezone.utc)
    tasks.mark_sent(d, "t1", at=NOW, data_through=None)
    assert tasks.get(d, "t1")["status"] == "done"
    assert tasks.history(d, "t1")[-1]["action"] == "done"


@pytest.mark.parametrize("kind,payload,code", [
    ("reminder", {"text": ""}, "bad_payload"),
    ("reminder", {"text": "x" * 1001}, "too_long"),
    ("report", {}, "bad_payload"),  # template names: the data source checks them
    ("query", {"sql": ""}, "bad_payload"),
    ("ask", {"question": ""}, "bad_payload"),
    ("dance", {}, "bad_kind"),
])
def test_bad_payloads_refused(d, kind, payload, code):
    with pytest.raises(tasks.TaskError) as e:
        add(d, kind=kind, payload=payload)
    assert e.value.code == code


def test_at_most_10_active_tasks_per_person(d):
    for _ in range(10):
        add(d)
    with pytest.raises(tasks.TaskError) as e:
        add(d)
    assert e.value.code == "too_many_tasks"
    add(d, owner=MEENA)  # someone else still can


def test_channel_list_limits_targets(d):
    s = settings(allowed_channels=frozenset({"C0OK000001"}))
    with pytest.raises(tasks.TaskError) as e:
        add(d, target=TEAM, settings=s)
    assert e.value.code == "channel_not_allowed"
    add(d, target={"type": "channel", "id": "C0OK000001"}, settings=s)
    add(d, target=DM, settings=s)  # DMs always allowed
    add(d, target=TEAM)            # no list: any channel


# ---------- owner rules ----------

def test_owner_changes_at_once(d):
    add(d)
    r = tasks.change(d, "t1", "edit", by=RAVI, where="C0TEAM0001",
                     changes={"schedule": {"cron": "0 9 * * *", "tz": "Asia/Kolkata"}}, now=NOW, settings=settings())
    assert r["result"] == "done" and tasks.get(d, "t1")["schedule"]["cron"] == "0 9 * * *"
    e = tasks.history(d, "t1")[-1]
    assert e["action"] == "time_changed" and e["old"]["cron"] == "0 15 * * *" and e["new"]["cron"] == "0 9 * * *"
    tasks.change(d, "t1", "remove", by=RAVI, where="C0TEAM0001", now=NOW, settings=settings())
    assert tasks.get(d, "t1")["status"] == "removed"
    assert tasks.history(d, "t1")[-1]["action"] == "removed"


def test_someone_else_only_asks_then_owner_confirms(d):
    add(d, target=TEAM)
    r = tasks.change(d, "t1", "remove", by=MEENA, where="C0TEAM0001", now=NOW, settings=settings())
    assert r["result"] == "requested" and r["request"]["owner"] == RAVI
    assert tasks.get(d, "t1")["status"] == "active"  # keeps running
    rid = r["request"]["id"]
    with pytest.raises(tasks.TaskError) as e:  # Meena can't confirm her own request
        tasks.resolve(d, rid, by=MEENA, accept=True, now=NOW, settings=settings())
    assert e.value.code == "not_owner"
    out = tasks.resolve(d, rid, by=RAVI, accept=True, now=NOW, settings=settings())
    assert out["request"]["by"] == MEENA and tasks.get(d, "t1")["status"] == "removed"
    assert [e["action"] for e in tasks.history(d, "t1")] == ["created", "stop_requested", "confirmed", "removed"]
    assert tasks.history(d, "t1")[-1]["by"] == RAVI


def test_owner_declines(d):
    add(d, target=TEAM)
    rid = tasks.change(d, "t1", "pause", by=MEENA, where="C0TEAM0001", now=NOW, settings=settings())["request"]["id"]
    tasks.resolve(d, rid, by=RAVI, accept=False, now=NOW, settings=settings())
    assert tasks.get(d, "t1")["status"] == "active"
    assert tasks.history(d, "t1")[-1]["action"] == "declined"
    with pytest.raises(tasks.TaskError) as e:  # already answered
        tasks.resolve(d, rid, by=RAVI, accept=True, now=NOW, settings=settings())
    assert e.value.code == "no_such_request"


def test_admin_acts_without_owner(d):
    add(d, target=TEAM)
    r = tasks.change(d, "t1", "remove", by=ADMIN, where="C0TEAM0001", now=NOW,
                     settings=settings(admins=frozenset({ADMIN})))
    assert r["result"] == "done" and tasks.history(d, "t1")[-1]["by"] == ADMIN


def test_unanswered_request_is_accepted_after_7_days_with_a_reminder_on_day_6(d):
    """D5: a creator who left must not block a request forever."""
    add(d, target=TEAM)
    tasks.change(d, "t1", "remove", by=MEENA, where="C0TEAM0001", now=NOW, settings=settings())
    r = tasks.process_requests(d, NOW + timedelta(days=5), settings=settings())
    assert r == {"reminders": [], "accepted": [], "failed": [], "expired": []}
    r = tasks.process_requests(d, NOW + timedelta(days=6, minutes=1), settings=settings())
    assert [q["by"] for q in r["reminders"]] == [MEENA] and r["accepted"] == []
    r = tasks.process_requests(d, NOW + timedelta(days=6, minutes=2), settings=settings())
    assert r["reminders"] == []  # reminded once
    r = tasks.process_requests(d, NOW + timedelta(days=7, minutes=1), settings=settings())
    assert [q["by"] for q in r["accepted"]] == [MEENA]
    assert tasks.get(d, "t1")["status"] == "removed"
    actions = [e["action"] for e in tasks.history(d, "t1")]
    assert actions[-2:] == ["auto_accepted", "removed"]
    assert tasks.history(d, "t1")[-1]["by"] == "system"


def test_unknown_task(d):
    with pytest.raises(tasks.TaskError) as e:
        tasks.change(d, "t9", "remove", by=RAVI, where="x", now=NOW, settings=settings())
    assert e.value.code == "no_such_task"


# ---------- history size ----------

def _edits(d, n):
    for i in range(n):
        tasks.change(d, "t1", "edit", by=RAVI, where="x", changes={"name": f"name {i}"}, now=NOW, settings=settings())


def test_history_keeps_last_5_changes_plus_created(d):
    add(d)
    _edits(d, 7)
    h = tasks.history(d, "t1")
    assert h[0]["action"] == "created"
    changes = [e for e in h if e["action"] != "created"]
    assert len(changes) == 5 and changes[-1]["new"]["name"] == "name 6" and changes[0]["new"]["name"] == "name 2"


def test_history_max_from_settings_and_removed_always_kept(d):
    add(d)
    s = settings(history_max=2)
    for i in range(4):
        tasks.change(d, "t1", "edit", by=RAVI, where="x", changes={"name": f"name {i}"}, now=NOW, settings=s)
    tasks.change(d, "t1", "remove", by=RAVI, where="x", now=NOW, settings=s)
    h = tasks.history(d, "t1")
    # removed is kept on top of the last 2 changes, like created
    assert [e["action"] for e in h] == ["created", "name_changed", "name_changed", "removed"]
    assert [e["new"]["name"] for e in h[1:3]] == ["name 2", "name 3"]
    with pytest.raises(tasks.TaskError) as e:  # a removed task can't be changed again
        tasks.change(d, "t1", "resume", by=RAVI, where="x", now=NOW, settings=s)
    assert e.value.code == "task_removed"


def test_history_of_other_tasks_untouched(d):
    add(d)
    add(d)
    _edits(d, 9)
    assert [e["action"] for e in tasks.history(d, "t2")] == ["created"]


def test_settings_from_env(monkeypatch):
    for k in ("FOCUS_REPORT_HISTORY_MAX", "FOCUS_ASK_MAX_PER_DAY", "FOCUS_REPORT_CHANNELS", "FOCUS_REPORT_ADMINS",
              "FOCUS_REPORT_ALLOW_MENTIONS"):
        monkeypatch.delenv(k, raising=False)
    s = tasks.Settings.from_env()
    assert (s.history_max, s.ask_max_per_day, s.allowed_channels, s.admins, s.allow_mentions) == \
        (5, 1000, frozenset(), frozenset(), False)
    monkeypatch.setenv("FOCUS_REPORT_HISTORY_MAX", "20")
    monkeypatch.setenv("FOCUS_ASK_MAX_PER_DAY", "3")
    monkeypatch.setenv("FOCUS_REPORT_CHANNELS", "C0AAA00001, C0BBB00002")
    monkeypatch.setenv("FOCUS_REPORT_ADMINS", "U0ADMIN001")
    monkeypatch.setenv("FOCUS_REPORT_ALLOW_MENTIONS", "true")
    s = tasks.Settings.from_env()
    assert (s.history_max, s.ask_max_per_day, s.allowed_channels, s.admins, s.allow_mentions) == \
        (20, 3, frozenset({"C0AAA00001", "C0BBB00002"}), frozenset({"U0ADMIN001"}), True)


# ---------- ask limit ----------

def test_ask_limit_per_day(d):
    s = settings(ask_max_per_day=2)
    assert tasks.take_ask_slot(d, NOW, s) and tasks.take_ask_slot(d, NOW, s)
    assert not tasks.take_ask_slot(d, NOW, s)
    assert tasks.take_ask_slot(d, NOW + timedelta(days=1), s)  # a new UTC day


def test_list_for_user_and_channel(d):
    add(d)                               # Ravi's DM task
    add(d, owner=MEENA, target=TEAM)     # Meena's, in #team
    add(d, owner=MEENA, target={"type": "user", "id": MEENA})
    ids = [t["id"] for t in tasks.list_for(d, user=RAVI, channel="C0TEAM0001")]
    assert ids == ["t1", "t2"]



# ---------- review fixes ----------

@pytest.mark.parametrize("cron,days", [
    ("0 17 * * 5", ["Fri", "Fri"]),                         # cron 5 = Friday (APScheduler alone would say Sat)
    ("0 9 * * 1-5", ["Fri", "Mon", "Tue", "Wed", "Thu"]),   # weekdays
    ("0 9 * * 0", ["Sun", "Sun"]),
    ("0 9 * * 7", ["Sun", "Sun"]),
    ("0 9 * * 0,6", ["Sat", "Sun", "Sat"]),
    ("0 9 * * mon-fri", ["Fri", "Mon", "Tue"]),
    ("0 9 * * 0-6", ["Fri", "Sat", "Sun"]),
])
def test_cron_weekdays_follow_cron_numbering(cron, days):
    sched, now, got = {"cron": cron, "tz": "UTC"}, NOW, []  # NOW is Thu 5 Nov 09:00 UTC
    for _ in days:
        now = tasks.next_run(sched, now + timedelta(seconds=1))
        got.append(now.strftime("%a"))
    assert got == days


def test_removed_task_is_not_revived_by_a_late_send(d):
    add(d)
    add(d, schedule={"at": "2026-11-06 10:00", "tz": "UTC"})
    for tid in ("t1", "t2"):
        tasks.change(d, tid, "remove", by=RAVI, where="x", now=NOW, settings=settings())
    tasks.auto_pause(d, "t1", reason="r", now=NOW, settings=settings())
    tasks.mark_sent(d, "t2", at=NOW, data_through=None, settings=settings())
    assert tasks.get(d, "t1")["status"] == "removed" and tasks.get(d, "t2")["status"] == "removed"
    assert tasks.history(d, "t2")[-1]["action"] == "removed"


def test_removing_a_task_closes_its_requests(d):
    add(d, target=TEAM)
    rid = tasks.change(d, "t1", "pause", by=MEENA, where="C0TEAM0001", now=NOW, settings=settings())["request"]["id"]
    tasks.change(d, "t1", "remove", by=RAVI, where="x", now=NOW, settings=settings())
    with pytest.raises(tasks.TaskError) as e:
        tasks.resolve(d, rid, by=RAVI, accept=True, now=NOW, settings=settings())
    assert e.value.code == "no_such_request"
    assert tasks.process_requests(d, NOW + timedelta(days=8), settings=settings()) == \
        {"reminders": [], "accepted": [], "failed": [], "expired": []}


def test_confirm_that_can_no_longer_apply_is_closed_cleanly(d):
    add(d, target=TEAM)
    later = {"at": "2026-11-06 10:00", "tz": "UTC"}
    rid = tasks.change(d, "t1", "edit", by=MEENA, where="C0TEAM0001", changes={"schedule": later}, now=NOW,
                       settings=settings())["request"]["id"]
    with pytest.raises(tasks.TaskError) as e:  # days later that time has passed
        tasks.resolve(d, rid, by=RAVI, accept=True, now=NOW + timedelta(days=3), settings=settings())
    assert e.value.code == "time_passed"
    actions = [x["action"] for x in tasks.history(d, "t1")]
    assert "confirmed" not in actions and actions[-1] == "request_failed"
    with pytest.raises(tasks.TaskError):
        tasks.resolve(d, rid, by=RAVI, accept=True, now=NOW, settings=settings())  # closed


def test_resume_of_a_one_time_task_in_the_past_refused(d):
    add(d, schedule={"at": "2026-11-06 10:00", "tz": "UTC"})
    tasks.change(d, "t1", "pause", by=RAVI, where="x", now=NOW, settings=settings())
    with pytest.raises(tasks.TaskError) as e:
        tasks.change(d, "t1", "resume", by=RAVI, where="x", now=NOW + timedelta(days=2), settings=settings())
    assert e.value.code == "time_passed"


def test_missed_one_time_task(d):
    add(d, schedule={"at": "2026-11-06 10:00", "tz": "UTC"})
    tasks.mark_missed(d, "t1", now=NOW + timedelta(days=2), settings=settings())
    assert tasks.get(d, "t1")["status"] == "done"
    assert tasks.history(d, "t1")[-1]["action"] == "missed"



# ---------- D1: only listed Slack users create channel tasks ----------

def test_channel_tasks_only_for_listed_creators(d):
    s = settings(creators=frozenset({RAVI}), admins=frozenset({ADMIN}))
    with pytest.raises(tasks.TaskError) as e:
        add(d, owner=MEENA, target=TEAM, settings=s)
    assert e.value.code == "not_a_creator"
    add(d, owner=MEENA, target={"type": "user", "id": MEENA}, settings=s)  # her own DM: fine
    add(d, owner=RAVI, target=TEAM, settings=s)
    add(d, owner=ADMIN, target=TEAM, settings=s)                         # admins count as listed


def test_empty_creator_list_means_nobody(d):
    with pytest.raises(tasks.TaskError) as e:
        add(d, target=TEAM, settings=settings(creators=frozenset()))
    assert e.value.code == "not_a_creator"


def test_moving_a_task_to_a_channel_needs_a_listed_creator(d):
    s = settings(creators=frozenset({RAVI}))
    add(d, owner=MEENA, target={"type": "user", "id": MEENA}, settings=s)
    with pytest.raises(tasks.TaskError) as e:
        tasks.change(d, "t1", "edit", by=MEENA, where="x", changes={"target": TEAM}, now=NOW, settings=s)
    assert e.value.code == "not_a_creator"


def test_creators_from_env(monkeypatch):
    monkeypatch.setenv("FOCUS_REPORT_CREATORS", "U0RAVI0001, U0MEENA001")
    assert tasks.Settings.from_env().creators == frozenset({RAVI, MEENA})
    monkeypatch.delenv("FOCUS_REPORT_CREATORS")
    assert tasks.Settings.from_env().creators == frozenset()


def test_auto_accept_that_can_no_longer_apply_fails_cleanly(d):
    add(d, target=TEAM)
    later = {"at": "2026-11-06 10:00", "tz": "UTC"}
    tasks.change(d, "t1", "edit", by=MEENA, where="C0TEAM0001", changes={"schedule": later}, now=NOW,
                 settings=settings())
    r = tasks.process_requests(d, NOW + timedelta(days=7, minutes=1), settings=settings())
    assert [q["by"] for q in r["failed"]] == [MEENA] and r["accepted"] == []
    assert tasks.history(d, "t1")[-1]["action"] == "request_failed"
    assert tasks.get(d, "t1")["schedule"]["cron"] == "0 15 * * *"



# ---------- review 2026-11-06 ----------

def test_two_old_requests_on_one_task_do_not_crash(d):
    add(d, target=TEAM)
    tasks.change(d, "t1", "remove", by=MEENA, where="C0TEAM0001", now=NOW, settings=settings())
    tasks.change(d, "t1", "pause", by=MEENA, where="C0TEAM0001", now=NOW, settings=settings())
    r = tasks.process_requests(d, NOW + timedelta(days=7, minutes=1), settings=settings())
    assert len(r["accepted"]) == 1 and tasks.get(d, "t1")["status"] == "removed"


def test_resume_of_a_channel_task_needs_a_listed_creator(d):
    add(d, target=TEAM)
    tasks.change(d, "t1", "pause", by=RAVI, where="x", now=NOW, settings=settings())
    with pytest.raises(tasks.TaskError) as e:
        tasks.change(d, "t1", "resume", by=RAVI, where="x", now=NOW, settings=settings(creators=frozenset()))
    assert e.value.code == "not_a_creator"



def test_only_stop_pause_resume_time_and_name_go_through_unanswered(d):
    """Security: an unanswered request may not change where a task posts or what it says."""
    add(d, target=TEAM)
    tasks.change(d, "t1", "edit", by=MEENA, where="C0TEAM0001", changes={"target": {"type": "user", "id": MEENA}},
                 now=NOW, settings=settings())
    tasks.change(d, "t1", "edit", by=MEENA, where="C0TEAM0001", changes={"payload": {"text": "hi"}}, now=NOW,
                 settings=settings())
    tasks.change(d, "t1", "edit", by=MEENA, where="C0TEAM0001",
                 changes={"schedule": {"cron": "0 9 * * *", "tz": "UTC"}}, now=NOW, settings=settings())
    r = tasks.process_requests(d, NOW + timedelta(days=7, minutes=1), settings=settings())
    assert len(r["expired"]) == 2 and len(r["accepted"]) == 1
    t = tasks.get(d, "t1")
    assert t["target"] == TEAM and t["payload"] == {"text": "stop the dev instances"}
    assert t["schedule"]["cron"] == "0 9 * * *"
    assert "request_expired" in [e["action"] for e in tasks.history(d, "t1")]
