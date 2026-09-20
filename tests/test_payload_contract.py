"""Notification contract (§10): payload shape, HMAC signature, delivery."""

from __future__ import annotations

import json

from conftest import make_mr

from ownership_bot.models import (
    MR_PIPELINE_GREEN,
    OWNERSHIP_CHANGED,
    Approvals,
    Decision,
    OwnershipMatch,
    OwnershipResult,
)
from ownership_bot.notify import Notifier, NullNotifier, build_payload, sign


class FakeResponse:
    def __init__(self, status_code: int = 202, text: str = "accepted") -> None:
        self.status_code = status_code
        self.text = text


class FakeSession:
    def __init__(self, response: FakeResponse | None = None) -> None:
        self.calls: list[dict] = []
        self.response = response or FakeResponse()

    def post(self, url, data=None, headers=None, timeout=None):  # noqa: ANN001
        self.calls.append({"url": url, "data": data, "headers": headers or {}, "timeout": timeout})
        return self.response


def make_payload(manifest, *, events=(MR_PIPELINE_GREEN,), added=(), removed=()):
    ownership = OwnershipResult(
        matches={
            "payments": [
                OwnershipMatch(
                    team="payments",
                    kind="codeowners",
                    value="src/main/java/com/acme/payments/**",
                    files=("src/main/java/com/acme/payments/Client.java",),
                )
            ]
        },
        files_total=12,
        files_ignored=3,
        files_unclaimed=2,
        codeowners_ref="master",
    )
    decision = Decision(
        team="payments",
        events=tuple(events),
        matches=tuple(ownership.matches["payments"]),
        added_patterns=tuple(added),
        removed_patterns=tuple(removed),
    )
    return build_payload(
        decision=decision,
        mr=make_mr(labels=("team::payments",)),
        ownership=ownership,
        approvals=Approvals(approved_by=("alice", "k.lee")),
        manifest=manifest,
        actions_taken=["label_added:team::payments"],
        trigger={
            "pipeline_id": 90210,
            "job_url": "https://gitlab.example.com/jobs/1",
            "pipeline_source": "merge_request_event",
        },
    )


def test_payload_envelope_matches_the_contract(manifest):
    payload = make_payload(manifest)

    assert payload["schema"] == 1
    assert payload["events"] == [MR_PIPELINE_GREEN]
    assert payload["dedupe_key"] == "acme/services/payment-service!412:team::payments"
    assert payload["team"] == {
        "id": "payments",
        "channel": "Payments Engineering",
        "label": "team::payments",
        "branches": ["master", "develop", "release/**"],
        "mentions": [],
        "roster_resolved": True,
    }
    assert payload["trigger"]["pipeline_id"] == 90210
    assert payload["merge_request"]["draft"] is False
    assert payload["merge_request"]["target_branch"] == "master"
    assert payload["ownership"]["source"] == "CODEOWNERS@master"
    assert payload["ownership"]["matched"][0]["kind"] == "codeowners"
    assert payload["ownership"]["files_unclaimed"] == 2
    assert payload["ownership_change"] == {
        "file": ".gitlab/CODEOWNERS",
        "added": [],
        "removed": [],
    }
    assert payload["approvals"]["approved_by"] == ["alice", "k.lee"]
    assert payload["approvals"]["owners_approved"] == ["alice"]
    assert payload["actions_taken"] == ["label_added:team::payments"]


def test_two_events_travel_in_one_message(manifest):
    payload = make_payload(
        manifest, events=(MR_PIPELINE_GREEN, OWNERSHIP_CHANGED), removed=("src/legacy/**",)
    )
    assert payload["events"] == [MR_PIPELINE_GREEN, OWNERSHIP_CHANGED]
    assert payload["ownership_change"]["removed"] == ["src/legacy/**"]


def test_signature_format_and_determinism():
    body = b'{"a":1}'
    first = sign(body, "s3cret")
    assert first == sign(body, "s3cret")
    assert first.startswith("sha256=")
    assert len(first) == len("sha256=") + 64
    assert sign(body, "other") != first
    assert sign(b'{"a":2}', "s3cret") != first


def test_notifier_posts_signed_body(manifest):
    session = FakeSession()
    notifier = Notifier(workflow_url="https://flow.example.com/hook", shared_secret="s", session=session)

    assert notifier.post(make_payload(manifest)) is True
    call = session.calls[0]
    assert call["url"] == "https://flow.example.com/hook"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["headers"]["X-Ownership-Signature"].startswith("sha256=")

    body = json.loads(call["data"].decode("utf-8"))
    assert body["events"] == [MR_PIPELINE_GREEN]
    assert body["team"]["channel"] == "Payments Engineering"


def test_notifier_reports_failure_without_raising(manifest):
    notifier = Notifier(
        workflow_url="https://flow.example.com/hook",
        session=FakeSession(FakeResponse(status_code=500, text="boom")),
    )
    assert notifier.post(make_payload(manifest)) is False


def test_notifier_without_url_is_a_no_op(manifest):
    assert Notifier(workflow_url="").post(make_payload(manifest)) is False


def test_null_notifier_records_instead_of_sending(manifest):
    notifier = NullNotifier()
    payload = make_payload(manifest)
    assert notifier.post(payload) is True
    assert notifier.sent == [payload]
