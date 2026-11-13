# Security Policy

Pico runs inside your AWS account with IAM roles, and it holds Slack and language-model API keys. We take reports about it seriously.

## Reporting a vulnerability

**Please do not open a public issue, pull request or discussion for a security problem.**

Report it privately through GitHub instead:

1. Go to the [Security tab](https://github.com/CloudpiAi/Pico/security) of this repository.
2. Choose **Report a vulnerability** ([direct link](https://github.com/CloudpiAi/Pico/security/advisories/new)).
3. Describe the problem, the affected version or commit, and how to reproduce it. A proof of concept helps, but please don't access data that isn't yours.

Only the maintainers can see the report. If you can't use GitHub's form, open a public issue that says only "please contact me about a security report" — no product, file or vulnerability details — and a maintainer will reach out to you privately.

## What happens next

| Step | Our target |
|------|------------|
| We acknowledge your report | within **3 working days** (and never later than 14 days) |
| We confirm or rule out the problem and agree a severity with you | within **10 working days** |
| We release a fix for a confirmed Medium, High or Critical issue | within **60 days** of the report |
| We publish a GitHub Security Advisory (and request a CVE where it applies) | when the fix is released |

We will keep you updated while we work on it, and we credit reporters in the advisory unless you ask us not to. Please give us the time above to fix the problem before you disclose it publicly (coordinated disclosure). If a fix needs longer, we will tell you why and agree a new date with you.

## Supported versions

Security fixes go into the newest release line. Please upgrade to the latest release before reporting.

| Version | Supported |
|---------|-----------|
| 0.3.x   | ✅ |
| < 0.3   | ❌ |

## Scope

In scope:

- The agent and framework code in this repository (`pico_finops_agent/`, `pico/`, `plugins/`, `loader/`)
- The container image `ghcr.io/cloudpiai/pico` and its `Dockerfile` / `docker-compose.yml`
- The IAM policies in `policies/aws/` — for example a permission that is broader than the docs say it needs to be

Out of scope:

- Problems in AWS, Slack or a language-model provider themselves (report those to the vendor)
- Problems that need an attacker who already controls your EC2 host or your AWS account
- Questions a Slack user is allowed to ask: anyone you add to the Slack channel can ask Pico about the cost data you connected

## What Pico sends outside your AWS account

Pico sends a question, and the rows it read to answer it, to the language model you chose. It sends answers to Slack. It sends nothing to CloudPi or PurpleData. If you find Pico sending data anywhere else, that is a security bug — please report it.

## Verifying a release image

Release images are built and published only by this repository's GitHub Actions. From 0.3.0 on, each image also has a signed build-provenance attestation and an SBOM (0.1.0 and 0.2.0 were published before this and have neither). To check an image before you run it:

```bash
gh attestation verify oci://ghcr.io/cloudpiai/pico:<version> -R CloudpiAi/Pico
```
