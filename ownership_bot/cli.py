"""Command line entry points.

    ownership-bot mr-check      # last stage of the MR pipeline  (condition 1)
    ownership-bot merge-audit   # first stage of the post-merge pipeline (condition 2)
    ownership-bot drift         # weekly governance report

Both pipeline commands always exit 0: a notification failure must never break a
build and must never delay a deployment (docs/design.md §11). Set
``--mode report`` to decide without writing anything.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from . import drift as drift_module
from . import runner
from .config import VALID_MODES, Settings, from_env
from .gitlab import GitLab, GitLabError, to_merge_request
from .models import MergeRequest

log = logging.getLogger("ownership-bot")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ownership-bot", description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    parser.add_argument("--mode", choices=VALID_MODES, help="report | label | notify")
    parser.add_argument("--teams-file", help="local teams.yml (instead of fetching it)")
    parser.add_argument("--codeowners-file", help="local CODEOWNERS (target-branch version)")
    parser.add_argument("--codeowners-after-file", help="local CODEOWNERS as changed by the MR")
    parser.add_argument("--changed-files-file", help="newline-separated changed paths")
    parser.add_argument("--mr-json", help="MR payload (API shape) for offline runs")
    parser.add_argument("--project", help="project id or path (defaults to CI)")
    parser.add_argument("--iid", type=int, help="merge request IID")
    parser.add_argument("--author", help="MR author username (offline)")
    parser.add_argument("--target-branch", help="MR target branch (offline)")
    parser.add_argument("--labels", help="comma-separated MR labels (offline)")
    parser.add_argument(
        "--state",
        help="MR state for offline runs (default: 'opened' when synthesising an MR, "
        "otherwise whatever --mr-json says)",
    )
    parser.add_argument("--draft", action="store_true", help="treat the MR as a draft (offline)")
    parser.add_argument("--skip-upstream-check", action="store_true")
    parser.add_argument(
        "--json-out",
        help="where to write the machine-readable decision "
        "(default: OWNERSHIP_DECISION_ARTIFACT, else decision.json)",
    )

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("mr-check", help="condition 1: green pipeline, owner label absent")
    sub.add_parser("merge-audit", help="condition 2: merged without an owner approval")

    drift_parser = sub.add_parser("drift", help="scan all repos for ownership drift")
    drift_parser.add_argument("--group", required=True, help="top-level group path")
    drift_parser.add_argument("--ref", default="", help="ref to scan (default: project default)")
    drift_parser.add_argument("--out-json", default="drift-report.json")
    drift_parser.add_argument("--out-md", default="drift-report.md")

    return parser.parse_args(argv)


def _settings(args: argparse.Namespace) -> Settings:
    settings = from_env(
        {
            "mode": args.mode,
            "manifest_path": args.teams_file,
            "codeowners_path": args.codeowners_file,
            "decision_artifact": args.json_out,
            "project_id": args.project,
            "target_branch": args.target_branch,
            "verify_upstream": not args.skip_upstream_check,
        }
    )
    if args.iid:
        settings.merge_request_iid = str(args.iid)
    return settings


def _offline_mr(args: argparse.Namespace, settings: Settings) -> MergeRequest:
    """Build the MR for an offline run, from ``--mr-json`` or from the flags."""
    if args.mr_json:
        payload = json.loads(Path(args.mr_json).read_text(encoding="utf-8"))
        mr = to_merge_request(payload)
        return replace(mr, state=args.state) if args.state else mr

    labels = frozenset(label.strip() for label in (args.labels or "").split(",") if label.strip())
    return MergeRequest(
        project_id=int(settings.project_id or 0),
        project_path=settings.project_path or args.project or "local/project",
        iid=int(settings.merge_request_iid or 0),
        title="(offline run)",
        url="",
        author_username=args.author or "",
        author_name=args.author or "",
        state=args.state or "opened",
        draft=args.draft,
        source_branch="feature/offline",
        target_branch=args.target_branch or settings.default_branch,
        created_at=datetime.now(UTC),
        labels=labels or frozenset(),
    )


def _client(settings: Settings) -> GitLab | None:
    if not settings.bot_token:
        log.warning("no bot token configured — running without GitLab API access")
        return None
    return GitLab(settings.gitlab_url, settings.bot_token, timeout=settings.timeout)


def _cmd_drift(args: argparse.Namespace, manifest, client: GitLab | None) -> int:  # noqa: ANN001
    if client is None:
        log.error("drift needs a bot token")
        return 2
    drift_module.run_drift(
        manifest=manifest,
        client=client,
        group=args.group,
        ref=args.ref,
        out_json=Path(args.out_json),
        out_md=Path(args.out_md),
    )
    return 0


def _changed_files(args: argparse.Namespace, settings: Settings, client, mr, offline: bool):  # noqa: ANN001
    return runner.changed_files_for(
        None if offline else client,
        project=settings.project_id,
        iid=mr.iid,
        changed_files_file=args.changed_files_file,
    )


def _cmd_mr_check(
    args: argparse.Namespace, ctx: runner.RunContext, settings: Settings, client, offline: bool  # noqa: ANN001
) -> None:
    if not offline and runner.upstream_failed(settings, client):
        log.info("upstream failure detected — exiting quietly")
        return

    mr = _offline_mr(args, settings) if offline else _fetch_mr(client, settings)
    changed_files, truncated = _changed_files(args, settings, client, mr, offline)

    codeowners_files = (None, None)
    if args.codeowners_after_file:
        codeowners_files = (None, Path(args.codeowners_after_file).read_text(encoding="utf-8"))

    runner.run_mr_check(
        ctx,
        mr=mr,
        changed_files=changed_files,
        diffs_truncated=truncated,
        codeowners_files=codeowners_files,
    )


def _cmd_merge_audit(
    args: argparse.Namespace, ctx: runner.RunContext, settings: Settings, client, offline: bool  # noqa: ANN001
) -> None:
    if not offline:
        mr = _fetch_merged_mr(client, settings)
        if mr is None:
            log.info("no merge request found for %s — nothing to audit", settings.commit_sha)
            return
    else:
        mr = _offline_mr(args, settings)

    changed_files, truncated = _changed_files(args, settings, client, mr, offline)
    runner.run_merge_audit(ctx, mr=mr, changed_files=changed_files, diffs_truncated=truncated)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )

    try:
        settings = _settings(args)
    except ValueError:
        log.exception("invalid configuration")
        return 2

    client = _client(settings)

    # Offline runs (the demo in the README) need a teams.yml and a changed-files list.
    offline = bool(args.changed_files_file and args.teams_file)

    try:
        manifest = runner.load_manifest_for(settings, client)
    except (GitLabError, OSError, ValueError):
        log.exception("cannot load teams.yml")
        return 2

    if args.command == "drift":
        return _cmd_drift(args, manifest, client)

    ctx = runner.RunContext(settings=settings, client=client, manifest=manifest)

    try:
        if args.command == "mr-check":
            _cmd_mr_check(args, ctx, settings, client, offline)
        else:
            _cmd_merge_audit(args, ctx, settings, client, offline)
        return 0
    except GitLabError:
        log.exception("GitLab error")
        return 0  # never fail the pipeline
    except Exception:  # noqa: BLE001 - defensive: jobs must not break builds
        log.exception("unexpected error")
        return 0


def _fetch_mr(client: GitLab | None, settings: Settings) -> MergeRequest:
    if client is None or not settings.merge_request_iid:
        raise GitLabError("no CI merge request context (set CI_MERGE_REQUEST_IID or --iid)")
    data = client.merge_request(settings.project_id, int(settings.merge_request_iid))
    return to_merge_request(data, project_path=settings.project_path)


def _fetch_merged_mr(client: GitLab | None, settings: Settings) -> MergeRequest | None:
    if client is None or not settings.commit_sha:
        raise GitLabError("no CI commit context (set CI_COMMIT_SHA)")
    data = client.find_merge_request_for_commit(
        settings.project_id, settings.commit_sha, settings.target_branch or settings.default_branch
    )
    if data is None:
        return None
    return to_merge_request(data, project_path=settings.project_path)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
