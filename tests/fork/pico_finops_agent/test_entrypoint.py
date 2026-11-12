"""pico_finops_agent/entrypoint.sh: start-time check of the .env values, then exec the command.

Runs the script with POSIX ``sh`` and a controlled environment (nothing inherited from the runner
except PATH). Linux only (dash on python:3.12-slim); run the tests on Linux.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "pico_finops_agent" / "entrypoint.sh"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh script; runs on Linux")

GOOD = {
    "FOCUS_REGION": "us-east-1",
    "FOCUS_EXPORT_BUCKET": "acme-focus-export",
    "FOCUS_EXPORT_PREFIX": "focus/acme-focus/data/",
    "FOCUS_TABLE_BUCKET_ARN": "arn:aws:s3tables:us-east-1:111122223333:bucket/acme-focus",
    "FOCUS_STATE_BUCKET": "acme-focus-state",
    "FOCUS_LAMBDA_ROLE_ARN": "arn:aws:iam::111122223333:role/pico-finops-agent-loader-role",
    "GEMINI_API_KEY": "test-llm-key-value",
}
REQUIRED = [k for k in GOOD if k != "GEMINI_API_KEY"]
STARTED = [sys.executable, "-c", "print('started')"]


def run(tmp_path, env_over=None, drop=(), cmd=None):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "FOCUS_DATA_DIR": str(data)}
    env.update({k: v for k, v in GOOD.items() if k not in drop})
    env.update(env_over or {})
    return subprocess.run(["sh", str(SCRIPT), *(cmd or STARTED)], env=env, capture_output=True, text=True, timeout=30)


def test_good_env_execs_command(tmp_path):
    r = run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "started"


@pytest.mark.parametrize("name", REQUIRED)
def test_each_required_missing_names_it(tmp_path, name):
    r = run(tmp_path, drop=(name,))
    assert r.returncode == 1
    assert name in r.stderr
    assert "started" not in r.stdout


@pytest.mark.parametrize("name", REQUIRED)
def test_each_required_empty_names_it(tmp_path, name):
    r = run(tmp_path, env_over={name: ""})
    assert r.returncode == 1 and name in r.stderr


@pytest.mark.parametrize("name", ["FOCUS_NAMESPACE", "FOCUS_TABLE", "FOCUS_STATE_KEY", "FOCUS_SCHEDULE", "FOCUS_MONTHS"])
def test_optional_set_but_empty_refused(tmp_path, name):
    """Pico applies ${VAR:-default} only when VAR is unset, so an empty value would reach the plugin."""
    r = run(tmp_path, env_over={name: ""})
    assert r.returncode == 1 and name in r.stderr


def test_optional_good_values_accepted(tmp_path):
    r = run(tmp_path, env_over={"FOCUS_NAMESPACE": "focus", "FOCUS_TABLE": "focus_v2",
                                "FOCUS_STATE_KEY": "pico-finops-agent/state.json", "FOCUS_SCHEDULE": "0 6,18 * * *",
                                "FOCUS_MONTHS": "12"})
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize(
    "name,value",
    [
        ("FOCUS_EXPORT_BUCKET", "Bad_Bucket"),
        ("FOCUS_STATE_BUCKET", "x"),
        ("FOCUS_EXPORT_PREFIX", "focus/data"),
        ("FOCUS_EXPORT_PREFIX", "/abs/data/"),
        ("FOCUS_EXPORT_PREFIX", "focus/../data/"),
        ("FOCUS_EXPORT_PREFIX", "focus/da'ta/"),
        ("FOCUS_TABLE_BUCKET_ARN", "arn:aws:s3:::acme-focus"),
        ("FOCUS_TABLE_BUCKET_ARN", "arn:aws:s3tables:eu-west-1:111122223333:bucket/acme-focus"),
        ("FOCUS_NAMESPACE", "focus;drop"),
        ("FOCUS_TABLE", "Focus"),
        ("FOCUS_LAMBDA_ROLE_ARN", "arn:aws:iam::12345:role/x"),
        ("FOCUS_LAMBDA_ROLE_ARN", "pico-finops-agent-loader-role"),
        ("FOCUS_STATE_KEY", "focus/state.txt"),
        ("FOCUS_STATE_KEY", "../state.json"),
        ("FOCUS_SCHEDULE", "0 6 * *"),
        ("FOCUS_MONTHS", "0"),
        ("FOCUS_MONTHS", "121"),
        ("FOCUS_MONTHS", "4.5"),
        ("FOCUS_MONTHS", "four"),
        ("FOCUS_REGION", "us east 1"),
    ],
)
def test_formats_refused(tmp_path, name, value):
    r = run(tmp_path, env_over={name: value})
    assert r.returncode == 1, f"{name}={value!r} was accepted"
    assert name in r.stderr


def test_no_llm_key_refused(tmp_path):
    r = run(tmp_path, drop=("GEMINI_API_KEY",))
    assert r.returncode == 1 and "GEMINI_API_KEY" in r.stderr


@pytest.mark.parametrize("key", ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"])
def test_another_provider_key_alone_is_not_enough(tmp_path, key):
    """Without AGENT_LLM_PROVIDER the agent uses Gemini, so its key is the one that must be set."""
    r = run(tmp_path, drop=("GEMINI_API_KEY",), env_over={key: "k"})
    assert r.returncode == 1 and "GEMINI_API_KEY" in r.stderr


PROVIDER_KEYS = [("gemini", "GEMINI_API_KEY"), ("openai", "OPENAI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY"),
                 ("claude", "ANTHROPIC_API_KEY"), ("deepseek", "DEEPSEEK_API_KEY")]


@pytest.mark.parametrize("provider,key", PROVIDER_KEYS)
def test_chosen_provider_needs_only_its_own_key(tmp_path, provider, key):
    r = run(tmp_path, drop=("GEMINI_API_KEY",), env_over={"AGENT_LLM_PROVIDER": provider, key: "k"})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "started"


@pytest.mark.parametrize("provider,key", [p for p in PROVIDER_KEYS if p[0] != "gemini"])
def test_chosen_provider_without_its_key_names_it(tmp_path, provider, key):
    """A Gemini key doesn't help when another provider is chosen."""
    r = run(tmp_path, env_over={"AGENT_LLM_PROVIDER": provider})
    assert r.returncode == 1 and key in r.stderr and "AGENT_LLM_PROVIDER" in r.stderr
    assert "test-llm-key-value" not in r.stderr


@pytest.mark.parametrize("value", ["kimi", "Gemini", "open ai", "gemini;id", ""])
def test_unknown_provider_refused(tmp_path, value):
    r = run(tmp_path, env_over={"AGENT_LLM_PROVIDER": value})
    assert r.returncode == 1 and "AGENT_LLM_PROVIDER" in r.stderr


@pytest.mark.parametrize("value,ok", [
    ("gpt-5.4-mini", True), ("claude-sonnet-4-6", True), ("models/gemini-3.8-flash", True),
    ("gpt 5", False), ("x;id", False), ("$(id)", False), ("", False),
])
def test_model_name_format(tmp_path, value, ok):
    r = run(tmp_path, env_over={"AGENT_LLM_MODEL": value})
    assert (r.returncode == 0) is ok, r.stderr
    if not ok:
        assert "AGENT_LLM_MODEL" in r.stderr


def test_slack_both_tokens_accepted(tmp_path):
    r = run(tmp_path, env_over={"SLACK_APP_TOKEN": "xapp-1-test", "SLACK_BOT_TOKEN": "xoxb-test"})
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("over", [{"SLACK_APP_TOKEN": "xapp-1-test"}, {"SLACK_BOT_TOKEN": "xoxb-test"}])
def test_slack_half_set_refused(tmp_path, over):
    r = run(tmp_path, env_over=over)
    assert r.returncode == 1 and "SLACK_" in r.stderr


def test_slack_bad_prefix_refused(tmp_path):
    r = run(tmp_path, env_over={"SLACK_APP_TOKEN": "xoxb-wrong", "SLACK_BOT_TOKEN": "xoxb-test"})
    assert r.returncode == 1 and "SLACK_APP_TOKEN" in r.stderr


def test_notify_channel_needs_bot_token(tmp_path):
    r = run(tmp_path, env_over={"SLACK_NOTIFY_CHANNEL": "C0TESTCHAN1"})
    assert r.returncode == 1 and "SLACK_NOTIFY_CHANNEL" in r.stderr


@pytest.mark.parametrize("value", ["C0TESTCHAN1", "#pico-finops-agent", "pico-finops-agent"])
def test_notify_channel_accepted_with_tokens(tmp_path, value):
    r = run(tmp_path, env_over={"SLACK_NOTIFY_CHANNEL": value, "SLACK_APP_TOKEN": "xapp-1", "SLACK_BOT_TOKEN": "xoxb-1"})
    assert r.returncode == 0, r.stderr


def test_notify_channel_bad_format_refused(tmp_path):
    r = run(tmp_path, env_over={"SLACK_NOTIFY_CHANNEL": "pico agent;x", "SLACK_APP_TOKEN": "xapp-1",
                                "SLACK_BOT_TOKEN": "xoxb-1"})
    assert r.returncode == 1 and "SLACK_NOTIFY_CHANNEL" in r.stderr


@pytest.mark.parametrize("name", ["FOCUS_ALERT_INCREASE_PCT", "FOCUS_ALERT_MIN_SERVICE_USD", "FOCUS_ALERT_MIN_RESOURCE_USD"])
def test_alert_thresholds_must_be_numbers(tmp_path, name):
    assert run(tmp_path, env_over={name: "25.5"}).returncode == 0
    r = run(tmp_path, env_over={name: "lots"})
    assert r.returncode == 1 and name in r.stderr


def test_gateway_without_tokens_explains(tmp_path):
    r = run(tmp_path, cmd=["pico-gateway", "--config", "x"])
    assert r.returncode == 1
    assert "pico-gateway needs both" in r.stderr


def test_agent_without_tokens_still_starts(tmp_path):
    """Slack is optional: the one-process agent then collects data only (echo stands in for python)."""
    r = run(tmp_path, cmd=["echo", "-m", "pico_finops_agent.agent", "--config", "x"])
    assert r.returncode == 0, r.stderr


def test_data_dir_not_writable_refused(tmp_path):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    data = tmp_path / "data"
    data.mkdir()
    data.chmod(0o500)
    try:
        r = run(tmp_path)
    finally:
        data.chmod(0o700)
    assert r.returncode == 1 and "FOCUS_DATA_DIR" in r.stderr


def test_values_never_printed(tmp_path):
    secrets = {"SLACK_APP_TOKEN": "xapp-1-SECRETVALUE", "SLACK_BOT_TOKEN": "bad-SECRETBOT"}
    for over, drop in [(secrets, ()), ({"FOCUS_EXPORT_BUCKET": "Bad_Bucket_Name"}, ()), ({}, ("GEMINI_API_KEY",))]:
        r = run(tmp_path, env_over=over, drop=drop)
        assert r.returncode == 1
        out = r.stdout + r.stderr
        for value in [*over.values(), *GOOD.values()]:
            assert value not in out, f"value leaked: {value}"


def test_writes_no_files(tmp_path):
    run(tmp_path)  # creates the dirs
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    r = run(tmp_path)
    assert r.returncode == 0
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before


def test_exec_replaces_shell(tmp_path):
    """exec, so SIGTERM reaches the app: the command's PID is the PID that ran the script."""
    data = tmp_path / "data"
    data.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "FOCUS_DATA_DIR": str(data), **GOOD}
    p = subprocess.Popen(["sh", str(SCRIPT), "sh", "-c", "echo $$"], env=env, stdout=subprocess.PIPE, text=True)
    out, _ = p.communicate(timeout=30)
    assert p.returncode == 0
    assert int(out.strip()) == p.pid


def test_script_is_executable_and_posix():
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n")
    assert "set -x" not in text and "[[" not in text
    assert os.access(SCRIPT, os.X_OK)


# ---------- scheduled tasks settings (PRD R12, R15, R18, R19) ----------

@pytest.mark.parametrize("name", ["FOCUS_REPORT_HISTORY_MAX", "FOCUS_ASK_MAX_PER_DAY"])
@pytest.mark.parametrize("value,ok", [("20", True), ("1", True), ("0", False), ("abc", False), ("", False),
                                      ("2.5", False)])
def test_task_counts(tmp_path, name, value, ok):
    r = run(tmp_path, env_over={name: value})
    assert (r.returncode == 0) is ok, r.stderr
    if not ok:
        assert name in r.stderr


# ---------- Slack answer hardening (pico_finops_agent/slack_guard.py; optional, defaults 8 / 180 / 30) ----------

@pytest.mark.parametrize("name,value,ok", [
    ("AGENT_HISTORY_TURNS", "1", True), ("AGENT_HISTORY_TURNS", "8", True), ("AGENT_HISTORY_TURNS", "50", True),
    ("AGENT_HISTORY_TURNS", "0", False), ("AGENT_HISTORY_TURNS", "51", False), ("AGENT_HISTORY_TURNS", "abc", False),
    ("AGENT_HISTORY_TURNS", "", False), ("AGENT_HISTORY_TURNS", "2.5", False),
    ("AGENT_ANSWER_DEADLINE_S", "30", True), ("AGENT_ANSWER_DEADLINE_S", "180", True),
    ("AGENT_ANSWER_DEADLINE_S", "900", True), ("AGENT_ANSWER_DEADLINE_S", "29", False),
    ("AGENT_ANSWER_DEADLINE_S", "901", False), ("AGENT_ANSWER_DEADLINE_S", "1000", False),
    ("AGENT_ANSWER_DEADLINE_S", "x", False), ("AGENT_ANSWER_DEADLINE_S", "", False),
    ("AGENT_QUESTIONS_PER_HOUR", "0", True), ("AGENT_QUESTIONS_PER_HOUR", "30", True),
    ("AGENT_QUESTIONS_PER_HOUR", "1000", True), ("AGENT_QUESTIONS_PER_HOUR", "-1", False),
    ("AGENT_QUESTIONS_PER_HOUR", "07", False), ("AGENT_QUESTIONS_PER_HOUR", "x", False),
    ("AGENT_QUESTIONS_PER_HOUR", "", False),
])
def test_slack_answer_settings(tmp_path, name, value, ok):
    r = run(tmp_path, env_over={name: value})
    assert (r.returncode == 0) is ok, r.stderr
    if not ok:
        assert name in r.stderr


@pytest.mark.parametrize("value,ok", [("", True), ("C0AAA00001", True), ("C0AAA00001,G0BBB00002", True),
                                      ("C0AAA00001, C0BBB00002", True), ("general", False), ("#finance", False)])
def test_task_channels(tmp_path, value, ok):
    r = run(tmp_path, env_over={"FOCUS_REPORT_CHANNELS": value})
    assert (r.returncode == 0) is ok, r.stderr


@pytest.mark.parametrize("value,ok", [("U0ADMIN001", True), ("U0ADMIN001,W0ADMIN002", True), ("", True),
                                      ("ravi", False)])
def test_task_admins(tmp_path, value, ok):
    r = run(tmp_path, env_over={"FOCUS_REPORT_ADMINS": value})
    assert (r.returncode == 0) is ok, r.stderr


@pytest.mark.parametrize("value,ok", [("true", True), ("false", True), ("yes", False), ("", False)])
def test_task_mentions_switch(tmp_path, value, ok):
    r = run(tmp_path, env_over={"FOCUS_REPORT_ALLOW_MENTIONS": value})
    assert (r.returncode == 0) is ok, r.stderr



@pytest.mark.parametrize("value,ok", [("U0RAVI0001", True), ("U0RAVI0001,U0MEENA001", True), ("", True),
                                      ("ravi", False)])
def test_task_creators(tmp_path, value, ok):
    r = run(tmp_path, env_over={"FOCUS_REPORT_CREATORS": value})
    assert (r.returncode == 0) is ok, r.stderr


# ---------- FOCUS_EXPORT_PREFIX is a key path inside the bucket (2026-11-11: an s3:// prefix loaded nothing) ----------

@pytest.mark.parametrize("value", ["s3://acme-focus-export/focus2/focus2/data/", "S3://acme-focus-export/focus2/data/"])
def test_export_prefix_must_be_a_key_path_not_a_url(tmp_path, value):
    r = run(tmp_path, env_over={"FOCUS_EXPORT_PREFIX": value})
    assert r.returncode == 1 and "FOCUS_EXPORT_PREFIX" in r.stderr and "key path" in r.stderr
    assert value not in r.stderr


def test_export_prefix_without_data_warns_but_starts(tmp_path):
    r = run(tmp_path, env_over={"FOCUS_EXPORT_PREFIX": "focus2/flat/"})
    assert r.returncode == 0 and r.stdout.strip() == "started"
    assert "FOCUS_EXPORT_PREFIX" in r.stderr and "data/" in r.stderr and "focus2/flat/" not in r.stderr


def test_export_prefix_ending_with_data_does_not_warn(tmp_path):
    r = run(tmp_path, env_over={"FOCUS_EXPORT_PREFIX": "focus2/focus2/data/"})
    assert r.returncode == 0 and "FOCUS_EXPORT_PREFIX" not in r.stderr


def test_export_prefix_starting_with_the_bucket_name_warns_but_starts(tmp_path):
    """Review: a real folder can have the bucket's name, so this is a warning, not a refusal."""
    r = run(tmp_path, env_over={"FOCUS_EXPORT_PREFIX": "acme-focus-export/focus2/data/"})
    assert r.returncode == 0 and "FOCUS_EXPORT_PREFIX" in r.stderr and "bucket name" in r.stderr
