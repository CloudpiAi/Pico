# Pico FinOps Agent: Threat Model

A lightweight, STRIDE-style threat model of the agent as it ships in this repository. It lists what we protect,
where trust changes, the main threats, and the mitigations that exist today, with the file that implements each.

**How to read this page:**

- **Verified** means we found the mitigation in the code or config named next to it.
- **Setup step** means the customer does it by following the [setup guide](CUSTOMER-SETUP-GUIDE.md). The agent
  doesn't enforce it.
- **Open** means a known gap or residual risk. Section 6 lists them all.

See [ARCHITECTURE.md](ARCHITECTURE.md) for how the parts fit together.

---

## 1. Assets

| Asset | Where it lives | Why it matters |
|---|---|---|
| AWS role credentials (EC2 instance role) | Fetched from the instance metadata service (IMDS) by boto3 and DuckDB | Can create, update and invoke the loader Lambda, pass the loader role, and read the cost table and `state.json` (`policies/aws/ec2-role-policy.json`). |
| Loader role credentials | Inside the Lambda only | Can read the export, write the cost table and write `state.json` (`policies/aws/loader-role-policy.json`). |
| Slack bot token (`xoxb-`) and app token (`xapp-`) | `.env`, then the container's environment | Post as the bot, read channels the bot is in, open the Socket Mode connection. |
| LLM API key (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` or `DEEPSEEK_API_KEY`) | `.env`, then the container's environment | Spends money on the customer's LLM account. |
| Cost data | S3 export bucket, S3 table, `/data/focus.duckdb` | Spend by service, account, resource and tag. Business-sensitive. |
| Slack signing key `slack-ctx.key` | `/data` volume, mode 0600 (`plugins/schedule/pico_plugin_schedule/slack_ctx.py`) | Proves who asked for a scheduled task and where. |
| Scheduled tasks `tasks.json` | `/data` volume | Decide what the bot posts, where and when. |

---

## 2. Trust boundaries

```
  Slack users --(1)--> Slack --(Socket Mode, outbound)--> [ container on EC2 ] --(2)--> LLM provider
                                                               |    |
                                                          (3)  |    | (5) host: Docker, IMDS, /data volume
                                                               v
                                          AWS APIs: Lambda, IAM PassRole, S3, S3 Tables
                                                               |
                                                          (4)  v
                                          Loader Lambda --> export bucket, table bucket, state bucket
```

1. **Slack users -> agent.** Anyone who can message the bot in the workspace. Their text is untrusted input to the
   LLM.
2. **Agent -> LLM provider.** The provider sees the system prompt, the conversation and the query results.
3. **Agent -> AWS APIs and S3.** The container acts with the EC2 instance role.
4. **The loader Lambda.** It runs with its own role and takes a payload built by the agent.
5. **The container host.** The EC2 instance, its Docker daemon, IMDS and the `agent_data` volume.

---

## 3. Mitigations that exist today

### Container and host

| Mitigation | Where | Status |
|---|---|---|
| Read-only root filesystem; writable paths are tmpfs with `noexec,nosuid,nodev` | `docker-compose.yml` (`read_only: true`, `tmpfs`) | Verified |
| All Linux capabilities dropped; `no-new-privileges` | `docker-compose.yml` (`cap_drop: [ALL]`, `security_opt`) | Verified |
| Runs as a non-root user, uid 10001 | `Dockerfile` (`useradd --uid 10001`, `USER pico`); checked by `check-image.sh` check 10 | Verified |
| No port is opened or published; Slack is reached outbound | `Dockerfile` (no `EXPOSE`), `docker-compose.yml` (no `ports:`); checked by `check-image.sh` checks 5 and 6 | Verified |
| CPU, memory and PID limits (default 2 CPUs, 3 GB, 512 PIDs) | `docker-compose.yml` (`deploy.resources.limits`) | Verified |
| Log size capped (5 files of 50 MB) | `docker-compose.yml` (`logging`) | Verified |
| No secret or `.env` in the image | `.dockerignore` (allow-list, excludes `.env`, `*.pem`, `*.key`); `check-image.sh` check 7 scans for token-like strings | Verified |
| IMDSv2 required, hop limit 1. With host networking the agent still reaches IMDS, but containers on Docker's bridge network cannot. | `docker-compose.yml` comment, setup guide step 6 | Setup step |
| No inbound security group rules; Session Manager instead of SSH | Setup guide step 6 | Setup step |

### Slack input (Spoofing, Tampering, Denial of service)

| Mitigation | Where | Status |
|---|---|---|
| Per-person limit of questions per hour (`AGENT_QUESTIONS_PER_HOUR`, default 30, 0 turns it off) | `pico_finops_agent/slack_guard.py` (`RateLimiter`, keyed by Slack sender id) | Verified |
| Answer deadline (`AGENT_ANSWER_DEADLINE_S`, default 180 s, range 30-900): a longer run is stopped | `pico_finops_agent/slack_guard.py` | Verified |
| History cap (`AGENT_HISTORY_TURNS`, default 8): the model sees only recent turns | `pico_finops_agent/slack_guard.py` (`trim_to_last_turns`) | Verified |
| At most 15 model/tool rounds per answer | `deploy/pico-finops-agent.yml` (`agentic_nodes.chat.max_turns: 15`) | Verified |
| Raw error text is never posted to Slack; it becomes a fixed sorry message | `pico_finops_agent/slack_guard.py` | Verified |
| In a channel thread, only the person who started it can reset it | `pico_finops_agent/slack_guard.py` (`RESET_NOT_YOURS`) | Verified |
| Bad `AGENT_*` values are refused at start | `pico_finops_agent/entrypoint.sh` | Verified |
| A system prompt keeps the LLM on AWS cost questions | `plugins/focus/pico_plugin_focus_data/prompts/scope.j2` | Verified (a prompt, not a hard control) |
| Slack users can't change the server's config or approve out-of-workspace file access | `pico_finops_agent/agent.py` (`config_mutable = False`, `filesystem_strict = True`) | Verified |

### What the LLM can do (Elevation of privilege, Information disclosure)

The LLM drives tools, so its tool rights are the real boundary.

| Mitigation | Where | Status |
|---|---|---|
| One datasource, the local DuckDB file, opened read-only | `deploy/pico-finops-agent.yml` (`read_only: true`); `pico/tools/db_tools/duckdb_connector.py` passes `read_only` to `duckdb.connect` | Verified |
| No statement that writes, on any datasource | `deploy/pico-finops-agent.yml` (`sql_read_only: true`); used by `pico/tools/func_tool/database.py` | Verified in config; enforcement code in `pico/` not reviewed line by line |
| Agent SQL can't read other files or URLs | `deploy/pico-finops-agent.yml` (`enable_external_access: false`); `duckdb_connector.py` runs `SET enable_external_access=false` | Verified |
| File tools and web fetch denied | `deploy/pico-finops-agent.yml` (`permissions.rules`: `filesystem_tools` and `web_tool` deny; `filesystem.strict: true`) | Verified in config |
| Bash allowed only for `pico focus-data` and `pico schedule` | `deploy/pico-finops-agent.yml` (`bash.allowed_patterns`); pinned by `tests/fork/test_pico_finops_agent_config.py` | Verified |
| `--config` can't be passed to either plugin command | `plugins/focus/.../pico-plugin.yml`, `plugins/schedule/.../pico-plugin.yml` (`deny` rules) | Verified |
| `pico focus-data` allows only `status` and `load` | `plugins/focus/pico_plugin_focus_data/pico-plugin.yml` | Verified |
| No `$` or backtick expansion outside single quotes in allow-listed bash, so `"$SLACK_BOT_TOKEN"` is never expanded | `pico_finops_agent/bash_guard.py`, `plugins/schedule/pico_plugin_schedule/hooks/bash_guard.py` | Verified |
| Refused bash commands are logged, with the signed `--ctx` value redacted | `plugins/schedule/pico_plugin_schedule/hooks/bash_guard.py` (`log_denials`) | Verified |

### Scheduled tasks (Spoofing, Repudiation, Information disclosure)

| Mitigation | Where | Status |
|---|---|---|
| Each Slack message gets an HMAC-signed `[slack ctx=channel.user.thread.message.sig]` line; any such line the user typed is removed first | `plugins/schedule/pico_plugin_schedule/hooks/pico_gateway.py`, `slack_ctx.py` (`strip_tags`, `hmac.compare_digest`) | Verified |
| A ctx value only works for the newest message of its conversation, so it can't be replayed | `slack_ctx.py` (`is_current`), `task_cli.py` | Verified |
| Only a task's creator (or an admin) changes it; posting to a channel needs `FOCUS_REPORT_CREATORS`; channels can be limited with `FOCUS_REPORT_CHANNELS` | `plugins/schedule/pico_plugin_schedule/tasks.py`, `entrypoint.sh` | Verified |
| Task history records who created, changed or stopped a task | `pico schedule history` (`task_cli.py`, `tasks.py`) | Verified |
| A task's text is refused when it holds a secret value from the environment (also a 12-character piece of one) | `plugins/schedule/pico_plugin_schedule/secret_guard.py`, used in `task_cli.py` | Verified |
| A task's final text is checked again before it is posted | `plugins/schedule/pico_plugin_schedule/runner.py` (`contains_secret`) | Verified |
| A scheduled `ask` runs in a child process without the Slack tokens, and with `PICO_FINOPS_AGENT_UNATTENDED=1` it can't manage tasks | `plugins/schedule/pico_plugin_schedule/kinds.py` (`_ask_env`), `task_cli.py` | Verified |
| `@channel`, `@here`, `@everyone` and user groups are removed from posts, except a reminder's own text when `FOCUS_REPORT_ALLOW_MENTIONS=true` | `plugins/schedule/pico_plugin_schedule/kinds.py` | Verified from the module docstring; filter code not reviewed |
| A daily cap on scheduled asks (`FOCUS_ASK_MAX_PER_DAY`, default 1000) | `plugins/schedule/pico_plugin_schedule/tasks.py` | Verified |

### AWS access (Elevation of privilege, Tampering)

| Mitigation | Where | Status |
|---|---|---|
| No AWS access keys; the EC2 instance role is used | `.env.example`, `lambda_ops.py` (default credential chain) | Verified |
| EC2 role: Lambda actions only on `function:pico-finops-agent-loader` and the `pico-finops-agent-duckdb` layer | `policies/aws/ec2-role-policy.json` | Verified |
| EC2 role can pass only the loader role, and only to Lambda (`iam:PassedToService: lambda.amazonaws.com`) | `policies/aws/ec2-role-policy.json` (`PassOnlyTheLoaderRole`) | Verified |
| EC2 role reads only `state.json` and the one table bucket; no write to S3 | `policies/aws/ec2-role-policy.json` | Verified |
| Loader role reads only the export prefix and its `metadata/`; writes only the table bucket and `state.json` | `policies/aws/loader-role-policy.json` | Verified |
| Lambda reserved concurrency 1 | `plugins/focus/pico_plugin_focus_data/lambda_ops.py` (`put_function_concurrency`) | Verified |
| The Lambda checks every payload field before touching S3; file and folder names outside a strict pattern are never read, because they would go into SQL | `loader/pico_finops_agent_loader.py` (`validate`, `FILE_RE`, `DELIVERY_RE`) | Verified |
| `state.json` writes use an ETag condition (no lost updates) | `loader/pico_finops_agent_loader.py` (`write_state`) | Verified |
| `.env` format checks at start (bucket names, ARNs, region, prefix without `..` or quotes) | `pico_finops_agent/entrypoint.sh` | Verified |

### Supply chain (Tampering)

| Mitigation | Where | Status |
|---|---|---|
| Base image pinned by digest; `uv` pinned; Python dependencies installed from `uv.lock` with `--frozen` | `Dockerfile` | Verified |
| DuckDB wheel and extensions checked against SHA-256 pins | `loader/build_zip.py`, `loader/pins.json`; the installed DuckDB version is asserted in the `Dockerfile` | Verified |
| GitHub Actions pinned by commit SHA; `persist-credentials: false`; default token read-only | `.github/workflows/docker-build.yml`, `.github/actions/build-image/action.yml` | Verified |
| Images are published only from release tags on `main`, in this repository, after the image checks pass | `.github/workflows/docker-build.yml` (`publish` job) | Verified |
| Dependency scan (`pip-audit`) and code scan (`bandit`) on the agent, plugins and loader; CodeQL; OpenSSF Scorecard; weekly Dependabot updates | `.github/workflows/ci.yml` (`security` job, not a required check yet), `codeql.yml`, `scorecard.yml`, `.github/dependabot.yml` | Verified |

### Logging (Repudiation, Information disclosure)

| Mitigation | Where | Status |
|---|---|---|
| One `slack_question` log line per message: outcome, time, tool calls, tokens. Never the question or answer text. | `pico_finops_agent/slack_guard.py` | Verified |
| Error log lines carry `Type: message`, cut to 200 characters, never a traceback | `pico_finops_agent/log.py` (`describe`) | Verified |
| The entrypoint prints variable names only, never values | `pico_finops_agent/entrypoint.sh` | Verified |
| The pre-flight check sends the LLM key in a header, never in a URL | `pico_finops_agent/cli.py` (`llm_check`), `llm.py` | Verified |

---

## 4. Threats by boundary (STRIDE)

| # | Boundary | Threat | STRIDE | Mitigated by | Residual |
|---|---|---|---|---|---|
| T1 | Slack users | A workspace member who shouldn't see cost data asks the bot | I | Nothing per user: see O1 | **Open** |
| T2 | Slack users | A user floods the bot to burn LLM spend | D | Hourly limit, deadline, 15-round cap | Low. The limit is per person, not global. |
| T3 | Slack users | A user tricks the LLM into running other commands or reading files | E | Bash allow-list, `$`/backtick guard, file and web tools denied, `--config` denied | Low |
| T4 | Slack users | A user tricks the LLM into writing or deleting data | T | Read-only DuckDB file, `sql_read_only`, no S3 write on the EC2 role | Low |
| T5 | Slack users | A user fakes who they are to change someone's scheduled task | S | HMAC-signed ctx, newest-message check, owner rules | Low |
| T6 | Slack users / billing data | Prompt injection in a message, a tag, a resource name or a charge description steers the answer | T, I | Scope prompt, limited tools | **Open**: see O2 |
| T7 | Agent -> LLM | The provider sees questions and result rows | I | Prompt asks for aggregates, at most 20 rows | **Open**: see O3 |
| T8 | Agent -> LLM | A secret reaches the LLM or a Slack post | I | `bash_guard`, `secret_guard`, short error lines | Low. See O6. |
| T9 | Agent -> AWS | A compromised container uses the role to reach other resources | E | Least-privilege EC2 role, `iam:PassedToService` | **Open**: see O4 |
| T10 | Lambda loader | A crafted export file name injects SQL | T | `FILE_RE`, `DELIVERY_RE`, payload `validate` | Low |
| T11 | Lambda loader | Two loads race and corrupt state | T | `/data/load.lock`, reserved concurrency 1, ETag condition | Low |
| T12 | Container host | Another container on the host takes the instance role | E | IMDSv2 hop limit 1 | Setup step. Any process on the host itself can still reach IMDS. |
| T13 | Container host | A user with shell or Docker access on the EC2 reads `.env` or `docker inspect` | I | `umask 077` for `.env`; guide says not to share the server | **Open**: see O5 |
| T14 | Supply chain | A tampered or vulnerable dependency or extension ends up in the image | T | Digest, lock file and SHA-256 pins; pinned actions; `pip-audit`, grype, Dependabot; signed provenance and SBOM per release | Low (see O7) |

---

## 5. Data flow to the LLM

For each Slack question the LLM provider receives:

- the system prompt (`scope.j2`, including the table schema),
- the last `AGENT_HISTORY_TURNS` turns of that DM or thread,
- the signed `[slack ctx=...]` line (Slack channel, user, thread and message ids, and the signature),
- the SQL it wrote and the rows that came back.

The prompt tells the LLM to aggregate and return at most 20 rows. That is guidance to the model, not a hard limit
on what a query returns.

---

## 6. Residual risks and open items

| # | Item | Notes |
|---|---|---|
| O1 | **No per-user access control in Slack.** | Anyone who can message the bot can ask cost questions and run `pico focus-data status` or `load` (stated in `deploy/pico-finops-agent.yml`). A user or channel allow-list is not built yet. Until then, install the app only in a workspace whose members may all see the cost data. |
| O2 | **Prompt injection through Slack or billing data.** | Message text, and fields like `tags`, `resourcename` and `chargedescription`, reach the LLM. Anyone who can tag resources in the AWS account can put text there. The tool limits in section 3 bound what an injected instruction can do. It can still change the answer the asker sees, or start a load. |
| O3 | **The LLM provider sees the question and result rows.** | This is by design (README, setup guide). The customer picks the provider and its data terms. There is no self-hosted model option today. |
| O4 | **The EC2 role can replace the Lambda's code.** | `lambda:UpdateFunctionCode` plus `iam:PassRole` on the loader role lets a compromised container run its own code with the loader role. That role can read the export and write the cost table and `state.json`. It can't reach anything else, apart from what `AWSLambdaBasicExecutionRole` allows (CloudWatch Logs). The loader trust policy has no `aws:SourceAccount` / `aws:SourceArn` condition. |
| O5 | **Secrets live in the container environment.** | `.env` is on the host disk, and the values show in `docker inspect` and in `/proc` for the agent's processes. There is no secrets manager integration. Root or `docker` group access on the EC2 equals access to all keys and tokens. |
| O6 | **`secret_guard` covers task text only.** | It checks scheduled task arguments and posts. Ordinary Slack answers are not scanned for secret values. They are protected only by the bash guard and the tool limits. |
| O7 | **Image signing and SBOM are new.** | From the next release, `.github/workflows/docker-build.yml` attaches a Sigstore-signed build-provenance attestation and an SPDX SBOM to each published image and scans it with grype (a fixable Critical vulnerability blocks the release). Releases v0.1.0 and v0.2.0 were published before this and have neither. Customers can verify with `gh attestation verify` (SECURITY.md) or build the image themselves with `docker-compose.build.yml`. |
| O8 | **Session history is not persisted, and not audited.** | Slack sessions live on tmpfs (`/home/pico/.pico/sessions`) and are lost on restart. The log keeps only metadata per question. That is good for privacy, but there is no record of what was asked. |
| O9 | **Global LLM spend has no cap.** | The rate limit is per Slack user. Many users together, or many scheduled asks (up to `FOCUS_ASK_MAX_PER_DAY`), can still run up provider cost. Set a budget on the provider account. |
| O10 | **Unrestricted outbound network.** | The container uses host networking and has no egress filter. The design needs only AWS, Slack and the LLM endpoint. Customers who want more can add an egress rule on the security group or a proxy. |
| O11 | **Inconsistent hop-limit hint.** | `lambda_ops.py` `NO_CREDENTIALS_MSG` says "IMDSv2 hop limit 2 for Docker". The setup guide and `docker-compose.yml` say hop limit 1 with host networking. The message should be fixed so it doesn't steer customers to the weaker setting. |

### Not verified

- How Pico's `sql_read_only` setting is enforced inside `pico/` (we confirmed the setting and where it is read, not
  every code path).
- The comment in `plugins/focus/.../pico-plugin.yml` that "the Slack gateway refuses every `ask`" permission prompt.
- The mention-removal code in `plugins/schedule/pico_plugin_schedule/kinds.py` (we read the docstring only).
- Anything the setup guide asks the customer to do (IMDSv2, no inbound rules, `.env` mode): the agent can't check
  these.
