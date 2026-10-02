"""Rollout helpers (§12, appendix B): drafts, validation, include insertion."""

from __future__ import annotations

import pytest
import yaml

from ownership_bot.rollout import (
    RepoDraft,
    RolloutError,
    component_include_block,
    ensure_include,
    has_include,
    load_mapping,
    render_codeowners,
    uncovered_prefixes,
    validate_codeowners,
)

MAPPING = """
repositories:
  acme/services/payment-service:
    target_branch: master
    default_owner: "@acme/teams/platform"
    rules:
      "src/main/java/com/acme/payments/**": "@acme/teams/payments @alice"
      "src/main/resources/db/migration/**": "@acme/teams/devops"
    ci_inputs:
      mode: report
"""

COMPONENT = "gitlab.example.com/example-org/ownership-notify@1.2.0"


def test_mapping_loads_repositories_and_ci_inputs():
    drafts = load_mapping(MAPPING)

    draft = drafts["acme/services/payment-service"]
    assert draft.default_owner == "@acme/teams/platform"
    assert draft.target_branch == "master"
    assert list(draft.rules) == [
        "src/main/java/com/acme/payments/**",
        "src/main/resources/db/migration/**",
    ]
    assert draft.ci_inputs == {"mode": "report"}


def test_mapping_requires_repositories():
    with pytest.raises(RolloutError):
        load_mapping("version: 1\n")


def test_malformed_mapping_is_a_rollout_error_not_a_raw_yaml_error():
    with pytest.raises(RolloutError, match="invalid YAML"):
        load_mapping("repositories: [oops\n")


def test_mapping_rejects_a_repository_without_a_mapping():
    with pytest.raises(RolloutError):
        load_mapping("repositories:\n  a/b: []\n")


def test_render_codeowners_puts_the_fallback_rule_first():
    draft = load_mapping(MAPPING)["acme/services/payment-service"]
    text = render_codeowners(draft)
    body = [line for line in text.splitlines() if line and not line.startswith("#")]

    assert body[0] == "* @acme/teams/platform"
    assert body[1] == "[Payments]"
    assert body[2] == "src/main/java/com/acme/payments/** @acme/teams/payments @alice"
    assert body[3] == "[Devops]"
    assert body[4] == "src/main/resources/db/migration/** @acme/teams/devops"


def test_rendered_codeowners_is_valid_and_resolves_the_expected_owners():
    draft = load_mapping(MAPPING)["acme/services/payment-service"]
    text = render_codeowners(draft)

    assert (
        validate_codeowners(
            text,
            known_tokens={
                "@acme/teams/platform",
                "@acme/teams/payments",
                "@acme/teams/devops",
                "@alice",
            },
        )
        == []
    )

    from ownership_bot.codeowners import parse

    parsed = parse(text)
    assert parsed.owners_for("src/main/java/com/acme/payments/A.java") == (
        "@acme/teams/payments",
        "@alice",
    )
    assert parsed.owners_for("tools/run.sh") == ("@acme/teams/platform",)


def test_validation_flags_unknown_tokens_and_empty_files():
    draft = RepoDraft(project="a/b", rules={"src/**": "@acme/teams/ghost"})
    problems = validate_codeowners(render_codeowners(draft), known_tokens=set())

    assert any("no team" in problem for problem in problems)
    assert any("unclaimed" in problem for problem in validate_codeowners(""))


def test_render_refuses_a_rule_without_owner():
    draft = RepoDraft(project="a/b", rules={"src/**": ""})

    with pytest.raises(RolloutError):
        render_codeowners(draft)


def test_include_block_is_valid_yaml():
    block = component_include_block(COMPONENT, {"mode": "report", "manifest-project": "example-org/ownership"})
    assert yaml.safe_load(block) == {
        "include": [
            {
                "component": COMPONENT,
                "inputs": {"mode": "report", "manifest-project": "example-org/ownership"},
            }
        ]
    }


def test_ensure_include_prepends_when_there_is_no_include():
    ci = "stages: [test]\n\ntest:\n  script: echo hi\n"
    updated = ensure_include(ci, COMPONENT, {"mode": "report"})

    assert updated.startswith("include:\n  - component: " + COMPONENT)
    assert ci.strip() in updated
    assert yaml.safe_load(updated)["stages"] == ["test"]


def test_ensure_include_merges_into_an_existing_block_list():
    ci = "include:\n  - project: ops/templates\n    file: /base.yml\n\nstages: [test]\n"
    updated = ensure_include(ci, COMPONENT)
    document = yaml.safe_load(updated)

    assert document["include"] == [
        {"component": COMPONENT},
        {"project": "ops/templates", "file": "/base.yml"},
    ]
    assert document["stages"] == ["test"]
    assert "\n\n\n" not in updated  # no spurious blank line between include entries


def test_ensure_include_is_idempotent():
    ci = "stages: [test]\n"
    once = ensure_include(ci, COMPONENT)
    assert ensure_include(once, COMPONENT) == once
    assert has_include(once, COMPONENT)


def test_ensure_include_matches_the_existing_indentation():
    """A 4-space include list must stay one coherent list, not mixed indentation."""
    ci = "include:\n    - project: ops/templates\n      file: /base.yml\nstages: [test]\n"
    updated = ensure_include(ci, COMPONENT)

    assert yaml.safe_load(updated) == {
        "include": [
            {"component": COMPONENT},
            {"project": "ops/templates", "file": "/base.yml"},
        ],
        "stages": ["test"],
    }


def test_ensure_include_handles_a_sequence_at_column_zero():
    ci = "include:\n- project: ops/templates\n  file: /base.yml\nstages: [test]\n"
    updated = ensure_include(ci, COMPONENT)

    assert yaml.safe_load(updated)["include"] == [
        {"component": COMPONENT},
        {"project": "ops/templates", "file": "/base.yml"},
    ]


def test_has_include_matches_the_entry_not_a_substring():
    """A mention in a comment, or a longer component name, is not an existing include."""
    commented = "# see host/path/ownership-notify@1.0.0 for details\nstages: [test]\n"
    longer = "include:\n  - component: host/path/ownership-notify-extra@1.0.0\n"
    exact = f"include:\n  - component: {COMPONENT}\n"

    assert has_include(commented, COMPONENT) is False
    assert has_include(longer, COMPONENT) is False
    assert has_include(exact, COMPONENT) is True


def test_has_include_tolerates_an_unparseable_file():
    assert has_include("include: [unclosed\n", COMPONENT) is False


def test_has_include_falls_back_when_the_file_has_custom_tags():
    """`!reference` defeats the YAML parse; an existing include must still be found."""
    ci = f"stages: [test]\n.base:\n  script:\n    - !reference [.other, script]\ninclude:\n  - component: {COMPONENT}\n"

    assert has_include(ci, COMPONENT) is True
    assert ensure_include(ci, COMPONENT) == ci  # idempotent, not duplicated


def test_ensure_include_refuses_an_inline_include():
    """Never guess: a shape we do not understand is reported, not rewritten."""
    with pytest.raises(RolloutError):
        ensure_include("include: [{project: ops/templates}]\n", COMPONENT)


def test_ensure_include_refuses_a_block_mapping_include():
    with pytest.raises(RolloutError):
        ensure_include("include:\n  project: ops/templates\n", COMPONENT)


def test_ensure_include_keeps_comments_untouched():
    ci = "# our pipeline\ndefault:\n  image: python:3.14\n"
    updated = ensure_include(ci, COMPONENT)

    assert "# our pipeline" in updated
    assert "image: python:3.14" in updated


def test_uncovered_prefixes_lists_directories_no_rule_claims():
    tree = [
        "src/main/java/A.java",
        "libs/payment-core/src/B.java",
        "tools/run.sh",
        "README.md",
    ]

    assert uncovered_prefixes(tree, {"src/**": "@acme/teams/payments"}) == [
        "libs/payment-core/src",
        "tools/run.sh",
    ]


def test_uncovered_prefixes_is_empty_when_a_fallback_rule_exists():
    tree = ["libs/payment-core/src/B.java", "tools/run.sh"]

    assert uncovered_prefixes(tree, {"*": "@acme/teams/platform"}) == []
    assert uncovered_prefixes(tree, {"**/*": "@acme/teams/platform"}) == []
