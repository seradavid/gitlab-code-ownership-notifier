"""Notification contract (§10): payload, HMAC signature, Power Automate delivery."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import requests

from . import __version__
from .codeowners import CODEOWNERS_PATHS
from .models import Approvals, Decision, MergeRequest, OwnershipResult
from .teams import Manifest

log = logging.getLogger(__name__)

SCHEMA = 1


class HttpSession(Protocol):
    """The one method :class:`Notifier` needs; lets tests pass a stub session."""

    def post(self, url: str, *, data: bytes, headers: dict[str, str], timeout: int) -> Any: ...


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_payload(
    *,
    decision: Decision,
    mr: MergeRequest,
    ownership: OwnershipResult,
    approvals: Approvals,
    manifest: Manifest,
    actions_taken: list[str] | None = None,
    roster_resolved: bool = True,
    trigger: dict | None = None,
    roster_for: Callable[[str], set[str]] | None = None,
) -> dict:
    """Build the versioned payload consumed by the Power Automate flow."""
    team = manifest.team(decision.team)
    roster = roster_for(decision.team) if roster_for is not None else manifest.roster(team)

    matched = [{"kind": match.kind, "value": match.value, "files": list(match.files)} for match in decision.matches]

    return {
        "schema": SCHEMA,
        "events": list(decision.events),
        "dedupe_key": f"{mr.project_path}!{mr.iid}:{team.label}",
        "trigger": trigger
        or {
            "pipeline_id": None,
            "job_url": None,
            "pipeline_source": None,
        },
        "team": {
            "id": team.key,
            "channel": team.channel,
            "label": team.label,
            "branches": list(team.branches),
            "mentions": list(team.mentions),
            "roster_resolved": roster_resolved,
        },
        "merge_request": {
            "project_path": mr.project_path,
            "iid": mr.iid,
            "title": mr.title,
            "url": mr.url,
            "author": {"username": mr.author_username, "name": mr.author_name},
            "state": mr.state,
            "draft": mr.draft,
            "source_branch": mr.source_branch,
            "target_branch": mr.target_branch,
            "merged_at": _iso(mr.merged_at),
        },
        "ownership": {
            "source": f"CODEOWNERS@{ownership.codeowners_ref}"
            if ownership.codeowners_found
            else "CODEOWNERS@<missing>",
            "path": ownership.codeowners_path,
            "matched": matched,
            "files_total": ownership.files_total,
            "files_owned": ownership.files_owned,
            "files_unclaimed": ownership.files_unclaimed,
            "files_ignored": ownership.files_ignored,
            "diffs_truncated": ownership.diffs_truncated,
        },
        "ownership_change": {
            "file": decision.ownership_path or CODEOWNERS_PATHS[1],
            "added": list(decision.added_patterns),
            "removed": list(decision.removed_patterns),
        },
        "approvals": {
            "approved_by": list(approvals.approved_by),
            "owners_approved": [username for username in approvals.approved_by if username in roster],
            "checked_at": _iso(approvals.checked_at),
        },
        "actions_taken": list(actions_taken or []),
    }


def sign(body: bytes, secret: str) -> str:
    """``X-Ownership-Signature`` value: ``sha256=<hex hmac>``."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@dataclass
class Notifier:
    """POSTs signed payloads to one Power Automate flow (channel chosen in the flow)."""

    workflow_url: str
    shared_secret: str = ""
    timeout: int = 15
    session: HttpSession = field(default_factory=requests.Session)

    def post(self, payload: dict) -> bool:
        if not self.workflow_url:
            log.error("no Power Automate workflow URL configured; nothing sent")
            return False
        if not self.shared_secret:
            # Without the secret the body is unsigned, and the flow cannot tell a real
            # notification from a forged one (SECURITY.md). Fail loudly rather than send.
            log.error(
                "no OWNERSHIP_PA_SHARED_SECRET configured; refusing to send an unsigned notification for team %s",
                payload["team"]["id"],
            )
            return False

        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"ownership-bot/{__version__}",
            "X-Ownership-Signature": sign(body, self.shared_secret),
        }

        try:
            response = self.session.post(self.workflow_url, data=body, headers=headers, timeout=self.timeout)
        except requests.RequestException:
            log.exception("notification failed for team %s", payload["team"]["id"])
            return False

        if response.status_code >= 300:
            log.error(
                "notification rejected for team %s: HTTP %s %s",
                payload["team"]["id"],
                response.status_code,
                response.text[:200],
            )
            return False
        return True


@dataclass
class NullNotifier:
    """Used by ``--mode report``: records instead of sending."""

    sent: list[dict] = field(default_factory=list)

    def post(self, payload: dict) -> bool:
        self.sent.append(payload)
        log.info(
            "[report] would notify %s (%s) about %s!%s",
            payload["team"]["id"],
            ",".join(payload["events"]),
            payload["merge_request"]["project_path"],
            payload["merge_request"]["iid"],
        )
        return True
