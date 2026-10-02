"""Drift report: stale patterns, unclaimed files, the index and the markdown output."""

from __future__ import annotations

from conftest import MANIFEST_TEXT

from ownership_bot.drift import DriftReport, ProjectDrift, render_markdown, run_drift
from ownership_bot.teams import load_manifest_text

# No fallback rule, so a stale pattern and unclaimed files are possible.
CODEOWNERS = """
[Payments]
src/main/java/com/acme/payments/** @acme/teams/payments
src/legacy/** @acme/teams/devops
"""

TREE = [
    "src/main/java/com/acme/payments/Client.java",
    "src/main/java/com/acme/platform/Util.java",
    "README.md",
]


class StubClient:
    def __init__(self, codeowners: str | None, tree: list[str], projects=None):
        self.codeowners_text = codeowners
        self.tree = tree
        self.projects = projects or []

    def codeowners(self, project, ref):  # noqa: ANN001
        return self.codeowners_text, ".gitlab/CODEOWNERS" if self.codeowners_text else "CODEOWNERS"

    def tree_paths(self, project, ref):  # noqa: ANN001
        return list(self.tree)

    def group_projects(self, group, include_subgroups=True):  # noqa: ANN001
        return list(self.projects)


def make_manifest():
    return load_manifest_text(MANIFEST_TEXT)


def test_drift_flags_stale_patterns_and_counts_files(tmp_path):
    client = StubClient(
        CODEOWNERS,
        TREE,
        projects=[{"id": 1, "path_with_namespace": "acme/a", "default_branch": "master"}],
    )
    report = run_drift(
        manifest=make_manifest(),
        client=client,
        group="acme",
        out_json=tmp_path / "drift.json",
        out_md=tmp_path / "drift.md",
    )

    entry = report.projects[0]
    assert entry.rules == 2
    assert entry.files == 3
    assert entry.matched_files == 1
    assert entry.unclaimed_files == 2
    assert entry.stale_patterns == ["src/legacy/**"]
    assert entry.uncovered is False

    # the index has one entry per non-stale rule
    assert [row["pattern"] for row in report.index["acme/a"]] == ["src/main/java/com/acme/payments/**"]

    assert (tmp_path / "drift.json").exists()
    assert (tmp_path / "drift.md").exists()


def test_repo_without_codeowners_is_uncovered(tmp_path):
    client = StubClient(
        None,
        TREE,
        projects=[{"id": 1, "path_with_namespace": "acme/no-ownership", "default_branch": "main"}],
    )
    report = run_drift(
        manifest=make_manifest(),
        client=client,
        group="acme",
        out_json=tmp_path / "drift.json",
        out_md=tmp_path / "drift.md",
    )

    entry = report.projects[0]
    assert entry.found is False
    assert entry.uncovered is True
    assert entry.unclaimed_files == 3


def test_report_to_dict_adds_the_uncovered_flag():
    report = DriftReport(group="acme", ref="master")
    report.projects.append(ProjectDrift(project="acme/a", found=True, rules=1, matched_files=0))

    data = report.to_dict()

    assert data["projects"][0]["uncovered"] is True


def test_markdown_lists_uncovered_stale_and_roster_problems():
    report = DriftReport(group="acme", ref="master")
    report.projects.append(ProjectDrift(project="acme/a", found=False, files=10))
    report.roster_problems = ["team 'x': no roster source"]
    report.top_ignored = {"docs/**": 5}

    markdown = render_markdown(report)

    assert "Uncovered repositories" in markdown
    assert "acme/a" in markdown
    assert "Roster and policy problems" in markdown
    assert "Most-ignored paths" in markdown


def test_drift_survives_an_unwritable_output_path(tmp_path):
    """The report is advisory; a bad path must not fail the job (§11)."""
    client = StubClient(
        None,
        TREE,
        projects=[{"id": 1, "path_with_namespace": "acme/a", "default_branch": "main"}],
    )
    unwritable = tmp_path / "missing-dir" / "drift.json"

    report = run_drift(
        manifest=make_manifest(),
        client=client,
        group="acme",
        out_json=unwritable,
        out_md=tmp_path / "drift.md",
    )

    assert len(report.projects) == 1  # it still produced the report in memory
