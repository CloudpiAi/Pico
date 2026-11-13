# Pico — AI FinOps Analyst

[![CI](https://github.com/CloudpiAi/Pico/actions/workflows/ci.yml/badge.svg)](https://github.com/CloudpiAi/Pico/actions/workflows/ci.yml)
[![Image](https://github.com/CloudpiAi/Pico/actions/workflows/docker-build.yml/badge.svg)](https://github.com/CloudpiAi/Pico/actions/workflows/docker-build.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/CloudpiAi/Pico/badge)](https://scorecard.dev/viewer/?uri=github.com/CloudpiAi/Pico)
[![Release](https://img.shields.io/github/v/release/CloudpiAi/Pico)](https://github.com/CloudpiAi/Pico/releases)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

**Ask about your AWS costs in Slack, and get an answer in plain words.** Pico is an open-source FinOps analyst that runs in your own AWS account, reads your AWS billing export, answers questions, and tells you when spending jumps.

<!-- TODO: add a screenshot or short GIF of a Slack answer and the 📊 summary, e.g.
     ![Pico answering a cost question in Slack](docs/images/slack-answer.png) -->

- **Your data stays in your AWS account.** Pico runs on one EC2 server you own and keeps the cost data in your S3 buckets.
- **It lives in Slack.** Ask in a channel or a direct message; summaries and alerts arrive where your team already works.
- **Open source and self-hosted.** Apache 2.0, no account to sign up for, no access keys to hand over.

## What you can do

**Ask questions** — mention the bot in a channel it's in, or send it a direct message:

```
@Pico FinOps Agent what did I spend on AWS in October?
@Pico FinOps Agent top 5 services this month
@Pico FinOps Agent why did EC2 cost go up yesterday?
@Pico FinOps Agent when was the cost data last updated?
```

**Get told automatically** — after each data load (06:00 and 18:00 UTC by default) Pico posts in its channel:

| Message | What it tells you |
|---|---|
| 📊 **Fresh cost data is in…** | Yesterday vs. usual, the month so far, sudden increases, new resources |
| ⚠️ **No new AWS cost data for N days** | Your AWS export may have stopped |
| ⚠️ **I couldn't refresh the cost data: …** | Something broke, and what to fix (sent once per problem) |

**Schedule things** — just ask in Slack:

```
@Pico FinOps Agent every Monday at 9 send this channel the cost by service
@Pico FinOps Agent every day check EC2 cost; only tell me if it's over $200
@Pico FinOps Agent remind me on Friday at 4 to stop the dev instances
```

Anyone can schedule tasks to their own DM. To let people post tasks in a channel, list their Slack user IDs in `FOCUS_REPORT_CREATORS` in `.env` (see the scheduled-task settings in [`.env.example`](.env.example)).

## Quick start

**You need:**

- [ ] Admin access to an AWS account (for the one-time setup)
- [ ] A Slack workspace where you can create apps
- [ ] An API key for one language model: Google Gemini (default), OpenAI, Anthropic Claude or DeepSeek
- [ ] A web browser — you run every command in AWS CloudShell or on the EC2 server, so nothing is installed on your computer

Pick **one AWS Region** and use it for everything. Setup takes about an hour the first time. The [setup guide](docs/CUSTOMER-SETUP-GUIDE.md) walks through each step and has a [worksheet](docs/CUSTOMER-SETUP-GUIDE.md#3-your-setup-worksheet) for the values you collect on the way.

| Part | Steps |
|---|---|
| **A. Prepare AWS** | 1. [Turn on the AWS cost export](docs/CUSTOMER-SETUP-GUIDE.md#step-1-turn-on-the-aws-cost-export) · 2. [Create two storage buckets](docs/CUSTOMER-SETUP-GUIDE.md#step-2-create-two-storage-buckets) · 3. [Create the two permission roles](docs/CUSTOMER-SETUP-GUIDE.md#step-3-create-the-two-permission-roles) |
| **B. Get your keys** | 4. [Get a language model API key](docs/CUSTOMER-SETUP-GUIDE.md#step-4-get-a-language-model-api-key) · 5. [Create the Slack app](docs/CUSTOMER-SETUP-GUIDE.md#step-5-create-the-slack-app) |
| **C. Start the agent** | 6. [Launch the EC2 server](docs/CUSTOMER-SETUP-GUIDE.md#step-6-launch-the-ec2-server) · 7. [Configure and start the agent](docs/CUSTOMER-SETUP-GUIDE.md#step-7-configure-and-start-the-agent) · 8. [Confirm it works](docs/CUSTOMER-SETUP-GUIDE.md#step-8-confirm-it-works) |

Step 7 on the EC2 server comes down to this:

```bash
git clone https://github.com/CloudpiAi/Pico.git ~/pico-finops-agent
cd ~/pico-finops-agent
git checkout v0.2.0                # the latest release (see Releases)
cp .env.example .env && nano .env  # fill in your worksheet values
docker compose pull                # the released image, ghcr.io/cloudpiai/pico-finops-agent
docker compose run --rm agent check    # checks your settings and permissions; posts nothing
docker compose up -d
```

**It worked when** `docker compose ps` shows **healthy**, a 📊 **Fresh cost data is in…** message appears in Slack, and `@Pico FinOps Agent what did I spend this month?` gets an answer within about a minute.

Rather build the image yourself? Use `docker compose -f docker-compose.yml -f docker-compose.build.yml build` instead of `docker compose pull`; everything that runs is in this repository.

## How it works

```
                     Your AWS account
 ┌──────────────────────────────────────────────────────────┐
 │                                                          │
 │  AWS Billing ──► S3 export bucket (daily FOCUS files)    │
 │                        │                                 │
 │                        ▼  1. new files                   │
 │                  Loader Lambda (the agent creates it)    │
 │                        │                                 │
 │                        ▼  2. new rows                    │
 │                  S3 table bucket (cost table)            │
 │                        │                                 │
 │                        ▼  3. synced copy                 │
 │                  Pico agent on your EC2 (Docker)         │
 │                                                          │
 └───────────────────┬──────────────────────┬───────────────┘
                     │                      │
      4. question /  │                      │  5. question +
         answer      ▼                      ▼     result rows
                   Slack            Language model
                                    (Gemini, OpenAI, Claude or DeepSeek)
```

1. **Every day** AWS writes your cost data (the [FOCUS](https://focus.finops.org/) export) into your export bucket.
2. **Twice a day** the agent runs its loader Lambda, which adds only the new files to a cost table in S3.
3. The agent copies the new rows to a fast local copy on its server and posts the 📊 summary in Slack.
4. **When someone asks**, the agent sends the question to the language model, runs the read-only query it writes on the local copy, and posts the answer.

**What leaves your AWS account:**

| Data | Where it goes |
|---|---|
| Your full cost data | **Stays in your AWS account** |
| AWS credentials | **None are created** — the agent uses IAM roles |
| One question and the few rows that answer it | Your language model provider, to write the answer |
| Answers and summaries | Your Slack |
| Anything to the Pico maintainers | **Nothing** — no telemetry |

## Security and privacy

- **No access keys.** The EC2 server and the loader each use an IAM role with [least-privilege policies](policies/aws/) that you can read before you apply them.
- **Read-only on your costs.** Pico reads the export and its own cost table; it never changes billing data, and the queries the model writes run read-only.
- **No inbound ports.** The server only makes outgoing HTTPS connections to AWS, Slack and your language model.
- **Hardened container.** It runs as a non-root user on a read-only file system with all Linux capabilities dropped.
- **Your keys stay on your server.** The Slack tokens and the model API key live in the `.env` file on your EC2 server — keep it readable only by you — never in the code or the image.
- **Verifiable images.** Release images are built only by this repository's GitHub Actions. From the release after 0.2.0, each one has a signed build-provenance attestation and an SBOM: `gh attestation verify oci://ghcr.io/cloudpiai/pico-finops-agent:<version> -R CloudpiAi/Pico`

More detail: the guide's [security summary](docs/CUSTOMER-SETUP-GUIDE.md#8-security-summary) and the [threat model](docs/THREAT-MODEL.md). **Found a security problem?** Report it privately — see [SECURITY.md](SECURITY.md), not a public issue.

## What it costs to run

| Item | Typical cost |
|---|---|
| EC2 server | One `t3.medium` (2 vCPU, 4 GB) or larger — the main cost |
| S3 storage and the loader Lambda | Small: a few months of cost data and two short runs a day |
| Language model | Depends on your provider, model and how many questions you ask |
| Pico itself | Free and open source |

## Common questions

**Can I use Pico with Azure or GCP?** Not yet — Pico supports AWS today. Other clouds are a future enhancement.

**Which language model should I pick?** Gemini is the default and works well for cost questions. Switch with `AGENT_LLM_PROVIDER` (and optionally `AGENT_LLM_MODEL`) in `.env`.

**How much history does it keep?** Four months by default; change it with `FOCUS_MONTHS`. See the guide's [optional settings](docs/CUSTOMER-SETUP-GUIDE.md#5-using-the-agent).

**Can I load the newest data right now?** Ask `@Pico FinOps Agent set up the cost agent`.

## Update, troubleshoot, get help

- **Something isn't working?** The guide's [troubleshooting](docs/CUSTOMER-SETUP-GUIDE.md#6-troubleshooting) section lists the common messages and fixes.
- **Update, stop or remove Pico:** see [updating, stopping and removing](docs/CUSTOMER-SETUP-GUIDE.md#7-updating-stopping-and-removing). What changed in each release is in the [changelog](CHANGELOG.md).
- **Questions and bugs:** see [SUPPORT.md](SUPPORT.md).

## Contributing

Contributions are welcome — docs, IAM policies, deployment setups and code. Start with [CONTRIBUTING.md](CONTRIBUTING.md) (how to run the tests, DCO sign-off) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (how the code fits together). Everyone taking part follows our [Code of Conduct](CODE_OF_CONDUCT.md); how decisions are made is in [GOVERNANCE.md](GOVERNANCE.md).

## License

[Apache License 2.0](LICENSE). Pico includes software derived from Datus-agent — see [NOTICE](NOTICE).
