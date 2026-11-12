"""Temporary vs permanent failures.

A Slack hiccup (network, timeout, 429, 5xx, `ratelimited`) or a render that couldn't finish (RenderError: the agent
didn't answer, the cost data couldn't be read) must not pause a task for good: Slack calls retry a few times in the
call, and a run that still fails is tried again in 5 minutes, at most 3 times, before the task is paused. Problems
that a retry can't fix (bot not in the channel, channel archived, the person has left) pause at once with a clear
message; when the owner can't be reached, the report admins are told instead."""
import io
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from pico_plugin_schedule import kinds, runner, slack_api, tasks

from .test_slack_api import FakeSlack

RAVI, ADMIN = "U0RAVI0001", "U0ADMIN001"
NOW = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
ME = {"type": "user", "id": RAVI}
TEAM = {"type": "channel", "id": "C0TEAM0001"}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(slack_api, "_sleep", slept.append)
    return slept


@pytest.fixture
def hw(tmp_path, monkeypatch):
    """A runner with a scheduler (not started), a fake Slack whose answers a test can change, and a clock."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    monkeypatch.setenv("FOCUS_REPORT_CREATORS", RAVI)
    monkeypatch.setenv("FOCUS_REPORT_ADMINS", ADMIN)
    for k in ("FOCUS_REPORT_CHANNELS", "FOCUS_ASK_MAX_PER_DAY"):
        monkeypatch.delenv(k, raising=False)
    world = {"now": NOW, "post": {"ok": True, "ts": "1.1"}, "info": None, "open": None}
    fake = FakeSlack(
        conversations_open=lambda p: world["open"] or {"ok": True, "channel": {"id": "D0" + p["users"][2:]}},
        conversations_info=lambda p: world["info"] or {"ok": True, "channel": {"id": p["channel"], "is_member": True}},
        chat_postMessage=lambda p: world["post"](p) if callable(world["post"]) else world["post"])
    monkeypatch.setattr(runner, "_utcnow", lambda: world["now"])
    monkeypatch.setattr(runner, "_HELD", {})
    monkeypatch.setattr(runner.kinds, "render", lambda t, *a, **k: (f"TEXT {t['id']}", "2026-11-09"))
    d = str(tmp_path)
    sched = BackgroundScheduler()
    runner.install(sched, data_dir=d, slack_client_factory=lambda: slack_api.Client("xoxb-test-token", transport=fake))

    def add(**kw):
        args = dict(owner=RAVI, where="D0RAVI0001", kind="report", name="Daily cost", target=ME,
                    schedule={"cron": "0 9 * * *", "tz": "UTC"}, payload={"template": "anything"},
                    now=NOW - timedelta(hours=2), settings=tasks.Settings(creators=frozenset({RAVI})))
        args.update(kw)
        return tasks.add(d, **args)

    def posts(to=None):
        return [p for m, p in fake.calls if m == "chat.postMessage" and (to is None or p["channel"] == to)]

    world.update(d=d, sched=sched, add=add, posts=posts, fake=fake)
    return world


# ---------- Slack calls retry in the call ----------

def _transport(answers):
    """A transport that returns/raises the given answers in turn."""
    calls = []

    def t(method, params):
        calls.append(method)
        a = answers.pop(0) if answers else ({"ok": True}, {})
        if isinstance(a, Exception):
            raise a
        return a

    return t, calls


def _http_error(code, retry_after=None):
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return urllib.error.HTTPError("https://slack.com/api/x", code, "err", headers, io.BytesIO(b""))


def test_a_429_is_retried_after_retry_after(no_sleep):
    t, calls = _transport([_http_error(429, retry_after=7), ({"ok": True, "ts": "1.2"}, {})])
    c = slack_api.Client("xoxb-x", transport=t)
    assert c._call("chat.postMessage", channel="C1", text="hi")[0]["ok"]
    assert calls == ["chat.postMessage", "chat.postMessage"] and no_sleep == [7]


@pytest.mark.parametrize("first", [_http_error(503), TimeoutError("timed out"), OSError("reset"),
                                   ({"ok": False, "error": "ratelimited"}, {}),
                                   ({"ok": False, "error": "internal_error"}, {})])
def test_temporary_slack_trouble_is_retried(first, no_sleep):
    t, calls = _transport([first, ({"ok": True}, {})])
    c = slack_api.Client("xoxb-x", transport=t)
    assert c._call("conversations.info")[0]["ok"] and len(calls) == 2 and len(no_sleep) == 1


def test_retries_stop_after_three_tries(no_sleep):
    t, calls = _transport([_http_error(500)] * 5)
    c = slack_api.Client("xoxb-x", transport=t)
    with pytest.raises(slack_api.SlackError) as e:
        c._call("chat.postMessage")
    assert e.value.code == "slack_error" and len(calls) == 3


def test_a_long_retry_after_is_capped(no_sleep):
    t, _ = _transport([_http_error(429, retry_after=900), ({"ok": True}, {})])
    slack_api.Client("xoxb-x", transport=t)._call("x")
    assert no_sleep == [slack_api.RETRY_AFTER_MAX_S]


def test_a_permanent_error_is_not_retried(no_sleep):
    t, calls = _transport([({"ok": False, "error": "channel_not_found"}, {})])
    c = slack_api.Client("xoxb-x", transport=t)
    assert c._call("chat.postMessage")[0]["error"] == "channel_not_found" and len(calls) == 1 and no_sleep == []


# ---------- a run that still fails is tried again, then paused ----------

def _slack_down(p):
    raise urllib.error.URLError(ConnectionRefusedError("connection refused"))  # surely not sent


def test_slack_down_during_a_send_keeps_the_task_and_retries_in_5_minutes(hw):
    t = hw["add"]()
    hw["post"] = _slack_down
    runner.run_task(t["id"])
    assert tasks.get(hw["d"], t["id"])["status"] == "active"
    job = runner._booking(hw["sched"], t['id'])
    assert job is not None and job.trigger.run_date == NOW + timedelta(minutes=5)
    assert hw["posts"]("D0RAVI0001") == [] or all("paused" not in p["text"] for p in hw["posts"]("D0RAVI0001"))


def test_the_retry_sends_when_slack_is_back(hw):
    t = hw["add"]()
    hw["post"] = _slack_down
    runner.run_task(t["id"])
    hw["post"] = {"ok": True, "ts": "1.3"}
    hw["now"] = NOW + timedelta(minutes=5)
    runner.run_task(t["id"], hold=False)                     # what the taskrun_ job does
    task = tasks.get(hw["d"], t["id"])
    assert task["status"] == "active" and task["last_sent_at"] and not task.get("fail_count")


def test_three_failed_runs_in_a_row_pause_and_tell_the_owner(hw):
    t = hw["add"](target=TEAM, where="C0TEAM0001")
    hw["post"] = lambda p: _slack_down(p) if p["channel"] == "C0TEAM0001" else {"ok": True, "ts": "1"}
    for i in range(runner.RETRY_MAX):
        hw["now"] = NOW + timedelta(minutes=5 * i)
        runner.run_task(t["id"], hold=False)
    assert tasks.get(hw["d"], t["id"])["status"] == "paused"
    notice = hw["posts"]("D0RAVI0001")
    assert len(notice) == 1 and "3 times" in notice[0]["text"] and "resume task" in notice[0]["text"]


def test_a_render_that_could_not_finish_is_retried_not_paused(hw, monkeypatch):
    t = hw["add"](kind="ask", payload={"question": "q?"})

    def no_answer(*a, **k):
        raise kinds.RenderError("the agent didn't answer within 10 minutes")

    monkeypatch.setattr(runner.kinds, "render", no_answer)
    runner.run_task(t["id"])
    assert tasks.get(hw["d"], t["id"])["status"] == "active"
    assert runner._booking(hw["sched"], t['id']) is not None


def test_a_broken_saved_query_still_pauses_at_once(hw, monkeypatch):
    t = hw["add"](kind="query", payload={"sql": "SELECT 1"})

    def binder(*a, **k):
        raise RuntimeError("Binder Error: column x not found")

    monkeypatch.setattr(runner.kinds, "render", binder)
    runner.run_task(t["id"])
    assert tasks.get(hw["d"], t["id"])["status"] == "paused"


def test_a_temporary_check_failure_is_retried(hw):
    t = hw["add"](target=TEAM, where="C0TEAM0001")
    hw["info"] = {"ok": False, "error": "ratelimited"}
    runner.run_task(t["id"])
    assert tasks.get(hw["d"], t["id"])["status"] == "active"
    assert runner._booking(hw["sched"], t['id']) is not None


def test_a_one_time_task_with_a_retry_pending_is_not_closed_as_missed(hw):
    t = hw["add"](schedule={"at": "2026-11-10 09:00", "tz": "UTC"})
    hw["post"] = _slack_down
    runner.run_task(t["id"])
    hw["now"] = NOW + timedelta(minutes=2)
    runner.tasks_sync()
    assert tasks.get(hw["d"], t["id"])["status"] == "active"
    assert not any("missed" in p["text"] for p in hw["posts"]())


# ---------- permanent problems: clear message at once; owner gone -> admins ----------

def test_archived_channel_pauses_with_a_clear_message(hw):
    t = hw["add"](target=TEAM, where="C0TEAM0001")
    hw["info"] = {"ok": True, "channel": {"id": "C0TEAM0001", "is_member": True, "is_archived": True}}
    runner.run_task(t["id"])
    assert tasks.get(hw["d"], t["id"])["status"] == "paused"
    assert "archived" in hw["posts"]("D0RAVI0001")[-1]["text"]


def test_post_to_an_archived_channel_pauses_at_once(hw):
    t = hw["add"](target=TEAM, where="C0TEAM0001")
    hw["post"] = lambda p: ({"ok": False, "error": "is_archived"} if p["channel"] == "C0TEAM0001"
                            else {"ok": True, "ts": "1"})
    runner.run_task(t["id"])
    assert tasks.get(hw["d"], t["id"])["status"] == "paused"
    assert "archived" in hw["posts"]("D0RAVI0001")[-1]["text"]


@pytest.mark.parametrize("err", ["user_not_found", "user_disabled", "account_inactive"])
def test_owner_who_left_slack_pauses_and_admins_are_told(hw, err):
    t = hw["add"]()                                          # DMs Ravi, who has left
    hw["fake"].answers["conversations_open"] = lambda p: (
        {"ok": False, "error": err} if p["users"] == RAVI else {"ok": True, "channel": {"id": "D0" + p["users"][2:]}})
    runner.run_task(t["id"])
    task = tasks.get(hw["d"], t["id"])
    assert task["status"] == "paused" and "left" in task["paused_reason"]
    to_admin = hw["posts"]("D0ADMIN001")
    assert len(to_admin) == 1 and t["id"] in to_admin[0]["text"] and f"<@{RAVI}>" in to_admin[0]["text"]
