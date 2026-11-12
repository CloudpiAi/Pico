"""The command line: python -m tests.e2e.standalone_ui.run (tests/e2e/standalone_ui/run.py)."""

import contextlib
import glob
import json
import os
import subprocess

import pytest

from tests.e2e.standalone_ui import run
from tests.e2e.standalone_ui.ground_truth import SnapshotError
from tests.e2e.standalone_ui.runner import SignedOut
from tests.fork.e2e_standalone_ui.test_runner import Bot, FakePage, Resp

SECRET = "SECRETCOOKIE-xoxd-123"


class Truth:
    closed = False

    def __init__(self, path):
        self.path = path

    def vars(self):
        return {"prev_month_name": "October 2026", "prev_month_start": "2026-10-01 00:00:00",
                "top_service": "Amazon EC2"}

    def value(self, sql):
        return 10.0

    def close(self):
        Truth.closed = True


@pytest.fixture
def env(tmp_path):
    state = tmp_path / "state.json"
    state.write_text('{"cookies": [{"name": "d", "value": "%s"}]}' % SECRET, encoding="utf-8")
    data = tmp_path / "focus.duckdb"
    data.write_bytes(b"duck")
    path = tmp_path / "e2e.env"
    path.write_text(
        "E2E_SLACK_URL=https://x.slack.com\nE2E_TEAM_ID=T1\nE2E_BOT_NAME=Pico FinOps Agent\n"
        f"E2E_STATE_FILE={state.as_posix()}\nE2E_DATA_COPY={data.as_posix()}\n"
        f"E2E_REPORT_DIR={(tmp_path / 'runs').as_posix()}\nE2E_AGENT_CONTAINER=pico-finops-agent\n"
        "E2E_NOTIFY_CHANNEL=\nE2E_TEST_CHANNELS=\n",
        encoding="utf-8")
    return tmp_path, str(path)


def _launcher(bot=None, seen=None, raise_on_open=None):
    @contextlib.contextmanager
    def launch(cfg, *, headless):
        if seen is not None:
            seen["headless"] = headless
        page = FakePage(bot or Bot())
        if raise_on_open:
            def bad(*a, **k):
                raise raise_on_open
            page.open = page.open_bot_dm = bad
        yield page
    return launch


def _docker(*a, **k):
    return subprocess.CompletedProcess(a, 0, stdout="pico-finops-agent:nightly sha256:abc\n", stderr="")


def _main(argv, **kw):
    kw.setdefault("truth_factory", Truth)
    kw.setdefault("docker", _docker)
    return run.main(argv, **kw)


def _report(tmp):
    (path,) = glob.glob(os.path.join(str(tmp), "runs", "*", "report.json"))
    return path, json.load(open(path, encoding="utf-8"))


def test_list_needs_no_config_browser_or_session(capsys, tmp_path):
    rc = run.main(["--list", "--env", str(tmp_path / "missing.env")], launch=None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "qa_greeting" in out and "sch_channel_post_soon" in out and "qa_new" not in out


def test_list_honours_only_and_case(capsys):
    assert run.main(["--list", "--only", "refusal", "--case", "qa_help"]) == 0
    out = capsys.readouterr().out
    assert "ref_web" in out and "qa_help" in out and "qa_greeting" not in out


def test_missing_settings_is_exit_2(capsys, tmp_path):
    bad = tmp_path / "bad.env"
    bad.write_text("E2E_TEAM_ID=T1\n", encoding="utf-8")
    assert _main(["--env", str(bad)], launch=_launcher()) == 2
    assert "E2E_SLACK_URL" in capsys.readouterr().out


def test_missing_env_file_is_exit_2(tmp_path):
    assert _main(["--env", str(tmp_path / "nope.env")], launch=_launcher()) == 2


def test_missing_session_is_exit_2_and_names_the_login_script(capsys, env):
    tmp, path = env
    os.remove(tmp / "state.json")
    assert _main(["--env", path], launch=_launcher()) == 2
    assert "login" in capsys.readouterr().out


def test_a_failed_data_copy_is_exit_2(capsys, env):
    tmp, path = env
    text = open(path, encoding="utf-8").read().replace(f"E2E_DATA_COPY={(tmp / 'focus.duckdb').as_posix()}",
                                                      "E2E_DATA_COPY=")
    open(path, "w", encoding="utf-8").write(text)

    def snap(container, dest):
        raise SnapshotError("docker cp failed: no such container")
    assert _main(["--env", path], launch=_launcher(), snapshot_fn=snap) == 2
    assert "no such container" in capsys.readouterr().out


def test_a_passing_run_is_exit_0_with_reports_and_no_session_content(capsys, env):
    tmp, path = env
    seen = {}
    rc = _main(["--env", path, "--case", "qa_greeting", "--case", "qa_prev_total"],
               launch=_launcher(Bot(rules=[("hi", Resp(texts=("Hi! Ask me about your AWS cost.",)))]), seen))
    assert rc == 0, capsys.readouterr().out
    assert seen["headless"] is True
    rep_path, rep = _report(tmp)
    assert {c["id"] for c in rep["cases"]} == {"qa_greeting", "qa_prev_total"}
    assert rep["meta"]["agent_image"] == "pico-finops-agent:nightly sha256:abc"
    assert rep["meta"]["config"]["team_id"] == "T1"
    run_dir = os.path.dirname(rep_path)
    for name in ("report.json", "report.html", "run.log"):
        body = open(os.path.join(run_dir, name), encoding="utf-8").read()
        assert SECRET not in body and "state.json" not in body
    out = capsys.readouterr().out
    assert "report.html" in out and "PASS" in out
    assert Truth.closed


def test_a_failing_check_is_exit_1(env):
    tmp, path = env
    bot = Bot(rules=[("hi", Resp(texts=("(No response generated)",)))])
    assert _main(["--env", path, "--case", "qa_greeting"], launch=_launcher(bot)) == 1


def test_an_expired_session_is_exit_2(capsys, env):
    _, path = env
    rc = _main(["--env", path, "--case", "qa_greeting"], launch=_launcher(raise_on_open=SignedOut("sign-in page")))
    assert rc == 2
    assert "login" in capsys.readouterr().out


def test_headed_shows_the_browser(env):
    _, path = env
    seen = {}
    _main(["--env", path, "--case", "qa_greeting", "--headed"], launch=_launcher(seen=seen))
    assert seen["headless"] is False


def test_default_env_prefers_the_local_file(tmp_path):
    local = tmp_path / ".env"
    assert run.default_env(str(tmp_path), home=str(tmp_path / "home")) is None
    home_env = tmp_path / "home" / ".pico-e2e" / "e2e.env"
    home_env.parent.mkdir(parents=True)
    home_env.write_text("", encoding="utf-8")
    assert run.default_env(str(tmp_path), home=str(tmp_path / "home")) == str(home_env)
    local.write_text("", encoding="utf-8")
    assert run.default_env(str(tmp_path), home=str(tmp_path / "home")) == str(local)
