"""Which LLM the agent uses, chosen in .env.

    AGENT_LLM_PROVIDER   gemini (default) | openai | anthropic (or claude) | deepseek
    AGENT_LLM_MODEL      optional; the provider's default model when unset

The Slack agent gets this choice on every start (pico_finops_agent/agent.py); nothing is saved to disk. The
entrypoint's start-time check (entrypoint.sh, same names) and `pico-finops-agent check` (cli.py) test the key
of the provider chosen here. Each `probe_url` lists models with the key in a header: no tokens are spent.
"""
import os
from dataclasses import dataclass
from typing import Callable, Dict


@dataclass(frozen=True)
class Provider:
    label: str  # what a customer reads
    pico: str  # Pico's provider id (pico/conf/providers.yml)
    key_env: str
    default_model: str
    probe_url: str
    headers: Callable[[str], Dict[str, str]]


PROVIDERS = {
    "gemini": Provider("Google Gemini", "gemini", "GEMINI_API_KEY", "gemini-3.8-flash",
                       "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
                       lambda key: {"x-goog-api-key": key}),
    "openai": Provider("OpenAI", "openai", "OPENAI_API_KEY", "gpt-5.5",
                       "https://api.openai.com/v1/models",
                       lambda key: {"Authorization": f"Bearer {key}"}),
    "anthropic": Provider("Anthropic Claude", "claude", "ANTHROPIC_API_KEY", "claude-sonnet-4-6",
                          "https://api.anthropic.com/v1/models?limit=1",
                          lambda key: {"x-api-key": key, "anthropic-version": "2023-06-01"}),
    "deepseek": Provider("DeepSeek", "deepseek", "DEEPSEEK_API_KEY", "deepseek-v4-pro",
                         "https://api.deepseek.com/models",
                         lambda key: {"Authorization": f"Bearer {key}"}),
}
ALIASES = {"claude": "anthropic"}
DEFAULT = "gemini"


def selected(env=None):
    """(name, Provider, model) from AGENT_LLM_PROVIDER / AGENT_LLM_MODEL; empty or unset = the default."""
    env = os.environ if env is None else env
    raw = env.get("AGENT_LLM_PROVIDER") or DEFAULT
    name = ALIASES.get(raw, raw)
    if name not in PROVIDERS:
        choices = ", ".join([*PROVIDERS, *ALIASES])
        raise ValueError(f"AGENT_LLM_PROVIDER must be one of {choices}")
    provider = PROVIDERS[name]
    return name, provider, env.get("AGENT_LLM_MODEL") or provider.default_model
