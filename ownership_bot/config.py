"""Configuration: everything comes from the environment so CI stays declarative."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

MODE_REPORT = "report"  # decide + log, never write, never send
MODE_LABEL = "label"  # labels only, no Teams
MODE_NOTIFY = "notify"  # labels + Teams (default in CI)

VALID_MODES = (MODE_REPORT, MODE_LABEL, MODE_NOTIFY)


@dataclass
class Settings:
    # GitLab connection
    gitlab_url: str = "https://gitlab.example.com"
    bot_token: str = ""
    project_id: str = ""
    project_path: str = ""
    default_branch: str = "main"
    # Pipeline context
    pipeline_id: str = ""
    job_id: str = ""
    pipeline_source: str = ""
    commit_sha: str = ""
    merge_request_iid: str = ""
    target_branch: str = ""
    diff_base_sha: str = ""
    # Ownership metadata (central repo holding teams.yml)
    manifest_project: str = ""
    manifest_ref: str = "main"
    manifest_path: str = ""
    # Behaviour
    mode: str = MODE_NOTIFY
    verify_upstream: bool = True
    repo_root: Path = field(default_factory=Path.cwd)
    codeowners_path: str = ""
    decision_artifact: str = "decision.json"
    timeout: int = 30
    # Teams delivery
    pa_workflow_url: str = ""
    pa_shared_secret: str = ""

    @property
    def notify_enabled(self) -> bool:
        return self.mode == MODE_NOTIFY

    @property
    def writes_enabled(self) -> bool:
        return self.mode in (MODE_LABEL, MODE_NOTIFY)

    @property
    def job_url(self) -> str | None:
        if not (self.gitlab_url and self.project_path and self.job_id):
            return None
        return f"{self.gitlab_url.rstrip('/')}/{self.project_path}/-/jobs/{self.job_id}"


def _first(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def from_env(overrides: dict | None = None) -> Settings:
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}

    settings = Settings(
        gitlab_url=_first("OWNERSHIP_GITLAB_URL", "CI_SERVER_URL", "GITLAB_URL")
        or "https://gitlab.example.com",
        bot_token=_first("OWNERSHIP_BOT_TOKEN", "GITLAB_TOKEN", "BOT_TOKEN"),
        project_id=_first("CI_PROJECT_ID"),
        project_path=_first("CI_PROJECT_PATH", "CI_MERGE_REQUEST_PROJECT_PATH"),
        default_branch=_first("CI_DEFAULT_BRANCH") or "main",
        pipeline_id=_first("CI_PIPELINE_ID"),
        job_id=_first("CI_JOB_ID"),
        pipeline_source=_first("CI_PIPELINE_SOURCE"),
        commit_sha=_first("CI_MERGE_REQUEST_SHA", "CI_COMMIT_SHA"),
        merge_request_iid=_first("CI_MERGE_REQUEST_IID"),
        target_branch=_first(
            "CI_MERGE_REQUEST_TARGET_BRANCH_NAME", "CI_COMMIT_BRANCH", "CI_COMMIT_REF_NAME"
        ),
        diff_base_sha=_first("CI_MERGE_REQUEST_DIFF_BASE_SHA"),
        manifest_project=_first("OWNERSHIP_MANIFEST_PROJECT"),
        manifest_ref=_first("OWNERSHIP_MANIFEST_REF") or "main",
        mode=_first("OWNERSHIP_MODE") or MODE_NOTIFY,
        verify_upstream=(_first("OWNERSHIP_VERIFY_UPSTREAM") or "true").lower()
        not in ("false", "0", "no"),
        repo_root=Path(_first("CI_PROJECT_DIR") or Path.cwd()),
        codeowners_path=_first("OWNERSHIP_CODEOWNERS_PATH"),
        decision_artifact=_first("OWNERSHIP_DECISION_ARTIFACT") or "decision.json",
        pa_workflow_url=_first("OWNERSHIP_PA_WORKFLOW_URL"),
        pa_shared_secret=_first("OWNERSHIP_PA_SHARED_SECRET"),
    )

    for key, value in overrides.items():
        setattr(settings, key, value)

    if settings.mode not in VALID_MODES:
        raise ValueError(f"OWNERSHIP_MODE must be one of {VALID_MODES}, got '{settings.mode}'")

    return settings
