"""Drift detector (docs/design.md §7.2).

Reads every repo's CODEOWNERS plus ``teams.yml`` and reports what has gone stale:
patterns that match nothing after a code move, repos with no CODEOWNERS at all,
unclaimed files, unknown owner tokens, roster problems and what ``ignore:`` hides.

It only reports — it never notifies a team about a merge request.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .codeowners import parse, unknown_owner_tokens
from .gitlab import GitLab, GitLabError
from .teams import Manifest

log = logging.getLogger(__name__)

MAX_TREE_ENTRIES = 50_000


@dataclass
class ProjectDrift:
    project: str
    ref: str = ""
    codeowners_path: str = ""
    found: bool = False
    rules: int = 0
    files: int = 0
    matched_files: int = 0
    ignored_files: int = 0
    unclaimed_files: int = 0
    stale_patterns: list[str] = field(default_factory=list)
    unknown_owner_tokens: list[str] = field(default_factory=list)
    syntax_problems: list[str] = field(default_factory=list)
    truncated_tree: bool = False

    @property
    def uncovered(self) -> bool:
        return not self.found or self.rules == 0 or self.matched_files == 0


@dataclass
class DriftReport:
    group: str
    ref: str
    projects: list[ProjectDrift] = field(default_factory=list)
    roster_problems: list[str] = field(default_factory=list)
    index: dict[str, list[dict]] = field(default_factory=dict)
    top_ignored: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "group": self.group,
            "ref": self.ref,
            "roster_problems": self.roster_problems,
            "top_ignored": self.top_ignored,
            "projects": [asdict(project) | {"uncovered": project.uncovered} for project in self.projects],
            "index": self.index,
        }


def _rules_index(
    parsed, tree: list[str], manifest: Manifest, entry: ProjectDrift  # noqa: ANN001
) -> list[dict]:
    """Per-rule file counts; a rule that matches nothing is stale (§7.2)."""
    index_entries: list[dict] = []
    for rule in parsed.rules:
        matches = [path for path in tree if rule.regex.match(path)]
        if not matches:
            entry.stale_patterns.append(rule.pattern)
            continue
        index_entries.append(
            {
                "pattern": rule.pattern,
                "teams": sorted(
                    {manifest.team_for_token(token) or token for token in rule.owners}
                ),
                "files": len(matches),
            }
        )
    return index_entries


def _count_files(
    tree: list[str],
    manifest: Manifest,
    parsed,  # noqa: ANN001
    entry: ProjectDrift,
    ignored_counts: dict[str, int],
) -> None:
    """Split the repository into ignored / owned / unclaimed (§7.1)."""
    for path in tree:
        if manifest.is_ignored(path):
            entry.ignored_files += 1
            for pattern in manifest.ignore_patterns_for(path):
                ignored_counts[pattern] = ignored_counts.get(pattern, 0) + 1
            continue
        if parsed.rule_for(path) is not None:
            entry.matched_files += 1
        else:
            entry.unclaimed_files += 1


def _scan_project_content(
    client: GitLab,
    manifest: Manifest,
    project: dict,
    ref: str,
    entry: ProjectDrift,
    ignored_counts: dict[str, int],
) -> tuple[ProjectDrift, list[dict]]:
    text, path = client.codeowners(project["id"], ref)
    entry.codeowners_path = path
    entry.found = text is not None
    parsed = parse(text or "")

    try:
        tree = client.tree_paths(project["id"], ref)
    except GitLabError as exc:
        log.warning("%s: could not list tree: %s", entry.project, exc)
        tree = []

    if len(tree) > MAX_TREE_ENTRIES:
        entry.truncated_tree = True
        tree = tree[:MAX_TREE_ENTRIES]

    entry.rules = len(parsed.rules)
    entry.files = len(tree)
    entry.syntax_problems = list(parsed.problems)
    entry.unknown_owner_tokens = unknown_owner_tokens(parsed, manifest.token_to_team)

    index_entries = _rules_index(parsed, tree, manifest, entry)
    _count_files(tree, manifest, parsed, entry, ignored_counts)
    return entry, index_entries


def _scan_project(
    client: GitLab,
    manifest: Manifest,
    project: dict,
    ref: str,
    ignored_counts: dict[str, int],
) -> tuple[ProjectDrift, list[dict]]:
    entry = ProjectDrift(
        project=project.get("path_with_namespace") or str(project.get("id")),
        ref=ref or project.get("default_branch") or "main",
    )
    try:
        return _scan_project_content(client, manifest, project, entry.ref, entry, ignored_counts)
    except GitLabError as exc:
        log.exception("%s: drift scan failed", entry.project)
        entry.syntax_problems.append(f"scan failed: {exc}")
        return entry, []


def run_drift(
    *,
    manifest: Manifest,
    client: GitLab,
    group: str,
    ref: str = "",
    out_json: Path = Path("drift-report.json"),
    out_md: Path = Path("drift-report.md"),
) -> DriftReport:
    report = DriftReport(group=group, ref=ref or "<each project's default branch>")
    report.roster_problems = manifest.validation_problems()

    try:
        projects = client.group_projects(group)
    except GitLabError:
        log.exception("could not list projects in %s", group)
        return report

    ignored_counts: dict[str, int] = {}
    for project in projects:
        entry, index_entries = _scan_project(client, manifest, project, ref, ignored_counts)
        report.index[entry.project] = index_entries
        report.projects.append(entry)

    report.top_ignored = dict(
        sorted(ignored_counts.items(), key=lambda item: item[1], reverse=True)[:10]
    )

    out_json.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    out_md.write_text(render_markdown(report), encoding="utf-8")
    log.info("drift report written: %s, %s", out_json, out_md)
    return report


def render_markdown(report: DriftReport) -> str:
    lines = [f"# Ownership drift report — {report.group}", ""]

    uncovered = [project for project in report.projects if project.uncovered]
    stale = [(project, pattern) for project in report.projects for pattern in project.stale_patterns]

    lines.append(f"- projects scanned: **{len(report.projects)}**")
    lines.append(f"- uncovered repos: **{len(uncovered)}**")
    lines.append(f"- stale patterns: **{len(stale)}**")
    lines.append(f"- roster problems: **{len(report.roster_problems)}**")
    lines.append("")

    if uncovered:
        lines += ["## Uncovered repositories", ""]
        lines += [f"- `{project.project}` (rules={project.rules}, files={project.files})" for project in uncovered]
        lines.append("")

    if stale:
        lines += ["## Stale patterns (a code move broke these)", ""]
        for project, pattern in stale:
            lines.append(f"- `{project.project}`: `{pattern}`")
        lines.append("")

    if report.roster_problems:
        lines += ["## Roster and policy problems", ""]
        lines += [f"- {problem}" for problem in report.roster_problems]
        lines.append("")

    if report.top_ignored:
        lines += ["## Most-ignored paths (`ignore:`)", ""]
        lines += [f"- `{pattern}`: {count} files" for pattern, count in report.top_ignored.items()]
        lines.append("")

    unclaimed = sorted(report.projects, key=lambda p: p.unclaimed_files, reverse=True)[:10]
    if unclaimed and any(project.unclaimed_files for project in unclaimed):
        lines += ["## Unclaimed files by repo (top 10)", ""]
        lines += [
            f"- `{project.project}`: {project.unclaimed_files} of {project.files}"
            for project in unclaimed
            if project.unclaimed_files
        ]
        lines.append("")

    return "\n".join(lines) + "\n"
