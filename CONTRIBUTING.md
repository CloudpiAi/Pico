# Contributing to Pico

Welcome! Pico is an open-source FinOps tool, and we're excited to have contributions from the community. This guide will help you get started.

Everyone taking part follows our [Code of Conduct](CODE_OF_CONDUCT.md). Found a security problem? Don't open an issue — follow [SECURITY.md](SECURITY.md).

## What we're looking for

We welcome contributions in these areas:

- **Documentation** — Improvements to the setup guide, troubleshooting, or FAQ
- **IAM policies** — Refinements to AWS permissions (more specific, more secure, or supporting new services)
- **Deployment configs** — Better Docker Compose setups, Kubernetes manifests, or deployment patterns
- **Bug reports** — Issues with setup, policies, or docs (open a GitHub issue)
- **Feature ideas** — Suggestions for new capabilities (start with a [feature request](https://github.com/CloudpiAi/Pico/issues/new?template=feature-request.yml) or a [discussion](https://github.com/CloudpiAi/Pico/discussions))
- **Agent source code** — Fixes and improvements to the agent (`pico_finops_agent/`, `loader/`, `plugins/focus/`); for larger changes, and for changes to the Pico framework in `pico/`, open an issue first

## Before you start

1. **Check existing issues/PRs** — Someone might already be working on what you want to do
2. **Propose big changes first** — Open a GitHub issue to discuss before spending time on a large change
3. **Read the conventions below** — For documentation, IAM policies and code, and how the project is run ([GOVERNANCE.md](GOVERNANCE.md))

## How to contribute

### Documentation changes

If you're improving the setup guide, troubleshooting, or README:

1. **Make your edits** — Write for a customer setting Pico up: short sentences, one action per step, plain words, and say what the reader will see when a step worked
2. **Test the steps** (if applicable) — Walk through the guide yourself or describe the test plan in your PR
3. **Link to AWS docs** — Include references so readers can go deeper
4. **Check for hardcoded values** — Remove customer account IDs, secrets, or specific examples that shouldn't be in the guide

### IAM policy changes

If you're creating or updating policies in `policies/aws/`:

1. **Follow the conventions** — Two files per role (`*-policy.json` and `*-trust.json`), comments explaining each permission
2. **Validate the JSON** — Run `jq . < policies/aws/your-file.json` to check syntax
3. **Verify permissions are minimal** — Only include what Pico actually needs (principle of least privilege)
4. **Document the `Sid`** — Each statement should have a clear, short identifier (e.g., `"ReadBillingExport"`)
5. **Update the setup guide** if the policy changes what customers must do
6. **Change both copies** — The agent reads `pico_finops_agent/iam/` and customers apply `policies/aws/`; CI fails if the two differ

### Agent source code changes

1. **Add or update tests** — The agent's tests are in `tests/fork/`
2. **Run the tests and the linter** — All of `tests/fork` must pass, and `ruff check` must be clean (see [Running the tests](#running-the-tests))
3. **Build the image** — Run `docker compose -f docker-compose.yml -f docker-compose.build.yml build` if you changed the agent, the Dockerfile or the dependencies; the PR's "Build Docker Image" check does the same and runs the image checks
4. **Update the docs** — both `.env.example` files (the root one, which customers copy, and `pico_finops_agent/.env.example`) and the setup guide when a setting or a setup step changes

### Docker Compose or deployment config changes

1. **Explain the change** — Why is this safer, simpler, or more secure?
2. **Document new settings** — If you add environment variables, update both `.env.example` files
3. **Test on your own setup** — Or describe the test plan in the PR
4. **Update the customer guide** if setup steps change

### Bug reports

If you find an issue with the setup, a policy, or the guide:

1. **Open a GitHub issue** with:
   - What you expected to happen
   - What actually happened
   - Steps to reproduce (if applicable)
   - Your environment (AWS region, OS, etc.)

2. **Be specific** — "Setup guide is confusing" is less helpful than "Step 3 says 'create a role' but doesn't mention where to attach the policy"

### Feature ideas

For ideas on new capabilities:

1. **Open a GitHub issue or discussion** — Describe what Pico could do and why it matters
2. **Consider deployment impact** — Will it require new IAM permissions, environment variables, or Docker changes?
3. **Discuss first** — Let the community chime in before you spend time implementing

## Submission checklist

Before submitting a PR, make sure:

- [ ] Your changes follow the conventions in this guide
- [ ] Every commit is signed off (`git commit -s`, see [Sign your commits](#sign-your-commits-dco))
- [ ] `ruff check .` and `pytest tests/fork` pass (if you changed code)
- [ ] No hardcoded secrets (API keys, account IDs, customer-specific values)
- [ ] All links work and point to current AWS/Slack/Pico docs
- [ ] JSON files are valid syntax (`jq` can parse them)
- [ ] Commit message follows the format: `<type>: <description>` (e.g., `docs: improve EC2 role explanation`)
- [ ] If documentation changed, it's clear and tested
- [ ] If IAM policies changed, they're minimal and documented

## Commit message format

Use conventional commits:

```
docs: improve clarity of S3 bucket setup steps

Added example S3 bucket names and clarified when to use
regional vs. global settings.

Fixes #123
```

Types:
- `docs:` — Documentation updates
- `feat:` — New features, policies, or configs
- `fix:` — Corrections or bug fixes
- `refactor:` — Reorganizing without changing functionality
- `chore:` — Non-functional updates (typos, formatting)

## Running the tests

You need Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
# Install the locked dependencies, the agent extra and the two plugins (the same set as the image)
uv sync --dev --extra pico-finops-agent
uv pip install --no-deps ./plugins/focus ./plugins/schedule

# Lint
uv run ruff check .

# The agent's test suite (what the "Tests" check runs on every pull request)
uv run pytest tests/fork -m "not nightly and not product_e2e and not provider_health and not quarantine"

# Optional: the same checks before every commit (ruff, gitleaks, file checks)
uv run pre-commit install
```

The browser end-to-end tests in `tests/fork/e2e_standalone_ui` skip themselves unless Playwright is installed; they need a running agent and a Slack workspace — see `tests/e2e/standalone_ui/README.md`.

Each pull request also runs CodeQL, a dependency and code security scan, the DCO check and, when the agent or the Dockerfile changed, the image build with its checks.

## Sign your commits (DCO)

Pico uses the [Developer Certificate of Origin](https://developercertificate.org/) (DCO) instead of a contributor license agreement. By adding a `Signed-off-by` line to a commit, you certify that you wrote the change or otherwise have the right to submit it under the project's license.

Sign off each commit with `-s`:

```bash
git commit -s -m "docs: clarify the S3 bucket step"
# adds: Signed-off-by: Your Name <you@example.com>
```

The name and email must match the commit's author. Forgot? Fix the whole branch with `git rebase --signoff main` and push again with `--force-with-lease`. The "DCO sign-off" check on your pull request tells you which commits are missing it.

To keep your work email private, you can use your GitHub `noreply` address ([GitHub Docs](https://docs.github.com/en/account-and-profile/setting-up-and-managing-your-personal-account-on-github/managing-email-preferences/setting-your-commit-email-address)) — set it with `git config user.email` before you commit.

## License

By contributing, you agree that your contributions will be licensed under the same license as this project ([Apache 2.0](LICENSE)), as stated in Section 5 of the license, and you certify the DCO with your sign-off.

## Questions?

- **Setup or deployment questions?** Check [docs/CUSTOMER-SETUP-GUIDE.md](docs/CUSTOMER-SETUP-GUIDE.md)
- **How is the code organised?** See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- **Anything else?** See [SUPPORT.md](SUPPORT.md)

Thanks for contributing! 🚀
