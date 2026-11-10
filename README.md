# Pico FinOps Agent

Ask about your AWS costs in Slack, and get a short note when something changes:

> **@Pico FinOps Agent** what did I spend on AWS in October?
> **@Pico FinOps Agent** which service grew the most this week?

The agent runs on one EC2 server in **your own AWS account**. It reads your AWS cost export (FOCUS), keeps its copy of
the data in your own S3, and answers with the language model you choose: Google Gemini (default), OpenAI,
Anthropic Claude or DeepSeek. There's no hosted service and no database to run.

**Set it up:** [docs/CUSTOMER-SETUP-GUIDE.md](docs/CUSTOMER-SETUP-GUIDE.md) (about 45 minutes, step by step, with a check after each step).

## What's in this repo

| Path | What it is |
|---|---|
| `pico_finops_agent/` | The agent: scheduler, Slack bot, start-up check (`entrypoint.sh`), the `pico-finops-agent serve / check / health` command, the LLM choice (`llm.py`) |
| `loader/` | The loader Lambda the agent creates in your account |
| `plugins/focus/` | The `focus-data` plugin: loads the cost data and answers questions about it |
| `deploy/pico-finops-agent.yml` | The agent's Pico profile (in the image: `~/.pico/conf/agent.yml`) |
| `pico/` | The Pico framework the agent runs on, a fork of [Datus-agent](https://github.com/datus-ai/datus-agent) 0.4.1 |
| `Dockerfile`, `docker-compose.yml`, `.env.example` | Build and run the agent |
| `policies/aws/` | The IAM policies for the agent's two roles |
| `tests/` | The agent's tests |

## Build and test

Build the image (Linux, x86_64, Docker with the compose plugin):

```bash
docker compose build          # pico-finops-agent:local
```

Run the tests (Linux, Python 3.12, [uv](https://docs.astral.sh/uv/)):

```bash
uv sync --frozen --extra pico-finops-agent --no-install-project
uv pip install --python .venv/bin/python --no-deps ./plugins/focus
.venv/bin/python -m pytest tests/fork -o addopts="" -q
```

## Licence

Apache License 2.0: see [LICENSE](LICENSE) and [NOTICE](NOTICE). Copyright 2026 PurpleData Inc. (CloudPi).
