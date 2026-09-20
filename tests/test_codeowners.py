"""CODEOWNERS parsing semantics (§7.1) — the subtle rules are pinned here."""

from __future__ import annotations

import pytest

from ownership_bot.codeowners import (
    parse,
    team_pattern_delta,
    translate_pattern,
    unknown_owner_tokens,
)


def test_last_matching_pattern_wins():
    parsed = parse(
        """
        *.java @first
        src/**/*.java @second
        """
    )
    assert parsed.owners_for("src/a/B.java") == ("@second",)
    assert parsed.owners_for("other/C.java") == ("@first",)


def test_unmatched_path_is_unclaimed():
    parsed = parse("src/** @team")
    assert parsed.owners_for("docs/readme.md") == ()


def test_pattern_without_slash_matches_at_any_depth():
    parsed = parse("*.md @docs")
    assert parsed.owners_for("README.md") == ("@docs",)
    assert parsed.owners_for("docs/deep/notes.md") == ("@docs",)


def test_leading_slash_anchors_to_repository_root():
    parsed = parse("/docs/** @docs")
    assert parsed.owners_for("docs/a.md") == ("@docs",)
    assert parsed.owners_for("src/docs/a.md") == ()


def test_single_star_does_not_cross_directory_separators():
    shallow = translate_pattern("src/*.ts")
    deep = translate_pattern("src/**/*.ts")
    assert shallow.match("src/a.ts")
    assert not shallow.match("src/a/b.ts")
    assert deep.match("src/a.ts")
    assert deep.match("src/a/b.ts")


def test_braces_and_character_classes():
    parsed = parse("web-{a,b}/src/** @web\nlib/[a-z]*.ts @lib")
    assert parsed.owners_for("web-a/src/index.ts") == ("@web",)
    assert parsed.owners_for("web-c/src/index.ts") == ()
    assert parsed.owners_for("lib/model.ts") == ("@lib",)


def test_double_star_suffix_matches_directory_contents():
    parsed = parse("plugins/** @plugins")
    assert parsed.owners_for("plugins/a/b.ts") == ("@plugins",)


def test_sections_are_recorded_but_not_matched():
    parsed = parse("[Infra]\n*.tf @sre\n")
    assert parsed.sections == ["Infra"]
    assert parsed.rules[0].section == "Infra"
    assert parsed.owners_for("main.tf") == ("@sre",)


def test_comments_and_trailing_comments_are_ignored():
    parsed = parse("# a comment\n*.py @python  # inline comment\n\n")
    assert len(parsed.rules) == 1
    assert parsed.rules[0].pattern == "*.py"
    assert parsed.problems == []


def test_invalid_lines_are_reported_not_silently_interpreted():
    parsed = parse("*.py\n!*.lock @a\n*.js not-a-token\n")
    assert len(parsed.rules) == 1  # only the .js rule is usable
    assert any("no owner" in problem for problem in parsed.problems)
    assert any("negation" in problem for problem in parsed.problems)
    assert any("unrecognised owner token" in problem for problem in parsed.problems)


def test_multiple_owners_on_one_line():
    parsed = parse("api/** @acme/teams/api @acme/teams/sre alice@acme.com")
    assert parsed.owners_for("api/x.ts") == (
        "@acme/teams/api",
        "@acme/teams/sre",
        "alice@acme.com",
    )


def test_unknown_owner_tokens_are_listed():
    parsed = parse("a/** @known\nb/** @stranger")
    assert unknown_owner_tokens(parsed, {"@known": "team-a"}) == ["@stranger"]


def test_team_pattern_delta_detects_additions_and_removals():
    token_to_team = {"@acme/teams/payments": "payments", "@acme/teams/sre": "sre"}
    before = parse("a/** @acme/teams/payments\nb/** @acme/teams/sre")
    after = parse("a/** @acme/teams/payments\nb/** @acme/teams/sre\nc/** @acme/teams/payments")

    delta = team_pattern_delta(before, after, token_to_team)
    assert delta["payments"].added == ["c/**"]
    assert delta["payments"].removed == []
    assert "sre" not in delta

    removed = team_pattern_delta(after, before, token_to_team)
    assert removed["payments"].removed == ["c/**"]


def test_team_pattern_delta_reports_unknown_tokens_separately():
    delta = team_pattern_delta(parse("a/** @known"), parse("a/** @stranger"), {"@known": "known"})
    assert "known" in delta
    assert "__unknown__@stranger" in delta
    assert delta["known"].removed == ["a/**"]
    assert not team_pattern_delta(parse("a/** @known"), parse("a/** @known"), {"@known": "known"})
    assert "__unknown__@stranger" in delta


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        ("*", "anything/at/all.ts", True),
        ("src/api/**", "src/api/v1/x.ts", True),
        ("src/api/**", "src/other/x.ts", False),
        ("**/*.md", "deep/nested/doc.md", True),
        ("*.md", "deep/nested/doc.md", True),
    ],
)
def test_pattern_matrix(pattern, path, expected):
    assert bool(translate_pattern(pattern).match(path)) is expected
