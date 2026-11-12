"""Guard for the data-collection scheduler while the scheduled Slack tasks move into their own plugin.

These pin what the load side does today; they must stay green, unchanged, through the split. test_scheduler.py and
test_failure_alerts.py stay as they are too.
"""
import builtins

import pytest

pytest.importorskip("apscheduler", reason="Needs the pico-finops-agent extra")
from apscheduler.triggers.cron import CronTrigger  # noqa: E402

from pico_finops_agent import scheduler as agent_api  # noqa: E402

LOAD_JOBS = {"pico_finops_agent_load", "pico_finops_agent_watch"}


def _fields(trigger):
    return [str(f) for f in trigger.fields]


def test_load_jobs_and_cron_unchanged(monkeypatch):
    monkeypatch.delenv("FOCUS_SCHEDULE", raising=False)
    sched = agent_api.build_scheduler()
    jobs = {j.id: j for j in sched.get_jobs()}
    assert LOAD_JOBS <= set(jobs)
    assert _fields(jobs["pico_finops_agent_load"].trigger) == _fields(CronTrigger.from_crontab("0 6,18 * * *", timezone="UTC"))
    assert str(jobs["pico_finops_agent_load"].trigger.timezone) == "UTC"
    assert jobs["pico_finops_agent_load"].func is agent_api._run_job
    assert jobs["pico_finops_agent_watch"].func is agent_api.watch
    assert jobs["pico_finops_agent_watch"].trigger.interval.total_seconds() == 60
    assert sched._job_defaults == {"coalesce": True, "max_instances": 1, "misfire_grace_time": 300}


def test_customer_schedule_still_drives_the_load(monkeypatch):
    monkeypatch.setenv("FOCUS_SCHEDULE", "30 2 * * 1-5")
    jobs = {j.id: j for j in agent_api.build_scheduler().get_jobs()}
    assert _fields(jobs["pico_finops_agent_load"].trigger) == _fields(CronTrigger.from_crontab("30 2 * * 1-5", timezone="UTC"))


def test_a_load_runs_its_command_and_logs(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(agent_api.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or type("R", (), {"returncode": 0})())
    monkeypatch.setattr(agent_api, "_notify_after_load", lambda *a, **k: None)
    agent_api._run_job(retry=False)
    assert calls == [["pico", "focus-data", "load"]]
    assert '"event": "scheduled_load_done"' in capsys.readouterr().out
    assert agent_api.load_running() is False


def _block(prefixes):
    real = builtins.__import__

    def fake(name, *a, **k):
        if any(name == p or name.startswith(p + ".") for p in prefixes):
            raise ImportError(f"blocked for the test: {name}")
        return real(name, *a, **k)

    return fake


def test_load_runs_without_the_schedule_plugin(monkeypatch, capsys, tmp_path):
    """The agent without the schedule plugin (or with a broken one) still loads, watches and posts notices."""
    monkeypatch.setenv("FOCUS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(builtins, "__import__", _block(["pico_plugin_schedule"]))
    sched = agent_api.build_scheduler()
    assert LOAD_JOBS <= {j.id for j in sched.get_jobs()}
    assert "tasks_sync" not in {j.id for j in sched.get_jobs()}
    monkeypatch.setattr(agent_api.subprocess, "run", lambda cmd, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(agent_api, "_notify_after_load", lambda *a, **k: None)
    agent_api._run_job(retry=False)
    out = capsys.readouterr().out
    assert '"event": "scheduled_load_done"' in out and '"event": "tasks_off"' in out
