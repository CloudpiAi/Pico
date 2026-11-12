# Pico — AI FinOps Analyst

Pico is an **open-source AI FinOps analyst** for startups and teams. Ask Pico about your AWS costs in Slack, and it answers your questions and monitors for unexpected spending.

**Your data stays in your AWS account.** Pico runs on a single EC2 instance in your account. It reads your AWS billing data (FOCUS exports) and keeps everything in your own S3 buckets. Nothing is sent to Pico FinOps or any third party — only questions and answers go to your chosen language model (Gemini by default, or OpenAI, Anthropic Claude, or DeepSeek).

## What can Pico do?

Ask Pico questions about your costs — **right in Slack:**

```
@Pico what did I spend on AWS in October?
@Pico which service grew the most this week?
@Pico show me EC2 costs by region for September
```

Pico also **watches your costs automatically:**

- **Twice daily summaries** — Posts what changed yesterday, the month so far, and any sudden increases
- **Cost alerts** — Warns you about unexpected spikes or unusual patterns
- **Data stays local** — All cost data lives in your S3, not in a third-party database

## Architecture

Everything inside the dashed box runs **in your AWS account**. Only Slack and the language model are external.

```
┌─────────────────────────────────────────────┐
│            Your AWS Account                 │
│                                             │
│  AWS Billing ──┐                           │
│                ├─→ S3 (FOCUS export)       │
│                │                           │
│                └─→ Lambda (loader)         │
│                    ├─→ S3 (cost table)     │
│                    └─→ S3 (state/progress) │
│                                             │
│  EC2 (Docker container)                     │
│  ├─ Data loader (runs 2x daily)             │
│  ├─ Local cost data cache                   │
│  ├─ Slack bot (listens for questions)       │
│  └─ Query executor                          │
│                                             │
└─────────────────────────────────────────────┘
         ↓                        ↑
       Slack                     LLM
   (questions/answers)    (Gemini, OpenAI, Claude, DeepSeek)
```

### Data flow

1. **AWS Billing** writes FOCUS cost exports to your S3 export bucket once daily
2. **Pico loader Lambda** runs at 06:00 and 18:00 UTC, reads new export files, and writes rows to a cost table in S3
3. **Pico on EC2** syncs the cost table into a fast local copy and posts a daily summary in Slack
4. **Slack user asks** `@Pico what did I spend?`
5. **Pico queries** its local copy and sends the results + question to the LLM
6. **LLM writes** a short answer; Pico posts it back in Slack

### What's included

| Component | Purpose | Runs where |
|-----------|---------|------------|
| Docker Compose config | Define the Pico container, volumes, environment | Your EC2 |
| IAM policies | EC2 and Lambda role definitions with minimal permissions | AWS IAM |
| Customer setup guide | Step-by-step to get Pico running | This repository |
| `.env.example` | Configuration template (Slack tokens, LLM choice, etc.) | Your EC2 |

The **agent code** (query execution, Slack integration, LLM interaction) runs inside the Docker image (`cloudpi1/cloudpi:*`), which is pulled from Docker Hub.

## Getting started

1. **Read the [setup guide](docs/CUSTOMER-SETUP-GUIDE.md)** — Step-by-step instructions to run Pico on your EC2
2. **Prepare your AWS account** — Create S3 buckets, set up IAM roles (use the policies in `policies/aws/`)
3. **Pull the Docker image** and start the container with your `.env` configuration
4. **Ask Pico a question in Slack** and get an answer

**Estimated setup time:** 20–30 minutes for first-time AWS users

## Features

- ✅ **AI-powered cost analysis** — Ask natural language questions about your AWS spending
- ✅ **Self-hosted** — Runs on your EC2, all data in your account
- ✅ **Zero databases** — Uses S3 as the data store (no RDS, no separate DB)
- ✅ **Multiple LLM options** — Gemini (default), OpenAI, Anthropic Claude, DeepSeek
- ✅ **Automatic monitoring** — Daily summaries and alerts for cost anomalies
- ✅ **Minimal IAM permissions** — Follows principle of least privilege
- ✅ **Open source** — Apache 2.0 license; contribute improvements

## Security and privacy

- 🔒 **Your data, your infrastructure** — Nothing leaves your AWS account except questions sent to the LLM
- 🔒 **IAM-based access** — EC2 instance assumes roles; no access keys needed
- 🔒 **Read-only cost data** — Pico only reads FOCUS exports and the cost table, never modifies billing data
- 🔒 **Encrypted secrets** — API keys and Slack tokens are read from `.env`, not stored in code

See [CUSTOMER-SETUP-GUIDE.md](docs/CUSTOMER-SETUP-GUIDE.md) for a detailed privacy and data flow diagram.

## Directory structure

```
.
├── README.md                          # This file
├── CONTRIBUTING.md                    # How to contribute
├── docker-compose.yml                 # Docker container definition
├── .env.example                       # Configuration template
├── pico_finops_agent/                 # Agent source code (agent.py, llm.py, cli.py, scheduler.py, health.py)
├── loader/                            # Lambda loader code
├── deploy/                            # Deployment configuration
├── policies/aws/                      # IAM role policies
│   ├── ec2-role-policy.json          # Permissions for EC2 instance
│   ├── ec2-role-trust.json           # Who can assume EC2 role
│   ├── loader-role-policy.json       # Permissions for Lambda loader
│   └── loader-role-trust.json        # Who can assume Lambda role
├── docs/
│   └── CUSTOMER-SETUP-GUIDE.md       # Complete setup instructions
└── LICENSE                            # Apache 2.0

```

## Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for:
- How to propose changes or report bugs
- Guidelines for documentation, IAM policies, and configurations
- Commit message format and submission checklist

## Common questions

**Q: Does Pico have access to my AWS credentials?**  
A: No. Pico uses IAM roles; no access keys are created. The EC2 instance's IAM role is the only credential needed.

**Q: Where does my cost data go?**  
A: Entirely to your S3 buckets in your AWS account. The only data sent outside AWS is the question and result rows (to the LLM).

**Q: What language model should I use?**  
A: Gemini is the default and works well for cost analysis. You can switch to OpenAI, Anthropic Claude, or DeepSeek in `.env`.

**Q: Can I use Pico with Azure or GCP?**  
A: Currently, Pico supports AWS. Support for other clouds is tracked as a future enhancement.

**Q: How much does it cost to run?**  
A: Primarily the EC2 instance cost (t3.medium recommended). S3 storage and Lambda invocations are minimal. LLM costs depend on your choice and usage.

**Q: How do I get help?**  
A: Check [CUSTOMER-SETUP-GUIDE.md](docs/CUSTOMER-SETUP-GUIDE.md), open a GitHub issue, or join the community discussions.

## License

Apache License 2.0 — See [LICENSE](LICENSE) for details.

---

**Ready to get started?** → [Customer Setup Guide](docs/CUSTOMER-SETUP-GUIDE.md)
