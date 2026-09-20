"""Job orchestration: modes, write ordering, idempotency and the upstream guard."""

from __future__ import annotations

import json

import pytest
from conftest import CODEOWNERS_TEXT, MANIFEST_TEXT, make_mr

from ownership_bot import runner
from ownership_bot.config import MODE_LABEL, MODE_NOTIFY, MODE_REPORT, Settings
from ownership_bot.notify import NullNotifier

OWNED_FILE = "src/main/java/com/acme/payments/Client.java"


class StubClient:
    def __init__(self) -> None:
        self.labels: list[list[str]] = []
        self.note_bodies: list[str] = []
        self.approved: list[str] = []
        self.members = {"alice", "bob", "carol", "erin", "payments-member"}
        self.jobs: list[dict] = []

    def approvals(self, project, iid):  # noqa: ANN001
        return list(self.approved)

    def add_labels(self, project, iid, labels):  # noqa: ANN001
        self.labels.append(list(labels))

    def add_note(self, project, iid, body):  # noqa: ANN001
        self.note_bodies.append(body)

    def group_members(self, group):  # noqa: ANN001
        return set(self.members)

    def pipeline_jobs(self, project, pipeline_id):  # noqa: ANN001
        return list(self.jobs)

    # runner calls client.notes(...) to read the merge-audit markers
    def notes(self, project, iid):  # noqa: ANN001
        return [{"body": body} for body in self.note_bodies]


class RecordingNotifier:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[dict] = []

    def post(self, payload: dict) -> bool:
        self.sent.append(payload)
        return self.ok


@pytest.fixture
def workspace(tmp_path):
    teams = tmp_path / "teams.yml"
    teams.write_text(MANIFEST_TEXT, encoding="utf-8")
    codeowners = tmp_path / "CODEOWNERS"
    codeowners.write_text(CODEOWNERS_TEXT, encoding="utf-8")
    return tmp_path, teams, codeowners


def build_settings(tmp_path, teams, codeowners, mode: str) -> Settings:
    return Settings(
        mode=mode,
        manifest_path=str(teams),
        codeowners_path=str(codeowners),
        decision_artifact=str(tmp_path / "decision.json"),
        repo_root=tmp_path,
        project_id="1",
        project_path="acme/services/payment-service",
        default_branch="master",
        pa_workflow_url="https://flow.example.com/hook",
    )


def run_check(workspace, mode: str, *, notifier=None, approvals=(), mr=None):
    tmp_path, teams, codeowners = workspace
    settings = build_settings(tmp_path, teams, codeowners, mode)
    client = StubClient()
    client.approved = list(approvals)
    ctx = runner.RunContext(settings=settings, client=client, manifest=runner.load_manifest_for(settings, client))
    summary = runner.run_mr_check(
        ctx,
        mr=mr or make_mr(),
        changed_files=[OWNED_FILE],
        notifier=notifier or NullNotifier(),
    )
    return summary, client


def test_report_mode_writes_nothing(workspace):
    summary, client = run_check(workspace, MODE_REPORT)

    assert client.labels == []
    actions = summary["decisions"][0]["actions_taken"]
    assert actions == ["report-only", "label-skipped:team::payments"]
    artifact = json.loads((workspace[0] / "decision.json").read_text(encoding="utf-8"))
    assert artifact["job"] == "mr-check"
    assert artifact["files"]["owned"] == 1


def test_label_mode_labels_without_notifying(workspace):
    summary, client = run_check(workspace, MODE_LABEL)

    assert client.labels == [["team::payments"]]
    assert summary["decisions"][0]["actions_taken"] == ["notify-skipped", "label_added:team::payments"]


def test_notify_mode_notifies_then_labels(workspace):
    notifier = RecordingNotifier()
    summary, client = run_check(workspace, MODE_NOTIFY, notifier=notifier)

    assert len(notifier.sent) == 1
    assert client.labels == [["team::payments"]]
    assert notifier.sent[0]["events"] == ["mr_pipeline_green"]
    assert summary["decisions"][0]["actions_taken"] == [
        "notified",
        "label_added:team::payments",
    ]


def test_failed_notification_does_not_label(workspace):
    """Ordering matters: webhook first, label second (otherwise the ping is lost)."""
    notifier = RecordingNotifier(ok=False)
    _summary, client = run_check(workspace, MODE_NOTIFY, notifier=notifier)

    assert client.labels == []


def test_owner_approved_team_is_silent(workspace):
    _summary, client = run_check(workspace, MODE_NOTIFY, approvals=["payments-member"])
    assert client.labels == []


def test_merge_audit_adds_marker_note_and_is_idempotent(workspace):
    tmp_path, teams, codeowners = workspace
    settings = build_settings(tmp_path, teams, codeowners, MODE_NOTIFY)
    client = StubClient()
    ctx = runner.RunContext(settings=settings, client=client, manifest=runner.load_manifest_for(settings, client))
    notifier = RecordingNotifier()
    merged = make_mr(state="merged")

    first = runner.run_merge_audit(
        ctx, mr=merged, changed_files=[OWNED_FILE], notifier=notifier
    )
    assert len(first["decisions"]) == 1
    assert "ownership-bot:merged:payments" in client.note_bodies[0]
    assert first["decisions"][0]["actions_taken"] == ["notified", "note_added"]

    second = runner.run_merge_audit(ctx, mr=merged, changed_files=[OWNED_FILE], notifier=notifier)
    assert second["decisions"] == []  # marker already present


def test_upstream_failure_suppresses_the_job(workspace):
    tmp_path, teams, codeowners = workspace
    settings = build_settings(tmp_path, teams, codeowners, MODE_NOTIFY)
    settings.pipeline_id = "90210"
    settings.job_id = "999"
    client = StubClient()
    client.jobs = [
        {"id": 1, "name": "test", "status": "failed", "allow_failure": False},
        {"id": 999, "name": "ownership-mr-check", "status": "running", "allow_failure": True},
    ]

    assert runner.upstream_failed(settings, client) is True

    client.jobs[0]["allow_failure"] = True  # an allowed failure still means "pipeline passed"
    assert runner.upstream_failed(settings, client) is False


def test_upstream_check_can_be_disabled(workspace):
    tmp_path, teams, codeowners = workspace
    settings = build_settings(tmp_path, teams, codeowners, MODE_NOTIFY)
    settings.verify_upstream = False
    client = StubClient()
    client.jobs = [{"id": 1, "name": "test", "status": "failed", "allow_failure": False}]
    assert runner.upstream_failed(settings, client) is False
