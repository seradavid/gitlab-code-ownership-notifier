"""Pure decision logic (docs/design.md §9.1, §9.2).

No I/O: everything needed is passed in, which keeps the behaviour table-testable
and means the pipeline jobs cannot make a side effect by accident.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from .codeowners import TeamPatternDelta
from .models import (
    MERGED_WITHOUT_OWNER_APPROVAL,
    MR_PIPELINE_GREEN,
    OWNERSHIP_CHANGED,
    Approvals,
    Decision,
    MergeRequest,
    OwnershipResult,
)
from .teams import Manifest

RosterLookup = Callable[[str], set[str]]


def _author_owns(team_key: str, mr: MergeRequest, roster_for: RosterLookup) -> bool:
    return mr.author_username in roster_for(team_key)


def _owner_approved(team_key: str, approvals: Approvals, roster_for: RosterLookup) -> bool:
    """True when someone in the team's roster approved (§7.5, D2)."""
    if not approvals.approved_by:
        return False
    return bool(set(approvals.approved_by) & roster_for(team_key))


def _team_is_done(team, mr: MergeRequest, approvals: Approvals, roster_for: RosterLookup) -> bool:
    """Any of these makes the team invisible for this MR, for every event."""
    return (
        _author_owns(team.key, mr, roster_for)
        or team.label in mr.labels
        or _owner_approved(team.key, approvals, roster_for)
    )


def _green_events(
    team_key: str,
    team,
    *,
    mr: MergeRequest,
    manifest: Manifest,
    ownership: OwnershipResult,
    now: datetime,
) -> list[str]:
    """The code ping applies only in scope, opted in, and above the noise thresholds."""
    if team_key not in ownership.teams():
        return []
    if MR_PIPELINE_GREEN not in team.notify_on:
        return []
    if not manifest.branch_in_scope(team, mr.target_branch):
        return []
    if not manifest.passes_thresholds(
        team, mr, owned_files=len(ownership.files_for(team_key)), now=now
    ):
        return []
    return [MR_PIPELINE_GREEN]


def _team_events(
    team_key: str,
    team,
    *,
    mr: MergeRequest,
    manifest: Manifest,
    ownership: OwnershipResult,
    delta: dict[str, TeamPatternDelta],
    now: datetime,
) -> list[str]:
    """Both reasons can apply at once; they travel in one message (§9.4)."""
    events: list[str] = []

    if team_key in delta and OWNERSHIP_CHANGED in team.notify_on:
        events.append(OWNERSHIP_CHANGED)

    events.extend(
        _green_events(team_key, team, mr=mr, manifest=manifest, ownership=ownership, now=now)
    )
    return events


def decide_mr_check(
    *,
    mr: MergeRequest,
    manifest: Manifest,
    ownership: OwnershipResult,
    approvals: Approvals,
    roster_for: RosterLookup,
    pattern_delta: dict[str, TeamPatternDelta] | None = None,
    now: datetime,
) -> list[Decision]:
    """``ownership-mr-check``: runs last in the MR pipeline, only if build/tests passed.

    There is deliberately **no draft check** — the green pipeline is the trigger.
    An ownership change is announced regardless of branch scope and thresholds; the
    code ping respects both.
    """
    if manifest.skip_label in mr.labels:
        return []
    if manifest.is_ignored_author(mr.author_username):
        return []

    delta = {team: change for team, change in (pattern_delta or {}).items() if not team.startswith("__")}
    affected = sorted(ownership.teams() | set(delta))

    decisions: list[Decision] = []
    for team_key in affected:
        team = manifest.team(team_key)
        if _team_is_done(team, mr, approvals, roster_for):
            continue

        events = _team_events(
            team_key,
            team,
            mr=mr,
            manifest=manifest,
            ownership=ownership,
            delta=delta,
            now=now,
        )
        if not events:
            continue

        change = delta.get(team_key)
        decisions.append(
            Decision(
                team=team_key,
                events=tuple(events),
                matches=tuple(ownership.matches.get(team_key, [])),
                added_patterns=tuple(change.added) if change else (),
                removed_patterns=tuple(change.removed) if change else (),
            )
        )

    return decisions


def decide_merge_audit(
    *,
    mr: MergeRequest,
    manifest: Manifest,
    ownership: OwnershipResult,
    approvals: Approvals,
    roster_for: RosterLookup,
) -> list[Decision]:
    """``ownership-merge-audit``: first, non-blocking step of the post-merge pipeline.

    No thresholds and no ownership-change events here — this is a post-hoc audit of
    "merged without an owner approval".
    """
    if not mr.is_merged:
        return []
    if manifest.skip_label in mr.labels:
        return []
    if manifest.is_ignored_author(mr.author_username):
        return []

    decisions: list[Decision] = []
    for team_key in sorted(ownership.teams()):
        team = manifest.team(team_key)

        if _author_owns(team_key, mr, roster_for):
            continue
        if not manifest.branch_in_scope(team, mr.target_branch):
            continue
        if MERGED_WITHOUT_OWNER_APPROVAL not in team.notify_on:
            continue
        if _owner_approved(team_key, approvals, roster_for):
            continue

        decisions.append(
            Decision(
                team=team_key,
                events=(MERGED_WITHOUT_OWNER_APPROVAL,),
                matches=tuple(ownership.matches.get(team_key, [])),
            )
        )

    return decisions
