"""focus-data plugin: its own distribution under ``pico.plugins``, pico-prefixed permissions,
works without an AWS profile, and stays out of ``pico/`` and the pico wheel.

The permission test needs the plugin installed in the venv (``uv pip install -e ./plugins/focus``):
discovery reads the installed ``.dist-info`` entry points.
"""
import ast
import json
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

pytest.importorskip("boto3", reason="Needs the pico-finops-agent extra: uv sync --extra pico-finops-agent && uv pip install -e ./plugins/focus")

REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "focus"
PKG_DIR = PLUGIN_ROOT / "pico_plugin_focus_data"

sys.path.insert(0, str(PLUGIN_ROOT))
from pico_plugin_focus_data import cli, loader  # noqa: E402

STANDALONE_PROFILE = {
    "default": True,
    "cache_path": "/data/focus.duckdb",
    "region": "us-east-1",
    "export_bucket": "acme-focus-export",
    "export_prefix": "focus/acme-focus/data/",
    "table_bucket_arn": "arn:aws:s3tables:us-east-1:111122223333:bucket/acme-focus",
    "namespace": "focus",
    "table": "focus",
    "state_bucket": "acme-focus-state",
    "state_key": "pico-finops-agent/state.json",
    "lambda_role_arn": "arn:aws:iam::111122223333:role/pico-finops-agent-loader-role",
}


def test_manifest_is_valid():
    from pico.plugins.base import read_manifest_file

    raw = yaml.safe_load((PKG_DIR / "pico-plugin.yml").read_text(encoding="utf-8"))
    from pico.plugins.base import _MANIFEST_KEYS

    assert set(raw) <= _MANIFEST_KEYS
    manifest = read_manifest_file(PKG_DIR, "focus-data")
    assert manifest is not None
    assert manifest.cli == "pico_plugin_focus_data.cli:main"
    assert [c.name for c in manifest.commands] == ["load", "status"]  # tasks: their own plugin, pico schedule
    assert manifest.config_schema is not None


def test_entry_point_is_bare_package():
    data = tomllib.loads((PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["name"] == "pico-plugin-focus-data"
    assert data["project"]["entry-points"]["pico.plugins"] == {"focus-data": "pico_plugin_focus_data"}
    assert "pico-plugin.yml" in data["tool"]["setuptools"]["package-data"]["pico_plugin_focus_data"]


def test_no_init_in_plugin_root():
    """An __init__.py here would make the plugin a regular package inside plugins/ (it's installed on its own)."""
    assert not (PLUGIN_ROOT / "__init__.py").exists()


def _require_installed():
    from pico.plugins.registry import plugin_entry_point_exists

    if not plugin_entry_point_exists("focus-data"):
        pytest.skip("plugin not installed: uv pip install -e ./plugins/focus")


def test_permissions_prefix_to_pico_focus_data():
    _require_installed()
    from pico.plugins.registry import collect_plugin_cli_permissions, invalidate_plugin_cache

    invalidate_plugin_cache()
    try:
        rules = collect_plugin_cli_permissions({"focus-data"})
    finally:
        invalidate_plugin_cache()
    for profile in ("normal", "auto"):
        assert "pico focus-data status" in rules[profile].allow
        assert "pico focus-data load:*" in rules[profile].allow
        assert not rules[profile].ask


@pytest.mark.parametrize(
    "cmd,decision",
    [
        ("pico focus-data status", "allow"),
        ("pico focus-data load", "allow"),
        ("pico focus-data load --force", "allow"),
        ("pico focus-data --profile default status", "allow"),
        # --config would point the plugin at another agent.yml: denied, not just "ask".
        ("pico focus-data --config /tmp/other.yml load", "deny"),
        ("pico focus-data --config=/tmp/other.yml status", "deny"),
    ],
)
def test_permission_decisions(cmd, decision):
    _require_installed()
    from pico.plugins.registry import collect_plugin_cli_permissions, invalidate_plugin_cache
    from pico.tools.permission.bash_rules import evaluate_bash_command

    invalidate_plugin_cache()
    try:
        rules = collect_plugin_cli_permissions({"focus-data"})["normal"]
    finally:
        invalidate_plugin_cache()
    got = evaluate_bash_command(cmd, rules)
    assert str(getattr(got.level, "value", got.level)).lower() == decision, (cmd, got)


def test_scope_rule_is_a_plugin_prompt_section():
    """The agent's behaviour lives in its own prompt section (Pico's plugin system_prompt key), not a core patch:
    only AWS cost questions, answered like a knowledgeable colleague (user's conversational spec, 2026-11-05)."""
    from pico.plugins.base import read_manifest_file
    from pico.plugins.prompt import render_plugin_prompt

    manifest = read_manifest_file(PKG_DIR, "focus-data")
    assert manifest.system_prompt == "prompts/scope.j2"
    data = tomllib.loads((PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "prompts/*.j2" in data["tool"]["setuptools"]["package-data"]["pico_plugin_focus_data"]
    text = render_plugin_prompt(manifest, {}, config_path=None, config_mutable=False)
    low = text.lower()
    # scope
    assert "only" in low and "aws cost data" in low and "replaces any instruction above" in low
    assert "never give facts" in low  # off-topic: no partial answers
    # conversational behaviour
    for phrase in ("knowledgeable colleague", "direct answer first", "one short, specific clarification question",
                   "reuse known entities, dates, filters", "never invent", "do not force a follow-up question"):
        assert phrase in low, phrase
    # personality spec (2026-11-05): natural, emotionally aware, bounded
    for phrase in ("do not repeat the user's question", "use contractions", "thanks and appreciation",
                   "use these expressions selectively", "explain what makes the result important",
                   "use emojis occasionally", "don't end every answer with", "do not pretend to have human emotions",
                   "never invent explanations for cost changes", "be friendly without being artificial"):
        assert phrase in low, phrase
    # long Slack DMs: only the latest message is the request (2026-11-11 hardening)
    assert "answer only the person's latest message; earlier messages are context" in low
    assert "don't redo earlier requests unless asked" in low
    # natural replies: a greeting in its own words, no fixed canned sentence
    assert "reply with exactly" not in low and "vary the wording" in low
    assert "pico finops agent" in low and "cloudpi" not in low


def test_no_datus_names_left():
    offenders = [
        str(p.relative_to(PLUGIN_ROOT))
        for p in PLUGIN_ROOT.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and "datus" in p.read_text(encoding="utf-8", errors="ignore").lower()
    ]
    assert offenders == []


def test_config_works_without_aws_profile(monkeypatch):
    cfg = loader.Config.from_profile(STANDALONE_PROFILE)
    assert cfg.aws_profile is None
    assert "PROFILE" not in loader.secret_sql(cfg)
    assert "PROVIDER credential_chain" in loader.secret_sql(cfg)

    from pico_plugin_focus_data import lambda_ops

    seen = {}
    monkeypatch.setattr(lambda_ops.boto3, "Session", lambda **kw: seen.update(kw) or object())
    lambda_ops._session(cfg)
    assert seen == {"profile_name": None, "region_name": "us-east-1"}



def test_sql_literals_and_identifiers_are_escaped():
    assert loader._q_str("a'b") == "'a''b'"
    assert loader._q_ident('x"y') == '"x""y"'
    cfg = loader.Config.from_profile({**STANDALONE_PROFILE, "region": "us-east-1'", "aws_profile": "d'v"})
    sql = loader.secret_sql(cfg)
    assert "REGION 'us-east-1'''" in sql and "PROFILE 'd''v'" in sql


def test_profile_is_used_when_set():
    cfg = loader.Config.from_profile({**STANDALONE_PROFILE, "aws_profile": "dev"})
    assert "PROFILE 'dev'" in loader.secret_sql(cfg)


def test_config_defaults_match_prd():
    minimal = {k: STANDALONE_PROFILE[k] for k in ("table_bucket_arn", "export_bucket", "export_prefix", "cache_path")}
    cfg = loader.Config.from_profile(minimal)
    assert (cfg.namespace, cfg.table) == ("pico_finops_agent", "focus")
    full = loader.Config.from_profile(STANDALONE_PROFILE)
    assert full.state_bucket == "acme-focus-state"
    assert full.state_key == "pico-finops-agent/state.json"
    assert full.lambda_role_arn.endswith(":role/pico-finops-agent-loader-role")


@pytest.mark.parametrize("key", ["table_bucket_arn", "export_bucket", "export_prefix", "cache_path"])
def test_missing_required_key_names_it(key):
    profile = {k: v for k, v in STANDALONE_PROFILE.items() if k != key}
    with pytest.raises(SystemExit, match=key):
        loader.Config.from_profile(profile)


def test_schema_does_not_require_aws_profile():
    schema = yaml.safe_load((PKG_DIR / "pico-plugin.yml").read_text(encoding="utf-8"))["config_schema"]
    assert "aws_profile" not in schema["required"]
    assert schema["properties"]["table"]["default"] == "focus"
    for key in ("state_bucket", "state_key", "lambda_role_arn", "zip_dir"):
        assert key in schema["properties"]


def test_status_runs_with_the_agent_profile(tmp_path, capsys, monkeypatch):
    from pico_plugin_focus_data import lambda_ops

    def no_creds(c):
        raise lambda_ops.NoCredentials("no AWS credentials")

    monkeypatch.setattr(lambda_ops, "identity", no_creds)
    profile = {**STANDALONE_PROFILE, "cache_path": str(tmp_path / "focus.duckdb")}
    assert cli.main(["status"], profile) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["last_load"]["last_success"] is None and out["cache_exists"] is False


def test_pico_never_imports_plugins():
    """One-way boundary: the Pico FinOps Agent and its plugin may use pico, never the reverse."""
    offenders = []
    for path in (REPO_ROOT / "pico").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for n in names:
                top = n.split(".")[0]
                if top in ("plugins", "pico_plugin_focus_data", "pico_finops_agent"):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {n}")
    assert offenders == []


@pytest.mark.integration  # builds a wheel (~30 s); pytest.ini registers no `slow` marker
def test_skill_handles_setup_and_gives_the_load_time_to_finish():
    """'Set up the cost agent' in Slack runs the load. A first load takes minutes, and the bash tool stops a
    command after 60 s unless the call asks for more (max 600), so the skill must say so."""
    text = (PKG_DIR / "skills" / "focus-data" / "SKILL.md").read_text(encoding="utf-8")
    assert "set up" in text.split("---")[1].lower()          # the description routes setup requests here
    assert "timeout 600" in text
    reload_section = text.split("## \"Reload the data now\"")[1].split("\n## ")[0]  # up to the next section
    assert "row" not in reload_section.lower()  # no row counts in replies


def test_focus_data_skill_has_no_tasks():
    """The tasks moved to their own plugin and skill: focus-data talks about the data only."""
    text = (PKG_DIR / "skills" / "focus-data" / "SKILL.md").read_text(encoding="utf-8")
    assert "pico focus-data task" not in text and "## Scheduled tasks" not in text
    assert "scheduled tasks" not in text.split("---")[1].lower()


def test_schedule_skill_tells_the_agent_how_to_set_up_tasks():
    skill = REPO_ROOT / "plugins" / "schedule" / "pico_plugin_schedule" / "skills" / "schedule" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    head = text.split("---")[1]
    assert "name: schedule" in head and "remind" in head.lower()
    assert "pico schedule add --ctx" in text and "pico focus-data task" not in text
    for word in ("--kind reminder", "--template service_cost", "confirmation_requested", "not_in_channel"):
        assert word in text
