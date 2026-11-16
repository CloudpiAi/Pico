"""A question stopped at its deadline stops the commands it started and says what it had already done.

Live 2026-11-13: the deadline replied "That took too long" at 11:08:43, then a `pico schedule add` still running in
the bash tool's worker thread saved task t223 at 11:08:46. The person had a weekly task they were told failed."""

import subprocess
import sys
import time

import pytest

from pico_finops_agent import slack_guard

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="process groups")


def test_too_long_text_without_changes_is_unchanged():
    assert slack_guard.too_long_text([]) == slack_guard.TOO_LONG


def test_too_long_text_names_what_was_already_done():
    text = slack_guard.too_long_text([
        {"task_id": "t223", "action": "created", "name": "Weekly AWS cost summary"},
        {"task_id": "t222", "action": "removed", "name": "Weekly AWS cost summary"},
        {"task_id": "t223", "action": "created", "name": "Weekly AWS cost summary"},
    ])
    assert text.startswith(slack_guard.TOO_LONG)
    assert "saved task t223 (Weekly AWS cost summary); removed task t222 (Weekly AWS cost summary)." in text


def test_commands_are_recorded_only_inside_an_attempt(monkeypatch):
    from pico.tools.func_tool import bash_tool

    real = bash_tool.subprocess
    monkeypatch.setattr(bash_tool, "subprocess", real)
    slack_guard.install_child_tracking()
    slack_guard.install_child_tracking()  # idempotent
    view = bash_tool.subprocess
    assert view is not real and view.PIPE == subprocess.PIPE and view.TimeoutExpired is subprocess.TimeoutExpired
    outside = view.Popen([sys.executable, "-c", "pass"])
    outside.wait()
    children = set()
    token = slack_guard._CHILDREN.set(children)
    try:
        inside = view.Popen([sys.executable, "-c", "pass"])
        inside.wait()
    finally:
        slack_guard._CHILDREN.reset(token)
    assert children == {inside}
    assert subprocess.Popen is real.Popen  # nobody else's subprocess changed


@posix_only
def test_kill_children_stops_the_whole_group():
    proc = subprocess.Popen(["sh", "-c", "sleep 30 & sleep 30"], start_new_session=True)
    time.sleep(0.2)
    assert slack_guard.kill_children({proc}) == 1
    assert proc.wait(timeout=5) != 0
    done = subprocess.Popen(["true"])
    done.wait()
    assert slack_guard.kill_children({done}) == 0  # already finished: nothing to stop
