# Contributing to Pico

Welcome! Pico is an open-source FinOps tool, and we're excited to have contributions from the community. This guide will help you get started.

## What we're looking for

We welcome contributions in these areas:

- **Documentation** — Improvements to the setup guide, troubleshooting, or FAQ
- **IAM policies** — Refinements to AWS permissions (more specific, more secure, or supporting new services)
- **Deployment configs** — Better Docker Compose setups, Kubernetes manifests, or deployment patterns
- **Bug reports** — Issues with setup, policies, or docs (open a GitHub issue)
- **Feature ideas** — Suggestions for new capabilities (start with a GitHub discussion or issue)
- **Agent source code** — Fixes and improvements to the agent (`pico_finops_agent/`, `loader/`, `plugins/focus/`); for larger changes, and for changes to the Pico framework in `pico/`, open an issue first

## Before you start

1. **Check existing issues/PRs** — Someone might already be working on what you want to do
2. **Propose big changes first** — Open a GitHub issue to discuss before spending time on a large change
3. **Read CLAUDE.md** — It explains conventions for documentation, IAM policies, and tone

## How to contribute

### Documentation changes

If you're improving the setup guide, troubleshooting, or README:

1. **Make your edits** — Keep the [customer communication tone](CLAUDE.md#customer-communication-tone) in mind
2. **Test the steps** (if applicable) — Walk through the guide yourself or describe the test plan in your PR
3. **Link to AWS docs** — Include references so readers can go deeper
4. **Check for hardcoded values** — Remove customer account IDs, secrets, or specific examples that shouldn't be in the guide

Use the `/update-customer-guide` Claude Code skill if you want help structuring or refining docs.

### IAM policy changes

If you're creating or updating policies in `policies/aws/`:

1. **Follow the conventions** — Two files per role (`*-policy.json` and `*-trust.json`), comments explaining each permission
2. **Validate the JSON** — Run `jq . < policies/aws/your-file.json` to check syntax
3. **Verify permissions are minimal** — Only include what Pico actually needs (principle of least privilege)
4. **Document the `Sid`** — Each statement should have a clear, short identifier (e.g., `"ReadBillingExport"`)
5. **Update the setup guide** if the policy changes what customers must do

Use the `/validate-iam-policy` Claude Code skill for a thorough review.

### Agent source code changes

1. **Add or update tests** — The agent's tests are in `tests/fork/`; see the README for how to run them
2. **Run the tests** — All of `tests/fork` must pass
3. **Build the image** — Run `docker compose build` if you changed the agent, the Dockerfile or the dependencies
4. **Update the docs** — `.env.example` and the setup guide when a setting or a setup step changes

### Docker Compose or deployment config changes

1. **Explain the change** — Why is this safer, simpler, or more secure?
2. **Document new settings** — If you add environment variables, update `.env.example`
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

Use the `/plan-pico-feature` Claude Code skill to structure a proposal.

## Submission checklist

Before submitting a PR, make sure:

- [ ] Your changes follow the conventions in [CLAUDE.md](CLAUDE.md)
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

## License

By contributing, you agree that your contributions will be licensed under the same license as this project ([Apache 2.0](LICENSE)).

## Questions?

- **Setup or deployment questions?** Check [docs/CUSTOMER-SETUP-GUIDE.md](docs/CUSTOMER-SETUP-GUIDE.md)
- **How should I structure this?** See [CLAUDE.md](CLAUDE.md)
- **Want to use Claude Code?** Try `/update-customer-guide`, `/validate-iam-policy`, or `/plan-pico-feature`

Thanks for contributing! 🚀
