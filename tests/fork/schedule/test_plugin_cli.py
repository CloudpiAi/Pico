"""plugins/schedule is its own plugin: `pico schedule <action>`. Another host needs the report scheduler on
its own, so the plugin must not import the FOCUS plugin or the standalone agent."""
import ast
import io
import json
import re
import shutil
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from pico_plugin_schedule import cli, slack_ctx, tasks

PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins" / "schedule" / "pico_plugin_schedule"
FIXTURE = Path(__file__).parent / "fixtures" / "tasks-2026-11-10.json"
RAVI = "U0RAVI0001"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    monkeypatch.delenv("PICO_FINOPS_AGENT_UNATTENDED", raising=False)
    return tmp_path


def _ctx(d, channel="C0TEAM0001", user=RAVI, msg="1794300000.000001"):
    key = slack_ctx.load_or_create_key(str(d))
    slack_ctx.remember_latest(str(d), channel, "", msg, user)
    return slack_ctx.TAG_RE.search(slack_ctx.make_tag(key, channel, user, "", msg)).group("ctx")


def _run(argv, d):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(argv, {"data_dir": str(d)})
    return rc, json.loads(buf.getvalue())


def test_plugin_imports_nothing_from_focus_or_agent():
    bad = []
    for path in PLUGIN_DIR.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            bad += [f"{path.name}: {n}" for n in names if n.startswith(("pico_plugin_focus_data", "pico_finops_agent"))]
    assert bad == []


def test_manifest_is_valid_for_pico():
    import yaml

    from pico.plugins.base import _MANIFEST_KEYS, read_manifest_file

    raw = yaml.safe_load((PLUGIN_DIR / "pico-plugin.yml").read_text(encoding="utf-8"))
    assert set(raw) <= _MANIFEST_KEYS
    manifest = read_manifest_file(PLUGIN_DIR, "schedule")
    assert manifest is not None and manifest.cli == "pico_plugin_schedule.cli:main"
    # any `pico schedule` command (the CLI refuses unknown actions itself); --config never (live 2026-11-12)
    assert raw["permissions"]["normal"]["allow"] == ["*:*"] and "--config:*" in raw["permissions"]["normal"]["deny"]


def test_entry_point_is_the_schedule_command():
    import tomllib

    data = tomllib.loads((PLUGIN_DIR.parent / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["entry-points"]["pico.plugins"] == {"schedule": "pico_plugin_schedule"}


def test_list_from_cli(data_dir):
    rc, out = _run(["list", "--ctx", _ctx(data_dir)], data_dir)
    assert rc == 0 and out["ok"] is True and out["tasks"] == []


def test_unattended_refuses_every_action(data_dir, monkeypatch):
    monkeypatch.setenv("PICO_FINOPS_AGENT_UNATTENDED", "1")
    for action in ("add", "list", "remove"):
        rc, out = _run([action, "--ctx", "x"], data_dir)
        assert rc == 1 and out["code"] == "not_from_a_scheduled_run"


def test_an_unknown_action_says_which_actions_exist(data_dir, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["create", "--ctx", "x"], {"data_dir": str(data_dir)})
    assert e.value.code == 2
    err = capsys.readouterr().err
    # Python 3.12.8+ lists the choices without quotes ("choose from add, list, ..."); older versions quote them.
    assert "invalid choice: 'create'" in err
    assert re.search(r"choose from '?add'?, '?list'?", err) and re.search(r"'?remove'?", err)


@pytest.mark.parametrize("argv", [
    ["--conf", "/tmp/other.yml", "list", "--ctx", "x"],
    ["--con=/tmp/other.yml", "list", "--ctx", "x"],
    ["list", "--ctx", "x", "--config", "/tmp/other.yml"],
    ["--ctx", "x", "--config", "/tmp/other.yml", "list"],
])
def test_no_form_of_config_reaches_the_plugin(data_dir, argv):
    """Security review 2026-11-12 of allow '*:*': Pico's dispatcher takes only a LEADING, exact `--config` /
    `--config=` (split_plugin_globals; denied by the rules and refused by managed dispatch). Every other spelling or
    position reaches this parser, which has no config option and refuses it."""
    from pico.plugins.runtime_context import split_plugin_globals

    _, config, rest = split_plugin_globals(argv)
    assert config is None  # the dispatcher never picks these up as a config file
    with pytest.raises(SystemExit) as e:
        cli.main(rest, {"data_dir": str(data_dir)})
    assert e.value.code == 2


def test_the_plugin_entry_point_only_runs_the_task_actions():
    """'*:*' opens nothing beyond the 10 task actions: cli.main only calls task_cli.run, whose action is a fixed
    choice and needs a signed --ctx."""
    import inspect
    import textwrap

    fn = ast.parse(textwrap.dedent(inspect.getsource(cli.main))).body[0]
    calls = {ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)}
    assert calls <= {"task_cli.run", "source.load", "profile.get", "dict", "os.environ.get"}, calls


def test_ctx_before_the_action_works_like_after(data_dir):
    shutil.copy(FIXTURE, data_dir / "tasks.json")
    rc, out = _run(["--ctx", _ctx(data_dir, msg="1794300000.000009"), "show", "t3"], data_dir)
    assert rc == 0 and out["task"]["id"] == "t3"


def _skill_commands():
    """Every `pico schedule ...` command the schedule skill shows, as the model would copy it (<ctx> filled in)."""
    import re

    skill = Path(cli.__file__).resolve().parent / "skills" / "schedule" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    found = re.findall(r"^(pico schedule [^\n]+)$", text, re.M) + re.findall(r"`(pico schedule [^`]+)`", text)
    return [c.replace("<ctx>", "'D0.U0.0.1_1.ab'").strip() for c in found]


def test_every_command_in_the_skill_passes_the_allow_list_and_the_guard():
    """The model copies the skill's examples; an example the guard refuses teaches it a command that stops the answer
    (live 2026-11-12: --question "...more than $1,000,000,000,000..." in double quotes)."""
    _require_installed()
    from pico.plugins.registry import collect_plugin_cli_permissions, invalidate_plugin_cache
    from pico.tools.permission.bash_rules import evaluate_bash_command
    from pico_plugin_schedule.hooks.bash_guard import expands_outside_single_quotes

    invalidate_plugin_cache()
    try:
        rules = collect_plugin_cli_permissions({"schedule"})["normal"]
    finally:
        invalidate_plugin_cache()
    commands = _skill_commands()
    assert len(commands) >= 8
    for cmd in commands:
        assert not expands_outside_single_quotes(cmd), cmd
        got = evaluate_bash_command(cmd, rules)
        assert str(getattr(got.level, "value", got.level)).lower() == "allow", (cmd, got.reason)


def test_skill_values_are_single_quoted_and_the_action_comes_first():
    import re

    skill = (Path(cli.__file__).resolve().parent / "skills" / "schedule" / "SKILL.md").read_text(encoding="utf-8")
    assert not re.search(r'--(text|question|sql|service|name|at|cron|in) "', skill), "a double-quoted value"
    assert "pico schedule --ctx" not in skill
    low = skill.lower()
    assert "the action word comes right after `pico schedule`" in low
    assert "never run `--help`, `date`, `python3` or any other command" in low


def test_focus_data_has_no_task_command(tmp_path):
    from pico_plugin_focus_data import cli as focus_cli

    with pytest.raises(SystemExit) as e:
        focus_cli.main(["task", "list"], {"cache_path": str(tmp_path / "focus.duckdb")})
    assert e.value.code == 2


def test_existing_tasks_json_is_read(data_dir):
    """Tasks written by the image before the split (t1-t7, 2026-11-10) keep working after the upgrade."""
    shutil.copy(FIXTURE, data_dir / "tasks.json")
    assert [t["id"] for t in tasks.active_tasks(str(data_dir))] == ["t7"]
    rc, out = _run(["list", "--ctx", _ctx(data_dir)], data_dir)
    assert rc == 0 and {t["id"] for t in out["tasks"]} >= {"t7"}
    rc, out = _run(["show", "t3", "--ctx", _ctx(data_dir, msg="1794300000.000002")], data_dir)
    assert rc == 0 and out["task"]["kind"] == "ask" and out["task"]["status"] == "paused"




def _require_installed():
    from pico.plugins.registry import plugin_entry_point_exists

    if not plugin_entry_point_exists("schedule"):
        pytest.skip("plugin not installed: uv pip install -e ./plugins/schedule")


@pytest.mark.parametrize("cmd,decision", [
    ("pico schedule list --ctx x", "allow"),
    ("pico schedule add --ctx x --kind reminder --to me --text hi", "allow"),
    ("pico schedule remove t3 --ctx x", "allow"),
    ("pico schedule --config /tmp/other.yml list --ctx x", "deny"),
    ("pico schedule --config=/tmp/other.yml list --ctx x", "deny"),
    # Live 2026-11-12: the model sometimes puts --ctx before the action or asks for --help; both were refused and the
    # whole Slack answer stopped ("Sorry, I couldn't answer that just now").
    ("pico schedule --ctx 'D0.U0.0.1_1.ab' remove t67", "allow"),
    ("pico schedule --ctx 'D0.U0.0.1_1.ab' list", "allow"),
    ("pico schedule --ctx x add --kind reminder --to me --text 'hi'", "allow"),
    ("pico schedule --help", "allow"),
    ("pico schedule --ctx x --config /tmp/other.yml list", "deny"),
    ("date", "ask"),  # still not allowed: the skill says it isn't needed
    # Live 2026-11-12 15:47: the model made up `pico schedule create ... --target me` without reading the skill. Any
    # `pico schedule` command runs; the CLI itself refuses an unknown action with the list of real ones, so the model
    # can correct itself instead of the answer stopping. The checks that matter (signed ctx, owner rules) are in it.
    ("pico schedule create --ctx x --kind report --target me", "allow"),
    ("pico schedule add t --help", "allow"),
    ("pico schedule list --ctx x; rm -rf /tmp/x", "ask"),  # a chained second command is judged on its own
    ("pico schedule list --ctx x && date", "ask"),
])
def test_permission_decisions(cmd, decision):
    _require_installed()
    from pico.plugins.registry import collect_plugin_cli_permissions, invalidate_plugin_cache
    from pico.tools.permission.bash_rules import evaluate_bash_command

    invalidate_plugin_cache()
    try:
        rules = collect_plugin_cli_permissions({"schedule"})["normal"]
    finally:
        invalidate_plugin_cache()
    got = evaluate_bash_command(cmd, rules)
    assert str(getattr(got.level, "value", got.level)).lower() == decision, (cmd, got)
