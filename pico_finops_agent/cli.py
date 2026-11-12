"""The `pico-finops-agent` command.

    pico-finops-agent serve [--config agent.yml]   run the agent until stopped (the image's default command)
    pico-finops-agent check                        pre-flight: checks AWS, the agent's data, Slack and the LLM key; posts
                                               nothing and changes nothing; exit 0 when all is fine, else 1
    pico-finops-agent health                       Docker's HEALTHCHECK: the heartbeat the running agent writes

In the image: `docker compose run --rm agent check` (the entrypoint checks .env first, then runs the command).
Without the image: `python -m pico_finops_agent.cli <command>`.
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

from pico_finops_agent import agent, health, llm

WORKSPACE = "/pico-workspace"  # where the agent's own commands run (its config is found from there)


# ---------- check: the pre-flight ----------

def focus_status(timeout=120):
    """What `pico focus-data status` reports (read-only): AWS identity, the agent's state, the last load."""
    cwd = WORKSPACE if os.path.isdir(WORKSPACE) else None
    r = subprocess.run(["pico", "focus-data", "status"], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"pico focus-data status exited {r.returncode}: {(r.stderr or '').strip()[-200:]}")
    text = r.stdout
    start = text.find("{")
    if start < 0:
        raise RuntimeError("pico focus-data status printed no report")
    return json.loads(text[start:])


def slack_check():
    """(ok, line): are the Slack tokens accepted? Off without tokens (the agent then only collects data)."""
    if not agent.slack_tokens_set():
        return True, "Slack: off (no tokens): the agent collects data only"
    try:
        me = agent.slack_api("https://slack.com/api/auth.test", os.environ["SLACK_BOT_TOKEN"])
        if not me.get("ok"):
            return False, f"Slack: the bot token was refused ({me.get('error')}): fix SLACK_BOT_TOKEN in .env"
        sock = agent.slack_api("https://slack.com/api/apps.connections.open", os.environ["SLACK_APP_TOKEN"])
        if not sock.get("ok"):
            return False, (f"Slack: the app-level token was refused ({sock.get('error')}): fix SLACK_APP_TOKEN in "
                           ".env (scope connections:write, Socket Mode on)")
    except (OSError, ValueError) as e:
        return False, f"Slack: unreachable ({type(e).__name__}): check the EC2's outbound HTTPS to slack.com"
    return True, f"Slack: tokens accepted (bot @{me.get('user')} in {me.get('team')})"


def llm_check(timeout=15):
    """(ok, line): does the chosen LLM provider accept its key? Lists models: no tokens are spent."""
    try:
        _, provider, model = llm.selected()
    except ValueError as e:
        return False, f"LLM: {e}"
    key = os.environ.get(provider.key_env, "")
    if not key:
        return False, f"LLM: {provider.key_env} is not set (the agent's LLM is {provider.label})"
    req = urllib.request.Request(provider.probe_url, headers=provider.headers(key))  # header: never in a URL
    try:
        urllib.request.urlopen(req, timeout=timeout).read()
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            return False, f"LLM: the {provider.label} key was refused (HTTP {e.code}): fix {provider.key_env} in .env"
        return False, f"LLM: unexpected answer from {provider.label} (HTTP {e.code})"
    except (urllib.error.URLError, OSError) as e:
        return False, f"LLM: {provider.label} unreachable ({type(e).__name__}): check outbound HTTPS to {req.host}"
    return True, f"LLM: {provider.label} key accepted (model {model})"


_MODE_LABEL = {"create_new": "Create new", "overwrite": "Overwrite", "mixed": "both layouts, the newer one used"}


def _modes_text(modes):
    """'Overwrite' when every month has the same mode; else each mode with its months."""
    found = sorted(set(modes.values()))
    if len(found) == 1 and found[0] != "mixed":
        return _MODE_LABEL.get(found[0], found[0])
    parts = []
    for mode in sorted(found, key=lambda m: min(p for p, v in modes.items() if v == m)):
        months = sorted(p for p, v in modes.items() if v == mode)
        span = months[0] if len(months) == 1 else f"{months[0]}..{months[-1]}"
        parts.append(f"{_MODE_LABEL.get(mode, mode)} ({span})")
    return ", ".join(parts)


def _status_lines(status):
    """(ok, line) pairs from the focus-data status report."""
    lines = []
    who = str(status.get("aws_identity") or "")
    lines.append((who.startswith("arn:"), f"AWS: {who or 'no identity reported'}"))
    table = status.get("s3_table_state")
    if isinstance(table, dict):
        months = len(table.get("loaded") or {})
        lines.append((True, f"AWS: the agent's state is readable; {months} month(s) loaded into the S3 table"))
        if status.get("export_modes"):
            lines.append((True, f"AWS: export mode: {_modes_text(status['export_modes'])}"))
    elif table is None:
        lines.append((True, "AWS: nothing loaded yet (the first start loads every month)"))
    else:
        lines.append((False, f"AWS: the agent's state is {table}"))
    last = status.get("last_load") or {}
    problem = last.get("problem")
    if problem:
        lines.append((False, f"last load: {problem.get('summary')}. {problem.get('action')}"))
    elif last.get("data_through"):
        lines.append((True, f"data: through {last.get('data_through')} (local copy "
                            f"{'present' if status.get('cache_exists') else 'missing: rebuilt on the next load'})"))
    return lines


def run_check(*, status=focus_status, slack=slack_check, llm=llm_check, out=print):
    """Print OK / ALERT lines; 0 when everything is fine."""
    results = []
    try:
        results += _status_lines(status())
    except Exception as e:  # noqa: BLE001 - reported, not raised: the other checks still run
        results.append((False, f"AWS: couldn't read the agent's status ({e})"))
    for probe in (slack, llm):
        results.append(probe())
    for ok, line in results:
        out(f"{'OK' if ok else 'ALERT'}: {line}")
    all_ok = all(ok for ok, _ in results)
    out("All checks passed." if all_ok else "Some checks failed: see the ALERT lines.")
    return 0 if all_ok else 1


# ---------- the command ----------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="pico-finops-agent", description="The Pico FinOps Agent (standalone).")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Run the agent until stopped.")
    serve.add_argument("--config", default=agent.DEFAULT_CONFIG, help="Pico agent.yml (default: %(default)s)")
    commands.add_parser("check", help="Check AWS, the agent's data, Slack and the LLM key; posts nothing.")
    commands.add_parser("health", help="Docker's health check: is the running agent's heartbeat fresh and healthy?")
    args = parser.parse_args(argv)
    if args.command == "serve":
        return agent.main(["--config", args.config])
    if args.command == "check":
        return run_check()
    return health.check()


if __name__ == "__main__":
    code = main()
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        agent.exit_process(code)  # see agent.exit_process: library threads mustn't keep the process alive
    sys.exit(code)
