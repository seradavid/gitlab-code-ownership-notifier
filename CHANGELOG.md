# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because the notification payload is consumed by a Power Automate flow you control, the
payload's `schema` field is the compatibility contract: it changes only in a major release,
and the flow can read both during a migration.

## [Unreleased]

### Fixed

- The GitLab release job recreates a release that already exists instead of failing, so
  re-running a tag pipeline (after moving a tag onto a fix, for instance) is safe.

### Removed

- PyPI publishing. The container image (GHCR and the GitLab registry) and the GitLab CI/CD
  component are the two supported ways to consume this; there is no longer anything to
  install from an index, and no trusted-publisher configuration to get wrong.

## [0.1.0] - 2026-09-20

First release: the engine, both jobs, the rollout tooling and the drift report.

### Added

- **`ownership-mr-check`** — notifies the owning teams when a merge request's pipeline turns
  green, then applies each team's existing dashboard label. Runs in `stage: .post` and
  verifies via the pipelines API that no earlier job failed without `allow_failure`.
- **`ownership-merge-audit`** — records merges that happened without an approval from anyone
  on the owning team. Runs in `stage: .pre` of the post-merge pipeline, idempotent through a
  note marker.
- **Ownership model** — CODEOWNERS per repository (read from the MR's target branch) plus a
  small central `teams.yml` for the channel, label, branch scope, thresholds, ignore lists
  and rosters.
- **CODEOWNERS parser** with GitLab-exact semantics: last match wins, `*` versus `**`,
  `{a,b}`, `[abc]`, `?`, sections, trailing comments, and explicit reporting of negation,
  owner-less patterns and unrecognised tokens.
- **Rosters** from the union of a GitLab group, explicit usernames and same-line `@user`
  tokens in CODEOWNERS; an unresolvable roster is treated as "no approval" and flagged.
- **Ownership-change alerts** (`ownership_changed`) — pattern additions and removals are
  announced to the affected team and bypass branch scope and thresholds.
- **Three modes** — `report`, `label`, `notify` — so a rollout can observe before it acts.
- **Notification contract** (`schema: 1`) with HMAC-SHA256 signatures, a stable dedupe key
  per merge request and team, and one message per team even when several reasons coincide.
- **Drift report** — stale patterns, uncovered repositories, unclaimed files, unknown owner
  tokens, roster problems, most-ignored paths and an ownership index.
- **Rollout tooling** — `bootstrap_codeowners.py` renders a written intent mapping into
  reviewable CODEOWNERS drafts and reports what they leave unclaimed; `rollout.py` opens the
  CODEOWNERS and `include:` merge requests (dry-run by default).
- **CI/CD component** `templates/ownership-notify` — both jobs, `allow_failure: true`, no
  `stages:` change needed in consuming repositories.
- 105 unit tests with no network access, an offline end-to-end demo, and a component lint
  script.

[Unreleased]: https://github.com/seradavid/gitlab-code-ownership-notifier/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/seradavid/gitlab-code-ownership-notifier/releases/tag/v0.1.0
