"""Data structures shared by the engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# Notification reasons (§9.1, §9.2, §9.4).
MR_PIPELINE_GREEN = "mr_pipeline_green"
MERGED_WITHOUT_OWNER_APPROVAL = "merged_without_owner_approval"
OWNERSHIP_CHANGED = "ownership_changed"

ALL_EVENTS = (MR_PIPELINE_GREEN, MERGED_WITHOUT_OWNER_APPROVAL, OWNERSHIP_CHANGED)


@dataclass(frozen=True)
class MergeRequest:
    project_id: int
    project_path: str
    iid: int
    title: str
    url: str
    author_username: str
    author_name: str
    state: str
    draft: bool
    source_branch: str
    target_branch: str
    created_at: datetime
    labels: frozenset[str] = frozenset()
    sha: str | None = None
    merged_at: datetime | None = None

    @property
    def is_merged(self) -> bool:
        return self.state == "merged"

    def age_minutes(self, now: datetime) -> int:
        return max(0, int((now - self.created_at).total_seconds() // 60))


@dataclass(frozen=True)
class OwnershipMatch:
    """Why a team owns files in this MR."""

    team: str
    kind: str  # "codeowners" | "module_overlay"
    value: str  # the matching pattern, or the module/package identity
    files: tuple[str, ...]


@dataclass
class OwnershipResult:
    """Outcome of resolving the MR's changed files against CODEOWNERS."""

    matches: dict[str, list[OwnershipMatch]] = field(default_factory=dict)
    files_total: int = 0
    files_ignored: int = 0
    files_unclaimed: int = 0
    diffs_truncated: bool = False
    codeowners_ref: str = ""
    codeowners_found: bool = True
    problems: list[str] = field(default_factory=list)

    def teams(self) -> set[str]:
        return set(self.matches)

    def files_for(self, team: str) -> tuple[str, ...]:
        files: list[str] = []
        for match in self.matches.get(team, []):
            files.extend(match.files)
        return tuple(sorted(set(files)))

    @property
    def files_owned(self) -> int:
        owned = {f for matches in self.matches.values() for match in matches for f in match.files}
        return len(owned)


@dataclass(frozen=True)
class Approvals:
    approved_by: tuple[str, ...] = ()
    checked_at: datetime | None = None


@dataclass(frozen=True)
class Decision:
    """One notification to one team, with every reason it applies."""

    team: str
    events: tuple[str, ...]
    matches: tuple[OwnershipMatch, ...] = ()
    added_patterns: tuple[str, ...] = ()
    removed_patterns: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.events
