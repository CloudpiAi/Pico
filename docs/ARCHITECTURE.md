# Pico FinOps Agent: Architecture

This page is for new contributors. It explains how the code is laid out, what runs where, and how the main flows
work. For setup, see the [setup guide](CUSTOMER-SETUP-GUIDE.md). For security, see the [threat model](THREAT-MODEL.md).

---

## 1. The code areas

The repository has four areas. Each one has a clear job.

| Folder | What it is | Who changes it |
|---|---|---|
| `pico/` | The agent framework. It is a fork of Datus-agent 0.4.1 (see `NOTICE`). It gives the LLM chat loop, tools (SQL, bash), permissions, plugins, and the Slack gateway. | Rarely. Open an issue first (`CONTRIBUTING.md`). |
| `pico_finops_agent/` | The FinOps agent itself: one process that runs the scheduler and the Slack bot. | Most agent changes go here. |
| `plugins/focus/`, `plugins/schedule/` | Two plugin packages. They are installed separately into the image (`uv pip install ./plugins/focus ./plugins/schedule` in the `Dockerfile`). | Data loading and scheduled tasks. |
| `loader/` | The loader Lambda's code and its build script. `pins.json` pins the DuckDB wheel and extensions by SHA-256. | Changes to how export files are loaded. |

### How they relate

- `pico_finops_agent/` uses `pico/` as a library. It wraps a few `pico/` internals at run time instead of editing
  them (for example `ChannelBridge.handle_message` and `BashTool._is_command_allowed`). The internals it relies on
  are pinned by tests, such as `tests/fork/pico_finops_agent/test_agent.py` and `test_slack_guard_pins.py`.
- The plugins are found by `pico/`'s plugin system through their `pico-plugin.yml` manifests. Each one adds a
  `pico <name> ...` command and a skill that the LLM can use.
- `plugins/focus` builds and invokes the Lambda from the zips that `loader/build_zip.py` makes.

### `pico_finops_agent/` module by module

| File | Job |
|---|---|
| `agent.py` | The one process. The main thread is a supervisor: it owns signals, writes the heartbeat and exits for a restart when something is broken too long. It starts the scheduler and, when both Slack tokens are set, Pico's Slack gateway on its own thread. |
| `cli.py` | The `pico-finops-agent` command: `serve` (default), `check` (pre-flight, posts nothing) and `health` (Docker's health check). |
| `scheduler.py` | APScheduler jobs: the load (`FOCUS_SCHEDULE`, default `0 6,18 * * *` UTC), one retry 30 minutes after an error, a 1-minute watch, and the Slack notices and alerts. A load runs as its own `pico focus-data load` process. |
| `slack_guard.py` | Slack answer hardening: history cap, answer deadline, per-person hourly limit, empty-answer retry, reset phrases, and one log line per question (never the question or answer text). |
| `bash_guard.py` | Refuses `$` and backticks outside single quotes in allow-listed bash commands, so secrets in the environment are never expanded. It uses the schedule plugin's guard when that plugin is installed. |
| `llm.py` | Picks the LLM provider and model from `AGENT_LLM_PROVIDER` / `AGENT_LLM_MODEL`. Gemini is the default. |
| `health.py` | The health decision table, the heartbeat file (`health.json` in a private `0700` folder, `<tmp>/pico-finops-agent-<uid>/`) and the check Docker runs. |
| `cache_reconnect.py` | Lets Slack answers see the new local cache after a load without a restart. It closes DuckDB connections when the file's inode changes. |
| `log.py` | One JSON object per log line on stdout. |
| `entrypoint.sh` | Runs before every command. It checks the `.env` values (names only, never values are printed) and stops the start if one is missing or malformed. |
| `iam/` | Copies of the IAM policies in `policies/aws/`, which the agent's failure alerts quote. The trust policy for the EC2 role is only in `policies/aws/`. Change both copies together. |

### The two plugins

| Plugin | Command | What it does |
|---|---|---|
| `plugins/focus` (`pico_plugin_focus_data`) | `pico focus-data load`, `pico focus-data status` | `lambda_ops.py` creates or updates the loader Lambda and invokes it. `loader.py` then rebuilds the local DuckDB cache from the S3 table. `insights.py` works out the numbers for the post-load notice. `prompts/scope.j2` is the system prompt that keeps the LLM on AWS cost questions. |
| `plugins/schedule` (`pico_plugin_schedule`) | `pico schedule add/list/show/...` | Scheduled Slack tasks: reminders, cost reports, saved queries and recurring questions. `slack_ctx.py` signs "who asked and where" for each Slack message. `secret_guard.py` keeps secrets out of task text. `hooks/` holds the bash guard and the Slack gateway hook. |

---

## 2. What runs where

```
+------------------------------- Your AWS account --------------------------------+
|                                                                                  |
|  AWS Billing --> S3 export bucket (FOCUS files, daily)                           |
|                        |                                                         |
|                        v  read                                                   |
|                 Loader Lambda  ---- write ---->  S3 table bucket (cost table)    |
|                 (pico-finops-agent-loader)  \--> S3 state bucket (state.json)    |
|                        ^                                  |                      |
|          create/update |  invoke                          | read (sync)          |
|                        |                                  v                      |
|  EC2 instance -- Docker container "pico-finops-agent" (one process) ----------+  |
|  |  supervisor (heartbeat, restarts)                                          |  |
|  |  scheduler  ---> `pico focus-data load` ---> local DuckDB cache (/data)    |  |
|  |  Slack gateway (Socket Mode) ---> LLM chat loop ---> read-only SQL on cache|  |
|  +----------------------------------------------------------------------------+  |
|                                                                                  |
+------------------------------------------|---------------------|-----------------+
                                           | outbound HTTPS only |
                                           v                     v
                                         Slack           LLM provider
                                                  (Gemini, OpenAI, Claude, DeepSeek)
```

| Component | Where | Notes |
|---|---|---|
| Docker container | The customer's EC2 | One process (`agent.py`). No port is opened. Host networking so the instance role is reachable with IMDSv2 hop limit 1 (`docker-compose.yml`). |
| Loader Lambda `pico-finops-agent-loader` | The customer's account | Created by the agent on first load (`plugins/focus/pico_plugin_focus_data/lambda_ops.py`). Python 3.12, x86_64, 3072 MB, 900 s timeout, reserved concurrency 1. It has no environment variables: every setting travels in the invoke payload. DuckDB comes from the `pico-finops-agent-duckdb` layer. |
| S3 export bucket | The customer's account | AWS Data Exports writes FOCUS 1.2 files here. The agent only reads it, through the Lambda. |
| S3 table bucket (cost table) | The customer's account | An Iceberg table, one month per commit. Only the last `FOCUS_MONTHS` months are kept (default 4). |
| S3 state bucket | The customer's account | `state.json`: which deliveries were loaded. Written by the Lambda with an ETag condition. |
| Local DuckDB cache | `/data/focus.duckdb` in the `agent_data` volume | Rebuilt from the S3 table after a load, to a temp file, then renamed into place. Slack answers query this file only. |
| Slack | External | The bot connects out with Socket Mode (`SLACK_APP_TOKEN`) and posts with the bot token (`SLACK_BOT_TOKEN`). Without both tokens, the agent only collects data. |
| LLM provider | External | Gets the question, the system prompt and the query results it asks for. |

---

## 3. The main flows

### Twice-daily load and digest

1. At each `FOCUS_SCHEDULE` time, `scheduler.py` runs `pico focus-data load` as a child process (no shell, 1 hour
   timeout). At start, it also runs a load when the cache is missing or the last success is older than 12 hours.
2. `plugins/focus/pico_plugin_focus_data/loader.py` takes a lock on `/data/load.lock`, builds the payload from the
   profile, and calls `lambda_ops.ensure_lambda`. That creates the Lambda when it is missing, or updates its code
   when the zip's SHA-256 changed.
3. It invokes the Lambda. The Lambda (`loader/pico_finops_agent_loader.py`) checks every payload field, finds new
   deliveries in the export, and replaces each changed month in the S3 table. When it runs short of time it returns
   `partial`; the agent invokes it again, up to 3 more times.
4. When months were loaded, the agent rebuilds `/data/focus.duckdb` from the S3 table and writes its local
   `state.json`.
5. `scheduler.py` posts the notice to `SLACK_NOTIFY_CHANNEL`: the last full day against the 7-day average, the month
   so far, sudden increases, and new resources. A failed load posts one warning per problem, and a "refreshing again"
   note when it is fixed.
6. Exit codes decide what happens next: 0 is done; 1 (error or conflict) is retried once after 30 minutes; 2
   (bad request, schema change, config error) needs a person and is not retried.

### A question in Slack

1. A person @mentions the bot or sends it a DM. Slack delivers the event over the Socket Mode connection.
2. Pico's `ChannelBridge.handle_message` runs, wrapped twice:
   - `slack_guard` (outermost): rate limit per person, reset phrases, answer deadline, retry on an empty answer.
   - the schedule plugin's `hooks/pico_gateway.py`: puts a signed `[slack ctx=...]` line in front of the text.
3. Pico's chat loop sends the system prompt (`plugins/focus/.../prompts/scope.j2`), recent history (last
   `AGENT_HISTORY_TURNS` turns) and the question to the LLM. It allows at most 15 model/tool rounds
   (`deploy/pico-finops-agent.yml`).
4. The LLM writes SQL. Pico runs it on the `focus` DuckDB datasource, which is read-only and cannot read other files.
   The rows go back to the LLM.
5. The LLM may also run `pico focus-data status|load` or `pico schedule ...` through the bash tool. Nothing else is
   allowed.
6. The LLM writes the answer, and the bridge posts it in Slack.

---

## 4. Where config lives

| File | What is in it |
|---|---|
| `.env` (from `.env.example`) | Every customer value: AWS buckets and ARNs, schedule, alert thresholds, LLM provider and key, Slack tokens. Read by Docker Compose (`env_file`). Never committed, never in the image. |
| `pico_finops_agent/.env.example` | The full list of settings, including optional ones the root `.env.example` doesn't show (`AGENT_HISTORY_TURNS`, `AGENT_ANSWER_DEADLINE_S`, `AGENT_QUESTIONS_PER_HOUR`, `AGENT_SAVE_FAILED_CALLS`, `FOCUS_REPORT_*`). |
| `deploy/pico-finops-agent.yml` | Pico's `agent.yml` profile. It is copied into the image as `/home/pico/.pico/conf/agent.yml`. It holds no secrets: every customer value is a `${VAR}` placeholder. It sets the read-only datasource, the tool permissions, the bash allow-list, the plugins and the Slack channel. |
| `docker-compose.yml` | The container: image, host networking, read-only root, tmpfs mounts, dropped capabilities, resource limits. |
| `docker-compose.build.yml` | Add this to build the image locally instead of pulling it. |
| `policies/aws/` | The two IAM roles (EC2 and loader) and their trust policies, with `<placeholders>`. |

`entrypoint.sh` checks the `.env` values at every start, because Pico turns a missing `${VAR}` into the text
`<MISSING:VAR>` without an error.

---

## 5. Tests

`pytest.ini` sets `testpaths = tests` and `pythonpath = .`.

| Folder | What it covers | Needs |
|---|---|---|
| `tests/fork/pico_finops_agent/` | The agent modules, the focus plugin and the loader (build, overwrite mode, Lambda app). | Nothing external. |
| `tests/fork/schedule/` | The schedule plugin: tasks store, runner, Slack context, secret guard, bash guard. | Nothing external. |
| `tests/fork/test_pico_finops_agent_config.py` | The profile in `deploy/pico-finops-agent.yml`: bash allow-list, read-only datasource, denied tools, no inline secrets. | Nothing external. |
| `tests/fork/e2e_standalone_ui/` | Unit tests for the end-to-end harness itself. | Nothing external. |
| `tests/e2e/standalone_ui/` | A live end-to-end run: Playwright signs in to Slack as a real person and asks the running agent questions. Run by hand with `run.py`; see its `README.md`. | A running agent, real Slack, a real LLM. |

`CONTRIBUTING.md` asks that all of `tests/fork` passes. The markers in `pytest.ini` (`acceptance`, `nightly`,
`integration`, and so on) come from the upstream framework. In `tests/` today only `integration` is used, once.

The CI workflow `.github/workflows/ci.yml` runs the same suite, leaving out the markers that need a real LLM or
Slack:

```bash
uv sync --dev --extra pico-finops-agent
uv pip install --no-deps ./plugins/focus ./plugins/schedule
uv run --no-sync pytest tests/fork -m "not nightly and not product_e2e and not provider_health and not quarantine"
```

The same workflow also runs Ruff, checks the IAM JSON (and that `pico_finops_agent/iam/` matches `policies/aws/`),
and runs a security scan (`pip-audit` and `bandit`).

---

## 6. How the image is built and checked

**The `Dockerfile` has three stages:**

1. `zips`: runs `loader/build_zip.py`, which downloads the DuckDB wheel and the four extensions and checks each one
   against `loader/pins.json`. It makes `duckdb-layer.zip` and `loader.zip`. The zips are reproducible, so the same
   inputs give the same SHA-256.
2. `build`: installs the framework from `uv.lock` (`uv sync --frozen`), then the two plugins. It checks that the
   installed DuckDB version matches `pins.json`, and precompiles all Python, because the container's disk is
   read-only at run time.
3. The runtime stage: a `pico` user (uid 10001), the Lambda zips in `/opt/pico-finops-agent-loader/`, the same
   DuckDB extensions for the local cache, and the profile. No `EXPOSE`. A `HEALTHCHECK` runs
   `pico-finops-agent health`.

The base image is pinned by digest. `.dockerignore` is an allow-list, and it also excludes `.env`, `*.pem` and
`*.key` files everywhere.

**CI (`.github/workflows/docker-build.yml`):**

- On pull requests and `main`, it builds the image with `.github/actions/build-image` (load only, never pushed)
  and runs `.github/actions/build-image/check-image.sh`.
- `check-image.sh` runs numbered checks against the built image. Among them: the focus-data plugin runs without
  `--config`; the agent refuses to start with no settings; the entrypoint, command and health check are as
  expected and no port is declared; Compose uses host networking and publishes no port; no `.env`, key or token-like
  string is in the image; the DuckDB extensions load offline on a read-only filesystem; the health check passes
  only on a fresh healthy heartbeat; the image runs as uid 10001.
- On a release tag `vX.Y.Z` that is on `main`, in this repository only, the publish job pushes the exact image that
  passed the checks to `ghcr.io/cloudpiai/pico-finops-agent`. `:latest` only moves forward.
- Third-party actions are pinned by commit SHA. The workflow's default token is read-only; only the publish job
  gets `packages: write`.
- Other workflows: `ci.yml` (lint, tests, security scan; see section 5), `codeql.yml`, `scorecard.yml` and
  `dco.yml` (every PR commit needs a `Signed-off-by` line). Dependabot (`.github/dependabot.yml`) opens weekly
  update PRs.

To build locally:

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml build
sh .github/actions/build-image/check-image.sh pico-finops-agent:local .
```
