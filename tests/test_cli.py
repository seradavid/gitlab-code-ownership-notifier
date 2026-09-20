"""CLI plumbing: flag → settings mapping and the offline MR construction.

These cover the two things that are easy to get wrong and invisible until a job
runs with the wrong values: where the decision artifact goes, and whether an
offline run sees a merged or an open MR.
"""

from __future__ import annotations

import json

import pytest

from ownership_bot import cli

MR_JSON = {
    "project_id": 1,
    "project_path": "acme/services/payment-service",
    "iid": 412,
    "title": "Add retry to settlement call",
    "web_url": "https://gitlab.example.com/acme/services/payment-service/-/merge_requests/412",
    "author": {"username": "dave", "name": "Dave"},
    "state": "opened",
    "draft": False,
    "source_branch": "feature/retry",
    "target_branch": "master",
    "created_at": "2026-09-20T09:00:00Z",
}


@pytest.fixture
def mr_file(tmp_path):
    path = tmp_path / "mr.json"
    path.write_text(json.dumps(MR_JSON), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """The CLI reads CI_* and OWNERSHIP_* variables; keep the tests hermetic."""
    for name in (
        "OWNERSHIP_DECISION_ARTIFACT",
        "OWNERSHIP_MODE",
        "OWNERSHIP_MANIFEST_PROJECT",
        "CI_PROJECT_ID",
        "CI_MERGE_REQUEST_IID",
        "CI_COMMIT_SHA",
        "CI_MERGE_REQUEST_TARGET_BRANCH_NAME",
        "CI_DEFAULT_BRANCH",
    ):
        monkeypatch.delenv(name, raising=False)


def settings_for(*argv: str):
    return cli._settings(cli._parse_args(list(argv)))


def test_json_out_sets_the_decision_artifact():
    assert settings_for("--json-out", "out.json", "mr-check").decision_artifact == "out.json"


def test_decision_artifact_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("OWNERSHIP_DECISION_ARTIFACT", "from-env.json")
    assert settings_for("mr-check").decision_artifact == "from-env.json"


def test_a_flag_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("OWNERSHIP_MODE", "notify")
    assert settings_for("--mode", "report", "mr-check").mode == "report"


def test_offline_mr_uses_the_payload_state(mr_file):
    args = cli._parse_args(["--mr-json", str(mr_file), "merge-audit"])
    assert cli._offline_mr(args, settings_for("merge-audit")).is_merged is False


def test_offline_mr_state_override_makes_the_audit_possible(mr_file):
    """The demo needs this: mr.json is an open MR, `--state merged` audits it."""
    args = cli._parse_args(["--mr-json", str(mr_file), "--state", "merged", "merge-audit"])
    mr = cli._offline_mr(args, settings_for("merge-audit"))

    assert mr.is_merged
    assert mr.iid == 412
    assert mr.author_username == "dave"


def test_synthesised_mr_defaults_to_opened_and_honours_the_flags():
    args = cli._parse_args(
        ["--author", "dave", "--target-branch", "develop", "--labels", "team::payments", "mr-check"]
    )
    mr = cli._offline_mr(args, settings_for("mr-check"))

    assert mr.state == "opened"
    assert mr.target_branch == "develop"
    assert mr.labels == frozenset({"team::payments"})


def test_iid_flag_reaches_the_settings():
    assert settings_for("--iid", "77", "mr-check").merge_request_iid == "77"
