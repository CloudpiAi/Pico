"""Settings come from an .env-style file and the environment (tests/e2e/standalone_ui/config.py)."""

import os

import pytest

from tests.e2e.standalone_ui.config import Config, ConfigError, load_config

_REQUIRED = {
    "E2E_SLACK_URL": "https://yourco.slack.com",
    "E2E_TEAM_ID": "T0000000001",
    "E2E_BOT_NAME": "Pico FinOps Agent",
    "E2E_STATE_FILE": "/tmp/state.json",
}


def _write(tmp_path, text):
    path = tmp_path / "e2e.env"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_required_values_from_environ_and_defaults():
    cfg = load_config(environ=dict(_REQUIRED))
    assert isinstance(cfg, Config)
    assert cfg.slack_url == "https://yourco.slack.com"
    assert cfg.team_id == "T0000000001"
    assert cfg.bot_name == "Pico FinOps Agent"
    assert cfg.state_file == "/tmp/state.json"
    assert cfg.bot_user is None and cfg.bot_dm is None and cfg.me is None
    assert cfg.test_channels == ()
    assert cfg.outside_channel is None and cfg.notify_channel is None
    assert cfg.agent_container is None and cfg.data_copy is None
    assert cfg.headless is True
    assert cfg.answer_timeout_s == 180.0
    assert cfg.task_timeout_s == 240.0
    assert cfg.deliver_timeout_s == 480.0
    assert cfg.report_dir == os.path.expanduser("~/.pico-e2e/runs")


def test_file_parsing_comments_quotes_lists_and_types(tmp_path):
    env_file = _write(
        tmp_path,
        "# a comment\n"
        "\n"
        "E2E_SLACK_URL=https://yourco.slack.com\n"
        "E2E_TEAM_ID='T0000000002'\n"
        'E2E_BOT_NAME="Pico"\n'
        "E2E_STATE_FILE=~/.pico-e2e/state.json\n"
        "E2E_BOT_USER=U0BOT\n"
        "E2E_BOT_DM=D0DM\n"
        "E2E_ME=U0ME\n"
        "E2E_TEST_CHANNELS= C01 , ,C02,\n"
        "E2E_OUTSIDE_CHANNEL=C09\n"
        "E2E_NOTIFY_CHANNEL=\n"
        "E2E_AGENT_CONTAINER=pico-finops-agent\n"
        "E2E_DATA_COPY=~/focus.duckdb\n"
        "E2E_REPORT_DIR=~/runs\n"
        "E2E_HEADLESS=false\n"
        "E2E_ANSWER_TIMEOUT_S=30\n"
        "E2E_TASK_TIMEOUT_S=60.5\n"
        "E2E_DELIVER_TIMEOUT_S=90\n",
    )
    cfg = load_config(env_file, environ={})
    assert cfg.team_id == "T0000000002"
    assert cfg.bot_name == "Pico"
    assert cfg.state_file == os.path.expanduser("~/.pico-e2e/state.json")
    assert not cfg.state_file.startswith("~")
    assert cfg.bot_user == "U0BOT" and cfg.bot_dm == "D0DM" and cfg.me == "U0ME"
    assert cfg.test_channels == ("C01", "C02")
    assert cfg.outside_channel == "C09"
    assert cfg.notify_channel is None
    assert cfg.agent_container == "pico-finops-agent"
    assert cfg.data_copy == os.path.expanduser("~/focus.duckdb")
    assert cfg.report_dir == os.path.expanduser("~/runs")
    assert cfg.headless is False
    assert (cfg.answer_timeout_s, cfg.task_timeout_s, cfg.deliver_timeout_s) == (30.0, 60.5, 90.0)


def test_environ_overrides_the_file(tmp_path):
    env_file = _write(tmp_path, "".join(f"{k}={v}\n" for k, v in _REQUIRED.items()))
    cfg = load_config(env_file, environ={"E2E_BOT_NAME": "Pico", "E2E_HEADLESS": "TRUE"})
    assert cfg.bot_name == "Pico"
    assert cfg.headless is True


def test_missing_required_keys_named_together():
    with pytest.raises(ConfigError) as err:
        load_config(environ={"E2E_SLACK_URL": "https://x.slack.com", "E2E_BOT_NAME": "  "})
    msg = str(err.value)
    for key in ("E2E_TEAM_ID", "E2E_BOT_NAME", "E2E_STATE_FILE"):
        assert key in msg
    assert "E2E_SLACK_URL" not in msg


def test_bad_boolean_or_number_is_a_config_error():
    with pytest.raises(ConfigError, match="E2E_HEADLESS"):
        load_config(environ={**_REQUIRED, "E2E_HEADLESS": "maybe"})
    with pytest.raises(ConfigError, match="E2E_ANSWER_TIMEOUT_S"):
        load_config(environ={**_REQUIRED, "E2E_ANSWER_TIMEOUT_S": "soon"})


def test_missing_env_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "nope.env"), environ=dict(_REQUIRED))


def test_environ_defaults_to_os_environ(monkeypatch):
    for k, v in _REQUIRED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("E2E_BOT_NAME", "Pico")
    assert load_config().bot_name == "Pico"


def test_config_is_frozen_and_holds_no_secrets():
    cfg = load_config(environ=dict(_REQUIRED))
    assert cfg.redact_values() == ()
    with pytest.raises(Exception):
        cfg.bot_name = "other"  # type: ignore[misc]
