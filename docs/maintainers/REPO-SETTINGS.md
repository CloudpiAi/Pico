# Repository settings (for admins)

Some of what an open-source project needs can't live in a file: it is a GitHub setting. This page lists each one, why it matters, and how to set it. An org or repository admin does these; the `gh` commands need a token with admin rights (`gh auth refresh -s admin:org`).

Tick each item when it's done. Do the **Before launch** ones before Pico is announced.

## Before launch

- [ ] **Make the image public.** The README and the setup guide tell customers to `docker compose pull` it without logging in.
  First an org owner allows public packages (Org settings → Member privileges → Package creation → Public), then a package admin sets
  Org → Packages → `pico-finops-agent` → Package settings → Change visibility → Public. Check from a logged-out machine:
  `docker pull ghcr.io/cloudpiai/pico-finops-agent:latest`
- [ ] **Protect `main` with a ruleset.** Settings → Rules → Rulesets → New branch ruleset, target the default branch, no bypass list:
  - Restrict deletions; block force pushes
  - Require a pull request: 1 approval, dismiss stale approvals, require approval of the most recent push, require review from Code Owners
  - Require status checks to pass: **Lint**, **Tests**, **DCO sign-off** (they appear after the workflows have run once)
  - Require conversation resolution
- [ ] **Turn on private vulnerability reporting** (SECURITY.md points to it).
  `gh api -X PUT repos/CloudpiAi/Pico/private-vulnerability-reporting`
- [ ] **Turn on Dependabot alerts and security updates.**
  `gh api -X PUT repos/CloudpiAi/Pico/vulnerability-alerts` and `gh api -X PUT repos/CloudpiAi/Pico/automated-security-fixes`
- [ ] **Turn on secret scanning and push protection.** Push protection is off by default for a repository.
  Settings → Code security → Secret Protection → Enable, and Push protection → Enable.
- [ ] **Require two-factor authentication for the org.** Org settings → Authentication security → Require 2FA.
- [ ] **Create the `pico-maintainers` team** with write access, then fill in [MAINTAINERS.md](../../MAINTAINERS.md). [CODEOWNERS](../../.github/CODEOWNERS) uses it.
- [ ] **Set the Code of Conduct contact.** Replace `<CONTACT_EMAIL>` in [CODE_OF_CONDUCT.md](../../CODE_OF_CONDUCT.md) with a monitored address.
- [ ] **Create the labels** that the issue forms, `release.yml` and Dependabot use:
  ```bash
  gh label create "type: bug"            -R CloudpiAi/Pico -c d73a4a -d "Something isn't working"
  gh label create "type: feature"        -R CloudpiAi/Pico -c a2eeef -d "New capability or improvement"
  gh label create "status: triage needed" -R CloudpiAi/Pico -c fbca04 -d "A maintainer hasn't looked at it yet"
  gh label create "semver: major"        -R CloudpiAi/Pico -c b60205 -d "Breaking change"
  gh label create "semver: minor"        -R CloudpiAi/Pico -c 0e8a16 -d "New feature, backwards compatible"
  gh label create "area: dependencies"   -R CloudpiAi/Pico -c 0366d6 -d "Dependency updates"
  gh label create "good first issue"     -R CloudpiAi/Pico -c 7057ff -d "Good for newcomers" --force
  gh label create "help wanted"          -R CloudpiAi/Pico -c 008672 -d "Extra attention is needed" --force
  ```
- [ ] **Turn on Discussions** (SUPPORT.md and the issue chooser link to them).
  `gh repo edit CloudpiAi/Pico --enable-discussions`
- [ ] **Fix the "About" panel.**
  ```bash
  gh repo edit CloudpiAi/Pico \
    --description "Pico: an open-source AI FinOps analyst that answers AWS cost questions in Slack from your own FOCUS billing export" \
    --homepage "https://github.com/CloudpiAi/Pico/blob/main/docs/CUSTOMER-SETUP-GUIDE.md" \
    --add-topic finops --add-topic cloud-cost --add-topic aws --add-topic focus \
    --add-topic slack-bot --add-topic llm-agent --add-topic python
  ```

## Before the next release

- [ ] **Turn on immutable releases** (Settings → General → Releases). They lock a release's tag and files and add a signed attestation; they don't apply to releases made before.
- [ ] **Make the Actions token read-only by default.** Settings → Actions → General → Workflow permissions → Read repository contents. The workflows ask for more only where they need it.
- [ ] **Require approval for workflows from first-time contributors** (same page → Fork pull request workflows).
- [ ] **Delete branches after merge**, and remove the merged feature branches already on the remote.
  `gh repo edit CloudpiAi/Pico --delete-branch-on-merge`
- [ ] **Turn on CodeQL** — the `codeql.yml` workflow does this; don't also enable "default setup", or the two will conflict.

## After launch

- [ ] **Register for the OpenSSF Best Practices badge** at <https://www.bestpractices.dev/> and complete the "passing" level, then add its badge to the README. Most answers are in SECURITY.md, CONTRIBUTING.md and the CI workflows.
- [ ] **Make the "Security scan" CI job a required check** once its first runs are clean.
- [ ] **Check the OpenSSF Scorecard result** after `scorecard.yml` has run on `main` (the README badge links to it). "Maintained" needs 90 days of history and "Contributors" needs contributors from several organizations, so a perfect score isn't expected at first.
