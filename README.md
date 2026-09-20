# ownership-bot

[![CI](https://github.com/seradavid/gitlab-code-ownership-notifier/actions/workflows/ci.yml/badge.svg)](https://github.com/seradavid/gitlab-code-ownership-notifier/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

Notifies the team that owns the code in a GitLab merge request — **exactly twice** in a
change's life — using the CODEOWNERS files you already have and the dashboard labels your
teams already use. No service to run, no database, no scheduler, and nothing that can block a
build or a deployment.

| Job | Runs | Fires when |
| --- | --- | --- |
| `ownership-mr-check` | last step of the merge request pipeline | **the pipeline turned green** and the diff is owned by a team that is not already labelled and is not the author's own |
| `ownership-merge-audit` | first step of the post-merge pipeline | the MR **merged without an approval from anyone on the owning team** |

Both jobs are `allow_failure: true` and exit 0 even when GitLab or Teams is unreachable.

The reasoning behind every choice — including the Free-tier constraints that rule out the
alternatives — is in [`docs/design.md`](docs/design.md).

---

## Requirements

- **GitLab** self-managed, Free tier or above. Tested against the behaviour documented for
  self-managed Free; nothing here needs Premium.
- A **project access token** per repository, with `api` + `write_repository`, stored as a
  masked CI/CD variable. `CI_JOB_TOKEN` cannot read merge request diffs, approvals or write
  labels, which is why a token is required ([why](docs/design.md#4-verified-platform-constraints)).
- Somewhere to send the notification. This ships with a
  **Power Automate** notifier (the only supported Teams path since the Office 365 connectors
  were retired): one flow with a "when an HTTP request is received" trigger, a `Switch` on
  `team.channel`, and HMAC verification of the request body.
- A repository to hold `teams.yml` — the small central file with each team's channel, label
  and roster. One file for the whole instance.

## Install in a repository

Three things, all of them reviewable in a single merge request:

```yaml
# .gitlab-ci.yml
include:
  - component: gitlab.example.com/devops/ownership-notify@1.0.0
    inputs:
      manifest-project: devops/ownership   # the repository holding teams.yml
      mode: report                         # observe for a week before notifying
```

```
# .gitlab/CODEOWNERS — you probably already have this
* @devops/teams/platform

[Payments]
src/main/java/com/example/payments/** @devops/teams/payments @alice
src/main/resources/db/migration/** @devops/teams/dba
```

```yaml
# teams.yml, in the manifest repository — channel, label, scope, roster
teams:
  payments:
    codeowners_token: "@devops/teams/payments"
    gitlab_group: devops/teams/payments
    teams_channel: "Payments Engineering"
    label: team::payments
    branches: [master, "release/**"]
```

No `stages:` change is needed: the audit runs in `stage: .pre` and the MR check in
`stage: .post`. Start with `mode: report`, which decides and logs but writes nothing. See
[§12 of the design notes](docs/design.md#12-rollout) for the full rollout order.

## Try it without GitLab

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                                    # 105 tests, no network access
```

The engine is pure enough to run offline against the files in `examples/`:

```bash
ownership-bot \
  --teams-file examples/teams.yml \
  --codeowners-file examples/CODEOWNERS \
  --changed-files-file examples/changed-files.txt \
  --mr-json examples/mr.json \
  --mode report --skip-upstream-check \
  --json-out decision.json mr-check
```

```
INFO    [report] would notify payments (mr_pipeline_green) about acme/services/payment-service!412
INFO    [report] would notify platform (mr_pipeline_green) about acme/services/payment-service!412
WARNING   !! CODEOWNERS names owner token(s) with no teams.yml entry: ['@alice', '@bob']
```

The other condition runs the same way — `--state merged` overrides the state in `mr.json`,
and `--json-out` sets where the machine-readable decision goes (`OWNERSHIP_DECISION_ARTIFACT`
otherwise):

```bash
ownership-bot --teams-file examples/teams.yml --codeowners-file examples/CODEOWNERS \
  --changed-files-file examples/changed-files.txt --mr-json examples/mr.json \
  --state merged --mode report --json-out audit.json merge-audit
# INFO    [report] would notify payments (merged_without_owner_approval) about …!412
```

## Modes

| Mode | Labels | Teams | Use |
| --- | --- | --- | --- |
| `report` | no | no (logged) | first week in a new repository |
| `label` | yes | no | teams that want the dashboard label only |
| `notify` | yes | yes | default |

A label is only applied **after** the notification succeeded, so a Teams outage leaves the MR
unlabelled and the next pipeline run retries instead of silently dropping the ping.

---

## Configuration

### Environment

| Variable | Meaning |
| --- | --- |
| `OWNERSHIP_BOT_TOKEN` | project access token with `api` + `write_repository`. Required for anything that reads or writes. |
| `OWNERSHIP_MANIFEST_PATH` | local `teams.yml` to read instead of fetching anything. Also settable with `--teams-file`. |
| `OWNERSHIP_MANIFEST_PROJECT` / `OWNERSHIP_MANIFEST_REF` / `OWNERSHIP_MANIFEST_FILE` | repository, ref and path of `teams.yml` (defaults: `main` and `teams.yml`). |
| `OWNERSHIP_MODE` | `report` \| `label` \| `notify` (default `notify`). |
| `OWNERSHIP_PA_WORKFLOW_URL` | Power Automate "when an HTTP request is received" URL. |
| `OWNERSHIP_PA_SHARED_SECRET` | HMAC secret; the flow must verify `X-Ownership-Signature`. |
| `OWNERSHIP_VERIFY_UPSTREAM` | `false` skips the "did an earlier job fail?" check. |
| `OWNERSHIP_DECISION_ARTIFACT` | where the machine-readable decision lands (default `decision.json`). |

Set the token, the manifest source and the two Power Automate values as **masked, protected**
CI/CD variables. A project access token works on any license on self-managed GitLab; group
access tokens are Premium.

### Where `teams.yml` lives

Two sources, and the **local file always wins** — that is what makes offline runs possible.

| Source | Configure with | Notes |
| --- | --- | --- |
| A local file | `OWNERSHIP_MANIFEST_PATH`, or `--teams-file` on the CLI | Nothing is fetched and no token is needed. |
| Any repository | `OWNERSHIP_MANIFEST_PROJECT` + `OWNERSHIP_MANIFEST_REF` + `OWNERSHIP_MANIFEST_FILE` | Fetched through the API, so the manifest can live in a project nobody runs pipelines in — which is the point of one central manifest. Any ref, any path within it. |

Two operational details that bite:

* **The token must be able to read that repository.** A *project* access token belongs to the
  project it was created in, so its bot user has to be added as a member (Reporter is enough)
  of the manifest project. A group access token, or a dedicated bot user's personal token,
  avoids that step.
* **The component runs with `GIT_STRATEGY: none`**, so there is no checkout inside the job:
  local-file mode is for developer machines and for pipelines that do check out the repository.
  In the component, use the repository source.

The `ownership-mr-check` job does not trust its position in the pipeline: it asks the
pipelines API whether any earlier job failed without `allow_failure` before doing
anything, so "we only ping on green" holds regardless of stage naming (§6.1).

### `teams.yml` (central)

```yaml
defaults:
  branches: ["**"]
  notify_on: [mr_pipeline_green, merged_without_owner_approval, ownership_changed]
  min_mr_age_minutes: 0      # noise control: ignore hot-fix-sized MRs
  min_owned_files: 1         # noise control: ignore one-line drive-bys
  mentions: []               # optional Microsoft 365 UPNs to @mention

teams:
  payments:
    codeowners_token: "@acme/teams/payments"   # the link to CODEOWNERS
    gitlab_group: acme/teams/payments          # any of these three roster sources
    members: [alice, bob]
    teams_channel: "Payments Engineering"
    label: team::payments                      # pre-existing dashboard label
    branches: [master, develop, "release/**"]  # target branches this team cares about

# Top-level keys: instance-wide policy, not per-team defaults.
ignore: ["**/target/**", "docs/**"]            # applied before CODEOWNERS matching
ignore_authors: ["renovate-bot", "*-ci-bot"]   # bot merge requests are not paged
skip_label: ownership-bot::skip                # escape hatch for hotfixes and reverts
module_overlay:                                # only for components that move
  - module: "*:payment-core"
    owner: payments
```

* **Rosters** are the union of `gitlab_group` members ∪ `members` ∪ the `@user` tokens
  written on the team's own CODEOWNERS lines. Groups are optional: a members-only team
  works, and `gitlab_group` can be added later (when subgroups arrive) without touching
  CODEOWNERS, because the token identity stays the same.
* An **unresolvable roster** is treated as "no owner approval": the audit still fires, and
  the payload carries `roster_resolved: false` so the gap is visible.
* `branches` and `ignore` live here and not in CODEOWNERS because CODEOWNERS has no branch
  concept and no `!` negation.

---

## Notification contract

One POST per (team, MR) to the Power Automate flow, `schema: 1`:

```json
{
  "schema": 1,
  "events": ["mr_pipeline_green"],
  "dedupe_key": "acme/services/payment-service!412:team::payments",
  "trigger": { "pipeline_id": 90210, "job_url": "…", "pipeline_source": "merge_request_event" },
  "team": { "id": "payments", "channel": "Payments Engineering", "label": "team::payments",
            "branches": ["master"], "mentions": [], "roster_resolved": true },
  "merge_request": { "project_path": "…", "iid": 412, "draft": false, "target_branch": "master" },
  "ownership": { "source": "CODEOWNERS@master", "matched": [ … ], "files_total": 7,
                 "files_owned": 4, "files_unclaimed": 1, "diffs_truncated": false },
  "ownership_change": { "file": ".gitlab/CODEOWNERS", "added": [], "removed": [] },
  "approvals": { "approved_by": ["alice"], "owners_approved": ["alice"], "checked_at": "…" },
  "actions_taken": ["notified", "label_added:team::payments"]
}
```

Events are combined: an MR that greens *and* moves ownership produces one message with two
entries in `events`, so a team never gets two pings for one change.

The body is signed with `X-Ownership-Signature: sha256=<hex hmac>`. In the flow, recompute
the HMAC over the raw body and compare before parsing; reject on mismatch.

---

## Rollout

```bash
# phase 1 — draft CODEOWNERS from a human-written intent file
python scripts/bootstrap_codeowners.py --group devops/services \
    --mapping examples/mapping.yml --teams-file examples/teams.yml \
    --out-dir drafts --suggest-from-history

# review drafts/bootstrap-report.md (uncovered directories, unknown owner tokens,
# git-history suggestions), then fill the gaps and finish teams.yml

# phase 3 — open one MR per repository (dry run by default)
python scripts/rollout.py --mapping examples/mapping.yml --teams-file examples/teams.yml \
    --component gitlab.example.com/devops/ownership-notify@1.0.0
python scripts/rollout.py … --apply --limit 3      # canary first
```

The rollout never guesses: a repository with an existing CODEOWNERS or an `include:`
shape the script does not understand is skipped and listed at the end.

Phase 4 is the weekly drift report (`.gitlab-ci.yml`, `drift-report` job): rules that
match nothing, directories nobody owns, unknown owner tokens, shared labels.

## Failure modes

| Situation | Behaviour |
| --- | --- |
| Teams flow down | job fails, **label not applied**, next pipeline run retries |
| GitLab API error | logged, exit 0 — deployments are never held up |
| CODEOWNERS missing | every file is unclaimed, log warning, nothing notified |
| Unknown owner token | reported in `problems` and in the drift report, token ignored |
| Diff truncated (`collapsed`) | `diffs_truncated: true` in the payload; the MR is still evaluated |
| Merge with no post-merge pipeline | the audit never runs (documented residual gap, §4.1) |
| Two teams own one file | both are notified — one message each, correct dedupe keys |

## Repository layout

```
ownership_bot/
  codeowners.py   CODEOWNERS parser with GitLab semantics (last match wins, * vs **, no `!`)
  teams.py        teams.yml loading, branch scope, thresholds, rosters, validation
  ownership.py    ignore → CODEOWNERS → module overlay, per-file attribution
  identity.py     stable identity for moved modules (pom.xml / package manifest)
  decisions.py    both decision tables, pure functions
  notify.py       payload + HMAC signing + delivery
  runner.py       job orchestration, write ordering, idempotency markers
  drift.py        the governance report
  rollout.py      CODEOWNERS rendering + CI include insertion (pure)
  gitlab.py       API client (project access token)
  cli.py          `ownership-bot mr-check | merge-audit | drift`
templates/ownership-notify/template.yml    the GitLab CI/CD component
scripts/          bootstrap_codeowners.py, rollout.py, check_component.py
docs/design.md    why it works this way
examples/         offline demo inputs and a reference teams.yml
```

## Development

```bash
pytest                                # unit tests, no network
ruff check .                          # lint
python scripts/check_component.py     # the component template must stay valid
```

CI runs all three on Python 3.11–3.13, plus the offline end-to-end demo and an image build.
Contributions are welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md), and note that changes
which could fail a pipeline or block a merge need a discussion first.

## Known gaps

- **Merges with no post-merge pipeline are not audited.** The audit rides the default
  branch's pipeline; a repository without one has no condition 2
  ([§4.1](docs/design.md#41-why-the-checks-are-split-across-two-pipelines)).
- **Unclaimed code needs a human.** Files no pattern matches are reported, never notified
  about — turning them into owned patterns is a process decision, not a bot feature.
- The drift report does not yet suggest owners from git history for unclaimed directories
  (the bootstrap script does).
- Only one notifier backend (Power Automate). The payload contract is stable and versioned,
  so another backend is a contained addition.

## Acknowledgements

The engine, the tests and most of this documentation were written by
**[GitHub Copilot](https://github.com/features/copilot)** (DeepSeek V4.1 Flash), working from
the design in [`docs/design.md`](docs/design.md) and a long series of review comments. The
requirements, the platform research behind §4, and the review of every change came from the
maintainer.

Copyright and maintenance stay with the human author: a language model cannot hold copyright,
so it is deliberately absent from [`LICENSE`](LICENSE) and from the `authors` field in
`pyproject.toml`.

## License

[MIT](LICENSE). Use it, fork it, ship it inside your company; a link back is appreciated but
not required.

