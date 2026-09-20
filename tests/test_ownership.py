"""File → owner resolution: ignore first, CODEOWNERS, then the optional overlay."""

from __future__ import annotations

from ownership_bot.codeowners import parse
from ownership_bot.ownership import resolve_ownership, users_on_team_lines


def test_ignore_wins_over_codeowners(manifest, codeowners):
    result = resolve_ownership(
        changed_files=["src/main/java/com/acme/payments/A.java", "service/target/B.java"],
        manifest=manifest,
        codeowners=codeowners,
    )
    assert result.files_total == 2
    assert result.files_ignored == 1
    assert result.files_owned == 1
    assert result.teams() == {"payments"}


def test_codeowners_pattern_and_files_are_reported(manifest, codeowners):
    result = resolve_ownership(
        changed_files=["src/main/java/com/acme/payments/deep/Client.java"],
        manifest=manifest,
        codeowners=codeowners,
    )
    match = result.matches["payments"][0]
    assert match.kind == "codeowners"
    assert match.value == "src/main/java/com/acme/payments/**"
    assert match.files == ("src/main/java/com/acme/payments/deep/Client.java",)


def test_repo_default_owner_covers_unknown_paths(manifest, codeowners):
    result = resolve_ownership(
        changed_files=["tools/scripts/run.sh"], manifest=manifest, codeowners=codeowners
    )
    assert result.teams() == {"platform"}
    assert result.files_unclaimed == 0


def test_unclaimed_when_no_pattern_matches(manifest):
    result = resolve_ownership(
        changed_files=["src/a.ts"], manifest=manifest, codeowners=parse("lib/** @x")
    )
    assert result.files_unclaimed == 1
    assert result.teams() == set()


def test_module_overlay_is_unioned_with_codeowners(manifest):
    """A component that moved out of its old path is still owned by the same team."""
    codeowners = parse("libs/payment-core/** @acme/teams/payments")
    result = resolve_ownership(
        changed_files=["libs/payment-core/src/main/java/A.java"],
        manifest=manifest,
        codeowners=codeowners,
        identity_for=lambda path: "com.acme:payment-core",
    )

    by_kind = {match.kind: match for match in result.matches["payments"]}
    assert set(by_kind) == {"codeowners", "module_overlay"}
    assert by_kind["codeowners"].value == "libs/payment-core/**"
    assert by_kind["module_overlay"].value == "com.acme:payment-core"


def test_module_overlay_adds_owners_codeowners_does_not_know(manifest):
    result = resolve_ownership(
        changed_files=["libs/payment-core/src/main/java/A.java"],
        manifest=manifest,
        codeowners=parse("libs/** @acme/teams/platform"),
        identity_for=lambda path: "com.acme:payment-core",
    )
    assert result.teams() == {"platform", "payments"}


def test_module_overlay_does_not_apply_when_identity_differs(manifest, codeowners):
    result = resolve_ownership(
        changed_files=["libs/other/src/main/java/A.java"],
        manifest=manifest,
        codeowners=codeowners,
        identity_for=lambda path: "com.acme:other",
    )
    assert all(
        match.kind == "codeowners" for matches in result.matches.values() for match in matches
    )


def test_unknown_tokens_are_reported(manifest):
    result = resolve_ownership(
        changed_files=["a/b.ts"],
        manifest=manifest,
        codeowners=parse("a/** @nobody"),
    )
    assert result.teams() == set()
    assert any("@nobody" in problem for problem in result.problems)


def test_users_on_team_lines_only_counts_same_line(manifest, codeowners):
    assert users_on_team_lines(codeowners, "@acme/teams/payments") == {"@alice"}
    assert users_on_team_lines(codeowners, "@acme/teams/devops") == set()
