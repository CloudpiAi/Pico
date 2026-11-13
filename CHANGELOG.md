# Changelog

All notable changes to Pico are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Pico uses [semantic versioning](https://semver.org/). Each release's GitHub Release page also has the full list of merged pull requests.

## [Unreleased]

### Added

- `SECURITY.md` (private vulnerability reporting, response times), `CODE_OF_CONDUCT.md` (Contributor Covenant 3.0), `SUPPORT.md`, `GOVERNANCE.md`, `MAINTAINERS.md`, this changelog, `.github/CODEOWNERS` and `.github/dependabot.yml`.
- CI workflow for pull requests: ruff, the fast test suite, a check that the two copies of the IAM policies match, and a dependency vulnerability scan.
- CodeQL, OpenSSF Scorecard and DCO sign-off workflows.
- Release images now get an SBOM, a signed build-provenance attestation and a vulnerability scan.
- `docs/ARCHITECTURE.md` and `docs/THREAT-MODEL.md`.
- Pre-commit configuration (ruff, gitleaks, basic file checks).

### Changed

- `LICENSE` now holds the unmodified Apache License 2.0 text, so tools detect it correctly.
- Project version in `pyproject.toml` set to the release line (`0.2.0`) instead of the inherited `0.4.1`; keywords and classifiers describe FinOps instead of SQL.
- `CONTRIBUTING.md` asks for a DCO sign-off and explains how to run the tests; it no longer points to files that aren't in the repository.

### Fixed

- The 12 existing ruff findings (unused variables and imports, import order, an ambiguous name, a loop-variable closure and a too-broad `pytest.raises`), so the new Lint check starts green.
- The root `.env.example` now lists the optional Slack-answer and scheduled-task settings that only the agent's copy had.

### Security

- The Dockerfile's precompile step no longer hides every failure.

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

[Unreleased]: https://github.com/CloudpiAi/Pico/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/CloudpiAi/Pico/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/CloudpiAi/Pico/releases/tag/v0.1.0
