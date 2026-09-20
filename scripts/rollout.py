#!/usr/bin/env python3
"""Phase 3 rollout (docs/design.md §12): open one reviewable MR per repository.

    # 1. see the plan, touch nothing (default)
    python scripts/rollout.py --mapping mapping.yml --teams-file teams.yml \
        --component gitlab.example.com/devops/ownership-bot/ownership-notify@1.0.0

    # 2. open the merge requests
    python scripts/rollout.py ... --apply

Each MR contains:

* `.gitlab/CODEOWNERS` — the draft rendered from the mapping, and
* the `include: component ...` block appended to the existing `.gitlab-ci.yml`.

Both files are review artefacts owned by the receiving team. Where a file cannot be
transformed safely (an inline `include:` shape we refuse to guess at, an existing
CODEOWNERS we would overwrite), the repository is skipped and listed at the end for
a human to finish — the script never force-pushes or rewrites a pipeline silently.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ownership_bot.gitlab import GitLab, GitLabError  # noqa: E402
from ownership_bot.rollout import (  # noqa: E402
    RolloutError,
    ensure_include,
    load_mapping,
    render_codeowners,
    validate_codeowners,
)
from ownership_bot.teams import ManifestError, load_manifest  # noqa: E402

log = logging.getLogger("rollout")

BRANCH = "ops/ownership-notify"
MR_TITLE = "Add CODEOWNERS and enable ownership notifications"

MR_BODY = """\
This merge request installs the ownership-notification feature for this repository.

It adds two things and nothing else:

1. `.gitlab/CODEOWNERS` — who owns what. **Please review the patterns**: the last
   matching pattern wins, and a path that matches nothing is reported as unclaimed.
2. an `include:` of the shared CI/CD component, which runs two non-blocking jobs
   (`allow_failure: true` on both):

   * `ownership-merge-audit` — first step of the post-merge pipeline;
   * `ownership-mr-check` — last step of the merge request pipeline.

Nothing here can block a build, a merge or a deployment: if the notification fails,
the job fails but the pipeline is unaffected.

Rollout note: start with `mode: report` (decide and log, write nothing) for a week,
then switch to `notify` — see §12 of the design document.
"""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mapping", required=True, help="mapping.yml: repository -> intent")
    parser.add_argument("--teams-file", help="teams.yml, to validate owner tokens")
    parser.add_argument(
        "--component",
        required=True,
        help="component reference, e.g. gitlab.example.com/group/ownership-notify@1.0.0",
    )
    parser.add_argument("--branch", default=BRANCH)
    parser.add_argument("--apply", action="store_true", help="actually push and open merge requests")
    parser.add_argument("--force-codeowners", action="store_true", help="overwrite an existing CODEOWNERS file")
    parser.add_argument("--limit", type=int, default=0, help="stop after N repositories (canary rollout)")
    return parser.parse_args(argv)


def _plan_codeowners(
    client: GitLab, draft, ref: str, *, force_codeowners: bool  # noqa: ANN001
) -> tuple[list[dict], list[str]]:
    path = draft.codeowners_path
    existing = client.raw_file(draft.project, path, ref)
    if existing is None:
        existing = client.raw_file(draft.project, "CODEOWNERS", ref)
        if existing is not None:
            path = "CODEOWNERS"

    text = render_codeowners(draft)
    if existing is None or force_codeowners:
        action = "update" if existing is not None else "create"
        return [{"action": action, "file_path": path, "content": text}], []

    if existing.strip() == text.strip():
        return [], [f"{path} already matches the draft"]

    raise RolloutError(
        f"{path} already exists; rerun with --force-codeowners once it is reviewed"
    )


def _plan_ci(
    client: GitLab, draft, component: str, ref: str  # noqa: ANN001
) -> tuple[list[dict], list[str]]:
    path = ".gitlab-ci.yml"
    current = client.raw_file(draft.project, path, ref) or ""
    updated = ensure_include(current, component, draft.ci_inputs or None)
    if updated == current:
        return [], ["component include already present"]
    return [
        {
            "action": "update" if current else "create",
            "file_path": path,
            "content": updated,
        }
    ], []


def plan_for_repo(
    client: GitLab,
    draft,  # noqa: ANN001
    component: str,
    ref: str,
    *,
    force_codeowners: bool,
) -> tuple[list[dict], list[str]]:
    """Return ``(commit actions, notes)``; raises :class:`RolloutError` to skip a repo."""
    codeowners_actions, codeowners_notes = _plan_codeowners(
        client, draft, ref, force_codeowners=force_codeowners
    )
    ci_actions, ci_notes = _plan_ci(client, draft, component, ref)
    return codeowners_actions + ci_actions, codeowners_notes + ci_notes


def _plan_repo(
    client: GitLab, project: str, draft, args: argparse.Namespace, known: set[str]  # noqa: ANN001
) -> tuple[list[dict], list[str], str]:
    """Read the repository and plan its merge request; never writes anything."""
    info = client.project(project)
    ref = draft.target_branch or info.get("default_branch") or "main"

    problems = validate_codeowners(render_codeowners(draft), known_tokens=known or None)
    if problems:
        raise RolloutError("draft problems: " + "; ".join(problems))

    actions, notes = plan_for_repo(
        client, draft, args.component, ref, force_codeowners=args.force_codeowners
    )
    return actions, notes, ref


def _apply(
    client: GitLab, project: str, args: argparse.Namespace, ref: str, actions: list[dict]
) -> dict:
    client.create_commit(
        project,
        args.branch,
        start_branch=ref,
        actions=actions,
        message="Add CODEOWNERS and enable ownership notifications",
    )
    return client.create_merge_request(
        project,
        source_branch=args.branch,
        target_branch=ref,
        title=MR_TITLE,
        description=MR_BODY,
    )


def _process_repo(
    client: GitLab, project: str, draft, args: argparse.Namespace, known: set[str]  # noqa: ANN001
) -> str | None:
    """Plan (and optionally apply) one repository; returns the MR summary line."""
    actions, notes, ref = _plan_repo(client, project, draft, args, known)
    for note in notes:
        log.info("%s: %s", project, note)

    if not actions:
        log.info("%s: nothing to do", project)
        return None

    if not args.apply:
        log.info(
            "[dry-run] %s: would commit %s to %s and open an MR into %s",
            project,
            ", ".join(action["file_path"] for action in actions),
            args.branch,
            ref,
        )
        return None

    mr = _apply(client, project, args, ref, actions)
    log.info("%s: merge request opened", project)
    return f"{project}: {mr.get('web_url') or mr.get('iid')}"


def _report(opened: list[str], skipped: list[str], applied: bool) -> None:
    print()
    if opened:
        print(f"opened {len(opened)} merge request(s):")
        for line in opened:
            print(f"  - {line}")
    if skipped:
        print(f"{len(skipped)} repository(ies) need a human:")
        for line in skipped:
            print(f"  - {line}")
    if not applied:
        print("\ndry run: nothing was changed (pass --apply to open the merge requests)")


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    mapping = load_mapping(Path(args.mapping).read_text(encoding="utf-8"), source=args.mapping)

    known: set[str] = set()
    if args.teams_file:
        try:
            manifest = load_manifest(args.teams_file)
        except (ManifestError, OSError):
            log.exception("cannot read %s", args.teams_file)
            return 2
        known = set(manifest.token_to_team) | {
            f"@{member}" for team in manifest.teams.values() for member in team.members
        }

    client = GitLab.from_env()
    if client is None:
        log.error("set OWNERSHIP_BOT_TOKEN / GITLAB_TOKEN before running the rollout")
        return 2

    skipped: list[str] = []
    opened: list[str] = []

    for index, (project, draft) in enumerate(sorted(mapping.items())):
        if args.limit and index >= args.limit:
            log.info("--limit reached, stopping")
            break

        try:
            line = _process_repo(client, project, draft, args, known)
        except (GitLabError, RolloutError) as exc:
            log.exception("%s: skipped", project)
            skipped.append(f"{project}: {exc}")
            continue

        if line:
            opened.append(line)

    _report(opened, skipped, args.apply)
    return 1 if skipped else 0


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main(sys.argv[1:]))
