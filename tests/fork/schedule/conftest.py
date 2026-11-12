"""plugins/schedule tests: the plugin (and, for the FOCUS source, plugins/focus) importable without an install."""
import sys
from pathlib import Path

import pytest

pytest.importorskip("apscheduler", reason="Needs the pico-finops-agent extra")

REPO_ROOT = Path(__file__).resolve().parents[3]
for sub in ("schedule", "focus"):
    p = str(REPO_ROOT / "plugins" / sub)
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(autouse=True)
def _no_slack_retry_wait(monkeypatch):
    """Slack calls wait between tries on temporary trouble; tests don't."""
    from pico_plugin_schedule import slack_api

    monkeypatch.setattr(slack_api, "_sleep", lambda s: None)
