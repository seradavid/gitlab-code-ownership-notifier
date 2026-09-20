"""``teams.yml`` loading plus branch scope, thresholds, ignore lists and rosters.

Everything CODEOWNERS cannot express lives here (docs/design.md §7.3, §7.4, §7.5).
"""

from __future__ import annotations

import contextlib
import fnmatch
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .codeowners import translate_pattern
from .models import ALL_EVENTS, MergeRequest


class ManifestError(ValueError):
    """Raised when ``teams.yml`` is structurally unusable."""


_OVERRIDABLE = (
    "branches",
    "exclude_branches",
    "notify_on",
    "min_mr_age_minutes",
    "min_owned_files",
    "mentions",
)


@dataclass(frozen=True)
class Team:
    key: str
    channel: str
    label: str
    codeowners_token: str
    gitlab_group: str | None = None
    members: tuple[str, ...] = ()
    branches: tuple[str, ...] = ("**",)
    exclude_branches: tuple[str, ...] = ()
    notify_on: frozenset[str] = frozenset(ALL_EVENTS)
    min_mr_age_minutes: int = 0
    min_owned_files: int = 1
    mentions: tuple[str, ...] = ()


@dataclass(frozen=True)
class OverlayRule:
    """Optional stable-identity rule for components that genuinely move (``module_overlay``)."""

    identity_pattern: str
    owners: tuple[str, ...]


@dataclass
class Manifest:
    teams: dict[str, Team]
    token_to_team: dict[str, str]
    ignore: tuple[str, ...]
    ignore_authors: tuple[str, ...]
    skip_label: str
    module_overlay: tuple[OverlayRule, ...]
    source: str = ""
    _ignore_regexes: tuple[re.Pattern[str], ...] = field(default=(), repr=False)

    # ---------------------------------------------------------------- lookups

    def team(self, key: str) -> Team:
        try:
            return self.teams[key]
        except KeyError as exc:  # pragma: no cover - guarded by callers
            raise ManifestError(f"unknown team '{key}'") from exc

    def team_for_token(self, token: str) -> str | None:
        return self.token_to_team.get(token)

    def is_ignored(self, path: str) -> bool:
        return any(regex.match(path) for regex in self._ignore_regexes)

    def ignore_patterns_for(self, path: str) -> list[str]:
        """Which ``ignore:`` patterns match ``path`` (the drift report's top-N).

        Uses the regexes compiled once at load time instead of recompiling per file.
        """
        return [
            pattern
            for pattern, regex in zip(self.ignore, self._ignore_regexes, strict=True)
            if regex.match(path)
        ]

    def is_ignored_author(self, username: str) -> bool:
        return any(fnmatch.fnmatchcase(username, pattern) for pattern in self.ignore_authors)

    # ------------------------------------------------------------ branch scope

    def branch_in_scope(self, team: Team, branch: str) -> bool:
        """Match the MR's *target* branch against the team's scope (§7.4)."""
        if any(fnmatch.fnmatchcase(branch, pattern) for pattern in team.exclude_branches):
            return False
        if not team.branches:
            return True
        return any(fnmatch.fnmatchcase(branch, pattern) for pattern in team.branches)

    # -------------------------------------------------------------- thresholds

    def passes_thresholds(self, team: Team, mr: MergeRequest, owned_files: int, now) -> bool:
        """Noise control: a team can opt out of tiny or very fresh merge requests."""
        if owned_files < team.min_owned_files:
            return False
        too_young = bool(team.min_mr_age_minutes) and mr.age_minutes(now) < team.min_mr_age_minutes
        return not too_young

    # ----------------------------------------------------------------- rosters

    def roster(
        self,
        team: Team,
        *,
        group_members: Callable[[str], Iterable[str]] | None = None,
        codeowners_users: Iterable[str] = (),
    ) -> set[str]:
        """Union of every membership source (§7.5).

        An empty result means membership cannot be proven, which callers must treat
        as "no owner approval" — never as silence.
        """
        names: set[str] = set(team.members)
        names.update(codeowners_users)
        if team.gitlab_group and group_members is not None:
            with contextlib.suppress(Exception):
                # A broken roster must not break a run: unproven membership simply
                # means "no owner approval" (§7.5).
                names.update(group_members(team.gitlab_group))
        return {name.lstrip("@") for name in names if name}

    # -------------------------------------------------------------- validation

    def validation_problems(self) -> list[str]:
        """Configuration smells, surfaced by the drift report (§7.2, §7.5)."""
        problems: list[str] = []

        seen_groups: dict[str, list[str]] = {}
        seen_labels: dict[str, list[str]] = {}
        seen_tokens: dict[str, list[str]] = {}
        for key, team in self.teams.items():
            problems.extend(_team_problems(key, team))
            _add_seen(seen_groups, team.gitlab_group, key, fold_case=True)
            _add_seen(seen_labels, team.label, key)
            _add_seen(seen_tokens, team.codeowners_token, key)

        problems.extend(
            _shared_value_problems(
                seen_groups,
                lambda group, keys: (
                    f"gitlab_group '{group}' is used by {len(keys)} teams {keys} — "
                    "every member would count for every one of them"
                ),
            )
        )
        problems.extend(
            _shared_value_problems(
                seen_labels, lambda label, keys: f"label '{label}' is shared by teams {keys}"
            )
        )
        problems.extend(
            _shared_value_problems(
                seen_tokens,
                lambda token, keys: f"codeowners_token '{token}' is shared by teams {keys}",
            )
        )

        return problems


def _as_tuple(value, name: str, team_key: str) -> tuple:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(value)
    raise ManifestError(f"team '{team_key}': '{name}' must be a list")


def _team_problems(key: str, team: Team) -> list[str]:
    problems: list[str] = []

    unknown = set(team.notify_on) - set(ALL_EVENTS)
    if unknown:
        problems.append(f"team '{key}': unknown notify_on entries {sorted(unknown)}")

    if not team.members and not team.gitlab_group:
        problems.append(
            f"team '{key}': no roster source at all — approvals can never be credited "
            "to this team (set gitlab_group or members)"
        )

    return problems


def _add_seen(
    index: dict[str, list[str]], value: str | None, key: str, *, fold_case: bool = False
) -> None:
    if not value:
        return
    index.setdefault(value.lower() if fold_case else value, []).append(key)


def _shared_value_problems(index: dict[str, list[str]], describe) -> list[str]:  # noqa: ANN001
    """Two teams claiming the same group, label or token is always a mistake."""
    return [describe(value, keys) for value, keys in index.items() if len(keys) > 1]


def _team_from(key: str, raw: dict, defaults: dict) -> Team:
    if not isinstance(raw, dict):
        raise ManifestError(f"team '{key}' must be a mapping")

    def pick(name: str, fallback):
        return raw.get(name, fallback)

    channel = pick("teams_channel", None)
    label = pick("label", None)
    if not channel:
        raise ManifestError(f"team '{key}': 'teams_channel' is required")
    if not label:
        raise ManifestError(f"team '{key}': 'label' is required")

    gitlab_group = pick("gitlab_group", None)
    token = pick("codeowners_token", None) or (f"@{gitlab_group}" if gitlab_group else None)
    if not token:
        raise ManifestError(
            f"team '{key}': set 'codeowners_token' (the token written in CODEOWNERS) "
            "or 'gitlab_group' to derive it"
        )

    notify_on = _as_tuple(pick("notify_on", defaults.get("notify_on")), "notify_on", key)
    return Team(
        key=key,
        channel=channel,
        label=label,
        codeowners_token=token,
        gitlab_group=gitlab_group,
        members=_as_tuple(pick("members", []), "members", key),
        branches=_as_tuple(pick("branches", defaults.get("branches", ["**"])), "branches", key),
        exclude_branches=_as_tuple(
            pick("exclude_branches", defaults.get("exclude_branches", [])),
            "exclude_branches",
            key,
        ),
        notify_on=frozenset(notify_on or ALL_EVENTS),
        min_mr_age_minutes=int(
            pick("min_mr_age_minutes", defaults.get("min_mr_age_minutes", 0)) or 0
        ),
        min_owned_files=int(pick("min_owned_files", defaults.get("min_owned_files", 1)) or 0),
        mentions=_as_tuple(pick("mentions", defaults.get("mentions", [])), "mentions", key),
    )


def _load_teams(raw_teams, defaults: dict, source: str) -> dict[str, Team]:  # noqa: ANN001
    if not isinstance(raw_teams, dict) or not raw_teams:
        raise ManifestError(f"{source}: 'teams' must be a non-empty mapping")
    return {key: _team_from(key, raw, defaults) for key, raw in raw_teams.items()}


def _load_overlay(data: dict, teams: dict[str, Team], source: str) -> list[OverlayRule]:
    overlay: list[OverlayRule] = []
    for entry in data.get("module_overlay") or ():
        if not isinstance(entry, dict) or "module" not in entry or "owner" not in entry:
            raise ManifestError(f"{source}: each module_overlay entry needs 'module' and 'owner'")
        raw_owners = entry["owner"]
        owners = (raw_owners,) if isinstance(raw_owners, str) else tuple(raw_owners)
        for owner in owners:
            if owner not in teams:
                raise ManifestError(f"{source}: module_overlay references unknown team '{owner}'")
        overlay.append(OverlayRule(identity_pattern=entry["module"], owners=owners))
    return overlay


def load_manifest_text(text: str, *, source: str = "teams.yml") -> Manifest:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ManifestError(f"{source}: top level must be a mapping")

    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ManifestError(f"{source}: 'defaults' must be a mapping")

    teams = _load_teams(data.get("teams"), defaults, source)
    token_to_team = {team.codeowners_token: key for key, team in teams.items()}
    ignore = tuple(data.get("ignore") or ())

    return Manifest(
        teams=teams,
        token_to_team=token_to_team,
        ignore=ignore,
        ignore_authors=tuple(data.get("ignore_authors") or ()),
        skip_label=data.get("skip_label") or "ownership-bot::skip",
        module_overlay=tuple(_load_overlay(data, teams, source)),
        source=source,
        _ignore_regexes=tuple(translate_pattern(pattern) for pattern in ignore),
    )


def load_manifest(path: str | Path) -> Manifest:
    text = Path(path).read_text(encoding="utf-8")
    return load_manifest_text(text, source=str(path))
