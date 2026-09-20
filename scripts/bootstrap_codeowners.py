#!/usr/bin/env python3
"""Phase 0/1 bootstrap (docs/design.md appendix B, §12).

Turns a human-written intent mapping into reviewable CODEOWNERS drafts:

    python scripts/bootstrap_codeowners.py \
        --group acme/services \
        --mapping mapping.yml \
        --teams-file examples/teams.yml \
        --out-dir drafts \
        --suggest-from-history

What it does **not** do: guess ownership. The mapping file states intent
("this path belongs to this team"); this script only renders it, checks it against
`teams.yml`, and reports which parts of each repository would stay *unclaimed* so a
human can decide about them before the draft is merged.

Outputs, into `--out-dir` (or stdout with `--out-dir -`):

* `<project>.CODEOWNERS` — a draft per repository, ready for a merge request;
* `bootstrap-report.md` — coverage per repository, uncovered directories, git-history
  author suggestions, and a `teams.yml` skeleton for any token that is not yet known.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ownership_bot.gitlab import GitLab, GitLabError  # noqa: E402
from ownership_bot.rollout import (  # noqa: E402
    RepoDraft,
    load_mapping,
    render_codeowners,
    uncovered_prefixes,
    validate_codeowners,
)
from ownership_bot.teams import ManifestError, load_manifest  # noqa: E402

log = logging.getLogger("bootstrap")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--group", help="top-level group to scan (defaults to the mapping's repositories)")
    parser.add_argument("--mapping", required=True, help="mapping.yml: repository -> intent")
    parser.add_argument("--teams-file", help="teams.yml, to validate owner tokens and build rosters")
    parser.add_argument("--out-dir", default="drafts", help="where drafts are written ('-' for stdout)")
    parser.add_argument("--ref", default="", help="ref to scan (default: each project's default branch)")
    parser.add_argument(
        "--suggest-from-history",
        action="store_true",
        help="suggest owners from recent commit authors for uncovered directories",
    )
    parser.add_argument("--history-limit", type=int, default=50, help="commits per directory")
    return parser.parse_args(argv)


def known_tokens(manifest) -> set[str]:  # noqa: ANN001
    tokens = set(manifest.token_to_team)
    for team in manifest.teams.values():
        tokens.update(f"@{member}" for member in team.members)
        tokens.update(team.mentions)
    return tokens


def suggest_owners(
    client: GitLab,
    project: str,
    prefix: str,
    ref: str,
    rosters: dict[str, set[str]],
    limit: int,
) -> list[str]:
    """Map recent commit authors of ``prefix`` onto roster usernames (suggestion only)."""
    try:
        counts = client.commit_authors(project, prefix, ref, limit=limit)
    except GitLabError as exc:
        log.warning("%s: history lookup for %s failed: %s", project, prefix, exc)
        return []

    suggestions: list[str] = []
    for email, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        local = email.split("@")[0].replace(".", "-")
        matches = [team for team, names in rosters.items() if local in names or email.split("@")[0] in names]
        if matches:
            suggestions.append(f"{email} ({count} commit(s)) -> {', '.join(matches)}")
        else:
            suggestions.append(f"{email} ({count} commit(s)) -> no roster match, decide by hand")
    return suggestions


def scan(
    client: GitLab,
    draft: RepoDraft,
    ref: str,
    known: set[str],
    manifest,  # noqa: ANN001
    suggest: bool,
    history_limit: int,
) -> tuple[str, list[str], list[str], list[str]]:
    """Return ``(codeowners_text, problems, uncovered, suggestions)`` for one project."""
    text = render_codeowners(draft)
    problems = validate_codeowners(text, known_tokens=known)

    tree: list[str] = []
    try:
        tree = client.tree_paths(draft.project, ref)
    except GitLabError as exc:
        problems.append(f"cannot list repository tree: {exc}")

    uncovered = uncovered_prefixes(tree, draft.rules)
    suggestions: list[str] = []
    if suggest and uncovered:
        rosters = {team.key: set(team.members) for team in manifest.teams.values()}
        for prefix in uncovered:
            suggestions.extend(
                f"{prefix}: {line}"
                for line in suggest_owners(
                    client, draft.project, prefix, ref, rosters, history_limit
                )
            )
    return text, problems, uncovered, suggestions


def teams_skeleton(mapping: dict[str, RepoDraft], known: set[str]) -> str:
    """A `teams.yml` skeleton for every owner token that no team claims yet."""
    missing: dict[str, str] = {}
    for draft in mapping.values():
        for owners in draft.rules.values():
            for token in owners.split():
                if token.startswith("@") and token not in known:
                    missing.setdefault(token, draft.project)

    if not missing:
        return "# every owner token in the mapping is already known to teams.yml\n"

    lines = ["# New owner tokens found in the mapping — fill these in, then delete the TODOs.", "teams:"]
    for token, project in sorted(missing.items()):
        key = token.lstrip("@").rsplit("/", 1)[-1].replace(".", "-")
        lines += [
            f"  {key}:",
            f"    codeowners_token: \"{token}\"",
            "    gitlab_group: TODO            # or members: [TODO]",
            f"    teams_channel: TODO           # first seen in {project}",
            f"    label: team::{key}",
        ]
    return "\n".join(lines) + "\n"


def render_report(
    drafts: dict[str, RepoDraft],
    results: dict[str, tuple[str, list[str], list[str], list[str]]],
    skeleton: str,
) -> str:
    lines = ["# CODEOWNERS bootstrap report", ""]

    for project, (_text, problems, uncovered, suggestions) in results.items():
        draft = drafts[project]
        covered = len(draft.rules)
        lines.append(f"## {project}")
        lines.append("")
        lines.append(f"- rules rendered: {covered}")
        lines.append(f"- fallback (`*`): {draft.default_owner or '_none_'}")
        lines.append(f"- uncovered directories: {len(uncovered)}")
        if problems:
            lines.append("- problems:")
            lines.extend(f"  - {problem}" for problem in problems)
        if uncovered:
            lines.append("- directories with no owner (decide before merging):")
            lines.extend(f"  - `{prefix}`" for prefix in uncovered)
        if suggestions:
            lines.append("- git-history suggestions (review, never applied automatically):")
            lines.extend(f"  - {line}" for line in suggestions)
        lines.append("")

    lines += ["## teams.yml skeleton", "", "```yaml", skeleton.rstrip(), "```", ""]
    return "\n".join(lines)


ScanResult = tuple[str, list[str], list[str], list[str]]


def _warn_about_group(client: GitLab, group: str, mapping: dict[str, RepoDraft]) -> None:
    """The mapping is the source of truth; membership in the group is only a sanity check."""
    try:
        in_group = {project["path_with_namespace"] for project in client.group_projects(group)}
    except GitLabError:
        log.exception("cannot list group %s", group)
        return

    for project in mapping:
        if project not in in_group:
            log.warning("%s is not in group %s — scanning it anyway", project, group)


def _scan_one(
    client: GitLab,
    project: str,
    draft: RepoDraft,
    args: argparse.Namespace,
    known: set[str],
    manifest,  # noqa: ANN001
) -> ScanResult:
    try:
        info = client.project(project)
    except GitLabError as exc:
        log.exception("%s: project not readable", project)
        return render_codeowners(draft), [f"project not readable: {exc}"], [], []

    ref = args.ref or info.get("default_branch") or "main"
    result = scan(
        client,
        draft,
        ref,
        known,
        manifest,
        args.suggest_from_history,
        args.history_limit,
    )
    _text, problems, uncovered, _suggestions = result
    log.info(
        "%s@%s: %d rule(s), %d uncovered, %d problem(s)",
        project,
        ref,
        len(draft.rules),
        len(uncovered),
        len(problems),
    )
    return result


def _scan_all(
    client: GitLab,
    mapping: dict[str, RepoDraft],
    args: argparse.Namespace,
    known: set[str],
    manifest,  # noqa: ANN001
) -> dict[str, ScanResult]:
    if args.group:
        _warn_about_group(client, args.group, mapping)
    return {
        project: _scan_one(client, project, draft, args, known, manifest)
        for project, draft in sorted(mapping.items())
    }


def _emit(results: dict[str, ScanResult], report: str, out_dir: str) -> None:
    if out_dir == "-":
        for project, (text, *_rest) in results.items():
            print(f"# ----- {project} -----")
            print(text)
        print(report)
        return

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for project, (text, *_rest) in results.items():
        (out / f"{project.replace('/', '__')}.CODEOWNERS").write_text(text, encoding="utf-8")
    (out / "bootstrap-report.md").write_text(report, encoding="utf-8")
    log.info("wrote %d draft(s) and bootstrap-report.md to %s", len(results), out)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    mapping = load_mapping(Path(args.mapping).read_text(encoding="utf-8"), source=args.mapping)

    manifest = None
    if args.teams_file:
        try:
            manifest = load_manifest(args.teams_file)
        except (ManifestError, OSError):
            log.exception("cannot read %s", args.teams_file)
            return 2

    known = known_tokens(manifest) if manifest else set()
    client = GitLab.from_env()
    if client is None:
        log.error("set OWNERSHIP_BOT_TOKEN / GITLAB_TOKEN to read repositories")
        return 2

    results = _scan_all(client, mapping, args, known, manifest)
    report = render_report(mapping, results, teams_skeleton(mapping, known))
    _emit(results, report, args.out_dir)
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main(sys.argv[1:]))
