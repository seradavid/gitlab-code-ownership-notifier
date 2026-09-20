"""Orchestration for the two pipeline jobs and the drift report.

Design rule: **these jobs must never fail a pipeline and must never block a
deployment.** Every public entry point here returns an exit code and swallows
operational errors after logging them, leaving the label/note marker unset so the
next pipeline run retries (docs/design.md §11).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import decisions, identity, notify, ownership
from .codeowners import CODEOWNERS_PATHS, ParsedCodeowners, parse, team_pattern_delta
from .config import MODE_REPORT, Settings
from .gitlab import GitLab, GitLabError
from .models import Approvals, MergeRequest, OwnershipResult
from .ownership import users_on_team_lines
from .teams import Manifest, load_manifest, load_manifest_text

log = logging.getLogger(__name__)

MERGE_MARKER = "<!-- ownership-bot:merged:{team} -->"


# --------------------------------------------------------------------------- context


@dataclass
class RunContext:
    settings: Settings
    client: GitLab | None = None
    manifest: Manifest | None = None
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    problems: list[str] = field(default_factory=list)

    def require_manifest(self) -> Manifest:
        if self.manifest is None:
            raise GitLabError("manifest (teams.yml) not loaded")
        return self.manifest


def load_manifest_for(settings: Settings, client: GitLab | None) -> Manifest:
    """Local override first (handy for dry runs), otherwise fetch from the manifest repo."""
    if settings.manifest_path:
        return load_manifest(settings.manifest_path)

    if not (client and settings.manifest_project):
        raise GitLabError(
            "no manifest: pass --teams-file, or set OWNERSHIP_MANIFEST_PROJECT (+ bot token)"
        )

    text = client.raw_file(settings.manifest_project, "teams.yml", settings.manifest_ref)
    if text is None:
        raise GitLabError(
            f"teams.yml not found in {settings.manifest_project}@{settings.manifest_ref}"
        )
    return load_manifest_text(text, source=f"{settings.manifest_project}/teams.yml")


def fetch_codeowners(
    settings: Settings, client: GitLab | None, project: str | int, ref: str
) -> tuple[ParsedCodeowners, bool, str]:
    """Read CODEOWNERS from the MR's **target branch** (§7.1)."""
    if settings.codeowners_path:
        local = Path(settings.codeowners_path)
        if local.is_file():
            return parse(local.read_text(encoding="utf-8")), True, str(local)
        return parse(""), False, str(local)

    if client is None:
        raise GitLabError("no GitLab client and no --codeowners-file given")

    text, path = client.codeowners(project, ref or settings.default_branch)
    if text is None:
        log.warning("no CODEOWNERS in %s@%s — every changed file is unclaimed", project, ref)
        return parse(""), False, path
    return parse(text), True, path


def changed_files_for(
    client: GitLab | None,
    *,
    project: str | int,
    iid: int,
    changed_files_file: str | None = None,
) -> tuple[list[str], bool]:
    if changed_files_file:
        paths = [
            line.strip()
            for line in Path(changed_files_file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return paths, False
    if client is None:
        raise GitLabError("no GitLab client and no --changed-files-file given")
    return client.merge_request_diffs(project, iid)


def make_roster_lookup(
    manifest: Manifest,
    codeowners: ParsedCodeowners,
    client: GitLab | None,
) -> tuple[callable, set[str]]:
    """Roster per team (§7.5) plus the set of teams whose roster could not be resolved."""
    cache: dict[str, set[str]] = {}
    unresolved: set[str] = set()

    def group_members(group: str) -> set[str]:
        if client is None:
            return set()
        return client.group_members(group)

    def roster_for(team_key: str) -> set[str]:
        if team_key not in cache:
            team = manifest.team(team_key)
            resolved = manifest.roster(
                team,
                group_members=group_members,
                codeowners_users=users_on_team_lines(codeowners, team.codeowners_token),
            )
            cache[team_key] = resolved
            if not resolved:
                unresolved.add(team_key)
                log.warning(
                    "team '%s' has no resolvable roster — approvals cannot be credited to it",
                    team_key,
                )
        return cache[team_key]

    return roster_for, unresolved


# ------------------------------------------------------------------------ ownership


def resolve_for_mr(
    ctx: RunContext,
    *,
    project: str | int,
    mr: MergeRequest,
    changed_files: list[str],
    diffs_truncated: bool,
    codeowners_files: tuple[str | None, str | None] = (None, None),
) -> tuple[OwnershipResult, dict, ParsedCodeowners]:
    """Resolve ownership and, when the MR edits CODEOWNERS, the per-team pattern delta.

    Returns the parsed target-branch CODEOWNERS as well, so callers can derive
    ``@user`` roster tokens without a second fetch.
    """
    manifest = ctx.require_manifest()
    before_text, after_text = codeowners_files

    if before_text is None:
        codeowners, found, ref = fetch_codeowners(ctx.settings, ctx.client, project, mr.target_branch)
    else:
        codeowners, found, ref = parse(before_text), True, mr.target_branch

    resolver = None
    if manifest.module_overlay:
        resolver = identity.identity_resolver(ctx.settings.repo_root)

    result = ownership.resolve_ownership(
        changed_files=changed_files,
        manifest=manifest,
        codeowners=codeowners,
        identity_for=resolver,
        codeowners_ref=ref or mr.target_branch,
        codeowners_found=found,
        diffs_truncated=diffs_truncated,
    )

    delta: dict = {}
    if after_text is not None:
        delta = team_pattern_delta(codeowners, parse(after_text), manifest.token_to_team)

    return result, delta, codeowners


def mr_edits_codeowners(changed_files: list[str]) -> bool:
    return any(path in CODEOWNERS_PATHS or path.endswith("CODEOWNERS") for path in changed_files)


# ----------------------------------------------------------------------- job runners


def _pipeline_trigger(settings: Settings) -> dict:
    return {
        "pipeline_id": settings.pipeline_id or None,
        "job_url": settings.job_url,
        "pipeline_source": settings.pipeline_source or None,
    }


def _write_decision_artifact(path: Path, payload: dict) -> None:
    try:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:  # pragma: no cover - disk issues
        log.warning("could not write %s: %s", path, exc)


def _send_or_mark_report(settings: Settings, notifier, payload: dict) -> bool:
    """Deliver, or record that we deliberately did not. Returns True when delivered."""
    if settings.notify_enabled:
        delivered = notifier.post(payload)
        payload["actions_taken"].append("notified" if delivered else "notify-failed")
        return delivered

    notifier.post(payload)  # NullNotifier in report mode
    payload["actions_taken"].append(
        "report-only" if settings.mode == MODE_REPORT else "notify-skipped"
    )
    return False


def _write_labels(ctx: RunContext, *, project: str | int, iid: int, labels: list[str]) -> None:
    if not (ctx.settings.writes_enabled and labels):
        return
    try:
        ctx.client.add_labels(project, iid, labels)  # type: ignore[union-attr]
    except GitLabError:
        log.exception("could not add labels %s", labels)


def _dispatch_mr_check(
    ctx: RunContext,
    *,
    mr: MergeRequest,
    project: str | int,
    decisions_: list,
    ownership_result: OwnershipResult,
    approvals: Approvals,
    unresolved: set[str],
    notifier,
) -> tuple[list[dict], list[str]]:
    """Notify first, then label: a lost notification must leave the MR unlabelled (§11)."""
    settings = ctx.settings
    manifest = ctx.require_manifest()

    sent: list[dict] = []
    labels_to_add: list[str] = []

    for decision in decisions_:
        payload = notify.build_payload(
            decision=decision,
            mr=mr,
            ownership=ownership_result,
            approvals=approvals,
            manifest=manifest,
            roster_resolved=decision.team not in unresolved,
            trigger=_pipeline_trigger(settings),
        )

        delivered = _send_or_mark_report(settings, notifier, payload)
        if settings.notify_enabled and not delivered:
            log.warning("notification failed for %s; label not applied", decision.team)
            sent.append(payload)
            continue

        label = manifest.team(decision.team).label
        payload["actions_taken"].append(
            f"label_added:{label}" if settings.writes_enabled else f"label-skipped:{label}"
        )
        if settings.writes_enabled:
            labels_to_add.append(label)
        sent.append(payload)

    _write_labels(ctx, project=project, iid=mr.iid, labels=labels_to_add)
    return sent, labels_to_add


def run_mr_check(
    ctx: RunContext,
    *,
    mr: MergeRequest,
    changed_files: list[str],
    diffs_truncated: bool = False,
    codeowners_files: tuple[str | None, str | None] = (None, None),
    approvals: Approvals | None = None,
    notifier=None,
) -> dict:
    """``ownership-mr-check`` — green-pipeline decisions, labels and notifications."""
    settings = ctx.settings
    manifest = ctx.require_manifest()
    project = settings.project_id or mr.project_path

    ownership_result, delta, codeowners = resolve_for_mr(
        ctx,
        project=project,
        mr=mr,
        changed_files=changed_files,
        diffs_truncated=diffs_truncated,
        codeowners_files=codeowners_files,
    )

    roster_for, unresolved = make_roster_lookup(manifest, codeowners, ctx.client)

    if approvals is None:
        approvals = _approvals_for(ctx, project, mr.iid)

    decided = decisions.decide_mr_check(
        mr=mr,
        manifest=manifest,
        ownership=ownership_result,
        approvals=approvals,
        roster_for=roster_for,
        pattern_delta=delta,
        now=ctx.now,
    )

    notifier = notifier or _notifier(settings)

    sent, _labels = _dispatch_mr_check(
        ctx,
        mr=mr,
        project=project,
        decisions_=decided,
        ownership_result=ownership_result,
        approvals=approvals,
        unresolved=unresolved,
        notifier=notifier,
    )

    summary = {
        "job": "mr-check",
        "mode": settings.mode,
        "project": mr.project_path,
        "mr": mr.iid,
        "draft": mr.draft,
        "target_branch": mr.target_branch,
        "codeowners": {
            "found": ownership_result.codeowners_found,
            "ref": ownership_result.codeowners_ref,
        },
        "files": {
            "total": ownership_result.files_total,
            "owned": ownership_result.files_owned,
            "ignored": ownership_result.files_ignored,
            "unclaimed": ownership_result.files_unclaimed,
            "truncated": ownership_result.diffs_truncated,
        },
        "decisions": sent,
        "problems": ownership_result.problems,
        "unresolved_rosters": sorted(unresolved),
    }
    _write_decision_artifact(Path(settings.decision_artifact), summary)
    _log_summary(summary)
    return summary


def _audit_note(team_key: str, marker: str) -> str:
    return (
        f"{marker}\n\n**Ownership audit:** this merge request was merged without an "
        f"approval from **{team_key}**, which owns files changed "
        "here. This is a post-merge record, not a request to revert.\n"
    )


def _dispatch_merge_audit(
    ctx: RunContext,
    *,
    mr: MergeRequest,
    project: str | int,
    decisions_: list,
    ownership_result: OwnershipResult,
    approvals: Approvals,
    unresolved: set[str],
    notifier,
) -> list[dict]:
    """One note per team per MR: the marker makes a re-run a no-op (§9.2)."""
    settings = ctx.settings
    existing_notes = _notes_text(ctx, project, mr.iid)
    sent: list[dict] = []

    for decision in decisions_:
        marker = MERGE_MARKER.format(team=decision.team)
        if marker in existing_notes:
            continue  # already audited for this MR

        payload = notify.build_payload(
            decision=decision,
            mr=mr,
            ownership=ownership_result,
            approvals=approvals,
            manifest=ctx.require_manifest(),
            roster_resolved=decision.team not in unresolved,
            trigger=_pipeline_trigger(settings),
        )

        delivered = _send_or_mark_report(settings, notifier, payload)
        if settings.notify_enabled and not delivered:
            log.warning("merge audit notification failed for %s", decision.team)
            sent.append(payload)
            continue

        if settings.writes_enabled:
            try:
                ctx.client.add_note(project, mr.iid, _audit_note(decision.team, marker))  # type: ignore[union-attr]
                payload["actions_taken"].append("note_added")
            except GitLabError:
                log.exception("could not add audit note")
        sent.append(payload)

    return sent


def run_merge_audit(
    ctx: RunContext,
    *,
    mr: MergeRequest,
    changed_files: list[str],
    diffs_truncated: bool = False,
    approvals: Approvals | None = None,
    notifier=None,
) -> dict:
    """``ownership-merge-audit`` — post-merge audit, first and non-blocking."""
    settings = ctx.settings
    manifest = ctx.require_manifest()
    project = settings.project_id or mr.project_path

    ownership_result, _delta, codeowners = resolve_for_mr(
        ctx,
        project=project,
        mr=mr,
        changed_files=changed_files,
        diffs_truncated=diffs_truncated,
    )

    roster_for, unresolved = make_roster_lookup(manifest, codeowners, ctx.client)

    if approvals is None:
        approvals = _approvals_for(ctx, project, mr.iid)

    decided = decisions.decide_merge_audit(
        mr=mr,
        manifest=manifest,
        ownership=ownership_result,
        approvals=approvals,
        roster_for=roster_for,
    )

    notifier = notifier or _notifier(settings)
    sent = _dispatch_merge_audit(
        ctx,
        mr=mr,
        project=project,
        decisions_=decided,
        ownership_result=ownership_result,
        approvals=approvals,
        unresolved=unresolved,
        notifier=notifier,
    )

    summary = {
        "job": "merge-audit",
        "mode": settings.mode,
        "project": mr.project_path,
        "mr": mr.iid,
        "commit": settings.commit_sha,
        "decisions": sent,
        "problems": ownership_result.problems,
        "unresolved_rosters": sorted(unresolved),
    }
    _write_decision_artifact(Path(settings.decision_artifact), summary)
    _log_summary(summary)
    return summary


# ---------------------------------------------------------------------------- helpers


def _approvals_for(ctx: RunContext, project: str | int, iid: int) -> Approvals:
    if ctx.client is None:
        return Approvals()
    try:
        return Approvals(approved_by=tuple(ctx.client.approvals(project, iid)), checked_at=ctx.now)
    except GitLabError as exc:
        log.warning("could not read approvals for !%s: %s", iid, exc)
        return Approvals()


def _notes_text(ctx: RunContext, project: str | int, iid: int) -> str:
    if ctx.client is None:
        return ""
    try:
        return "\n".join(note.get("body", "") for note in ctx.client.notes(project, iid))
    except GitLabError:
        return ""


def _notifier(settings: Settings):
    if not settings.notify_enabled:
        return notify.NullNotifier()
    return notify.Notifier(
        workflow_url=settings.pa_workflow_url, shared_secret=settings.pa_shared_secret
    )


def upstream_failed(settings: Settings, client: GitLab | None) -> bool:
    """True when a job *before* ours failed and was not allowed to fail.

    This is what makes the "only notify when the pipeline passed" rule
    independent of where the job sits, so ``.post`` and an explicit final stage
    behave identically (§6.1, Q4).
    """
    if not settings.verify_upstream or client is None:
        return False
    if not (settings.project_id and settings.pipeline_id):
        return False
    try:
        jobs = client.pipeline_jobs(settings.project_id, settings.pipeline_id)
    except GitLabError as exc:
        log.warning("could not read pipeline jobs: %s", exc)
        return False

    own = str(settings.job_id)
    for job in jobs:
        if str(job.get("id")) == own:
            continue
        if job.get("status") in ("failed", "canceled") and not job.get("allow_failure", False):
            log.info("upstream job '%s' failed — not sending notifications", job.get("name"))
            return True
    return False


def _log_summary(summary: dict) -> None:
    log.info(
        "%s: %d decision(s) for %s!%s (mode=%s)",
        summary["job"],
        len(summary["decisions"]),
        summary.get("project", "?"),
        summary.get("mr", "?"),
        summary["mode"],
    )
    for payload in summary["decisions"]:
        log.info(
            "  -> %s [%s] %s",
            payload["team"]["id"],
            ",".join(payload["events"]),
            ",".join(payload["actions_taken"]),
        )
    for problem in summary.get("problems", []):
        log.warning("  !! %s", problem)
