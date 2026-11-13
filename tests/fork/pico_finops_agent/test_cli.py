"""pico_finops_agent.cli: the `pico-finops-agent` command.

    pico-finops-agent serve [--config agent.yml]   run the agent until stopped (the image's default)
    pico-finops-agent check                        pre-flight: AWS, the agent's data, Slack, the LLM key; posts nothing
    pico-finops-agent health                       Docker's HEALTHCHECK (the heartbeat)
"""
import io
import json
import subprocess
import sys

import httpx
import pytest

from pico_finops_agent import agent, cli, health

GOOD_STATUS = {
    "aws_identity": "arn:aws:sts::111122223333:assumed-role/pico-finops-agent-ec2-role/i-0abc",
    "s3_table_state": {"loaded": {"2026-10": "s3://x", "2026-11": "s3://y"}},
    "cache_exists": True,
    "last_load": {"problem": None, "data_through": "2026-11-04", "last_success": "2026-11-05T06:00:40+00:00"},
}


def fakes(**over):
    checks = {
        "status": lambda: GOOD_STATUS,
        "slack": lambda: (True, "Slack: tokens accepted (bot @pico_finops_agent in Acme)"),
        "llm": lambda: (True, "LLM: Google Gemini key accepted (model gemini-3.8-flash)"),
    }
    checks.update(over)
    return checks


def run_check(capsys, **over):
    code = cli.run_check(**fakes(**over))
    return code, capsys.readouterr().out


# ---------- commands ----------

def test_serve_runs_the_agent(monkeypatch):
    seen = {}
    monkeypatch.setattr(agent, "main", lambda argv: seen.setdefault("argv", argv) and 0)
    assert cli.main(["serve", "--config", "/etc/agent.yml"]) == 0
    assert seen["argv"] == ["--config", "/etc/agent.yml"]


def test_serve_passes_the_agents_exit_code(monkeypatch):
    monkeypatch.setattr(agent, "main", lambda argv: health.EXIT_SLACK)
    assert cli.main(["serve"]) == health.EXIT_SLACK


def test_health_is_the_heartbeat_check(monkeypatch):
    monkeypatch.setattr(health, "check", lambda: 1)
    assert cli.main(["health"]) == 1


def test_a_command_is_required():
    with pytest.raises(SystemExit) as e:
        cli.main([])
    assert e.value.code == 2


def test_help_lists_the_commands():
    r = subprocess.run([sys.executable, "-m", "pico_finops_agent.cli", "--help"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and all(c in r.stdout for c in ("serve", "check", "health"))


# ---------- check: the pre-flight ----------

def test_everything_fine(capsys):
    code, out = run_check(capsys)
    assert code == 0 and "ALERT" not in out
    assert "111122223333" in out and "2 month(s)" in out and "2026-11-04" in out and "@pico_finops_agent" in out


def test_no_aws_credentials(capsys):
    code, out = run_check(capsys, status=lambda: {**GOOD_STATUS, "aws_identity": "no AWS credentials on this EC2"})
    assert code == 1 and "ALERT: AWS: no AWS credentials" in out


def test_state_bucket_unreadable(capsys):
    code, out = run_check(capsys, status=lambda: {**GOOD_STATUS, "s3_table_state": "unreadable: AccessDenied: x"})
    assert code == 1 and "ALERT" in out and "unreadable" in out


def test_new_install_with_nothing_loaded_yet_is_fine(capsys):
    status = {**GOOD_STATUS, "s3_table_state": None, "cache_exists": False, "last_load": {"problem": None}}
    code, out = run_check(capsys, status=lambda: status)
    assert code == 0 and "nothing loaded yet" in out


def test_last_load_problem_is_reported_with_the_fix(capsys):
    problem = {"code": "lambda_permission", "summary": "the loader Lambda's role isn't allowed to s3:GetObject",
               "action": "Add it from pico_finops_agent/iam/loader-role-policy.json.", "permanent": True}
    code, out = run_check(capsys, status=lambda: {**GOOD_STATUS, "last_load": {"problem": problem}})
    assert code == 1 and "s3:GetObject" in out and "loader-role-policy.json" in out


def test_status_command_failing(capsys):
    def broken():
        raise RuntimeError("pico focus-data status exited 1")

    code, out = run_check(capsys, status=broken)
    assert code == 1 and "ALERT" in out and "exited 1" in out


def test_slack_problem(capsys):
    code, out = run_check(capsys, slack=lambda: (False, "Slack: the bot token was refused (invalid_auth)"))
    assert code == 1 and "ALERT: Slack: the bot token was refused" in out


def test_llm_problem(capsys):
    code, out = run_check(capsys, llm=lambda: (False, "LLM: the Google Gemini key was refused (HTTP 400)"))
    assert code == 1 and "ALERT: LLM" in out


# ---------- the individual probes ----------

class Resp(io.BytesIO):
    status = 200


def test_slack_check_off_without_tokens(monkeypatch):
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    ok, line = cli.slack_check()
    assert ok and "off" in line


@pytest.mark.parametrize("answers,ok,word", [
    ([{"ok": True, "user": "pico_finops_agent", "team": "Acme"}, {"ok": True}], True, "@pico_finops_agent"),
    ([{"ok": False, "error": "invalid_auth"}, {"ok": True}], False, "bot token"),
    ([{"ok": True, "user": "b", "team": "t"}, {"ok": False, "error": "invalid_auth"}], False, "app-level token"),
    (httpx.ConnectError("no route"), False, "unreachable"),
])
def test_slack_check(monkeypatch, answers, ok, word):
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-1-x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-x")
    calls = []

    def fake_post(url, headers=None, timeout=None):
        calls.append(url)
        if isinstance(answers, Exception):
            raise answers
        return httpx.Response(200, json=answers[len(calls) - 1], request=httpx.Request("POST", url))

    monkeypatch.setattr(agent.httpx, "post", fake_post)
    got_ok, line = cli.slack_check()
    assert got_ok is ok and word in line
    assert "xoxb-x" not in line and "xapp-1-x" not in line


PROVIDERS = [
    # AGENT_LLM_PROVIDER, its key variable, the host the probe asks, the header the key goes in
    ("gemini", "GEMINI_API_KEY", "generativelanguage.googleapis.com", "X-goog-api-key"),
    ("openai", "OPENAI_API_KEY", "api.openai.com", "Authorization"),
    ("anthropic", "ANTHROPIC_API_KEY", "api.anthropic.com", "X-api-key"),
    ("deepseek", "DEEPSEEK_API_KEY", "api.deepseek.com", "Authorization"),
]
LLM_ENV = ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
           "AGENT_LLM_PROVIDER", "AGENT_LLM_MODEL")


def _fake_get(seen, outcome):
    """A stand-in for httpx.get: records the request; answers 200, the given status, or raises the given error."""
    def get(url, headers=None, timeout=None):
        req = httpx.Request("GET", url, headers=headers)
        seen.append(req)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(200 if outcome == "ok" else outcome, json={"data": []}, request=req)
    return get


@pytest.fixture
def no_llm_env(monkeypatch):
    for name in LLM_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize("provider,key_env,host,header", PROVIDERS)
@pytest.mark.parametrize("outcome,ok,word", [
    ("ok", True, "accepted"),
    (400, False, "refused"),
    (401, False, "refused"),
    (403, False, "refused"),
    (500, False, "unexpected"),
    (httpx.ConnectError("no route"), False, "unreachable"),
])
def test_llm_check(no_llm_env, provider, key_env, host, header, outcome, ok, word):
    no_llm_env.setenv("AGENT_LLM_PROVIDER", provider)
    no_llm_env.setenv(key_env, "secret-llm-key")
    seen = []

    no_llm_env.setattr(cli.httpx, "get", _fake_get(seen, outcome))
    got_ok, line = cli.llm_check()
    assert got_ok is ok and word in line and "secret-llm-key" not in line
    req = seen[0]
    assert req.url.host == host
    assert "secret-llm-key" not in str(req.url)  # the key goes in a header, never in the URL
    assert "secret-llm-key" in req.headers[header]
    if word == "refused":
        assert key_env in line


def test_llm_check_names_the_model(no_llm_env):
    no_llm_env.setenv("AGENT_LLM_PROVIDER", "openai")
    no_llm_env.setenv("AGENT_LLM_MODEL", "gpt-5.4-mini")
    no_llm_env.setenv("OPENAI_API_KEY", "k")
    no_llm_env.setattr(cli.httpx, "get", _fake_get([], "ok"))
    ok, line = cli.llm_check()
    assert ok and "OpenAI" in line and "gpt-5.4-mini" in line


@pytest.mark.parametrize("provider,key_env", [(p[0], p[1]) for p in PROVIDERS])
def test_llm_check_without_a_key(no_llm_env, provider, key_env):
    no_llm_env.setenv("AGENT_LLM_PROVIDER", provider)
    ok, line = cli.llm_check()
    assert not ok and key_env in line


def test_llm_check_unknown_provider(no_llm_env):
    no_llm_env.setenv("AGENT_LLM_PROVIDER", "kimi")
    ok, line = cli.llm_check()
    assert not ok and "AGENT_LLM_PROVIDER" in line


def test_llm_check_default_is_gemini(no_llm_env):
    no_llm_env.setenv("GEMINI_API_KEY", "k")
    seen = []
    no_llm_env.setattr(cli.httpx, "get", _fake_get(seen, "ok"))
    ok, line = cli.llm_check()
    assert ok and "Gemini" in line and "gemini-3.8-flash" in line
    assert seen[0].url.host == "generativelanguage.googleapis.com"


def test_focus_status_reads_the_plugins_json(monkeypatch):
    out = "noise line\n" + json.dumps(GOOD_STATUS, indent=2) + "\n"
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""))
    assert cli.focus_status()["aws_identity"].startswith("arn:")


def test_focus_status_failure_raises(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="boom"))
    with pytest.raises(RuntimeError, match="exited 1"):
        cli.focus_status()


# ---------- the export mode in the check (2026-11-11) ----------

@pytest.mark.parametrize("modes,said", [
    ({"2026-10": "overwrite", "2026-11": "overwrite"}, "export mode: Overwrite"),
    ({"2026-10": "create_new", "2026-11": "create_new"}, "export mode: Create new"),
    ({"2026-10": "create_new", "2026-11": "overwrite"}, "export mode: Create new (2026-10), Overwrite (2026-11)"),
    ({"2026-11": "mixed"}, "export mode: both layouts, the newer one used (2026-11)"),
])
def test_check_names_the_export_mode(capsys, modes, said):
    code, out = run_check(capsys, status=lambda: {**GOOD_STATUS, "export_modes": modes})
    assert code == 0 and said in out


def test_check_without_modes_says_nothing_about_them(capsys):
    code, out = run_check(capsys, status=lambda: GOOD_STATUS)
    assert code == 0 and "export mode" not in out
