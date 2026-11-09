"""pico_finops_agent.llm: which LLM the agent uses, chosen in .env (AGENT_LLM_PROVIDER / AGENT_LLM_MODEL).

Gemini (gemini-3.8-flash) stays the default; OpenAI, Anthropic Claude and DeepSeek are the other choices.
The entrypoint's start-time check and `pico-finops-agent check` test the key of the provider chosen here.
"""
from pathlib import Path

import pytest
import yaml

from pico_finops_agent import llm

REPO_ROOT = Path(__file__).resolve().parents[3]
CATALOG = REPO_ROOT / "pico" / "conf" / "providers.yml"
ENTRYPOINT = REPO_ROOT / "pico_finops_agent" / "entrypoint.sh"


def test_default_is_gemini_flash():
    name, provider, model = llm.selected({})
    assert (name, provider.pico, provider.key_env, model) == ("gemini", "gemini", "GEMINI_API_KEY", "gemini-3.8-flash")


@pytest.mark.parametrize("name,pico,key_env", [
    ("gemini", "gemini", "GEMINI_API_KEY"),
    ("openai", "openai", "OPENAI_API_KEY"),
    ("anthropic", "claude", "ANTHROPIC_API_KEY"),
    ("deepseek", "deepseek", "DEEPSEEK_API_KEY"),
])
def test_each_provider(name, pico, key_env):
    got_name, provider, model = llm.selected({"AGENT_LLM_PROVIDER": name})
    assert (got_name, provider.pico, provider.key_env) == (name, pico, key_env)
    assert model == provider.default_model


def test_claude_is_another_name_for_anthropic():
    name, provider, _ = llm.selected({"AGENT_LLM_PROVIDER": "claude"})
    assert name == "anthropic" and provider.pico == "claude"


def test_model_override():
    _, _, model = llm.selected({"AGENT_LLM_PROVIDER": "openai", "AGENT_LLM_MODEL": "gpt-5.4-mini"})
    assert model == "gpt-5.4-mini"


def test_empty_values_mean_the_default():
    name, _, model = llm.selected({"AGENT_LLM_PROVIDER": "", "AGENT_LLM_MODEL": ""})
    assert (name, model) == ("gemini", "gemini-3.8-flash")


@pytest.mark.parametrize("bad", ["kimi", "Gemini", "open ai", "../x"])
def test_unknown_provider_refused(bad):
    with pytest.raises(ValueError, match="AGENT_LLM_PROVIDER"):
        llm.selected({"AGENT_LLM_PROVIDER": bad})


def test_providers_exist_in_picos_catalog():
    """Pico builds the model from its catalog (type, base_url, key variable): every choice must be in it."""
    catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))["providers"]
    for name, provider in llm.PROVIDERS.items():
        assert provider.pico in catalog, name
        assert catalog[provider.pico]["api_key_env"] == provider.key_env, name
        if name != "gemini":  # Gemini's default is the agent's own choice (gemini-3.8-flash)
            assert provider.default_model == catalog[provider.pico]["default_model"], name


def test_entrypoint_knows_the_same_providers():
    text = ENTRYPOINT.read_text(encoding="utf-8")
    for name in [*llm.PROVIDERS, *llm.ALIASES]:
        assert name in text, f"entrypoint.sh doesn't accept AGENT_LLM_PROVIDER={name}"
    for provider in llm.PROVIDERS.values():
        assert provider.key_env in text


def test_probes_put_the_key_in_a_header_only():
    for name, provider in llm.PROVIDERS.items():
        assert provider.probe_url.startswith("https://"), name
        headers = provider.headers("SECRET-KEY")
        assert any("SECRET-KEY" in v for v in headers.values()), name
        assert "SECRET-KEY" not in provider.probe_url
