"""A secret never reaches Slack through a task.

The agent's bash runs allow-listed commands with `bash -c`, so `--text "$SLACK_BOT_TOKEN"` arrives at `pico schedule`
already expanded. The command refuses any text holding a secret's value, and the runner checks again before posting."""
import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

import pytest

from pico_plugin_schedule import cli, runner, secret_guard, slack_api, slack_ctx, tasks

from .test_slack_api import FakeSlack

RAVI = "U0RAVI0001"
BOT = "xoxb-test-only-not-a-real-token1"  # fake; the guard compares with the environment, not a shape
GEMINI = "AIzaSyD-not-a-real-key-0123456789"


@pytest.fixture
def secrets_env(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", BOT)
    monkeypatch.setenv("GEMINI_API_KEY", GEMINI)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    monkeypatch.setenv("FOCUS_REGION", "us-east-1")       # not a secret
    monkeypatch.setenv("SHORT_KEY", "abc")                  # too short to match ordinary text
    monkeypatch.delenv("PICO_FINOPS_AGENT_UNATTENDED", raising=False)


def _ctx(d, msg):
    key = slack_ctx.load_or_create_key(str(d))
    slack_ctx.remember_latest(str(d), "D0RAVI0001", "", msg, RAVI)
    return slack_ctx.TAG_RE.search(slack_ctx.make_tag(key, "D0RAVI0001", RAVI, "", msg)).group("ctx")


def _run(argv, d):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(argv, {"data_dir": str(d)})
    return rc, json.loads(buf.getvalue())


def test_secret_values_come_from_secret_names_only(secrets_env):
    vals = secret_guard.secret_values()
    assert BOT in vals and GEMINI in vals and "us-east-1" not in vals and "abc" not in vals


@pytest.mark.parametrize("text", [f"token is {BOT}", BOT, f"key={GEMINI} please", f"xx{BOT}xx"])
def test_text_with_a_secret_is_found(secrets_env, text):
    assert secret_guard.contains_secret(text)


@pytest.mark.parametrize("text", ["stand-up at 10", "EC2 in us-east-1", "abc", "", None])
def test_ordinary_text_is_not_a_secret(secrets_env, text):
    assert not secret_guard.contains_secret(text)


@pytest.mark.parametrize("flag,kind", [("--text", "reminder"), ("--question", "ask"), ("--name", "reminder")])
def test_add_refuses_a_secret_in_any_text(secrets_env, tmp_path, flag, kind):
    argv = ["add", "--ctx", _ctx(tmp_path, "1794300000.000001"), "--kind", kind, "--to", "me",
            "--at", "2099-01-01 10:00", "--text", "hi", "--question", "q?", "--name", "n"]
    argv[argv.index(flag) + 1] = f"remind me {BOT}"
    rc, out = _run(argv, tmp_path)
    assert rc == 1 and out["code"] == "secret_in_text"
    assert BOT not in json.dumps(out)                                 # the refusal never repeats it
    assert not (tmp_path / "tasks.json").exists() or tasks.list_for(str(tmp_path), user=RAVI, channel="x") == []


def test_edit_refuses_a_secret(secrets_env, tmp_path):
    t = tasks.add(str(tmp_path), owner=RAVI, where="D0RAVI0001", kind="reminder", name="n",
                  target={"type": "user", "id": RAVI}, schedule={"cron": "0 9 * * *", "tz": "UTC"},
                  payload={"text": "hi"}, now=datetime.now(timezone.utc), settings=tasks.Settings())
    rc, out = _run(["edit", t["id"], "--ctx", _ctx(tmp_path, "1794300000.000002"), "--text", GEMINI], tmp_path)
    assert rc == 1 and out["code"] == "secret_in_text"
    assert tasks.get(str(tmp_path), t["id"])["payload"]["text"] == "hi"


def test_runner_never_posts_a_secret(secrets_env, tmp_path, monkeypatch, capsys):
    """Second layer: an `ask` answer (or anything else) holding a secret is not posted; the task pauses."""
    now = datetime(2026, 11, 10, 7, 0, tzinfo=timezone.utc)
    fake = FakeSlack(conversations_open=lambda p: {"ok": True, "channel": {"id": "D0" + p["users"][2:]}},
                     chat_postMessage={"ok": True, "ts": "1.1"})
    runner.configure(data_dir=str(tmp_path), slack_client_factory=lambda: slack_api.Client("xoxb-x", transport=fake))
    monkeypatch.setattr(runner, "_utcnow", lambda: now)
    monkeypatch.setattr(runner, "_HELD", {})
    monkeypatch.setattr(runner.kinds, "render", lambda t, *a, **k: (f"the key is {GEMINI}", None))
    t = tasks.add(str(tmp_path), owner=RAVI, where="D0RAVI0001", kind="ask", name="n",
                  target={"type": "user", "id": RAVI}, schedule={"cron": "0 9 * * *", "tz": "UTC"},
                  payload={"question": "q"}, now=now - timedelta(hours=1), settings=tasks.Settings())
    runner.run_task(t["id"])
    posted = [p["text"] for m, p in fake.calls if m == "chat.postMessage"]
    assert all(GEMINI not in x for x in posted)                       # never posted, not even in the pause DM
    assert tasks.get(str(tmp_path), t["id"])["status"] == "paused"
    assert GEMINI not in capsys.readouterr().out                      # nor logged


# ---------- review of a28a924: wider names, partial and case-changed copies, every argument ----------

@pytest.mark.parametrize("name,value", [
    ("SLACK_SIGNING_SECRET_V2", "s3cr3t-signing-value-123"),      # SECRET in the middle
    ("DB_CREDENTIALS", "cred-value-abcdef-123"),
    ("ANTHROPIC_API_KEY_BACKUP", "sk-ant-backup-0123456789"),
])
def test_secret_names_anywhere_in_the_name(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    assert secret_guard.contains_secret(f"x {value} y")


def test_password_inside_a_url_is_a_secret(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://app:Pa55w0rd-xyz-77@db:3306/pidb")
    assert secret_guard.contains_secret("try Pa55w0rd-xyz-77 tomorrow")


@pytest.mark.parametrize("text", [
    BOT[3:24],                                 # a long piece of the token
    BOT.upper(),                                                # case changed
    " ".join(BOT),                                              # spaced out
    BOT.replace("-", "."),                                      # separators changed
])
def test_partial_or_disguised_copies_are_found(secrets_env, text):
    assert secret_guard.contains_secret(text)


@pytest.mark.parametrize("flag", ["--service", "--template", "--account", "--region", "--tz", "--to"])
def test_add_checks_every_argument(secrets_env, tmp_path, flag):
    argv = ["add", "--ctx", _ctx(tmp_path, "1794300000.000003"), "--kind", "report", "--to", "me",
            "--at", "2099-01-01 10:00", "--template", "t", "--service", "s", "--account", "a", "--region", "r",
            "--tz", "UTC", "--name", "n"]
    argv[argv.index(flag) + 1] = BOT
    rc, out = _run(argv, tmp_path)
    assert rc == 1 and out["code"] == "secret_in_text" and BOT not in json.dumps(out)


def test_an_s3_key_path_is_not_a_secret(monkeypatch):
    """FOCUS_STATE_KEY is an S3 object key (a path), not a credential: 'Pico FinOps Agent' in a task stays allowed."""
    monkeypatch.setenv("FOCUS_STATE_KEY", "pico-finops-agent/state.json")
    assert not secret_guard.contains_secret("Set up by the Pico FinOps Agent state check")


# ---------- review of a2eed16: fail closed on *_KEY; the ask child is guarded and has no secrets ----------

def test_any_key_name_is_a_secret_except_known_paths(monkeypatch):
    monkeypatch.setenv("SOME_VENDOR_KEY", "vendor-key-value-123456")
    monkeypatch.setenv("FOCUS_STATE_KEY", "pico-finops-agent/state.json")   # an S3 object key (a path), exempt by name
    vals = secret_guard.secret_values()
    assert "vendor-key-value-123456" in vals and "pico-finops-agent/state.json" not in vals


def test_ask_child_runs_through_the_guard_without_secrets(monkeypatch):
    import sys as _sys

    from pico_plugin_schedule import kinds

    for k, v in {"SLACK_BOT_TOKEN": BOT, "SLACK_APP_TOKEN": "xapp-1-abcdefghijk", "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE12345",
                 "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENGEXAMPLE", "AWS_SESSION_TOKEN": "FwoGZXIvYXdzEXAMPLE",
                 "HMAC_SECRET_KEY": "hmac-secret-value-123", "GEMINI_API_KEY": GEMINI}.items():
        monkeypatch.setenv(k, v)
    seen = {}

    def runner(cmd, **kw):
        seen.update(cmd=cmd, env=kw["env"])
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with pytest.raises(kinds.RenderError):  # no answer in the fake output: that's fine here
        kinds._ask("q", runner)
    assert seen["cmd"][:4] == [_sys.executable, "-m", "pico_plugin_schedule.hooks.ask_child", "-p"]
    env = seen["env"]
    for gone in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                 "HMAC_SECRET_KEY"):
        assert gone not in env, gone
    assert env["GEMINI_API_KEY"] == GEMINI and env["PICO_FINOPS_AGENT_UNATTENDED"] == "1"   # its LLM key stays


def test_ask_child_installs_the_bash_guard():
    pytest.importorskip("pico.tools.func_tool.bash_tool")
    from pico.tools.func_tool.bash_tool import BashTool
    from pico_plugin_schedule.hooks import ask_child

    ask_child.prepare()
    t = BashTool(workspace_root="/tmp", allowed_patterns=["pico:focus-data"])
    assert not t._is_command_allowed('pico focus-data status "$GEMINI_API_KEY"')
    assert t._is_command_allowed("pico focus-data status")
