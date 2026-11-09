"""pico_finops_agent.scheduler: the agent's APScheduler (twice-a-day load, the 1-minute watch, retries, notices).

It runs inside the one agent process (pico_finops_agent.agent); there is no FastAPI any more.
"""
import json
import time

import pytest

pytest.importorskip("apscheduler", reason="Needs the pico-finops-agent extra: uv sync --extra pico-finops-agent && uv pip install -e ./plugins/focus")

from apscheduler.schedulers.background import BackgroundScheduler  # noqa: E402

from pico_finops_agent import scheduler as agent_api  # noqa: E402


def test_job_defaults_from_env(monkeypatch):
    monkeypatch.delenv("FOCUS_SCHEDULE", raising=False)
    sched = agent_api.build_scheduler()
    jobs = sched.get_jobs()
    assert [j.id for j in jobs] == ["pico_finops_agent_load", "pico_finops_agent_watch"]
    assert sched._job_defaults["max_instances"] == 1
    assert sched._job_defaults["coalesce"] is True


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, "0 6,18 * * *"),
        ({"FOCUS_SCHEDULE": "0 2 * * *"}, "0 2 * * *"),
    ],
)
def test_schedule_from_focus_schedule(monkeypatch, env, expected):
    """FOCUS_SCHEDULE (the customer's .env setting) is the only schedule setting."""
    monkeypatch.delenv("FOCUS_SCHEDULE", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert agent_api.job_cron() == expected


def test_failed_job_is_logged_not_raised(monkeypatch, capsys):
    monkeypatch.setenv("FOCUS_JOB_CMD", "definitely-not-a-command-xyz")
    agent_api._run_job()
    out = capsys.readouterr().out
    assert '"event": "scheduled_load_failed"' in out



# ---------- Phase 2: what the job runs, start-up load, retry ----------

def test_default_job_command_is_load(monkeypatch):
    monkeypatch.delenv("FOCUS_JOB_CMD", raising=False)
    assert agent_api.job_command() == "pico focus-data load"


def test_startup_due_when_cache_missing_or_stale(tmp_path):
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    assert agent_api.startup_due(str(tmp_path), now) is True  # no cache
    (tmp_path / "focus.duckdb").write_bytes(b"x")
    assert agent_api.startup_due(str(tmp_path), now) is True  # no local state
    (tmp_path / "state.json").write_text('{"last_success": "%s"}' % (now - timedelta(hours=11)).isoformat())
    assert agent_api.startup_due(str(tmp_path), now) is False
    (tmp_path / "state.json").write_text('{"last_success": "%s"}' % (now - timedelta(hours=13)).isoformat())
    assert agent_api.startup_due(str(tmp_path), now) is True
    (tmp_path / "state.json").write_text("not json")
    assert agent_api.startup_due(str(tmp_path), now) is True


class _AddRecorder(BackgroundScheduler):
    def __init__(self):
        super().__init__()
        self.added = []

    def add_job(self, func, *a, **kw):
        self.added.append(kw.get("id"))
        return super().add_job(func, *a, **kw)


@pytest.mark.parametrize("due", [True, False])
def test_startup_job_only_when_due(monkeypatch, due):
    monkeypatch.setattr(agent_api, "startup_due", lambda *a, **k: due)
    monkeypatch.setattr(agent_api, "_run_job", lambda retry=True: None)
    sched = _AddRecorder()
    agent_api.start_scheduler(sched)
    agent_api.stop_scheduler(sched)
    assert ("pico_finops_agent_startup" in sched.added) is due


@pytest.mark.parametrize("rc,retried", [(0, False), (1, True), (2, False)])
def test_retry_once_after_30_minutes_only_for_rc_1(monkeypatch, rc, retried):
    sched = BackgroundScheduler()
    monkeypatch.setattr(agent_api, "_SCHEDULER", sched)
    monkeypatch.setattr(agent_api.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": rc})())
    agent_api._run_job()
    job = sched.get_job("pico_finops_agent_retry")
    assert (job is not None) is retried
    if retried:
        assert job.args == (False,)  # the retry itself never schedules another


def test_retry_run_does_not_reschedule(monkeypatch):
    sched = BackgroundScheduler()
    monkeypatch.setattr(agent_api, "_SCHEDULER", sched)
    monkeypatch.setattr(agent_api.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 1})())
    agent_api._run_job(False)
    assert sched.get_job("pico_finops_agent_retry") is None


def test_default_schedule_is_twice_a_day_utc():
    """0 6,18 * * * UTC: from 07:00 the next runs are 18:00 today, then 06:00 and 18:00 tomorrow."""
    from datetime import datetime, timezone

    from apscheduler.triggers.cron import CronTrigger

    trig = CronTrigger.from_crontab(agent_api.DEFAULT_CRON, timezone="UTC")
    t = datetime(2026, 11, 5, 7, 0, tzinfo=timezone.utc)
    runs = []
    for _ in range(3):
        t = trig.get_next_fire_time(None, t)
        runs.append(t.strftime("%d %H:%M"))
        t = t.replace(second=1)
    assert runs == ["05 18:00", "06 06:00", "06 18:00"]


def test_start_log_shows_next_runs(monkeypatch, capsys):
    monkeypatch.delenv("FOCUS_SCHEDULE", raising=False)
    monkeypatch.setattr(agent_api, "startup_due", lambda *a, **k: False)
    sched = agent_api.build_scheduler()
    agent_api.start_scheduler(sched)
    agent_api.stop_scheduler(sched)
    line = next(ln for ln in capsys.readouterr().out.splitlines() if '"scheduler_started"' in ln)
    assert '"next_runs"' in line and ("06:00:00+00:00" in line or "18:00:00+00:00" in line)


# ---------- Slack notice after a scheduled load that brought new data ----------

def _ran(monkeypatch, tmp_path, rc, months, channel="C0TESTCHAN1", token="xoxb-test"):
    import json as _json

    (tmp_path / "state.json").write_text(_json.dumps(
        {"last_result": {"status": "ok" if rc == 0 else "error", "months_loaded": months, "table_created": False}}))
    monkeypatch.setenv("FOCUS_DATA_DIR", str(tmp_path))
    for k, v in (("SLACK_NOTIFY_CHANNEL", channel), ("SLACK_BOT_TOKEN", token)):
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    monkeypatch.setattr(agent_api, "_SCHEDULER", None)
    monkeypatch.setattr(agent_api.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": rc})())
    posts = []
    monkeypatch.setattr(agent_api, "_post_slack", lambda token, channel, text: posts.append((channel, text)))
    agent_api._run_job()
    return posts


def test_new_months_are_announced(monkeypatch, tmp_path):
    posts = _ran(monkeypatch, tmp_path, 0, ["2026-10", "2026-11"])
    assert len(posts) == 1
    channel, text = posts[0]
    assert channel == "C0TESTCHAN1"
    assert "October 2026" in text and "November 2026" in text


@pytest.mark.parametrize("rc,months,channel,token", [
    (0, [], "C0TESTCHAN1", "xoxb-test"),          # nothing new: no noise twice a day
    (1, ["2026-11"], "C0TESTCHAN1", "xoxb-test"),  # failed run
    (0, ["2026-11"], None, "xoxb-test"),           # notices not configured
    (0, ["2026-11"], "C0TESTCHAN1", None),         # no bot token
])
def test_no_notice(monkeypatch, tmp_path, rc, months, channel, token):
    assert _ran(monkeypatch, tmp_path, rc, months, channel, token) == []


INSIGHTS = {
    "last_full_day": "2026-11-04", "latest_day_cost": 22.7, "avg7": 17.0,
    "mtd": {"cost": 73.7, "prev_same_days": 90.0},
    "spikes": [{"kind": "resource", "name": "i-jumpy", "today": 12.0, "avg7": 5.0},
               {"kind": "service", "name": "Amazon EC2", "today": 22.0, "avg7": 15.0}],
    "stopped": [{"name": "Databricks", "avg7": 2.0}],
    "new_resources": {"count": 1, "cost": 0.7},
}


def test_notice_with_insights_reads_naturally():
    text = agent_api.notice_text(["2026-11"], INSIGHTS)
    assert "through *Wed 4 Nov*" in text
    assert "$22.70" in text and "$17.00/day" in text and "34% above" in text
    assert "November so far: *$73.70*" in text and "18% lower than the same days in October" in text
    assert ":warning:" in text and "i-jumpy" in text and "$12.00" in text and "$5.00/day" in text
    assert "Databricks had no charges" in text and "$2.00/day" in text
    assert "1 new resource" in text and "$0.70" in text


def test_notice_without_anything_unusual():
    quiet = {**INSIGHTS, "latest_day_cost": 17.2, "spikes": [], "stopped": [], "new_resources": {"count": 0, "cost": 0}}
    text = agent_api.notice_text(["2026-11"], quiet)
    assert ":warning:" not in text and "Nothing unusual stood out" in text and "about the same as" in text


def test_notice_without_insights_falls_back():
    assert "November 2026 was updated" in agent_api.notice_text(["2026-11"], None)


def test_notice_uses_saved_insights(monkeypatch, tmp_path):
    import json as _json

    (tmp_path / "state.json").write_text(_json.dumps(
        {"last_result": {"status": "ok", "months_loaded": ["2026-11"], "insights": INSIGHTS}}))
    monkeypatch.setenv("FOCUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SLACK_NOTIFY_CHANNEL", "pico-finops-agent")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    posts = []
    monkeypatch.setattr(agent_api, "_post_slack", lambda token, channel, text: posts.append(text))
    agent_api._notify_after_load(0)
    assert posts and "i-jumpy" in posts[0]


def test_failed_post_is_logged_not_raised(monkeypatch, tmp_path, capsys):
    def boom(token, channel, text):
        raise OSError("network down")

    _ran(monkeypatch, tmp_path, 0, ["2026-11"])  # sets env
    monkeypatch.setattr(agent_api, "_post_slack", boom)
    agent_api._run_job()
    out = capsys.readouterr().out
    assert '"slack_notice_failed"' in out and "xoxb-test" not in out


# ---------- start / stop and the watch tick (the health check reads it) ----------

def test_start_then_stop_logs_and_clears(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("FOCUS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(agent_api, "startup_due", lambda *a, **k: False)
    sched = agent_api.build_scheduler()
    agent_api.start_scheduler(sched)
    assert sched.running and agent_api._SCHEDULER is sched
    agent_api.stop_scheduler(sched)
    assert not sched.running and agent_api._SCHEDULER is None
    events = [json.loads(ln)["event"] for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")]
    assert events.index("scheduler_started") < events.index("scheduler_stopped")


def test_start_seeds_the_tick(monkeypatch, tmp_path):
    """The health check must not see 'never ticked' in the first minute after start."""
    monkeypatch.setenv("FOCUS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(agent_api, "startup_due", lambda *a, **k: False)
    monkeypatch.setattr(agent_api, "_LAST_TICK", None)
    before = time.time()
    sched = agent_api.build_scheduler()
    agent_api.start_scheduler(sched)
    try:
        assert agent_api.last_tick() >= before
    finally:
        agent_api.stop_scheduler(sched)


def test_watch_job_stamps_the_tick_even_when_it_fails(monkeypatch):
    def boom():
        raise RuntimeError("slack down")

    monkeypatch.setattr(agent_api, "announce_new_data", boom)
    monkeypatch.setattr(agent_api, "_LAST_TICK", 0.0)
    with pytest.raises(RuntimeError):
        agent_api.watch()
    assert agent_api.last_tick() > 0.0


def test_watch_job_is_the_stamping_watch(monkeypatch):
    sched = agent_api.build_scheduler()
    assert sched.get_job(agent_api.WATCH_JOB_ID).func is agent_api.watch


def test_no_fastapi_glue_left():
    for name in ("check_args", "default_argv", "default_env", "wrap_app", "install_patch"):
        assert not hasattr(agent_api, name), name


# ---------- review fixes (2026-11-06) ----------

def test_load_running_while_the_job_runs(monkeypatch):
    seen = []
    monkeypatch.setattr(agent_api.subprocess, "run",
                        lambda *a, **k: seen.append(agent_api.load_running()) or type("R", (), {"returncode": 0})())
    monkeypatch.setattr(agent_api, "_notify_after_load", lambda *a, **k: None)
    assert agent_api.load_running() is False
    agent_api._run_job(retry=False)
    assert seen == [True] and agent_api.load_running() is False


def test_stop_during_a_load_says_so_and_twice_is_fine(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("FOCUS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(agent_api, "startup_due", lambda *a, **k: False)
    sched = agent_api.start_scheduler(agent_api.build_scheduler())
    monkeypatch.setattr(agent_api, "load_running", lambda: True)
    agent_api.stop_scheduler(sched)
    agent_api.stop_scheduler(sched)  # no error the second time
    assert '"load_interrupted"' in capsys.readouterr().out


def test_startup_load_after_an_interrupted_load(tmp_path):
    (tmp_path / "focus.duckdb").write_bytes(b"x")
    from datetime import datetime, timezone

    now = datetime(2026, 11, 6, 7, 0, tzinfo=timezone.utc)
    (tmp_path / "state.json").write_text(json.dumps({"last_success": "2026-11-06T06:59:00+00:00",
                                                     "load_in_progress": "2026-11-06T06:30:00+00:00"}))
    assert agent_api.startup_due(str(tmp_path), now=now) is True
