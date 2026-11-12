"""The one-time sign-in helper (tests/e2e/standalone_ui/login.py): arguments and the saved session file."""

import os
import stat
import sys

import pytest

from tests.e2e.standalone_ui import login


def test_parse_args_defaults():
    args = login.parse_args([])
    assert args.env is None
    assert args.timeout_s == 600.0


def test_parse_args_env_and_timeout():
    args = login.parse_args(["--env", "e2e.env", "--timeout-s", "30"])
    assert (args.env, args.timeout_s) == ("e2e.env", 30.0)


class FakeContext:
    def __init__(self):
        self.mode_at_write = None

    def storage_state(self, path):
        self.mode_at_write = stat.S_IMODE(os.stat(path).st_mode)
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"cookies": [], "origins": []}')


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_save_state_writes_a_600_file_and_creates_the_folder(tmp_path):
    dest = tmp_path / "state" / "slack.json"
    ctx = FakeContext()
    assert login.save_state(ctx, str(dest)) == str(dest)
    assert dest.read_text(encoding="utf-8").startswith("{")
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert ctx.mode_at_write == 0o600  # never readable by others, not even while being written


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_save_state_tightens_an_existing_file(tmp_path):
    dest = tmp_path / "slack.json"
    dest.write_text("old", encoding="utf-8")
    dest.chmod(0o644)
    login.save_state(FakeContext(), str(dest))
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert "cookies" in dest.read_text(encoding="utf-8")


def test_save_state_expands_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    out = login.save_state(FakeContext(), "~/s.json")
    assert out == str(tmp_path / "s.json")
    assert (tmp_path / "s.json").exists()
