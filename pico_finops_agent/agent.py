"""The whole Pico FinOps Agent in one process: the data-collection scheduler and Pico's own Slack agent.

    python -m pico_finops_agent.agent [--config /home/pico/.pico/conf/agent.yml]

- Main thread: the supervisor. It owns SIGTERM/SIGINT, writes the heartbeat every few seconds
  (pico_finops_agent.health) and exits with a restart code when something has been broken too long, so Docker's
  restart policy brings the agent back.
- APScheduler's threads: the scheduler (pico_finops_agent.scheduler): the twice-a-day load, its retry, the 1-minute
  watch, notices and alerts. A load runs as its own `pico focus-data load` process.
- One more thread: Pico's own Slack gateway (DatusGateway + ChannelBridge, built the way `pico-gateway` builds it),
  on its own event loop, so a slow or frozen Slack loop can't stop data collection or the health check.
  Without both Slack tokens there is no Slack thread: data collection only.

Nothing under pico/ is changed. The few Pico internals used here are in one block below and pinned by
tests/fork/pico_finops_agent/test_agent.py::test_pico_gateway_internals_the_agent_relies_on.
"""
import argparse
import asyncio
import logging
import os
import signal
import sys
import threading
import time

import httpx

from pico_finops_agent import bash_guard, health, llm, scheduler
from pico_finops_agent.log import describe
from pico_finops_agent.log import log_event as _log

DEFAULT_CONFIG = "/home/pico/.pico/conf/agent.yml"
GATEWAY_FLAGS = ("filesystem_strict", "compile_visual_html", "config_mutable")  # what _run_gateway sets
HEARTBEAT_S = 5
UNHEALTHY_LOG_EVERY_S = 3600  # a lasting problem (e.g. rejected tokens) is logged again once an hour
SLACK_SAMPLE_S = 5
SLACK_GRACE_S = 60  # SlackAdapter.start (auth + connect) gets this long before "rejected"
STOP_TIMEOUT_S = 15
PROBE_EVERY_S = 300  # how often to ask Slack again why it hasn't connected
# Slack's answers that mean the token itself is bad (a restart can't help)
TOKEN_ERRORS = {"invalid_auth", "not_authed", "account_inactive", "token_revoked", "token_expired",
                "not_allowed_token_type", "invalid_token"}


def slack_tokens_set():
    return bool(os.environ.get("SLACK_APP_TOKEN")) and bool(os.environ.get("SLACK_BOT_TOKEN"))


# ---------- Pico internals (keep them here; the contract test pins each one) ----------

def _adapters_of(gateway):
    return getattr(gateway, "_adapters", None) or {}


def _socket_client_of(adapter):
    return getattr(adapter, "_socket_client", None)


def _request_gateway_shutdown(gateway):
    """Ends DatusGateway.start() the way its own signal handler would. Returns False if start() isn't waiting yet."""
    event = getattr(gateway, "_shutdown_event", None)
    if event is None:
        return False
    event.set()
    return True


def _load_gateway_config(path):
    """What `pico-gateway` does before it builds the gateway (pico/gateway/main.py::_run_gateway)."""
    from pico.configuration.agent_config_loader import load_agent_config

    agent_config = load_agent_config(config=path or "")
    # The LLM chosen in .env (pico_finops_agent/llm.py), for this process only: nothing is written to disk.
    _, provider, model = llm.selected()
    agent_config.set_active_provider_model(provider.pico, model, persist=False)
    agent_config.filesystem_strict = True  # no one can confirm out-of-workspace file access from Slack
    agent_config.compile_visual_html = False  # Slack can't show a local HTML artifact
    agent_config.config_mutable = False  # Slack users never edit the server's config
    return agent_config


def _gateway_factory(agent_config):
    def build():
        from pico.gateway.runtime import DatusGateway

        return DatusGateway(agent_config=agent_config, channels_config=getattr(agent_config, "channels_config", {}))

    return build


def slack_api(url, token, timeout=10):
    """POST one Slack Web API call with a token in the header; the JSON answer.

    Raises httpx.HTTPError (no connection, a timeout or an error status) or ValueError (an answer that isn't JSON)."""
    r = httpx.post(url, headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def slack_token_probe(timeout=10):
    """Why hasn't Slack connected? Asks Slack itself: "rejected" when it refuses a token, else "unreachable"
    (network, DNS or Slack trouble: a later retry can work)."""
    calls = (("https://slack.com/api/auth.test", os.environ.get("SLACK_BOT_TOKEN", "")),
             ("https://slack.com/api/apps.connections.open", os.environ.get("SLACK_APP_TOKEN", "")))
    for url, token in calls:
        try:
            body = slack_api(url, token, timeout)
        except (httpx.HTTPError, ValueError):
            return "unreachable"
        if not body.get("ok") and body.get("error") in TOKEN_ERRORS:
            return "rejected"
    return "unreachable"


def _no_signal_handler(*_args, **_kwargs):
    """DatusGateway.start() registers SIGINT/SIGTERM on its loop; off the main thread that raises. The supervisor
    owns the signals and stops the gateway itself."""


# ---------- the Slack thread ----------

class SlackRunner:
    """Runs Pico's Slack gateway on its own thread and event loop, and reports how the connection is doing.

    States: disabled (no tokens / no channel), connecting, connected, disconnected (auto-reconnect is trying),
    rejected (Slack refused a token), unreachable (never connected, but not refused: network/Slack trouble),
    crashed (the gateway stopped or failed).
    """

    def __init__(self, gateway_factory, *, clock=time.time, grace_s=SLACK_GRACE_S, sample_s=SLACK_SAMPLE_S,
                 probe=slack_token_probe, probe_every_s=PROBE_EVERY_S):
        self._factory = gateway_factory
        self._clock = clock
        self._grace_s = grace_s
        self._sample_s = sample_s
        self._probe = probe
        self._probe_every_s = probe_every_s
        self._probed = None  # (when, answer)
        self._lock = threading.Lock()
        now = clock()
        self._snap = {"state": "disabled" if gateway_factory is None else "connecting",
                      "since": now, "sampled_at": now, "reason": ""}
        self._stopping = False
        self._loop = None
        self._gateway = None
        self._task = None
        self.thread = None

    def snapshot(self):
        with self._lock:
            return dict(self._snap)

    def _set(self, state, reason=""):
        now = self._clock()
        with self._lock:
            if self._snap["state"] != state:
                self._snap["since"] = now
            self._snap.update(state=state, sampled_at=now, reason=reason)

    def start(self):
        if self._factory is None:
            return
        self.thread = threading.Thread(target=self._thread_main, name="pico-finops-agent-slack", daemon=True)
        self.thread.start()

    def _thread_main(self):
        # asyncio.Runner, like the asyncio.run() pico-gateway uses: on the way out it cancels every task still
        # pending (slack_sdk leaves its process_messages task running after disconnect()), then closes async
        # generators and the default executor.
        try:
            with asyncio.Runner() as run:
                run.get_loop().add_signal_handler = _no_signal_handler  # see _no_signal_handler
                run.run(self._run())
        except BaseException as e:  # noqa: BLE001 - any failure here is reported, never raised into the process
            if not self._stopping:
                self._set("crashed", describe(e))

    async def _run(self):
        with self._lock:  # with stop(): either stop() sees this loop, or this sees the stop
            if self._stopping:
                return
            self._loop = asyncio.get_running_loop()
        self._gateway = self._factory()
        started = self._clock()
        self._task = asyncio.create_task(self._gateway.start())
        while True:
            done, _ = await asyncio.wait({self._task}, timeout=self._sample_s)
            if done:
                break
            await self._sample(started)
        if self._task.cancelled():
            return
        error = self._task.exception()
        if error is not None:
            raise error
        if self._stopping:
            return
        if not _adapters_of(self._gateway):
            self._set("disabled", "no Slack channel configured")
        else:
            self._set("crashed", "the Slack gateway stopped by itself")

    async def _sample(self, started):
        adapters = list(_adapters_of(self._gateway).values())
        if not adapters:
            self._set("connecting")
            return
        clients = [_socket_client_of(a) for a in adapters]
        if any(c is None for c in clients):
            if self._clock() - started < self._grace_s:
                self._set("connecting")
            else:
                self._set(await self._why_not_connected())
            return
        try:
            ok = all([await asyncio.wait_for(c.is_connected(), timeout=self._sample_s) for c in clients])
        except Exception:  # noqa: BLE001 - a failed probe counts as disconnected
            ok = False
        self._set("connected" if ok else "disconnected")

    async def _why_not_connected(self):
        """Ask Slack why (rejected / unreachable), at most every probe_every_s; off the loop (it blocks)."""
        now = self._clock()
        if self._probed is None or now - self._probed[0] >= self._probe_every_s:
            try:
                answer = await asyncio.to_thread(self._probe)
            except Exception:  # noqa: BLE001 - a failed probe can't tell: keep retrying
                answer = "unreachable"
            self._probed = (now, answer)
        return self._probed[1]

    def _stop_in_loop(self):
        if not _request_gateway_shutdown(self._gateway) and self._task is not None:
            self._task.cancel()

    def stop(self, timeout=STOP_TIMEOUT_S):
        with self._lock:
            self._stopping = True
            loop = self._loop
        if self.thread is None:
            return
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._stop_in_loop)
            except RuntimeError:
                pass  # the loop closed in between
        self.thread.join(timeout)
        if self.thread.is_alive():
            _log("slack_stop_timeout", seconds=timeout)


# ---------- the supervisor (main thread) ----------

def supervise(slack, last_tick, stop, *, heartbeat_path, clock=time.time, interval_s=HEARTBEAT_S,
              log_every_s=UNHEALTHY_LOG_EVERY_S, load_running=lambda: False, tasks_ok_at=lambda: None):
    """Heartbeat every interval_s until stop is set (returns 0) or a fatal verdict (returns its exit code)."""
    code = 0
    reported, reported_at = None, 0.0
    while True:
        now = clock()
        snap = slack.snapshot()
        tick = last_tick()
        verdict = health.evaluate(now, snap, tick, load_running=load_running(), tasks_ok_at=tasks_ok_at())
        _beat(heartbeat_path, now, verdict.status, verdict.reason, snap, tick)
        if verdict.exit_code is not None:
            _log("agent_restarting", reason=verdict.reason, exit_code=verdict.exit_code)
            code = verdict.exit_code
            break
        if verdict.status != "healthy":
            if verdict.reason != reported or now - reported_at >= log_every_s:
                _log("agent_unhealthy", reason=verdict.reason)
                reported, reported_at = verdict.reason, now
        elif reported is not None:
            _log("agent_healthy")
            reported = None
        if stop.wait(interval_s):
            break
    _beat(heartbeat_path, clock(), "stopping", "", slack.snapshot(), last_tick())
    return code


def _beat(path, now, status, reason, snap, tick):
    try:
        health.write_heartbeat(path, {"ts": now, "status": status, "reason": reason, "slack": snap.get("state"),
                                      "scheduler_tick": tick})
    except OSError as e:  # a full or read-only disk must not stop the agent; --check then reports it
        _log("heartbeat_failed", error=describe(e))


# ---------- wiring ----------

def _configure_logging():
    from pico.utils.loggings import configure_logging

    configure_logging(False)
    logging.getLogger().setLevel(logging.INFO)


def _install_cache_reconnect():
    from pico_finops_agent import cache_reconnect

    cache_reconnect.install()  # Slack answers see the new local copy after each load, without a restart


def _install_slack_context():
    """The signed who/where line scheduled tasks check (`--ctx`), from the schedule plugin. Without the plugin Slack
    answers work as before; only tasks are off."""
    try:
        from pico_plugin_schedule.hooks import pico_gateway
    except ImportError as e:
        _log("tasks_off", reason=describe(e, 200))
        return
    pico_gateway.install_context(scheduler.data_dir())


def _install_slack_guard():
    """Slack answer hardening (pico_finops_agent/slack_guard.py): last-N-turns model input, empty-answer retry, clear
    sentences instead of raw errors or a stuck answer, a per-person hourly limit, a reset phrase, one log line per
    question. Installed after the ctx line so it is the outermost wrapper. If it can't install (a Pico change the
    pin tests missed), Slack still answers, unguarded, and the log says so."""
    from pico_finops_agent import slack_guard

    try:
        slack_guard.install(slack_guard.settings_from_env())
    except Exception as e:  # noqa: BLE001
        _log("slack_guard_off", reason=describe(e, 200))


def _install_llm_log():
    """One `llm_call` line per model call of a Slack answer: why the model stopped, tokens, tools called
    (pico_finops_agent/llm_log.py). AGENT_SAVE_FAILED_CALLS=1 also keeps the request of an empty or failed call under
    <data dir>/llm-failures/. If it can't install, answers work as before and the log says so."""
    from pico_finops_agent import llm_log

    try:
        llm_log.install(save_dir=llm_log.save_dir_from_env(data_dir=scheduler.data_dir()))
    except Exception as e:  # noqa: BLE001
        _log("llm_log_off", reason=describe(e, 200))


def _make_signal_handler(stop, exit=os._exit):
    def handle(signum, _frame):
        if stop.is_set():
            exit(1)  # a second signal: stop now
            return
        stop.set()  # first, so the supervisor stops even if the log line below fails
        _log("agent_stopping", signal=signal.Signals(signum).name)

    return handle


def _install_signal_handlers(stop):
    handle = _make_signal_handler(stop)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handle)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m pico_finops_agent.agent")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Pico agent.yml (default: %(default)s)")
    args = parser.parse_args(argv)

    _configure_logging()
    bash_guard.install()  # no $ / backtick expansion in allow-listed commands (secrets)
    _install_cache_reconnect()
    factory = _gateway_factory(_load_gateway_config(args.config)) if slack_tokens_set() else None
    if factory is None:
        _log("slack_off", reason="SLACK_APP_TOKEN and SLACK_BOT_TOKEN not both set: data collection only")
    else:
        _install_slack_context()
        _install_slack_guard()
        _install_llm_log()

    stop = threading.Event()
    _install_signal_handlers(stop)
    sched = scheduler.start_scheduler(scheduler.build_scheduler())
    slack = SlackRunner(factory)
    try:
        slack.start()
        return supervise(slack, scheduler.last_tick, stop, heartbeat_path=health.heartbeat_path(),
                         load_running=scheduler.load_running, tasks_ok_at=scheduler.tasks_ok_at)
    finally:
        slack.stop()
        scheduler.stop_scheduler(sched)


def exit_process(code):
    """End the process after the clean stop. Library worker threads that aren't daemons (e.g. the default
    executor Pico's agent uses through asyncio.to_thread) would otherwise keep it alive until Docker kills it."""
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    exit_process(main())
