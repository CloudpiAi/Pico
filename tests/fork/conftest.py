"""Fixtures for fork-specific tests (the Pico FinOps Agent's pico fork)."""
import pytest

from pico.configuration.agent_config import AgentConfig


@pytest.fixture
def mock_agent_config(tmp_path):
    """Minimal real AgentConfig with bash ENABLED (upstream default).

    Mirrors the kwargs used by tests/unit_tests/conftest.py::_create_real_agent_config,
    trimmed to what ChatTaskManager.start_chat needs before the run loop.
    """
    return AgentConfig(
        nodes={},
        home=str(tmp_path),
        target="mock",
        models={
            "mock": {
                "type": "openai",
                "api_key": "mock-api-key",
                "model": "mock-model",
                "base_url": "http://localhost:0",
            },
        },
        services={"datasources": {}, "semantic_layer": {}, "bi_platforms": {}, "schedulers": {}},
        project_root=str(tmp_path),
        storage={},
        agentic_nodes={},
    )
