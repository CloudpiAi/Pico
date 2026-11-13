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


# ---------- quoted metacharacters in `pico schedule` (Pico's check ignores quotes) ----------
# Pico refuses any ; & < > | in a command, quoted or not, so the model's `--question '...; if not, stay quiet'` and
# `--sql '... >= ...'` were refused and the Slack answer stopped (sch_ask_not_met, sch_query, 2026-11-13). Inside
# single quotes bash takes every character literally, so for `pico schedule` those are allowed; nothing else changes.

from pico.tools.permission import bash_rules  # noqa: E402
from pico.tools.permission.bash_rules import BashCommandRules  # noqa: E402

PLUGIN_RULES = BashCommandRules(allow=["pico schedule:*"])
QUOTED_OK = [
    "pico schedule add --ctx 'C1.U1.1_1.sig' --kind ask --in '2 minutes' --question 'Check October; if it was more "
    "than $1,000, report it; if not, send nothing.' --name 'Big bill'",
    "pico schedule add --ctx x --kind query --cron '0 17 * * 5' --sql 'SELECT servicename FROM focus WHERE "
    "chargeperiodstart >= current_date - 7 AND billedcost > 0' --name 'Top 5'",
    "pico schedule add --ctx x --kind reminder --to me --text 'tea & biscuits <3' --name 'Tea'",
    "pico schedule add --ctx x --kind ask --question 'Is issue #4 fixed; tell me' --name 'Issue 4'",
]
STILL_REFUSED = [
    "pico schedule list --ctx x; rm -rf /tmp/x",  # a real second command
    "pico schedule list --ctx x > /tmp/out",  # a real redirection
    "pico schedule add --ctx x --text \"a'\" ; rm -rf /tmp/x ; \"'\"",  # a ' inside double quotes starts no quote
    r"pico schedule add --ctx x --text $'a\';rm -rf /tmp/x;'",  # ANSI-C quoting: \' does not end it
    r"pico schedule add --ctx x --text 'a' \; rm -rf /tmp/x",  # backslash: keep the strict check
    "pico schedule add --ctx x --text 'unbalanced; rm x",
    "pico schedule add --ctx x --kind ask --question \"report it; if not, stay quiet\"",  # double quotes: as before
    "pico schedule add --ctx x --text '$(id)'; $(id)",
    # security review 2026-11-13: bash ignores quotes inside a # comment, so a ' after # opens no quote there and
    # the next line runs. Any newline or CR, or a # outside single quotes, keeps the strict check.
    "pico schedule add x #'\nrm -rf /tmp/x\n#'",
    "pico schedule add x #'\necho $(id) INJ\n#'",
    "pico schedule add --ctx x --text 'a' #'; rm -rf /tmp/x'",
    "pico schedule add --ctx x --kind ask --question 'line one\nline two' --name 'two lines'",
    "pico schedule add --ctx x --question 'a\rb; c'",
    "pico schedule add x #'; rm /'",
    # security review 2026-11-13 (2): inside $[ ] / $(( )) single quotes don't quote, so '$(id)' would run
    "pico schedule add --text $[ '$(id)' ]",
    "pico schedule add --text $[ '`id`' ]",
    "pico schedule add --text $(( '$(id)' ))",
    "pico schedule add --text ${X:-'$(id)'}",
    "pico focus-data status 'a;b'",  # only `pico schedule`
    "echo 'a;b'",
]


@pytest.mark.parametrize("cmd", QUOTED_OK)
def test_quoted_metacharacters_in_pico_schedule_pass_both_checks(tool, cmd):
    decision = bash_rules.evaluate_bash_command(cmd, PLUGIN_RULES)
    assert decision.level == "allow" or getattr(decision.level, "value", None) == "allow", decision.reason
    assert tool._is_command_allowed(cmd)


@pytest.mark.parametrize("cmd", STILL_REFUSED)
def test_anything_else_keeps_picos_strict_check(tool, cmd):
    decision = bash_rules.evaluate_bash_command(cmd, PLUGIN_RULES)
    assert not (decision.level == "allow" or getattr(decision.level, "value", None) == "allow"), cmd
    assert not tool._is_command_allowed(cmd)


@pytest.mark.parametrize("cmd,expect", [
    ("pico schedule add --question 'a; b' --name 'n'", "pico schedule add --question '' --name ''"),
    ("pico schedule list", "pico schedule list"),
    ("pico schedule add --text \"it's\" --q 'x>y'", "pico schedule add --text \"it's\" --q ''"),
    (r"pico schedule add --text 'a\'", None),
    ("pico schedule add --text $'x'", None),
    ("pico schedule add --text 'open", None),
    ("pico schedule add --text \"open", None),
    ("pico schedule add --text 'a\nb'", None),
    ("pico schedule add x #'a'", None),
    # no relaxation for these either (Pico's own check then decides; neither runs a second command in bash)
    ("pico schedule add \"a\" #'x'", None),
    ("pico schedule add 'a'\rrm -rf /tmp/x", None),
    ("pico schedule add --text 'issue #4; urgent'", "pico schedule add --text ''"),
])
def test_single_quoted_spans_removed(cmd, expect):
    from pico_plugin_schedule.hooks import bash_guard as plugin_guard

    assert plugin_guard.single_quoted_removed(cmd) == expect


def test_quote_aware_check_is_installed_once(tool):
    from pico.tools.func_tool import bash_tool

    first = (bash_rules.contains_shell_metachars, bash_tool.contains_shell_metachars)
    bash_guard.install()
    assert (bash_rules.contains_shell_metachars, bash_tool.contains_shell_metachars) == first
    assert bash_rules.contains_shell_metachars("echo a; b")  # Pico's own answer for everything else


@pytest.mark.parametrize("cmd", [
    "pico schedule add --text $[ '$(id)' ]",
    "pico schedule add --text $(( '$(id)' ))",
    "pico schedule add --text ${X:-'a;b'}",
])
def test_permission_layer_alone_refuses_an_expansion_around_a_quote(cmd):
    """Each layer stands on its own: the permission check doesn't rely on the allow-list's $ guard."""
    bash_guard.install()
    decision = bash_rules.evaluate_bash_command(cmd, PLUGIN_RULES)
    assert not (decision.level == "allow" or getattr(decision.level, "value", None) == "allow"), cmd
