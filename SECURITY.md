# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub's
[security advisory](https://github.com/seradavid/gitlab-code-ownership-notifier/security/advisories/new)
form rather than in a public issue. Include what you found, how to reproduce it, and what
you think the impact is. Expect an initial response within a week.

## What this tool holds

Worth knowing when you deploy it, and when you assess a report:

| Secret | Where it lives | Blast radius if leaked |
| --- | --- | --- |
| `OWNERSHIP_BOT_TOKEN` — a GitLab **project access token** with `api` + `write_repository` | Masked, protected CI/CD variable | Can read merge requests, approvals and repository files, and write labels and notes in the projects it is scoped to. Use a dedicated bot user, scope it to one project, and rotate it like any credential. |
| `OWNERSHIP_PA_SHARED_SECRET` — the HMAC key | Masked CI/CD variable, and the Power Automate flow | Anyone holding it can forge a notification for any team channel the flow can post to. |
| `OWNERSHIP_PA_WORKFLOW_URL` | Masked CI/CD variable | The URL alone can post to the flow if the flow does not verify the signature. Always verify it. |

There are no credentials in this repository, and the code never logs a token, a secret or a
notification body.

## Design decisions that affect security

- **The signature is the only authentication** on the notification path. The Power Automate
  flow must recompute the HMAC over the raw request body and compare before parsing, and must
  reject on mismatch. A flow that posts first and validates later is a vulnerability.
- **Ownership is read from the MR's target branch**, so a merge request cannot edit
  CODEOWNERS to remove a team's ownership and skip its notification.
- **`CI_JOB_TOKEN` is deliberately not used** for the checks: it cannot read diffs,
  approvals or labels. That is why a project access token is required, and why it should be
  scoped as narrowly as the API allows.
- **Both jobs always exit 0**, including on authentication failures. A broken token therefore
  degrades to "no notifications" rather than "no deployments" — watch for repeated
  authentication errors in the job log, or you will not notice.
- **Group-level CI/CD variables are readable by project Maintainers.** Treat them as
  configuration, not as a secret store.

## Supported versions

The `main` branch is the supported version; releases are tagged. Fixes for security issues
are released as patch versions and listed in [`CHANGELOG.md`](CHANGELOG.md).
