"""deploy/pico-finops-agent.yml: the Pico FinOps Agent profile.

Bash is on but limited to ``pico focus-data``; one read-only DuckDB datasource (the local cache);
every customer value comes from ``FOCUS_*`` env vars (``pico_finops_agent/.env.example``); Slack is off
when its tokens are unset; the file also works from ``~/.pico/conf/agent.yml`` without ``--config``.
"""
import re
import shutil
from pathlib import Path

import pytest

from pico.configuration.agent_config_loader import load_agent_config
from pico.tools.func_tool.bash_tool import BashTool

REPO_ROOT = Path(__file__).resolve().parents[2]
STANDALONE_CONFIG = REPO_ROOT / "deploy" / "pico-finops-agent.yml"
ENV_EXAMPLE = REPO_ROOT / "pico_finops_agent" / ".env.example"
ENTRYPOINT = REPO_ROOT / "pico_finops_agent" / "entrypoint.sh"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"

REQUIRED_ENV = {
    "FOCUS_REGION": "us-east-1",
    "FOCUS_EXPORT_BUCKET": "acme-focus-export",
    "FOCUS_EXPORT_PREFIX": "focus/acme-focus/data/",
    "FOCUS_TABLE_BUCKET_ARN": "arn:aws:s3tables:us-east-1:111122223333:bucket/acme-focus",
    "FOCUS_STATE_BUCKET": "acme-focus-state",
    "FOCUS_LAMBDA_ROLE_ARN": "arn:aws:iam::111122223333:role/pico-finops-agent-loader-role",
}
OPTIONAL_ENV = ("FOCUS_NAMESPACE", "FOCUS_TABLE", "FOCUS_STATE_KEY", "FOCUS_SCHEDULE", "FOCUS_MONTHS", "SLACK_APP_TOKEN", "SLACK_BOT_TOKEN",
                "AGENT_LLM_PROVIDER", "AGENT_LLM_MODEL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY")


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("PICO_DISABLE_VERSION_CHECK", "1")
    for name in OPTIONAL_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def env(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(REPO_ROOT)
    return monkeypatch


@pytest.fixture
def agent_cfg(env):
    return load_agent_config(config=str(STANDALONE_CONFIG), reload=True)


def _focus_profile(cfg):
    return cfg.get_plugin_profile("focus-data")


def test_bash_on_only_for_focus_data_and_schedule(agent_cfg):
    assert agent_cfg.bash_tool_enabled is True
    assert agent_cfg.bash_allowed_patterns == ["pico:focus-data", "pico:schedule"]


@pytest.mark.parametrize("cmd", ["pico focus-data status", "pico focus-data load", "pico focus-data load --force",
                                 "pico schedule list --ctx C1.U1.1.1.sig",
                                 'pico schedule add --ctx C1.U1.1.1.sig --kind reminder --to me --at "2026-11-11 09:00" '
                                 '--text "stand-up"'])
def test_allowed_pattern_matches_focus_data(agent_cfg, tmp_path, cmd):
    tool = BashTool(workspace_root=str(tmp_path), allowed_patterns=agent_cfg.bash_allowed_patterns)
    assert tool._is_command_allowed(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "pico plugin install x",
        "pico --config x focus-data load",
        'bash -c "pico focus-data status"',
        "pico focus-data status; rm -rf /",
        "pico schedule list --ctx x; rm -rf /",
        "pico --config x schedule list",
        'bash -c "pico schedule list"',
        "pico focus-data status && curl http://x",
        "pico focus-data status $(id)",
        "pico focus-data status > /data/x",
        "FOCUS_EXPORT_BUCKET=other pico focus-data load",
        "env FOCUS_TABLE=x pico focus-data load",
        "python -c 'print(1)'",
    ],
)
def test_allowed_pattern_refuses_everything_else(agent_cfg, tmp_path, cmd):
    tool = BashTool(workspace_root=str(tmp_path), allowed_patterns=agent_cfg.bash_allowed_patterns)
    assert not tool._is_command_allowed(cmd)


def test_model_is_gemini_2_5_flash(env):
    """Pico's own provider-level selection (what /model writes), set in the profile: API and Slack share it."""
    env.setenv("GEMINI_API_KEY", "test-key")
    cfg = load_agent_config(config=str(STANDALONE_CONFIG), reload=True)
    m = cfg.active_model()
    assert (m.type, m.model) == ("gemini", "gemini-3.8-flash")
    assert m.api_key == "test-key"


def _slack_agent_model():
    """The model the Slack agent gets: the profile, then the .env choice (pico_finops_agent/agent.py)."""
    from pico_finops_agent import agent

    return agent._load_gateway_config(str(STANDALONE_CONFIG)).active_model()


def test_slack_agent_default_model_is_gemini(env):
    env.setenv("GEMINI_API_KEY", "g-key")
    m = _slack_agent_model()
    assert (m.type, m.model, m.api_key) == ("gemini", "gemini-3.8-flash", "g-key")


@pytest.mark.parametrize("provider,key_env,mtype,model", [
    ("openai", "OPENAI_API_KEY", "openai", "gpt-5.5"),
    ("anthropic", "ANTHROPIC_API_KEY", "claude", "claude-sonnet-4-6"),
    ("claude", "ANTHROPIC_API_KEY", "claude", "claude-sonnet-4-6"),
    ("deepseek", "DEEPSEEK_API_KEY", "deepseek", "deepseek-v4-pro"),
])
def test_slack_agent_uses_the_env_provider(env, provider, key_env, mtype, model):
    env.setenv("AGENT_LLM_PROVIDER", provider)
    env.setenv(key_env, "other-key")
    m = _slack_agent_model()
    assert (m.type, m.model, m.api_key) == (mtype, model, "other-key")


def test_slack_agent_uses_the_env_model(env):
    env.setenv("AGENT_LLM_PROVIDER", "openai")
    env.setenv("AGENT_LLM_MODEL", "gpt-5.4-mini")
    env.setenv("OPENAI_API_KEY", "k")
    assert _slack_agent_model().model == "gpt-5.4-mini"


def test_env_choice_is_not_written_to_disk(env, tmp_path):
    """The choice comes from .env on every start; nothing is saved to .pico/config.yml."""
    env.setenv("AGENT_LLM_PROVIDER", "openai")
    env.setenv("OPENAI_API_KEY", "k")
    before = sorted(p for p in tmp_path.rglob("config.yml"))
    _slack_agent_model()
    assert sorted(p for p in tmp_path.rglob("config.yml")) == before
    assert not (REPO_ROOT / ".pico" / "config.yml").exists()


def test_one_duckdb_datasource_read_only(agent_cfg):
    ds = agent_cfg.services.datasources
    assert list(ds) == ["focus"]
    focus = ds["focus"]
    assert focus.type == "duckdb"
    assert focus.uri == "duckdb:////data/focus.duckdb"
    assert (focus.extra or {}).get("read_only") is True
    # Agent SQL can't read local files or URLs (read_csv('/etc/...'), httpfs): duckdb_connector.py:157.
    assert (focus.extra or {}).get("enable_external_access") is False
    assert agent_cfg.sql_read_only is True


def test_db_tools_not_denied_filesystem_denied_and_strict(agent_cfg):
    denied = {
        (r.tool, r.pattern)
        for r in agent_cfg.permissions_config.rules
        if str(getattr(r.permission, "value", r.permission)) == "deny"
    }
    assert ("filesystem_tools", "*") in denied
    # No web access: a cost agent needs none, and a fetched page or URL is an easy way to leak data.
    assert ("web_tool", "*") in denied
    assert not any(tool == "db_tools" for tool, _ in denied)
    assert agent_cfg.filesystem_strict is True


def test_no_mcp_bi_scheduler_semantic_or_documents(agent_cfg):
    services = agent_cfg.services
    assert services.bi_platforms == {}
    assert services.schedulers == {}
    assert services.semantic_layer == {}
    assert services.mcp_servers == {}
    assert agent_cfg.document_configs == {}


def test_focus_data_plugin_profile_from_env(agent_cfg):
    p = _focus_profile(agent_cfg)
    assert p["region"] == "us-east-1"
    assert p["export_bucket"] == "acme-focus-export"
    assert p["export_prefix"] == "focus/acme-focus/data/"
    assert p["table_bucket_arn"].endswith(":bucket/acme-focus")
    assert p["state_bucket"] == "acme-focus-state"
    assert p["lambda_role_arn"].endswith(":role/pico-finops-agent-loader-role")
    assert p["cache_path"] == "/data/focus.duckdb"
    assert "aws_profile" not in p


def test_optional_values_default_when_unset(agent_cfg):
    p = _focus_profile(agent_cfg)
    assert (p["namespace"], p["table"], p["state_key"]) == ("pico_finops_agent", "focus", "pico-finops-agent/state.json")
    assert str(p["months"]) == "4"  # FOCUS_MONTHS unset: keep the last 4 months
    assert "schedule" not in p  # the wrapper reads FOCUS_SCHEDULE itself


def test_missing_required_value_is_visible(env):
    env.delenv("FOCUS_EXPORT_BUCKET")
    cfg = load_agent_config(config=str(STANDALONE_CONFIG), reload=True)
    assert _focus_profile(cfg)["export_bucket"] == "<MISSING:FOCUS_EXPORT_BUCKET>"


def _profile_env_names():
    return set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", STANDALONE_CONFIG.read_text(encoding="utf-8")))


def _env_example_names():
    names = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^#?\s?([A-Z][A-Z0-9_]*)=", line.strip())
        if m:
            names.add(m.group(1))
    return names


# Read outside the profile: the start check and the scheduler.
NON_PROFILE_ENV = {"FOCUS_SCHEDULE", "SLACK_NOTIFY_CHANNEL", "FOCUS_ALERT_INCREASE_PCT",
                   "FOCUS_ALERT_MIN_SERVICE_USD", "FOCUS_ALERT_MIN_RESOURCE_USD", "FOCUS_ALERT_STALE_DAYS",
                   "AGENT_LLM_PROVIDER", "AGENT_LLM_MODEL",
                   # scheduled tasks (read by the plugin and the scheduler, checked by the entrypoint)
                   "FOCUS_REPORT_CHANNELS", "FOCUS_REPORT_ADMINS", "FOCUS_REPORT_CREATORS", "FOCUS_REPORT_HISTORY_MAX",
                   "FOCUS_ASK_MAX_PER_DAY",
                   "FOCUS_REPORT_ALLOW_MENTIONS",
                   # Slack answer hardening (read by pico_finops_agent/slack_guard.py, checked by the entrypoint)
                   "AGENT_HISTORY_TURNS", "AGENT_ANSWER_DEADLINE_S", "AGENT_QUESTIONS_PER_HOUR",
                   # model-call log (pico_finops_agent/llm_log.py): keep failed requests on the data volume
                   "AGENT_SAVE_FAILED_CALLS"}


def test_env_example_matches_profile():
    profile = _profile_env_names() | NON_PROFILE_ENV
    example = _env_example_names()
    for name in NON_PROFILE_ENV:
        assert name in ENTRYPOINT.read_text(encoding="utf-8")
    assert profile <= example, f"in the profile but not in .env.example: {sorted(profile - example)}"
    assert example <= profile, f"in .env.example but unused: {sorted(example - profile)}"


def test_chat_tool_rounds_capped(agent_cfg):
    """At most 15 model/tool rounds per Slack answer (Pico's default is 50), like the analyst's tool-round cap."""
    # ChatAgenticNode reads agentic_nodes["chat"]["max_turns"] (pinned in tests/fork/pico_finops_agent/test_slack_guard_pins.py)
    assert agent_cfg.agentic_nodes["chat"]["max_turns"] == 15
    assert set(agent_cfg.agentic_nodes) == {"chat"}  # nothing else changed for the chat node (tools, permissions)
    assert set(agent_cfg.agentic_nodes["chat"]) <= {"max_turns", "system_prompt"}


def test_env_example_has_placeholders_only():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert not re.search(r"xapp-\d", text) and not re.search(r"xoxb-\d", text)
    assert set(re.findall(r"\b\d{12}\b", text)) <= {"111122223333"}
    assert not re.search(r"(?m)^[A-Z_]*API_KEY=\S", text), "API keys must be commented out / empty"


def test_slack_off_without_tokens(agent_cfg):
    extra = agent_cfg.channels_config["slack"]["extra"]
    assert extra["app_token"] == "" and extra["bot_token"] == ""


def test_slack_on_with_tokens(env):
    env.setenv("SLACK_APP_TOKEN", "xapp-test")
    env.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    cfg = load_agent_config(config=str(STANDALONE_CONFIG), reload=True)
    extra = cfg.channels_config["slack"]["extra"]
    assert (extra["app_token"], extra["bot_token"]) == ("xapp-test", "xoxb-test")
    assert cfg.channels_config["slack"]["datasource"] == "focus"
    # Users see only the answer, not tool steps (load_skill / execute_sql lines).
    assert cfg.channels_config["slack"]["verbose"] == "quiet"


def test_paths_absolute(agent_cfg):
    assert agent_cfg.project_root == "/pico-workspace"
    assert _focus_profile(agent_cfg)["cache_path"].startswith("/data/")


def test_config_found_without_flag(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    conf = tmp_path / ".pico" / "conf"
    conf.mkdir(parents=True)
    shutil.copy(STANDALONE_CONFIG, conf / "agent.yml")
    work = tmp_path / "workspace"
    work.mkdir()
    monkeypatch.chdir(work)
    cfg = load_agent_config(reload=True)
    assert cfg.bash_allowed_patterns == ["pico:focus-data", "pico:schedule"]


def test_no_inline_secrets_or_backend_urls():
    text = STANDALONE_CONFIG.read_text(encoding="utf-8").lower()
    for forbidden in ("claude_subscription", "auth_type", "password", "superset", "airflow", "snowflake",
                      "api_url", "5001", "be2", "xoxb-", "xapp-", "aws_access_key", "aws_profile"):
        assert forbidden not in text, forbidden


def test_dockerignore_excludes_nested_env():
    lines = {ln.strip() for ln in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()}
    assert {"**/.env", "**/.env.*", "!**/.env.example"} <= lines


def test_schedule_plugin_profile(agent_cfg):
    """plugins/schedule: the tasks plugin reads its data folder and the FOCUS source from agent.yml."""
    prof = agent_cfg.get_plugin_profile("schedule")
    assert prof["data_dir"] == "/data"
    assert prof["source"] == "pico_plugin_focus_data.schedule_source:FocusSource"
    assert prof["cache_path"] == "/data/focus.duckdb"
