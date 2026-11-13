"""pico_finops_agent.agent: the whole Pico FinOps Agent in one process.

The main thread owns the signals, the heartbeat and the self-heal exits; Pico's own Slack gateway runs on its own
thread with its own event loop (so a frozen Slack loop can't stop the health check); the scheduler runs in
APScheduler's threads. Fakes stand in for Pico's gateway; the contract tests at the end pin the Pico internals the
agent relies on, so an upstream sync that renames them fails here.
"""
import asyncio
import inspect
import json
import signal
import sys  # noqa: E402
import threading
import time
from pathlib import Path  # noqa: E402

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "plugins" / "schedule"))
from pico_finops_agent import agent, health  # noqa: E402

# ---------- a fake of Pico's DatusGateway / SlackAdapter ----------

class FakeSocketClient:
    def __init__(self, connected=True):
        self.connected = connected

    async def is_connected(self):
        return self.connected


class FakeAdapter:
    def __init__(self, socket_client):
        self._socket_client = socket_client


class FakeGateway:
    """Like DatusGateway.start(): register signals on the running loop, build adapters, wait for shutdown."""

    def __init__(self, adapters=None, fail=None, return_at_once=False):
        self._adapters = {}
        self._shutdown_event = None
        self._planned = adapters if adapters is not None else {"slack": FakeAdapter(FakeSocketClient())}
        self._fail = fail
        self._return_at_once = return_at_once
        self.signal_calls = 0
        self.shut_down = False

    async def start(self):
        self._shutdown_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._shutdown_event.set)  # raises off the main thread unless disabled
            self.signal_calls += 1
        if self._fail:
            raise self._fail
        self._adapters = dict(self._planned)
        if not self._adapters or self._return_at_once:
            return
        await self._shutdown_event.wait()
        self.shut_down = True


def runner(gateway, **kw):
    kw.setdefault("sample_s", 0.05)
    kw.setdefault("grace_s", 0.3)
    return agent.SlackRunner(lambda: gateway, **kw)


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ---------- the Slack thread ----------

def test_no_slack_tokens_means_disabled_and_no_thread():
    r = agent.SlackRunner(None)
    r.start()
    assert r.snapshot()["state"] == "disabled" and r.thread is None
    r.stop()


def test_gateway_runs_off_the_main_thread_and_reports_connected():
    gw = FakeGateway()
    r = runner(gw)
    r.start()
    try:
        assert wait_for(lambda: r.snapshot()["state"] == "connected")
        assert gw.signal_calls == 2  # Pico's signal setup ran, harmlessly, off the main thread
        assert r.thread is not threading.main_thread()
    finally:
        t0 = time.time()
        r.stop()
    assert gw.shut_down and not r.thread.is_alive() and time.time() - t0 < 3


def test_disconnect_keeps_its_start_time():
    client = FakeSocketClient(connected=True)
    gw = FakeGateway(adapters={"slack": FakeAdapter(client)})
    r = runner(gw)
    r.start()
    try:
        assert wait_for(lambda: r.snapshot()["state"] == "connected")
        client.connected = False
        assert wait_for(lambda: r.snapshot()["state"] == "disconnected")
        since = r.snapshot()["since"]
        time.sleep(0.2)
        assert r.snapshot()["since"] == since  # still the first moment it was seen down
        client.connected = True
        assert wait_for(lambda: r.snapshot()["state"] == "connected")
    finally:
        r.stop()


def test_tokens_rejected_after_the_grace_period():
    """SlackAdapter.start leaves _socket_client None when Slack refuses the tokens."""
    gw = FakeGateway(adapters={"slack": FakeAdapter(None)})
    r = runner(gw, grace_s=0.2, probe=lambda: "rejected")
    r.start()
    try:
        assert wait_for(lambda: r.snapshot()["state"] == "rejected")
    finally:
        r.stop()


def test_gateway_crash_is_reported():
    r = runner(FakeGateway(fail=RuntimeError("boom")))
    r.start()
    assert wait_for(lambda: r.snapshot()["state"] == "crashed")
    assert "RuntimeError" in r.snapshot()["reason"]
    r.stop()


def test_gateway_with_no_channels_is_disabled():
    r = runner(FakeGateway(adapters={}))
    r.start()
    assert wait_for(lambda: r.snapshot()["state"] == "disabled")
    r.stop()


def test_gateway_that_stops_by_itself_is_crashed():
    r = runner(FakeGateway(return_at_once=True))
    r.start()
    assert wait_for(lambda: r.snapshot()["state"] == "crashed")
    r.stop()


class LeakyGateway(FakeGateway):
    """Like slack_sdk's SocketModeClient: a task started at connect that the adapter's stop() never cancels."""

    def __init__(self):
        super().__init__()
        self.leftover_cancelled = threading.Event()

    async def start(self):
        async def process_messages():
            try:
                await asyncio.Event().wait()  # forever
            finally:
                self.leftover_cancelled.set()

        self.leftover = asyncio.ensure_future(process_messages())
        await super().start()


def test_stop_cancels_tasks_the_gateway_leaves_behind():
    """Found live 2026-11-06: the leftover slack_sdk task kept the process from exiting (Docker had to kill it)."""
    gw = LeakyGateway()
    r = runner(gw)
    r.start()
    assert wait_for(lambda: r.snapshot()["state"] == "connected")
    r.stop()
    assert gw.leftover_cancelled.is_set() and not r.thread.is_alive()


def test_exit_does_not_wait_for_library_threads():
    """After the clean stop the process ends even if a library left a non-daemon thread running."""
    import subprocess
    import sys

    code = ("import threading; from pico_finops_agent import agent; "
            "threading.Thread(target=threading.Event().wait, daemon=False).start(); agent.exit_process(3)")
    r = subprocess.run([sys.executable, "-c", code], timeout=20, capture_output=True, text=True)
    assert r.returncode == 3, r.stderr


def test_snapshots_are_fresh_while_running():
    r = runner(FakeGateway())
    r.start()
    try:
        assert wait_for(lambda: r.snapshot()["state"] == "connected")
        first = r.snapshot()["sampled_at"]
        assert wait_for(lambda: r.snapshot()["sampled_at"] > first)
    finally:
        r.stop()


# ---------- the supervisor (main thread) ----------

class StaticSlack:
    def __init__(self, snap):
        self.snap = snap

    def snapshot(self):
        return dict(self.snap)


def test_supervisor_writes_a_healthy_heartbeat_and_stops_cleanly(tmp_path):
    path = tmp_path / "health.json"
    stop = threading.Event()
    slack = StaticSlack({"state": "disabled", "since": time.time(), "sampled_at": time.time()})
    t = threading.Thread(target=lambda: setattr(t, "code", agent.supervise(
        slack, time.time, stop, heartbeat_path=str(path), interval_s=0.05)))
    t.start()
    assert wait_for(lambda: path.exists() and json.loads(path.read_text())["status"] == "healthy")
    stop.set()
    t.join(5)
    assert t.code == 0 and json.loads(path.read_text())["status"] == "stopping"


def test_supervisor_exits_for_a_restart(tmp_path, capsys):
    path = tmp_path / "health.json"
    long_ago = time.time() - 2 * health.DOWN_LIMIT_S
    slack = StaticSlack({"state": "disconnected", "since": long_ago, "sampled_at": time.time()})
    code = agent.supervise(slack, time.time, threading.Event(), heartbeat_path=str(path), interval_s=0.05)
    assert code == health.EXIT_SLACK
    assert '"agent_restarting"' in capsys.readouterr().out


def test_supervisor_reports_rejected_tokens_once_and_keeps_running(tmp_path, capsys):
    path = tmp_path / "health.json"
    stop = threading.Event()
    slack = StaticSlack({"state": "rejected", "since": time.time(), "sampled_at": time.time()})
    threading.Timer(0.5, stop.set).start()
    code = agent.supervise(slack, time.time, stop, heartbeat_path=str(path), interval_s=0.05, log_every_s=300)
    out = capsys.readouterr().out
    assert code == 0 and out.count('"agent_unhealthy"') == 1 and "Slack tokens rejected" in out


def test_supervisor_survives_an_unwritable_heartbeat(tmp_path):
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    slack = StaticSlack({"state": "disabled", "since": time.time(), "sampled_at": time.time()})
    code = agent.supervise(slack, time.time, stop, heartbeat_path=str(tmp_path / "no" / "dir" / "h.json"),
                           interval_s=0.05)
    assert code == 0


# ---------- main(): the wiring ----------

def test_main_without_slack_tokens(monkeypatch):
    calls = []
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setattr(agent, "_install_cache_reconnect", lambda: calls.append("cache_reconnect"))
    monkeypatch.setattr(agent, "_install_slack_context", lambda: calls.append("slack_context"))
    monkeypatch.setattr(agent, "_install_slack_guard", lambda: calls.append("slack_guard"))
    monkeypatch.setattr(agent, "_install_llm_log", lambda: calls.append("llm_log"))
    monkeypatch.setattr(agent, "_configure_logging", lambda: calls.append("logging"))
    monkeypatch.setattr(agent.scheduler, "start_scheduler", lambda s=None: calls.append("scheduler_start") or "S")
    monkeypatch.setattr(agent.scheduler, "build_scheduler", lambda: "S")
    monkeypatch.setattr(agent.scheduler, "stop_scheduler", lambda s: calls.append("scheduler_stop"))
    monkeypatch.setattr(agent, "_install_signal_handlers", lambda stop: calls.append("signals"))
    seen = {}

    def fake_supervise(slack, last_tick, stop, **kw):
        seen["slack"] = slack.snapshot()["state"]
        calls.append("supervise")
        return 0

    monkeypatch.setattr(agent, "supervise", fake_supervise)
    assert agent.main(["--config", "x.yml"]) == 0
    assert seen["slack"] == "disabled"
    assert calls.index("cache_reconnect") < calls.index("scheduler_start") < calls.index("supervise")
    assert calls[-1] == "scheduler_stop"
    assert "slack_context" not in calls  # no Slack, no who/where line
    assert "slack_guard" not in calls  # no Slack, Pico's bridge is left as it is
    assert "llm_log" not in calls  # no Slack answers, no in-process model calls to log


def test_main_with_slack_tokens_builds_the_gateway_from_the_config(monkeypatch):
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-1-x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-x")
    built = {}
    monkeypatch.setattr(agent, "_load_gateway_config", lambda path: built.setdefault("config", path) or "CFG")
    monkeypatch.setattr(agent, "_install_cache_reconnect", lambda: None)
    monkeypatch.setattr(agent, "_install_slack_context", lambda: built.setdefault("order", []).append("context"))
    monkeypatch.setattr(agent, "_install_slack_guard", lambda: built.setdefault("order", []).append("guard"))
    monkeypatch.setattr(agent, "_install_llm_log", lambda: built.setdefault("order", []).append("llm_log"))
    monkeypatch.setattr(agent, "_configure_logging", lambda: None)
    monkeypatch.setattr(agent, "_install_signal_handlers", lambda stop: None)
    monkeypatch.setattr(agent.scheduler, "build_scheduler", lambda: "S")
    monkeypatch.setattr(agent.scheduler, "start_scheduler", lambda s=None: "S")
    monkeypatch.setattr(agent.scheduler, "stop_scheduler", lambda s: None)

    class RecordingRunner:
        def __init__(self, factory, **kw):
            built["factory"] = factory

        def start(self):
            pass

        def stop(self, timeout=15):
            built["stopped"] = True

        def snapshot(self):
            return {"state": "connecting", "since": 0, "sampled_at": 0}

    monkeypatch.setattr(agent, "SlackRunner", RecordingRunner)
    monkeypatch.setattr(agent, "supervise", lambda *a, **k: 0)
    assert agent.main(["--config", "conf/agent.yml"]) == 0
    assert built["config"] == "conf/agent.yml" and callable(built["factory"]) and built["stopped"]
    # every Slack message gets the signed who/where line for scheduled tasks; the answer guard goes on after it, so
    # it is the outermost wrapper and sees the text the person typed (reset phrase)
    # and every model call of a Slack answer logs why the model stopped (llm_call)
    assert built["order"] == ["context", "guard", "llm_log"]


def test_llm_log_install_failure_is_logged_not_raised(monkeypatch, capsys):
    from pico_finops_agent import llm_log

    monkeypatch.setattr(llm_log, "install", lambda **kw: 1 / 0)
    agent._install_llm_log()
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["event"] == "llm_log_off" and "ZeroDivisionError" in line["reason"]


def test_llm_log_saves_failed_calls_only_when_asked(monkeypatch):
    from pico_finops_agent import llm_log

    seen = {}
    monkeypatch.setattr(llm_log, "install", lambda **kw: seen.update(kw))
    monkeypatch.setenv("FOCUS_DATA_DIR", "/data")
    monkeypatch.delenv("AGENT_SAVE_FAILED_CALLS", raising=False)
    agent._install_llm_log()
    assert seen["save_dir"] is None
    monkeypatch.setenv("AGENT_SAVE_FAILED_CALLS", "1")
    agent._install_llm_log()
    assert seen["save_dir"] == "/data/llm-failures"


def test_slack_needs_both_tokens(monkeypatch):
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-1-x")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    assert agent.slack_tokens_set() is False
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-x")
    assert agent.slack_tokens_set() is True


# ---------- contract with Pico's gateway (fails loudly after an upstream change) ----------

def test_pico_gateway_internals_the_agent_relies_on():
    pytest.importorskip("slack_sdk")
    from slack_sdk.socket_mode.aiohttp import SocketModeClient

    from pico.gateway import main as gw_main
    from pico.gateway.adapters.slack import SlackAdapter
    from pico.gateway.runtime import DatusGateway

    init_src = inspect.getsource(DatusGateway.__init__)
    assert "self._adapters" in init_src and "self._shutdown_event" in init_src
    start_src = inspect.getsource(DatusGateway.start)
    assert "add_signal_handler" in start_src and "_shutdown_event.wait()" in start_src
    assert "self._socket_client" in inspect.getsource(SlackAdapter)
    assert inspect.iscoroutinefunction(SocketModeClient.is_connected)
    # our gateway builder must keep setting what pico-gateway sets before it starts
    run_src = inspect.getsource(gw_main._run_gateway)
    for flag in agent.GATEWAY_FLAGS:
        assert f"agent_config.{flag}" in run_src, flag



# ---------- review fixes (2026-11-06) ----------

def test_slack_unreachable_is_not_called_rejected():
    """No socket after the grace period, but Slack didn't refuse the tokens (e.g. the network wasn't up at boot)."""
    gw = FakeGateway(adapters={"slack": FakeAdapter(None)})
    r = runner(gw, grace_s=0.2, probe=lambda: "unreachable")
    r.start()
    try:
        assert wait_for(lambda: r.snapshot()["state"] == "unreachable")
    finally:
        r.stop()


@pytest.mark.parametrize("answers,expected", [
    ([{"ok": False, "error": "invalid_auth"}, {"ok": True}], "rejected"),
    ([{"ok": True}, {"ok": False, "error": "invalid_auth"}], "rejected"),
    ([{"ok": False, "error": "token_revoked"}, {"ok": True}], "rejected"),
    ([{"ok": True}, {"ok": True}], "unreachable"),
    ([{"ok": False, "error": "ratelimited"}, {"ok": True}], "unreachable"),
    (httpx.ConnectError("network is unreachable"), "unreachable"),
])
def test_token_probe(monkeypatch, answers, expected):
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-1-x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-x")
    calls = []

    def fake_post(url, headers=None, timeout=None):
        calls.append(url)
        if isinstance(answers, Exception):
            raise answers
        return httpx.Response(200, json=answers[len(calls) - 1], request=httpx.Request("POST", url))

    monkeypatch.setattr(agent.httpx, "post", fake_post)
    assert agent.slack_token_probe() == expected


def test_a_stop_before_the_gateway_exists_is_not_lost():
    built = []
    r = agent.SlackRunner(lambda: built.append(1) or FakeGateway(), sample_s=0.05)
    r.stop()   # e.g. SIGTERM right at start
    r.start()
    r.thread.join(3)
    assert not r.thread.is_alive() and built == []


def test_supervisor_reads_the_tick_once_per_beat(tmp_path):
    ticks = iter([time.time(), 0.0])  # a second read would see a stale tick
    stop = threading.Event()
    stop.set()
    slack = StaticSlack({"state": "disabled", "since": time.time(), "sampled_at": time.time()})
    code = agent.supervise(slack, lambda: next(ticks), stop, heartbeat_path=str(tmp_path / "h.json"), interval_s=0.05)
    assert code == 0


def test_supervisor_waits_for_a_load_before_a_slack_restart(tmp_path):
    long_ago = time.time() - 2 * health.down_limit()
    slack = StaticSlack({"state": "disconnected", "since": long_ago, "sampled_at": time.time()})
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    code = agent.supervise(slack, time.time, stop, heartbeat_path=str(tmp_path / "h.json"), interval_s=0.05,
                           load_running=lambda: True)
    assert code == 0  # stopped by the test, not restarted


@pytest.mark.parametrize("state", ["disconnected", "unreachable"])
def test_a_lasting_outage_is_logged_once(tmp_path, capsys, state):
    """Found live 2026-11-06: the reason carried a seconds counter, so a new line was logged every 5 s."""
    import itertools

    start = time.time()
    clock = map(lambda i: start + 1.5 * i, itertools.count())  # 1.5 s per beat: the outage grows
    slack = StaticSlack({"state": state, "since": start - 5, "sampled_at": start})
    stop = threading.Event()
    threading.Timer(0.6, stop.set).start()
    agent.supervise(slack, lambda: start, stop, heartbeat_path=str(tmp_path / "h.json"), interval_s=0.02,
                    clock=lambda: next(clock))
    assert capsys.readouterr().out.count('"agent_unhealthy"') == 1


def test_unhealthy_is_logged_once_an_hour():
    assert agent.UNHEALTHY_LOG_EVERY_S == 3600


def test_signals(monkeypatch):
    exits = []
    stop = threading.Event()
    handle = agent._make_signal_handler(stop, exit=exits.append)
    handle(signal.SIGTERM, None)
    assert stop.is_set() and exits == []
    handle(signal.SIGTERM, None)  # a second signal: stop now
    assert exits == [1]


def test_main_returns_the_supervisors_exit_code(monkeypatch):
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    for name in ("_install_cache_reconnect", "_configure_logging"):
        monkeypatch.setattr(agent, name, lambda: None)
    monkeypatch.setattr(agent, "_install_signal_handlers", lambda stop: None)
    monkeypatch.setattr(agent.scheduler, "build_scheduler", lambda: "S")
    monkeypatch.setattr(agent.scheduler, "start_scheduler", lambda s=None: "S")
    monkeypatch.setattr(agent.scheduler, "stop_scheduler", lambda s: None)
    monkeypatch.setattr(agent, "supervise", lambda *a, **k: health.EXIT_SLACK)
    assert agent.main(["--config", "x"]) == health.EXIT_SLACK


# ---------- plugins/schedule split ----------

def test_slack_hook_comes_from_the_schedule_plugin(monkeypatch, tmp_path):
    from pico_plugin_schedule.hooks import pico_gateway

    seen = {}
    monkeypatch.setenv("FOCUS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(pico_gateway, "install_context", lambda data_dir=None: seen.setdefault("dir", data_dir))
    agent._install_slack_context()
    assert seen["dir"] == str(tmp_path)


def test_slack_guard_installs_with_env_settings(monkeypatch):
    from pico_finops_agent import slack_guard

    seen = {}
    monkeypatch.setenv("AGENT_HISTORY_TURNS", "5")
    monkeypatch.setattr(slack_guard, "install", lambda settings=None: seen.setdefault("settings", settings))
    agent._install_slack_guard()
    assert seen["settings"].history_turns == 5


def test_slack_still_starts_when_the_guard_cannot_install(monkeypatch, capsys):
    from pico_finops_agent import slack_guard

    monkeypatch.setattr(slack_guard, "install", lambda settings=None: 1 / 0)
    agent._install_slack_guard()  # no exception: Slack answers work, unguarded, and the log says so
    assert '"event": "slack_guard_off"' in capsys.readouterr().out


def test_slack_answers_still_work_without_the_schedule_plugin(monkeypatch, capsys):
    import builtins

    real = builtins.__import__

    def blocked(name, *a, **k):
        if name.startswith("pico_plugin_schedule"):
            raise ImportError(f"not installed: {name}")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked)
    agent._install_slack_context()  # no exception: the gateway starts without the who/where line
    assert '"event": "tasks_off"' in capsys.readouterr().out
