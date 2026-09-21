# Contributing

Thanks for taking a look. This is a small, deliberately boring tool: two CI jobs, no service,
no state. The design rationale is in [`docs/design.md`](docs/design.md) — please read §4 and
§9 before proposing behaviour changes, because most "obvious" improvements are ruled out by a
Free-tier constraint.

## Getting set up

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                      # 128 tests, no network access needed
```

Nothing in the test suite touches a GitLab instance: the decision tables are pure functions,
and the job runners are exercised against a stub client. Keep it that way — if a change needs
a live API to test, it probably needs another seam instead.

## Before you open a pull request

```bash
pytest                                   # must be green
python scripts/check_component.py        # the CI/CD component must stay valid
```

CI runs the same two checks on Python 3.11, 3.12 and 3.13, plus a build of the image.

## What is easy to accept

- Bug fixes with a test that fails before and passes after.
- New *interpretations* of the notification contract (e.g. another chat backend) behind the
  existing payload building code.
- Drift-report additions that only read and report.
- Documentation fixes, including the design notes.

## What needs discussion first

Open an issue before writing code for:

- **Anything that can fail a pipeline or block a merge.** Both jobs are `allow_failure: true`
  and exit 0 on every error path; that is a feature, not an oversight.
- **New required configuration.** Every new field is one more thing fifty repositories have
  to get right. `teams.yml` should stay small.
- **New triggers.** The two conditions map to the two pipelines that already exist. Adding an
  event source (webhook, scheduler, service) is a different architecture and is explained —
  and rejected — in appendix C of the design notes.
- **New label types.** The label is both the dashboard signal and the idempotency marker; a
  second one needs a story for what happens when they disagree.

## Style

- Python 3.11+, standard library first. `requests` and `PyYAML` are the only runtime
  dependencies and that should not change casually.
- Pure logic in pure functions; I/O at the edges (`runner.py`, `gitlab.py`, `notify.py`).
- Comments explain *why*, especially where GitLab's behaviour is surprising. The CODEOWNERS
  semantics and the write ordering are the two places where a well-meaning refactor can break
  something subtle.
- Tests are named after the behaviour they pin down, not the function they call.

## Commits and pull requests

Small commits with a clear message. In the pull request description, say what the change does
and which part of the design it touches. If it changes behaviour, update `docs/design.md` in
the same pull request — the code references its section numbers.

## Releasing

The project is published from two hosts, and the tag conventions differ:

| Host | Tag | What runs |
| --- | --- | --- |
| GitLab | `0.1.0` | `verify-release` re-runs the tests, `build-image` pushes the engine image as `:<tag>`, `create-release` publishes the version to the CI/CD Catalog |
| GitHub | `v0.1.0` | packages the project and checks the tag against the version, then pushes the image to GHCR |

GitLab asks for a semantic version on component releases, so the plain `0.1.0` form is the
safe one there; the GitHub workflow expects the `v` prefix. Push the same commit to both:

```bash
git push origin master && git push gitlab master
```

Before tagging on GitLab, bump the component's `image` input default in
`templates/ownership-notify/template.yml` to the tag you are about to push, so the component
and the engine image are pinned to the same version. Publishing to the CI/CD Catalog also
needs a one-time toggle in the project (Settings → General → Visibility → CI/CD Catalog
project) and a project description.

A tag pipeline reads the `.gitlab-ci.yml` **at that tag**, so a fix on `master` does not help
an existing tag: move the tag onto the fix (and force-push it) or cut a new patch version.

### If the GitLab release job fails

`create-release` is the only job whose failure is not our own code's fault. It shells out to
`glab`, so the job needs an image that contains it — a plain `python` image fails with:

```
Warning: release-cli will not be supported after 20.0. Please use glab >= 1.58.0
/usr/bin/bash: line 211: release-cli: command not found
ERROR: Job failed: exit code 127
```

The release itself being created is what makes the version appear in the CI/CD Catalog.
GitLab only counts releases created by the `release` keyword; a release created through the
Releases API or the UI does **not** publish to the catalog. The catalog also only indexes a
project after the toggle above is on, so enabling it after a release has been published may
need one more release before the project is listed.

The `release` keyword never *updates* an existing release, it fails with `Release for tag
"x" already exists`. Because a tag pipeline reads its configuration at the tag, any re-run of
a tag pipeline hits that — which is what happens whenever a tag is moved onto a fix. The job
therefore recreates the release itself (`glab release delete` in `before_script`, then the
keyword creates it again), so re-running a tag pipeline is safe.

## Reporting security issues

Please do not open a public issue; see [`SECURITY.md`](SECURITY.md).
