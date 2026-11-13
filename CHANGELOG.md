# Changelog

All notable changes to Pico are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Pico uses [semantic versioning](https://semver.org/). Each release's GitHub Release page also has the full list of merged pull requests.

## [Unreleased]

### Added

- Release images are also tagged `pico_vX.Y.Z` (for example `ghcr.io/cloudpiai/pico-finops-agent:pico_v0.4.0`), the same image as `X.Y.Z`.

## [0.3.0] - 2026-11-13

**Upgrade notes:** the image now runs Python 3.13 (the loader Lambda keeps python3.12). Pull the new image and run
`docker compose run --rm agent check` before `docker compose up -d`; no setting changes. Images from this release on
can be verified: `gh attestation verify oci://ghcr.io/cloudpiai/pico-finops-agent:0.3.0 -R CloudpiAi/Pico`.

### Added

- `SECURITY.md` (private vulnerability reporting, response times), `CODE_OF_CONDUCT.md` (Contributor Covenant 3.0), `SUPPORT.md`, `GOVERNANCE.md`, `MAINTAINERS.md`, this changelog, `.github/CODEOWNERS` and `.github/dependabot.yml`.
- CI workflow for pull requests: ruff, the fast test suite, a check that the two copies of the IAM policies match, and a dependency vulnerability scan.
- CodeQL, OpenSSF Scorecard and DCO sign-off workflows.
- Release images now get an SBOM, a signed build-provenance attestation and a vulnerability scan.
- `docs/ARCHITECTURE.md` and `docs/THREAT-MODEL.md`.
- Pre-commit configuration (ruff, gitleaks, basic file checks).

### Changed

- `LICENSE` now holds the unmodified Apache License 2.0 text, so tools detect it correctly.
- Project version in `pyproject.toml` follows the release tags (was the inherited `0.4.1`); keywords and classifiers describe FinOps instead of SQL.
- The README follows a new user's path: what Pico does, a quick start with the setup guide's 8 steps, how it works, security and costs (#15).
- `CONTRIBUTING.md` asks for a DCO sign-off and explains how to run the tests; it no longer points to files that aren't in the repository.

### Fixed

- The 12 existing ruff findings (unused variables and imports, import order, an ambiguous name, a loop-variable closure and a too-broad `pytest.raises`), so the new Lint check starts green.
- The root `.env.example` now lists the optional Slack-answer and scheduled-task settings that only the agent's copy had.

### Security

- The Dockerfile's precompile step no longer hides every failure.
- **The image's 31 Critical and 187 High vulnerabilities are fixed** (grype: 0 Critical, 0 High). Python 3.12.12 ->
  3.13.18 base image; litellm 1.81.10 -> 1.109.1 (Critical and CISA known-exploited findings; the malicious 1.107.0
  is excluded), httpx 0.28, openai-agents 0.14.8, pillow 13 (fastembed 0.9.1), pandas 2.3.3, numpy 2.5,
  SQLAlchemy 2.0.55, pyarrow 23, starlette 1.3.1, cryptography 50, filelock 4.2.1 and more. pip-audit reports no
  known vulnerabilities in the runtime dependencies.
- pip is removed from the runtime image.
- Slack and LLM-probe calls use httpx; the loader build opens https:// URLs only; the scheduled-report and summary
  queries are fixed, parameterised SQL; the health heartbeat moved from a fixed /tmp name to a private 0700 folder.
  bandit reports no medium or high findings.
- The CI security scan (pip-audit, bandit) now fails a pull request on a new finding.

### Fixed (Slack answers)

- An empty Slack answer is logged with its reason, the unanswered turn is undone, and the question is retried once
  with a lighter history; a busy reply marks the message finished (#17).

## [0.2.0] - 2026-11-12

### Added

- Scheduled tasks (`plugins/schedule`): reminders, cost reports, saved queries and recurring cost questions in Slack (`pico schedule add|list|show|history|remove|pause|resume|edit|confirm|decline`).

### Changed

- Slack answers: the last 8 turns of history, one retry on an empty answer, a deadline, a per-hour question limit and reset words.
- Cost questions: the model gets the 60-column FOCUS 1.2 schema and writes one query per question.

### Fixed

- The loader now also loads AWS exports in "Overwrite existing data export file" mode. **Upgrade note:** re-apply `policies/aws/loader-role-policy.json` (setup guide step 3c) so the loader role can read `<export-root>metadata/*`.

## [0.1.0] - 2026-11-12

### Added

- First release of the Pico FinOps Agent: the customer kit (setup guide, IAM policies, Docker Compose), the agent source code, and the image `ghcr.io/cloudpiai/pico-finops-agent:0.1.0`, built and checked by GitHub Actions.

[Unreleased]: https://github.com/CloudpiAi/Pico/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/CloudpiAi/Pico/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/CloudpiAi/Pico/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/CloudpiAi/Pico/releases/tag/v0.1.0
