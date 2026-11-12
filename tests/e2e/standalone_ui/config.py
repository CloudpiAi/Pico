"""Settings for the Slack end-to-end test, read from an .env-style file and the environment.

Only names and ids live here (no secrets): the sign-in is a saved browser session at `E2E_STATE_FILE`. The setting
names are in `.env.example`. The file format is simple: `KEY=VALUE` lines, `#` comments, blank lines, and an optional
pair of single or double quotes around the value.
"""

import os
from dataclasses import dataclass
from typing import Mapping, Optional

REQUIRED = ("E2E_SLACK_URL", "E2E_TEAM_ID", "E2E_BOT_NAME", "E2E_STATE_FILE")
_PATHS = ("E2E_STATE_FILE", "E2E_DATA_COPY", "E2E_REPORT_DIR")
_DEFAULT_REPORT_DIR = "~/.pico-e2e/runs"
_TIMEOUTS = {"E2E_ANSWER_TIMEOUT_S": 180.0, "E2E_TASK_TIMEOUT_S": 240.0, "E2E_DELIVER_TIMEOUT_S": 480.0}


class ConfigError(Exception):
    """A setting is missing or cannot be read."""


@dataclass(frozen=True)
class Config:
    slack_url: str
    team_id: str
    bot_name: str
    bot_user: Optional[str]
    bot_dm: Optional[str]
    me: Optional[str]
    test_channels: tuple[str, ...]
    outside_channel: Optional[str]
    notify_channel: Optional[str]
    state_file: str
    agent_container: Optional[str]
    data_copy: Optional[str]
    report_dir: str
    headless: bool
    answer_timeout_s: float
    task_timeout_s: float
    deliver_timeout_s: float

    def redact_values(self) -> tuple[str, ...]:
        """Values to mask in logs and reports: none, Config holds only names and ids."""
        return ()


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _read_env_file(path: str) -> dict[str, str]:
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        raise ConfigError(f"cannot read the settings file {path}: {exc}") from exc
    values: dict[str, str] = {}
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ConfigError(f"{path} line {number}: expected KEY=VALUE")
        values[key] = _unquote(value.strip())
    return values


def _bool(key: str, value: str) -> bool:
    low = value.strip().lower()
    if low in ("true", "1", "yes"):
        return True
    if low in ("false", "0", "no"):
        return False
    raise ConfigError(f"{key} must be true or false, got {value!r}")


def _float(key: str, value: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number of seconds, got {value!r}") from exc


def load_config(env_file: Optional[str] = None, environ: Optional[Mapping[str, str]] = None) -> Config:
    """Read the `E2E_*` settings from `env_file` (if given), then let `environ` (default `os.environ`) override."""
    values: dict[str, str] = _read_env_file(env_file) if env_file else {}
    env = os.environ if environ is None else environ
    values.update({k: v for k, v in env.items() if k.startswith("E2E_")})

    def get(key: str) -> Optional[str]:
        value = (values.get(key) or "").strip()
        if not value:
            return None
        return os.path.expanduser(value) if key in _PATHS else value

    missing = [key for key in REQUIRED if get(key) is None]
    if missing:
        raise ConfigError("missing required setting(s): " + ", ".join(missing))

    channels = tuple(c.strip() for c in (get("E2E_TEST_CHANNELS") or "").split(",") if c.strip())
    headless = get("E2E_HEADLESS")
    timeouts = {key: _float(key, get(key)) if get(key) else default for key, default in _TIMEOUTS.items()}
    return Config(
        slack_url=get("E2E_SLACK_URL"),
        team_id=get("E2E_TEAM_ID"),
        bot_name=get("E2E_BOT_NAME"),
        bot_user=get("E2E_BOT_USER"),
        bot_dm=get("E2E_BOT_DM"),
        me=get("E2E_ME"),
        test_channels=channels,
        outside_channel=get("E2E_OUTSIDE_CHANNEL"),
        notify_channel=get("E2E_NOTIFY_CHANNEL"),
        state_file=get("E2E_STATE_FILE"),
        agent_container=get("E2E_AGENT_CONTAINER"),
        data_copy=get("E2E_DATA_COPY"),
        report_dir=get("E2E_REPORT_DIR") or os.path.expanduser(_DEFAULT_REPORT_DIR),
        headless=True if headless is None else _bool("E2E_HEADLESS", headless),
        answer_timeout_s=timeouts["E2E_ANSWER_TIMEOUT_S"],
        task_timeout_s=timeouts["E2E_TASK_TIMEOUT_S"],
        deliver_timeout_s=timeouts["E2E_DELIVER_TIMEOUT_S"],
    )
