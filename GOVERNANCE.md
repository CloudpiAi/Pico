# Pico governance

Pico is an open-source project sponsored by PurpleData Inc. (CloudPi). This page explains who decides what, and how a contributor can become a maintainer.

## Roles

- **Users** run Pico and report bugs or ideas.
- **Contributors** send pull requests, review them, answer questions or improve the docs. Anyone can be a contributor.
- **Maintainers** are listed in [MAINTAINERS.md](MAINTAINERS.md). They review and merge pull requests, triage issues, cut releases, and handle security reports ([SECURITY.md](SECURITY.md)) and Code of Conduct reports ([CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)).

## How decisions are made

1. **Most changes** are decided in the pull request. A change merges when a maintainer other than its author approves it and the required checks pass.
2. **Larger changes** — a new cloud, a new required setting, a change to the IAM policies, a breaking change, a new dependency on an outside service — start as an issue so the approach can be discussed before code is written.
3. Maintainers aim for **consensus**. If they can't reach it, the maintainers decide by a simple majority. The sponsoring company's maintainers do not get an extra vote, but a release can be held back for a legal or security reason.
4. Decisions that change this document, the license, or the contributor sign-off rule need agreement from at least two thirds of the maintainers, discussed in a public issue for at least one week.

## Becoming a maintainer

A contributor can be nominated by any maintainer after a record of good contributions — for example several merged pull requests, helpful reviews or issue triage over at least three months. The nomination is made in a pull request that adds the person to MAINTAINERS.md, and it is accepted when a majority of the maintainers approve it.

Maintainers who have been inactive for six months may be moved to an "emeritus" list, with thanks. They can return by asking.

## Releases

Maintainers cut releases from `main` by pushing a `vX.Y.Z` tag (semantic versioning). The release workflow builds, checks and publishes the image. Every release has notes in [CHANGELOG.md](CHANGELOG.md), including any security fixes.

## Code of Conduct

Everyone taking part in the project follows the [Code of Conduct](CODE_OF_CONDUCT.md).
