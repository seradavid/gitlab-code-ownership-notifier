"""Shared fixtures."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ownership_bot.models import MergeRequest

MANIFEST_TEXT = """
version: 1

defaults:
  branches: ["**"]
  notify_on: [mr_pipeline_green, merged_without_owner_approval, ownership_changed]
  min_mr_age_minutes: 0
  min_owned_files: 1

teams:
  payments:
    codeowners_token: "@acme/teams/payments"
    gitlab_group: acme/teams/payments
    members: [alice, bob]
    teams_channel: "Payments Engineering"
    label: team::payments
    branches: [master, develop, "release/**"]
  devops:
    codeowners_token: "@acme/teams/devops"
    members: [carol]
    teams_channel: "DevOps / Production"
    label: team::devops
    branches: [prod, "release/**"]
    notify_on: [merged_without_owner_approval, ownership_changed]
  platform:
    codeowners_token: "@acme/teams/platform"
    members: [erin]
    teams_channel: "Platform"
    label: team::platform

ignore:
  - "**/target/**"
  - "**/*.lock"
  - "docs/**"

ignore_authors: [renovate-bot, "*-ci-bot"]
skip_label: ownership-bot::skip

module_overlay:
  - module: "*:payment-core"
    owner: payments
"""

CODEOWNERS_TEXT = """
# default owner for the repository
* @acme/teams/platform

[Payments]
src/main/java/com/acme/payments/** @acme/teams/payments @alice
src/main/resources/db/migration/** @acme/teams/devops
"""


def make_mr(
    *,
    iid: int = 412,
    author: str = "dave",
    labels: tuple[str, ...] = (),
    draft: bool = False,
    state: str = "opened",
    target_branch: str = "master",
    age_minutes: int = 60,
) -> MergeRequest:
    return MergeRequest(
        project_id=1,
        project_path="acme/services/payment-service",
        iid=iid,
        title="Add retry to settlement call",
        url=f"https://gitlab.example.com/acme/services/payment-service/-/merge_requests/{iid}",
        author_username=author,
        author_name=author.title(),
        state=state,
        draft=draft,
        source_branch="feature/retry",
        target_branch=target_branch,
        created_at=datetime.now(UTC) - timedelta(minutes=age_minutes),
        labels=frozenset(labels),
    )


@pytest.fixture
def manifest():
    from ownership_bot.teams import load_manifest_text

    return load_manifest_text(MANIFEST_TEXT)


@pytest.fixture
def codeowners():
    from ownership_bot.codeowners import parse

    return parse(CODEOWNERS_TEXT)


@pytest.fixture
def mr():
    return make_mr()
