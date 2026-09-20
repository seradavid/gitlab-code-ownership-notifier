"""teams.yml: defaults, branch scope, thresholds, rosters, validation smells."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from conftest import make_mr

from ownership_bot.teams import ManifestError, load_manifest_text


def test_team_overrides_win_over_defaults(manifest):
    payments = manifest.team("payments")
    platform = manifest.team("platform")

    assert payments.branches == ("master", "develop", "release/**")
    assert platform.branches == ("**",)  # from defaults
    assert payments.min_owned_files == 1
    assert platform.min_owned_files == 1


def test_missing_required_fields_are_rejected():
    with pytest.raises(ManifestError, match="teams_channel"):
        load_manifest_text("teams:\n  a:\n    label: x\n")


def test_team_needs_a_roster_source_token():
    with pytest.raises(ManifestError, match="codeowners_token"):
        load_manifest_text(
            "teams:\n  a:\n    teams_channel: c\n    label: l\n    members: [x]\n"
        )


def test_token_is_derived_from_gitlab_group_when_absent():
    manifest = load_manifest_text(
        "teams:\n  a:\n    gitlab_group: acme/teams/a\n    teams_channel: c\n    label: l\n"
    )
    assert manifest.team("a").codeowners_token == "@acme/teams/a"
    assert manifest.team_for_token("@acme/teams/a") == "a"


@pytest.mark.parametrize(
    "branch,expected",
    [
        ("master", True),
        ("develop", True),
        ("release/1.2", True),
        ("prod", False),
        ("feature/x", False),
    ],
)
def test_branch_scope_uses_target_branch(manifest, branch, expected):
    assert manifest.branch_in_scope(manifest.team("payments"), branch) is expected


def test_devops_is_scoped_to_production_branches(manifest):
    devops = manifest.team("devops")
    assert manifest.branch_in_scope(devops, "prod")
    assert manifest.branch_in_scope(devops, "release/2.0")
    assert not manifest.branch_in_scope(devops, "master")


def test_exclude_branches_wins_over_include():
    manifest = load_manifest_text(
        """
        teams:
          a:
            codeowners_token: "@a"
            members: [x]
            teams_channel: c
            label: l
            branches: ["**"]
            exclude_branches: ["hotfix/**"]
        """
    )
    team = manifest.team("a")
    assert manifest.branch_in_scope(team, "main")
    assert not manifest.branch_in_scope(team, "hotfix/urgent")


def test_thresholds():
    manifest = load_manifest_text(
        """
        teams:
          a:
            codeowners_token: "@a"
            members: [x]
            teams_channel: c
            label: l
            min_owned_files: 2
            min_mr_age_minutes: 30
        """
    )
    team = manifest.team("a")
    now = datetime.now(UTC)

    fresh = make_mr(age_minutes=5)
    old = make_mr(age_minutes=120)

    assert not manifest.passes_thresholds(team, fresh, owned_files=5, now=now)
    assert not manifest.passes_thresholds(team, old, owned_files=1, now=now)
    assert manifest.passes_thresholds(team, old, owned_files=2, now=now)


def test_roster_is_the_union_of_all_sources(manifest):
    payments = manifest.team("payments")
    roster = manifest.roster(
        payments, group_members=lambda group: {"zoe", "bob"}, codeowners_users={"alice"}
    )
    assert roster == {"alice", "bob", "zoe"}


def test_unresolved_roster_is_empty_not_silent(manifest):
    team = manifest.team("platform")
    assert manifest.roster(team) == {"erin"}

    orphan = load_manifest_text(
        "teams:\n  a:\n    codeowners_token: '@a'\n    gitlab_group: acme/teams/a\n"
        "    teams_channel: c\n    label: l\n"
    ).team("a")
    assert manifest.roster(orphan, group_members=lambda group: set()) == set()


def test_ignore_and_author_patterns(manifest):
    assert manifest.is_ignored("service/target/classes/A.class")
    assert manifest.is_ignored("docs/readme.md")
    assert not manifest.is_ignored("src/main/java/A.java")
    assert manifest.is_ignored_author("renovate-bot")
    assert manifest.is_ignored_author("deps-ci-bot")
    assert not manifest.is_ignored_author("alice")


def test_validation_problems_are_reported():
    manifest = load_manifest_text(
        """
        teams:
          a:
            codeowners_token: "@shared"
            gitlab_group: acme/one-group
            members: [x]
            teams_channel: c1
            label: same-label
          b:
            codeowners_token: "@shared"
            gitlab_group: acme/one-group
            members: [y]
            teams_channel: c2
            label: same-label
          c:
            codeowners_token: "@c"
            teams_channel: c3
            label: c-label
        """
    )
    problems = " | ".join(manifest.validation_problems())
    assert "acme/one-group" in problems
    assert "same-label" in problems
    assert "codeowners_token '@shared'" in problems
    assert "no roster source" in problems
