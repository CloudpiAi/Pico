"""pico_finops_agent.health: what "healthy" means for the one agent process, the heartbeat file and Docker's check.

User decisions (2026-11-06): self-heal (exit so Docker restarts) when Slack has been down 10 minutes, the Slack
thread died, or the scheduler hasn't ticked for 10 minutes; Slack tokens rejected -> unhealthy, keep collecting,
no restart loop; no Slack tokens -> healthy (data collection only).
"""
import json
import os
import stat
import subprocess
import sys

import pytest

from pico_finops_agent import health

NOW = 1_800_000_000.0
LIMIT = health.down_limit()


def slack(state, since=NOW, sampled_at=NOW):
    return {"state": state, "since": since, "sampled_at": sampled_at}


def verdict(slack_snap, last_tick=NOW):
    return health.evaluate(NOW, slack_snap, last_tick)


# ---------- the decision table ----------

@pytest.mark.parametrize("state", ["disabled", "connecting", "connected"])
def test_healthy_states(state):
    v = verdict(slack(state))
    assert (v.status, v.exit_code) == ("healthy", None)


def test_rejected_tokens_are_unhealthy_but_never_restart():
    v = verdict(slack("rejected", since=NOW - 10 * LIMIT))
    assert v.status == "unhealthy" and v.exit_code is None
    assert "Slack tokens rejected" in v.reason


def test_short_disconnect_is_unhealthy_only():
    v = verdict(slack("disconnected", since=NOW - (LIMIT - 1)))
    assert v.status == "unhealthy" and v.exit_code is None and "disconnected" in v.reason


def test_ten_minute_disconnect_restarts():
    v = verdict(slack("disconnected", since=NOW - LIMIT))
    assert v.exit_code == health.EXIT_SLACK


def test_dead_slack_thread_restarts():
    assert verdict(slack("crashed")).exit_code == health.EXIT_SLACK


def test_frozen_slack_loop_restarts():
    """The Slack thread stopped reporting (a blocking call, a hang): the supervisor still sees it."""
    v = verdict(slack("connected", sampled_at=NOW - LIMIT))
    assert v.exit_code == health.EXIT_SLACK and "not responding" in v.reason


def test_disabled_slack_never_counts_as_frozen():
    assert verdict(slack("disabled", sampled_at=NOW - 10 * LIMIT)).exit_code is None


def test_stuck_scheduler_restarts():
    v = verdict(slack("connected"), last_tick=NOW - LIMIT)
    assert v.exit_code == health.EXIT_SCHEDULER and "scheduler" in v.reason


def test_scheduler_wins_over_slack_problems():
    v = verdict(slack("rejected"), last_tick=NOW - LIMIT)
    assert v.exit_code == health.EXIT_SCHEDULER


def test_scheduler_just_under_the_limit_is_fine():
    assert verdict(slack("disabled"), last_tick=NOW - (LIMIT - 1)).status == "healthy"


def test_never_ticked_scheduler_restarts():
    assert verdict(slack("disabled"), last_tick=None).exit_code == health.EXIT_SCHEDULER


# ---------- heartbeat file + Docker's check ----------

def test_heartbeat_round_trip(tmp_path):
    path = tmp_path / "health.json"
    health.write_heartbeat(str(path), {"ts": NOW, "status": "healthy", "reason": ""})
    assert json.loads(path.read_text()) == {"ts": NOW, "status": "healthy", "reason": ""}
    assert not list(tmp_path.glob("*.tmp"))  # written atomically


@pytest.mark.parametrize("content,expected", [
    ({"ts": NOW - 5, "status": "healthy", "reason": ""}, 0),
    ({"ts": NOW - 5, "status": "unhealthy", "reason": "Slack tokens rejected"}, 1),
    ({"ts": NOW - 5, "status": "stopping", "reason": ""}, 1),
    ({"ts": NOW - health.HEARTBEAT_MAX_AGE_S - 1, "status": "healthy", "reason": ""}, 1),  # supervisor frozen
    ("not json", 1),
    (None, 1),  # no file yet
])
def test_check(tmp_path, content, expected):
    path = tmp_path / "health.json"
    if content is not None:
        path.write_text(content if isinstance(content, str) else json.dumps(content))
    assert health.check(str(path), now=NOW) == expected


def test_check_command_for_docker(tmp_path):
    """`python -m pico_finops_agent.health --check` is the image's HEALTHCHECK; it prints why when it fails."""
    path = tmp_path / "health.json"
    path.write_text(json.dumps({"ts": 0, "status": "healthy", "reason": ""}))
    r = subprocess.run([sys.executable, "-m", "pico_finops_agent.health", "--check"], capture_output=True, text=True,
                       env={"PICO_FINOPS_AGENT_HEALTH_FILE": str(path), "PATH": ""}, timeout=30)
    assert r.returncode == 1 and "old" in (r.stdout + r.stderr)


# ---------- review fixes (2026-11-06) ----------

def test_limit_can_be_tuned(monkeypatch):
    monkeypatch.setenv("PICO_FINOPS_AGENT_DOWN_LIMIT_S", "30")
    assert health.down_limit() == 30
    assert health.evaluate(NOW, slack("disconnected", since=NOW - 30), NOW).exit_code == health.EXIT_SLACK
    monkeypatch.setenv("PICO_FINOPS_AGENT_DOWN_LIMIT_S", "not a number")
    assert health.down_limit() == health.DOWN_LIMIT_S


def test_slack_unreachable_at_start_restarts_after_the_limit():
    """Not the tokens (Slack answered nothing): a restart can help, unlike 'rejected'."""
    short = verdict(slack("unreachable", since=NOW - 60))
    assert short.status == "unhealthy" and short.exit_code is None and "unreachable" in short.reason
    assert verdict(slack("unreachable", since=NOW - LIMIT)).exit_code == health.EXIT_SLACK


def test_slack_restart_waits_for_a_running_load():
    v = health.evaluate(NOW, slack("disconnected", since=NOW - LIMIT), NOW, load_running=True)
    assert v.exit_code is None and v.status == "unhealthy" and "after the running data load" in v.reason


def test_stuck_scheduler_is_not_deferred_by_a_load():
    v = health.evaluate(NOW, slack("connected"), NOW - LIMIT, load_running=True)
    assert v.exit_code == health.EXIT_SCHEDULER


# ---------- the heartbeat lives in a private folder, not at a fixed name in the shared temp folder ----------

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file ownership and modes")


@posix_only
def test_the_default_heartbeat_is_in_a_private_folder(monkeypatch, tmp_path):
    monkeypatch.delenv("PICO_FINOPS_AGENT_HEALTH_FILE", raising=False)
    monkeypatch.setattr(health.tempfile, "gettempdir", lambda: str(tmp_path))
    path = health.heartbeat_path()
    folder = os.path.dirname(path)
    assert folder == str(tmp_path / f"pico-finops-agent-{os.getuid()}")
    assert stat.S_IMODE(os.lstat(folder).st_mode) == 0o700


@posix_only
@pytest.mark.parametrize("mode", [0o750, 0o705, 0o777])
def test_a_folder_others_can_enter_is_refused(monkeypatch, tmp_path, mode):
    """Any access for the group or others is refused. The folder's mode is faked, so no test file is ever opened up."""
    monkeypatch.setattr(health.tempfile, "gettempdir", lambda: str(tmp_path))
    (tmp_path / f"pico-finops-agent-{os.getuid()}").mkdir(mode=0o700)
    real_lstat = os.lstat

    def lstat(path):
        st = real_lstat(path)
        return os.stat_result((stat.S_IFDIR | mode,) + tuple(st)[1:])

    monkeypatch.setattr(health.os, "lstat", lstat)
    with pytest.raises(PermissionError):
        health.private_dir()


@posix_only
def test_a_symlink_in_place_of_the_folder_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(health.tempfile, "gettempdir", lambda: str(tmp_path))
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    (tmp_path / f"pico-finops-agent-{os.getuid()}").symlink_to(target)
    with pytest.raises(PermissionError):
        health.private_dir()


@posix_only
def test_the_check_reports_a_bad_folder_as_unhealthy(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("PICO_FINOPS_AGENT_HEALTH_FILE", raising=False)
    monkeypatch.setattr(health.tempfile, "gettempdir", lambda: str(tmp_path))
    (tmp_path / f"pico-finops-agent-{os.getuid()}").symlink_to(tmp_path)
    assert health.check() == 1 and "unhealthy" in capsys.readouterr().out
