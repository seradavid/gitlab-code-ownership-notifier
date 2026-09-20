"""Decision matrix (docs/design.md §9, §13).

These tests encode the agreed behaviour, including the deliberate oddities:
no draft gate, ownership changes bypassing branch scope, and an owner approval
suppressing every notification for that team.
"""

from __future__ import annotations

from datetime import UTC, datetime

from conftest import make_mr

from ownership_bot.codeowners import parse, team_pattern_delta
from ownership_bot.decisions import decide_merge_audit, decide_mr_check
from ownership_bot.models import (
    MERGED_WITHOUT_OWNER_APPROVAL,
    MR_PIPELINE_GREEN,
    OWNERSHIP_CHANGED,
    Approvals,
    OwnershipMatch,
    OwnershipResult,
)
from ownership_bot.teams import load_manifest_text

NOW = datetime.now(UTC)


def ownership(*teams: str, files: int = 2) -> OwnershipResult:
    result = OwnershipResult(files_total=files, codeowners_ref="master")
    for team in teams:
        result.matches[team] = [
            OwnershipMatch(
                team=team,
                kind="codeowners",
                value="src/**",
                files=tuple(f"src/f{index}.ts" for index in range(files)),
            )
        ]
    return result


def roster_for(*teams: str):
    rosters = {team: {f"{team}-member"} for team in teams}
    return lambda team_key: rosters.get(team_key, set())


def decide(mr, manifest, result, *, approvals=None, delta=None, rosters=None):
    return decide_mr_check(
        mr=mr,
        manifest=manifest,
        ownership=result,
        approvals=approvals or Approvals(),
        roster_for=rosters or roster_for("payments", "devops", "platform"),
        pattern_delta=delta,
        now=NOW,
    )


# ------------------------------------------------------------------ condition 1


def test_green_pipeline_notifies_owning_team(manifest):
    decisions = decide(make_mr(), manifest, ownership("payments"))
    assert [d.team for d in decisions] == ["payments"]
    assert decisions[0].events == (MR_PIPELINE_GREEN,)


def test_draft_mr_is_treated_exactly_like_a_ready_one(manifest):
    """v4 decision: the green pipeline is the trigger, draft status is irrelevant."""
    decisions = decide(make_mr(draft=True), manifest, ownership("payments"))
    assert decisions[0].events == (MR_PIPELINE_GREEN,)


def test_team_label_present_suppresses(manifest):
    decisions = decide(make_mr(labels=("team::payments",)), manifest, ownership("payments"))
    assert decisions == []


def test_skip_label_suppresses_everything(manifest):
    decisions = decide(
        make_mr(labels=("ownership-bot::skip",)), manifest, ownership("payments")
    )
    assert decisions == []


def test_author_is_skipped(manifest):
    """The author's own team is never pinged (D3)."""
    rosters = lambda key: ({"dave"} if key == "payments" else set())  # noqa: E731
    decisions = decide(make_mr(author="dave"), manifest, ownership("payments"), rosters=rosters)
    assert decisions == []


def test_only_unclaimed_files_means_nothing_happens(manifest):
    assert decide(make_mr(), manifest, ownership()) == []


def test_bot_authors_are_ignored(manifest):
    decisions = decide(make_mr(author="renovate-bot"), manifest, ownership("payments"))
    assert decisions == []


def test_owner_approval_suppresses_all_notifications(manifest):
    approvals = Approvals(approved_by=("payments-member",), checked_at=NOW)
    assert decide(make_mr(), manifest, ownership("payments"), approvals=approvals) == []


def test_non_owner_approval_does_not_suppress(manifest):
    approvals = Approvals(approved_by=("someone-else",), checked_at=NOW)
    assert decide(make_mr(), manifest, ownership("payments"), approvals=approvals)


def test_multiple_teams_get_their_own_decisions(manifest):
    decisions = decide(make_mr(), manifest, ownership("payments", "platform"))
    assert sorted(d.team for d in decisions) == ["payments", "platform"]


def test_branch_scope_excludes_out_of_scope_team():
    """A prod-only team stays silent on an MR targeting master."""
    scoped = load_manifest_text(
        """
        teams:
          prod-only:
            codeowners_token: "@acme/teams/prod-only"
            members: [erin]
            teams_channel: Prod
            label: team::prod-only
            branches: [prod, "release/**"]
        """
    )

    assert decide(make_mr(target_branch="master"), scoped, ownership("prod-only")) == []

    decisions = decide(make_mr(target_branch="prod"), scoped, ownership("prod-only"))
    assert decisions[0].team == "prod-only"


def test_out_of_scope_team_is_not_labelled_either(manifest):
    """The label means 'this team was pinged', so it is not applied out of scope."""
    decisions = decide(make_mr(target_branch="master"), manifest, ownership("payments", "devops"))
    assert [d.team for d in decisions] == ["payments"]


def test_thresholds_can_silence_a_team():
    strict = load_manifest_text(
        """
        teams:
          payments:
            codeowners_token: "@acme/teams/payments"
            members: [payments-member]
            teams_channel: c
            label: team::payments
            min_owned_files: 2
        """
    )
    assert decide(make_mr(), strict, ownership("payments", files=1)) == []
    assert decide(make_mr(), strict, ownership("payments", files=2))


def test_notify_on_can_exclude_the_green_pipeline(manifest):
    """devops opted out of green-pipeline pings, so a prod MR pings nobody."""
    decisions = decide(make_mr(target_branch="prod"), manifest, ownership("devops"))
    assert decisions == []


# ------------------------------------------------------------- ownership changes


def test_ownership_change_notifies_team_that_loses_patterns(manifest):
    token_to_team = manifest.token_to_team
    delta = team_pattern_delta(
        parse("a/** @acme/teams/devops"), parse("a/** @acme/teams/platform"), token_to_team
    )
    decisions = decide(make_mr(target_branch="master"), manifest, ownership(), delta=delta)

    losing = next(d for d in decisions if d.team == "devops")
    assert losing.events == (OWNERSHIP_CHANGED,)
    assert losing.removed_patterns == ("a/**",)

    gaining = next(d for d in decisions if d.team == "platform")
    assert gaining.events == (OWNERSHIP_CHANGED,)
    assert gaining.added_patterns == ("a/**",)


def test_ownership_change_bypasses_branch_scope(manifest):
    """Losing ownership on master still matters to a prod-only team (§9.4)."""
    delta = team_pattern_delta(
        parse("a/** @acme/teams/devops"), parse("a/** @acme/teams/platform"), manifest.token_to_team
    )
    decisions = decide(make_mr(target_branch="master"), manifest, ownership("payments"), delta=delta)
    devops = next(d for d in decisions if d.team == "devops")
    assert devops.events == (OWNERSHIP_CHANGED,)


def test_both_reasons_are_combined_into_one_message(manifest):
    delta = team_pattern_delta(
        parse("b/** @acme/teams/payments"), parse("c/** @acme/teams/payments"), manifest.token_to_team
    )
    decisions = decide(make_mr(), manifest, ownership("payments"), delta=delta)
    assert len(decisions) == 1
    assert set(decisions[0].events) == {MR_PIPELINE_GREEN, OWNERSHIP_CHANGED}


def test_ownership_change_respects_author_skip(manifest):
    delta = team_pattern_delta(
        parse("a/** @acme/teams/payments"), parse(""), manifest.token_to_team
    )
    rosters = lambda key: ({"dave"} if key == "payments" else set())  # noqa: E731
    assert decide(make_mr(author="dave"), manifest, ownership(), delta=delta, rosters=rosters) == []


# ------------------------------------------------------------------ condition 2


def audit(mr, manifest, result, *, approvals=None, rosters=None):
    return decide_merge_audit(
        mr=mr,
        manifest=manifest,
        ownership=result,
        approvals=approvals or Approvals(),
        roster_for=rosters or roster_for("payments", "devops", "platform"),
    )


def test_merge_without_owner_approval_is_audited(manifest):
    decisions = audit(make_mr(state="merged"), manifest, ownership("payments"))
    assert [d.team for d in decisions] == ["payments"]
    assert decisions[0].events == (MERGED_WITHOUT_OWNER_APPROVAL,)


def test_merge_with_owner_approval_is_silent(manifest):
    approvals = Approvals(approved_by=("payments-member",), checked_at=NOW)
    assert audit(make_mr(state="merged"), manifest, ownership("payments"), approvals=approvals) == []


def test_open_mr_is_not_audited(manifest):
    assert audit(make_mr(state="opened"), manifest, ownership("payments")) == []


def test_audit_respects_branch_scope(manifest):
    assert audit(make_mr(state="merged", target_branch="master"), manifest, ownership("devops")) == []
    assert audit(make_mr(state="merged", target_branch="prod"), manifest, ownership("devops"))


def test_audit_ignores_thresholds(manifest):
    """An audit has no noise knobs — it is a record, not a ping."""
    assert audit(make_mr(state="merged"), manifest, ownership("payments", files=1))


def test_audit_skips_the_authors_own_team(manifest):
    rosters = lambda key: ({"dave"} if key == "payments" else set())  # noqa: E731
    assert audit(make_mr(state="merged", author="dave"), manifest, ownership("payments"), rosters=rosters) == []
