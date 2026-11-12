"""The agent's allow-listed bash never expands a variable.

Allow-listed commands run with `bash -c`, so `"$SLACK_BOT_TOKEN"` would become the token before `pico schedule` sees it.
pico_finops_agent.bash_guard refuses `$` and backticks outside single quotes in commands that an allow-list restricts
(the only place bash leaves them alone), so `'over $200'` still works. No change under pico/."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "plugins" / "schedule"))
from pico_finops_agent import bash_guard  # noqa: E402

pytest.importorskip("pico.tools.func_tool.bash_tool")
from pico.tools.func_tool.bash_tool import BashTool  # noqa: E402

PATTERNS = ["pico:focus-data", "pico:schedule"]


@pytest.fixture
def tool(tmp_path):
    bash_guard.install()
    return BashTool(workspace_root=str(tmp_path), allowed_patterns=PATTERNS)


@pytest.mark.parametrize("cmd", [
    'pico schedule add --ctx x --kind reminder --to me --text "$SLACK_BOT_TOKEN"',
    "pico schedule add --ctx x --kind reminder --to me --text $SLACK_BOT_TOKEN",
    'pico schedule add --ctx x --kind ask --question "over $1, list them"',
    "pico schedule add --ctx x --kind reminder --name 'ok' --text \"$GEMINI_API_KEY\"",
    'pico schedule add --ctx x --text "`id`"',
    "pico schedule add --ctx x --text 'a'$X'b'",
    'pico focus-data load --force $X',
])
def test_expansion_outside_single_quotes_is_refused(tool, cmd):
    assert not tool._is_command_allowed(cmd)


@pytest.mark.parametrize("cmd", [
    "pico schedule add --ctx x --kind ask --question 'If EC2 is over $200 send me the resources'",
    "pico schedule add --ctx x --kind reminder --to me --text 'pay the $5 fee' --name 'Fee'",
    "pico schedule list --ctx C1.U1.1.1.sig",
    "pico focus-data status",
    'pico schedule add --ctx x --kind reminder --to me --text "stand-up at 10"',
])
def test_plain_and_single_quoted_commands_still_run(tool, cmd):
    assert tool._is_command_allowed(cmd)


def test_unrestricted_tools_are_not_touched(tmp_path):
    """Only allow-listed tools are guarded; an agent node with ["*"] keeps Pico's own rules."""
    bash_guard.install()
    assert BashTool(workspace_root=str(tmp_path), allowed_patterns=["*"])._is_command_allowed("echo $HOME")


def test_install_is_idempotent(tool):
    bash_guard.install()
    bash_guard.install()
    assert not tool._is_command_allowed('pico schedule list --ctx "$X"')


def test_agent_installs_the_guard_at_start(monkeypatch):
    from pico_finops_agent import agent

    calls = []
    monkeypatch.setattr(bash_guard, "install", lambda: calls.append("guard"))
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    for name in ("_install_cache_reconnect", "_configure_logging"):
        monkeypatch.setattr(agent, name, lambda: None)
    monkeypatch.setattr(agent, "_install_signal_handlers", lambda stop: None)
    monkeypatch.setattr(agent.scheduler, "build_scheduler", lambda: "S")
    monkeypatch.setattr(agent.scheduler, "start_scheduler", lambda s=None: "S")
    monkeypatch.setattr(agent.scheduler, "stop_scheduler", lambda s: None)
    monkeypatch.setattr(agent, "supervise", lambda *a, **k: 0)
    assert agent.main(["--config", "x.yml"]) == 0
    assert calls == ["guard"]


def test_the_guard_comes_from_the_schedule_plugin():
    from pico_plugin_schedule.hooks import bash_guard as plugin_guard

    assert bash_guard.expands_outside_single_quotes is plugin_guard.expands_outside_single_quotes
