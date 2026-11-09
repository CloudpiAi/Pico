"""Health of the one Pico FinOps Agent process: the decision table, the heartbeat file, and Docker's check.

The agent's supervisor (pico_finops_agent.agent) calls ``evaluate`` every few seconds and writes the result to the
heartbeat file; the image's HEALTHCHECK runs ``python -m pico_finops_agent.health --check``, which reads that file.
A fatal verdict makes the agent exit with EXIT_SLACK / EXIT_SCHEDULER so Docker's restart policy restarts it.

The limit is down_limit(): 10 minutes, or PICO_FINOPS_AGENT_DOWN_LIMIT_S.

| Situation                                                            | Status    | Exit for a restart |
|----------------------------------------------------------------------|-----------|--------------------|
| no Slack tokens; Slack connecting or connected                        | healthy   | no |
| Slack refused the tokens (auth.test / connections.open said so)       | unhealthy | no: a restart can't fix tokens |
| Slack unreachable since start, or disconnected (auto-reconnect tries) | unhealthy | after the limit (70); waits while a data load runs |
| the Slack thread died, or stopped reporting for the limit (frozen)    | unhealthy | yes (70); waits while a data load runs |
| the scheduler's 1-minute watch hasn't run for the limit               | unhealthy | yes (71) |
| the heartbeat is older than HEARTBEAT_MAX_AGE_S (the supervisor froze) | `--check` fails | - |
"""
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Optional

DOWN_LIMIT_S = 600  # the default limit: 10 minutes
HEARTBEAT_MAX_AGE_S = 30
EXIT_SLACK = 70
EXIT_SCHEDULER = 71
DEFAULT_PATH = "/tmp/pico-finops-agent-health.json"
_FROZEN_STATES = ("connecting", "connected", "disconnected", "unreachable", "rejected")  # the thread samples these


@dataclass(frozen=True)
class Verdict:
    status: str  # healthy | unhealthy
    reason: str
    exit_code: Optional[int] = None  # set: exit so Docker restarts the agent


def _minutes(seconds):
    return f"{int(seconds // 60)} min"


def down_limit():
    try:
        value = int(os.environ.get("PICO_FINOPS_AGENT_DOWN_LIMIT_S", "").strip())
    except ValueError:
        return DOWN_LIMIT_S
    return value if value > 0 else DOWN_LIMIT_S


def evaluate(now, slack, last_tick, load_running=False):
    """slack: SlackRunner.snapshot(); last_tick: scheduler.last_tick(); load_running: a data load is in progress
    (a Slack problem then waits for it, so a restart never cuts a load short)."""
    limit = down_limit()
    if last_tick is None or now - last_tick >= limit:
        idle = "never" if last_tick is None else f"for {_minutes(now - last_tick)}"
        return Verdict("unhealthy", f"the scheduler's 1-minute check hasn't run {idle}", EXIT_SCHEDULER)
    verdict = _slack_verdict(now, slack, limit)
    if verdict.exit_code == EXIT_SLACK and load_running:
        return Verdict("unhealthy", f"{verdict.reason}; restart after the running data load")
    return verdict


def _slack_verdict(now, slack, limit):
    state = slack.get("state")
    if state == "crashed":
        return Verdict("unhealthy", f"the Slack connection stopped: {slack.get('reason') or 'unknown'}", EXIT_SLACK)
    if state in _FROZEN_STATES and now - (slack.get("sampled_at") or 0) >= limit:
        return Verdict("unhealthy", f"the Slack connection is not responding (no report for "
                                    f"{_minutes(now - (slack.get('sampled_at') or 0))})", EXIT_SLACK)
    if state in ("disconnected", "unreachable"):
        down = now - (slack.get("since") or now)
        if down >= limit:
            what = "disconnected" if state == "disconnected" else "unreachable (since the agent started)"
            return Verdict("unhealthy", f"Slack has been {what} for {_minutes(down)}", EXIT_SLACK)
        # no running counter here: the reason must stay the same while the outage lasts (it is logged on change)
        what = "disconnected" if state == "disconnected" else "unreachable since the agent started"
        return Verdict("unhealthy", f"Slack {what}; retrying")
    if state == "rejected":
        return Verdict("unhealthy", "Slack tokens rejected: fix SLACK_APP_TOKEN / SLACK_BOT_TOKEN in .env and "
                                    "restart; data collection goes on")
    return Verdict("healthy", "")


def heartbeat_path():
    return os.environ.get("PICO_FINOPS_AGENT_HEALTH_FILE") or DEFAULT_PATH


def write_heartbeat(path, payload):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


def check(path=None, now=None):
    """0 when the heartbeat is fresh and healthy; else 1, printing why (Docker shows it in `docker inspect`)."""
    path = path or heartbeat_path()
    now = time.time() if now is None else now
    try:
        with open(path, encoding="utf-8") as fh:
            beat = json.load(fh)
    except (OSError, ValueError) as e:
        print(f"unhealthy: no heartbeat ({type(e).__name__})")
        return 1
    age = now - float(beat.get("ts") or 0)
    if age > HEARTBEAT_MAX_AGE_S:
        print(f"unhealthy: the heartbeat is {int(age)} s old (the agent isn't responding)")
        return 1
    if beat.get("status") != "healthy":
        print(f"{beat.get('status')}: {beat.get('reason') or ''}".strip())
        return 1
    print("healthy")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] != ["--check"]:
        print("usage: python -m pico_finops_agent.health --check", file=sys.stderr)
        sys.exit(2)
    sys.exit(check())
