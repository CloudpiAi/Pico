"""Review of the hardening (2026-11-10): never post twice, never block the sync on notices, keep good tasks running.

1. chat.postMessage is retried only when Slack surely didn't take it (429 / ratelimited / 5xx / the connection never
   opened); a read timeout or a reset after sending is "unsure" and the task counts as sent (at most once).
2. A failure to record the send after a good post never books a second post.
3. Notices (missed, paused, admin alerts) are one Slack try each, so a Slack outage can't hold tasks_sync for minutes.
4. Every booked run has its own job id (APScheduler removes a finished date job by id after starting it).
5. A run that ended without a failure (condition not met, ask limit) clears the failure count.
6. Only a task's own broken data pauses it in tasks_sync; a scheduler error keeps it as it is.
7. tasks.json missing but tasks.json.bak there: the backup is used, never an empty list.
8. A question that couldn't finish gives its daily slot back.
10. A task closed before closed_at existed is kept 30 days from now, not purged at once.
13. Data tasks run on their own small pool, so they never hold up reminders."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from pico_plugin_schedule import kinds, runner, slack_api, tasks

from .test_slack_api import FakeSlack

RAVI, ADMIN = "U0RAVI0001", "U0ADMIN001"
NOW = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
ME = {"type": "user", "id": RAVI}
S = tasks.Settings()


@pytest.fixture
def rv(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    monkeypatch.setenv("FOCUS_REPORT_ADMINS", ADMIN)
    for k in ("FOCUS_REPORT_CHANNELS", "FOCUS_ASK_MAX_PER_DAY", "FOCUS_REPORT_CREATORS"):
        monkeypatch.delenv(k, raising=False)
    w = {"post": {"ok": True, "ts": "1"}}
    fake = FakeSlack(conversations_open=lambda p: {"ok": True, "channel": {"id": "D0" + p["users"][2:]}},
                     chat_postMessage=lambda p: w["post"](p) if callable(w["post"]) else w["post"])
    monkeypatch.setattr(runner, "_utcnow", lambda: NOW)
    monkeypatch.setattr(runner, "_HELD", {})
    monkeypatch.setattr(runner.kinds, "render", lambda t, *a, **k: (f"TEXT {t['id']}", None))
    d = str(tmp_path)
    sched = BackgroundScheduler()
    runner.install(sched, data_dir=d, slack_client_factory=lambda: slack_api.Client("xoxb-test-token", transport=fake))

    def add(kind="report", **kw):
        args = dict(owner=RAVI, where="D0RAVI0001", kind=kind, name="n", target=ME,
                    schedule={"cron": "0 9 * * *", "tz": "UTC"},
                    payload={"text": "x"} if kind == "reminder" else {"question": "q?"} if kind == "ask"
                    else {"template": "t"}, now=NOW - timedelta(hours=1), settings=S)
        args.update(kw)
        return tasks.add(d, **args)

    def posts(to=None):
        return [p for m, p in fake.calls if m == "chat.postMessage" and (to is None or p["channel"] == to)]

    w.update(d=d, sched=sched, add=add, posts=posts, fake=fake)
    return w


# ---------- 1 ----------

def _raise(e):
    def f(*a, **k):
        raise e
    return f


@pytest.mark.parametrize("err", [TimeoutError("read timed out"), ConnectionResetError("reset")])
def test_a_post_that_may_have_gone_out_is_not_posted_again(err):
    calls = []

    def t(method, params):
        calls.append(method)
        raise err

    with pytest.raises(slack_api.SlackError) as e:
        slack_api.Client("xoxb-x", transport=t)._call("chat.postMessage", channel="C1", text="hi")
    assert e.value.code == "slack_unsure" and calls == ["chat.postMessage"]


def test_a_post_whose_connection_never_opened_is_tried_again():
    calls = []

    def t(method, params):
        calls.append(method)
        if len(calls) == 1:
            raise httpx.ConnectError("refused")
        return {"ok": True, "ts": "1"}, {}

    assert slack_api.Client("xoxb-x", transport=t)._call("chat.postMessage")[0]["ok"] and len(calls) == 2


def test_an_unsure_post_counts_as_sent_and_is_not_retried(rv):
    t = rv["add"]()
    rv["post"] = _raise(TimeoutError("read timed out"))
    runner.run_task(t["id"])
    task = tasks.get(rv["d"], t["id"])
    assert task["status"] == "active" and task["last_sent_at"] and runner._booking(rv["sched"], t["id"]) is None


# ---------- 2 ----------

def test_a_failure_to_record_the_send_never_posts_again(rv, monkeypatch):
    t = rv["add"]()
    monkeypatch.setattr(tasks, "mark_sent", _raise(OSError("disk full")))
    runner.run_task(t["id"])
    assert len(rv["posts"]()) == 1 and runner._booking(rv["sched"], t["id"]) is None
    assert tasks.get(rv["d"], t["id"]).get("sending_at")          # the next start won't send it again


# ---------- 3 ----------

def test_a_notice_is_one_slack_try(rv):
    rv["post"] = _raise(httpx.ConnectError("refused"))
    runner._tell_owner(None, {"id": "t1", "owner": RAVI}, "hello")
    assert len(rv["posts"]()) == 2                                  # the owner once, then the admin once


# ---------- 4 ----------

def test_each_booking_has_its_own_job(rv):
    runner._run_later("t1", NOW, why="retry")
    runner._run_later("t1", NOW, why="release")
    ids = [j.id for j in rv["sched"].get_jobs() if j.id.startswith(runner.RUN_PREFIX)]
    assert len(ids) == 2 and len(set(ids)) == 2 and runner._busy("t1")


# ---------- 5 ----------

def test_condition_not_met_clears_the_failure_count(rv, monkeypatch):
    t = rv["add"]()
    tasks.note_failure(rv["d"], t["id"])
    monkeypatch.setattr(runner.kinds, "render", lambda *a, **k: (None, None))
    runner.run_task(t["id"])
    assert not tasks.get(rv["d"], t["id"]).get("fail_count")


# ---------- 6 ----------

def test_a_scheduler_error_does_not_pause_a_good_task(rv, monkeypatch):
    t = rv["add"]()
    monkeypatch.setattr(rv["sched"], "add_job", _raise(RuntimeError("jobstore busy")))
    runner.tasks_sync()
    assert tasks.get(rv["d"], t["id"])["status"] == "active" and rv["posts"]() == []


# ---------- 7 ----------

def test_a_missing_tasks_file_is_read_from_the_backup(rv, tmp_path):
    t = rv["add"]()
    (tmp_path / "tasks.json").unlink()
    assert tasks.get(rv["d"], t["id"])["name"] == "n"


# ---------- 8 ----------

def test_a_question_whose_agent_never_ran_gives_its_slot_back(rv, monkeypatch):
    t = rv["add"](kind="ask")
    monkeypatch.setattr(runner.kinds, "render", _raise(kinds.RenderError("reading the cost data failed")))
    runner.run_task(t["id"])
    assert tasks._load(rv["d"])["ask_slots"]["used"] == 0


def test_a_question_whose_agent_ran_still_counts(rv, monkeypatch):
    """Security review: a timed-out or failed agent run used the LLM; giving the slot back would let failing
    questions run past the daily limit."""
    t = rv["add"](kind="ask")
    monkeypatch.setattr(runner.kinds, "render", _raise(kinds.RenderError("no answer in 10 minutes", spent=True)))
    runner.run_task(t["id"])
    assert tasks._load(rv["d"])["ask_slots"]["used"] == 1


# ---------- 10 ----------

def test_a_task_closed_before_closed_at_existed_is_not_purged_at_once(rv):
    tid = rv["add"]()["id"]
    tasks.mark_missed(rv["d"], tid, now=NOW - timedelta(days=60), settings=S)

    def legacy(data):
        data["tasks"][tid].pop("closed_at")
        data["tasks"][tid]["created_at"] = (NOW - timedelta(days=90)).isoformat()

    with tasks._locked(rv["d"]):
        data = tasks._load(rv["d"])
        legacy(data)
        tasks._save(rv["d"], data)
    assert tasks.purge_closed(rv["d"], NOW) == []
    assert tasks.purge_closed(rv["d"], NOW + timedelta(days=31)) == [tid]


# ---------- 13 ----------

def test_data_tasks_run_on_their_own_pool(rv):
    report, reminder = rv["add"](), rv["add"](kind="reminder")
    runner.tasks_sync()
    assert rv["sched"].get_job(f"task_{report['id']}").executor == runner.DATA_EXECUTOR
    assert rv["sched"].get_job(f"task_{reminder['id']}").executor == runner.EXECUTOR
    runner._run_later(report["id"], NOW, why="retry")
    assert runner._booking(rv["sched"], report["id"]).executor == runner.DATA_EXECUTOR
