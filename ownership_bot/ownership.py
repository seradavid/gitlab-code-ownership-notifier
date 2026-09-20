"""Resolve a changed file (or a set of them) to owning teams."""

from __future__ import annotations

import fnmatch

from .codeowners import ParsedCodeowners, Rule
from .models import OwnershipMatch, OwnershipResult
from .teams import Manifest


def _overlay_matches(manifest: Manifest, identity: str) -> list[tuple[str, str]]:
    """Return ``(team, identity_pattern)`` for every matching overlay rule."""
    hits: list[tuple[str, str]] = []
    for rule in manifest.module_overlay:
        if fnmatch.fnmatchcase(identity, rule.identity_pattern):
            hits.extend((owner, rule.identity_pattern) for owner in rule.owners)
    return hits


def users_on_team_lines(parsed: ParsedCodeowners, token: str) -> set[str]:
    """``@user`` tokens listed on the same line as ``token`` (§7.5).

    Same-line association keeps the rule unambiguous: a user belongs to the team
    named on their line.
    """
    users: set[str] = set()
    for rule in parsed.rules:
        if token in rule.owners:
            users.update(owner for owner in rule.owners if owner.startswith("@") and "/" not in owner)
    return users


def _codeowners_owner(
    path: str,
    rule: Rule | None,
    manifest: Manifest,
    unknown_tokens: set[str],
    raw: dict[tuple[str, str, str], set[str]],
) -> bool:
    """Record the CODEOWNERS rule that wins for ``path``; unknown tokens are collected."""
    if rule is None:
        return False

    owned = False
    for token in rule.owners:
        team = manifest.team_for_token(token)
        if team is None:
            unknown_tokens.add(token)
            continue
        raw.setdefault((team, "codeowners", rule.pattern), set()).add(path)
        owned = True
    return owned


def _overlay_owner(
    path: str, identity: str | None, manifest: Manifest, raw: dict[tuple[str, str, str], set[str]]
) -> bool:
    """Record the optional module-overlay hit for ``path``."""
    if not identity:
        return False

    hits = _overlay_matches(manifest, identity)
    for team, _pattern in hits:
        raw.setdefault((team, "module_overlay", identity), set()).add(path)
    return bool(hits)


def _finalise(
    result: OwnershipResult,
    raw: dict[tuple[str, str, str], set[str]],
    unknown_tokens: set[str],
) -> OwnershipResult:
    for (team, kind, value), files in raw.items():
        result.matches.setdefault(team, []).append(
            OwnershipMatch(team=team, kind=kind, value=value, files=tuple(sorted(files)))
        )

    for team in result.matches:
        result.matches[team].sort(key=lambda match: (match.kind, match.value))

    if unknown_tokens:
        result.problems.append(
            f"CODEOWNERS names owner token(s) with no teams.yml entry: {sorted(unknown_tokens)}"
        )

    return result


def resolve_ownership(
    *,
    changed_files: list[str],
    manifest: Manifest,
    codeowners: ParsedCodeowners,
    identity_for: callable | None = None,
    codeowners_ref: str = "",
    codeowners_found: bool = True,
    diffs_truncated: bool = False,
    extra_problems: list[str] | None = None,
) -> OwnershipResult:
    """Apply ignore → CODEOWNERS → module overlay, per file (§7.1)."""

    result = OwnershipResult(
        files_total=len(changed_files),
        codeowners_ref=codeowners_ref,
        codeowners_found=codeowners_found,
        diffs_truncated=diffs_truncated,
        problems=list(codeowners.problems) + list(extra_problems or []),
    )

    unknown_tokens: set[str] = set()
    raw: dict[tuple[str, str, str], set[str]] = {}

    for path in changed_files:
        if manifest.is_ignored(path):
            result.files_ignored += 1
            continue

        identity = identity_for(path) if identity_for is not None else None
        owned = _codeowners_owner(path, codeowners.rule_for(path), manifest, unknown_tokens, raw)
        owned = _overlay_owner(path, identity, manifest, raw) or owned

        if not owned:
            result.files_unclaimed += 1

    return _finalise(result, raw, unknown_tokens)
