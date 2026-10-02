# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because the notification payload is consumed by a Power Automate flow you control, the
payload's `schema` field is the compatibility contract: it changes only in a major release,
and the flow can read both during a migration.

## [Unreleased]

## [0.3.0] - 2026-10-02

### Fixed

- **Ownership-change alerts now fire in real pipelines.** `ownership_changed` was only ever
  emitted when a local `--codeowners-after-file` was supplied, so the shipped component never
  computed a delta. The MR's own CODEOWNERS is now read from the head commit of its source
  branch, but only when the diff touches a CODEOWNERS path, and not at all for a suppressed MR
  (skip label or ignored author), which produces no decisions anyway.
- **`module_overlay` now works under `GIT_STRATEGY: none`.** Identity files
  (`pom.xml` / `package.json`) are resolved from a local checkout when one exists and through
  the GitLab API otherwise, instead of silently matching nothing in the component.
- **Malformed YAML is a clean configuration error.** A broken `teams.yml` (or `mapping.yml`)
  is now reported as a manifest/rollout error instead of escaping as a raw `yaml.YAMLError`.
- **GitLab transport failures are normalised to `GitLabError`.** Connection errors and
  timeouts behave like any other API error, so the callers that log-and-continue actually do.
- **A missing client no longer crashes label/note writes.** With no bot token the run reports
  `label-skipped` (or skips the audit note) instead of raising `AttributeError`.
- **A shared `codeowners_token` keeps the first team and logs the collision** instead of
  letting the last declaration silently capture (and drop) a team.
- **CODEOWNERS parsing gained `[!abc]` negated classes, globs inside `{a,b}` (`{*.js,*.ts}`)
  and backslash escapes**, matching the pattern language GitLab actually uses.
- `--project` accepts a project path in offline runs without crashing `int()`, and a missing
  `created_at` no longer produces a naive datetime that breaks `age_minutes`.
- **The component's default engine image is pinned to the packaged version again.** It had
  been left on `0.1.0` while `ownership_bot.__version__` was `0.2.0`, so a consumer relying
  on the default would have run an older engine than the component they installed.
  `scripts/check_component.py` now fails when the two disagree, so this cannot ship silently.

### Changed

- **Pipeline jobs now exit 0 even on a configuration error** (a missing or malformed
  `teams.yml`), matching the documented "never fail a build" contract. `drift` keeps a
  non-zero exit because it is a diagnostic run on purpose.
- **The engine refuses to send an unsigned notification.** With no
  `OWNERSHIP_PA_SHARED_SECRET` the notifier logs an error and reports failure instead of
  POSTing a body the Power Automate flow cannot authenticate.
- The notification payload now carries `ownership.path` and `ownership.files_ignored`, and
  `ownership_change.file` reports the CODEOWNERS location the repository actually uses
  (root `CODEOWNERS`, `.gitlab/CODEOWNERS` or `docs/CODEOWNERS`) instead of always
  `.gitlab/CODEOWNERS`.
- `has_include` matches a real `include:` entry rather than any substring, so a mention in a
  comment or a longer component name no longer looks like an existing include. The component
  is inserted at the existing entries' indentation, a block mapping is refused rather than
  guessed at, and a file that does not parse (`!reference` and other custom tags) falls back
  to the conservative check so an existing include is never duplicated.
- `upstream_failed` is documented as "any non-allowed failure in the pipeline", which is what
  it has always checked (the jobs API does not expose a stable ordering); it can only
  suppress a notification, never send one on a red pipeline.
- An explicit `skip_label: ""` now disables the escape hatch instead of being replaced by the
  default label, and the drift report tolerates an unwritable output path.
- **Unparenthesized `except A, B:` (PEP 758, Python 3.14 only) was parenthesized** to
  `except (A, B):` across the engine and the scripts. Same behaviour, but the source no
  longer depends on a 3.14-only syntax detail; the supported runtime is still 3.14.

## [0.2.0] - 2026-10-02

### Changed

- The runtime floor is now Python 3.14. The engine image, the CI matrix and the project
  metadata all target 3.14 (was 3.11–3.13).
- The package version now comes from a single source (`ownership_bot.__version__`) via
  setuptools' dynamic version, instead of being duplicated in `pyproject.toml`.
- The runner, drift and notifier depend on narrow `Protocol` interfaces rather than the
  concrete GitLab client, which keeps them testable with stubs.

### Fixed

- The GitLab release job recreates a release that already exists instead of failing, so
  re-running a tag pipeline (after moving a tag onto a fix, for instance) is safe.
- The notification payload's `owners_approved` now credits approvers from the team's full
  roster (GitLab group members and same-line `@user`s), not only the explicit `members`.
- `teams.yml` now rejects an unknown `version` and reports a clear error for non-integer
  `min_mr_age_minutes` / `min_owned_files` instead of a raw `ValueError`.

### Removed

- PyPI publishing. The container image (GHCR and the GitLab registry) and the GitLab CI/CD
  component are the two supported ways to consume this; there is no longer anything to
  install from an index, and no trusted-publisher configuration to get wrong.

### Security

- Bumped dependency floors to the first versions with no known CVEs: `requests` to `>=2.33.0`
  (CVE-2024-35195, CVE-2024-47081, CVE-2026-25645), `pytest` to `>=9.0.3` (CVE-2025-71176)
  and `setuptools` to `>=83.0.0` (CVE-2025-47273, CVE-2026-59890). `PyYAML` (`>=6.0.3`) and
  `ruff` (`>=0.6`) already pin clean versions.
- The GitLab client no longer follows redirects, so the `PRIVATE-TOKEN` header cannot leak to
  a different host, and the HTTP `User-Agent` is now actually set (it previously fell back to
  `python-requests`).

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

[Unreleased]: https://github.com/seradavid/gitlab-code-ownership-notifier/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/seradavid/gitlab-code-ownership-notifier/releases/tag/v0.3.0
[0.2.0]: https://github.com/seradavid/gitlab-code-ownership-notifier/releases/tag/v0.2.0
[0.1.0]: https://github.com/seradavid/gitlab-code-ownership-notifier/releases/tag/v0.1.0
