"""Every plugin skill's SKILL.md header is read by Pico's own parser (pico/tools/skill_tools/skill_registry.py).

focus-data's description had an unquoted `: ` ("... (those are answered from the table guide in the system
prompt): "when was ..."), so Pico logged "Invalid YAML" on every request and loaded 24 skills without it; the
`status_data` question ("is the cost data up to date?") then had no skill to follow.
"""
from pathlib import Path

import pytest

from pico.tools.skill_tools.skill_registry import SkillRegistry

ROOT = Path(__file__).resolve().parents[3]
SKILLS = sorted(ROOT.glob("plugins/*/pico_plugin_*/skills/*/SKILL.md"))


def test_the_plugin_skills_are_found():
    names = {p.parent.name for p in SKILLS}
    assert {"focus-data", "schedule"} <= names


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_skill_header_parses(path):
    meta = SkillRegistry._parse_skill_file(SkillRegistry.__new__(SkillRegistry), path)
    assert meta is not None, f"Pico can't read {path}"
    assert meta.name == path.parent.name
    assert meta.description and "when was the cost data last loaded" in meta.description \
        if meta.name == "focus-data" else meta.description
