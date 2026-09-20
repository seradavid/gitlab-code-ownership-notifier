"""Thin GitLab REST client — only the endpoints the engine actually needs.

Note the credential split documented in docs/design.md §4: a pipeline job token can
read MR metadata and commit→MR mappings, but **not** diffs, approvals, group
membership or label writes. Everything here therefore uses the bot token.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote

import requests

from .codeowners import CODEOWNERS_PATHS
from .models import MergeRequest

log = logging.getLogger(__name__)

DEFAULT_PER_PAGE = 100


class GitLabError(RuntimeError):
    """Non-retryable API failure."""


@dataclass
class GitLab:
    base_url: str
    token: str
    timeout: int = 30
    session: requests.Session = field(default_factory=requests.Session)
    _members: dict[str, set[str]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.token:
            self.session.headers["PRIVATE-TOKEN"] = self.token
        self.session.headers.setdefault("User-Agent", "ownership-bot/0.1")

    @classmethod
    def from_env(cls, overrides: dict | None = None) -> GitLab | None:
        """Build a client from the environment; ``None`` when no token is configured.

        Used by the rollout scripts and the CLI: a missing token is a configuration
        problem, not an exception to handle at every call site.
        """
        from .config import from_env as settings_from_env

        settings = settings_from_env(overrides)
        if not settings.bot_token:
            return None
        return cls(settings.gitlab_url, settings.bot_token, timeout=settings.timeout)

    # ------------------------------------------------------------------ plumbing

    def _request(self, method: str, path: str, *, params=None, body=None, expect=(200, 201)):
        url = f"{self.base_url.rstrip('/')}/api/v4{path}"
        response = self.session.request(
            method, url, params=params, json=body, timeout=self.timeout
        )
        if response.status_code not in expect:
            raise GitLabError(
                f"{method} {path} -> HTTP {response.status_code}: {response.text[:300]}"
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def get(self, path: str, params: dict | None = None):
        return self._request("GET", path, params=params)

    def paginate(self, path: str, params: dict | None = None):
        params = dict(params or {})
        params.setdefault("per_page", DEFAULT_PER_PAGE)
        page = 1
        while True:
            params["page"] = page
            chunk = self.get(path, params)
            if not chunk:
                return
            if not isinstance(chunk, list):
                yield chunk
                return
            yield from chunk
            if len(chunk) < params["per_page"]:
                return
            page += 1

    @staticmethod
    def _ref(project: str | int) -> str:
        return str(project) if str(project).isdigit() else quote(str(project), safe="")

    # ------------------------------------------------------------------- projects

    def project(self, project: str | int) -> dict:
        return self.get(f"/projects/{self._ref(project)}")

    def group_projects(self, group: str, include_subgroups: bool = True) -> list[dict]:
        params = {"include_subgroups": str(include_subgroups).lower(), "archived": "false"}
        return list(self.paginate(f"/groups/{quote(group, safe='')}/projects", params))

    def tree_paths(self, project: str | int, ref: str) -> list[str]:
        params = {"ref": ref, "recursive": "true"}
        return [entry["path"] for entry in self.paginate(f"/projects/{self._ref(project)}/repository/tree", params)]

    def raw_file(self, project: str | int, path: str, ref: str) -> str | None:
        try:
            response = self.session.get(
                f"{self.base_url.rstrip('/')}/api/v4/projects/{self._ref(project)}"
                f"/repository/files/{quote(path, safe='')}/raw",
                params={"ref": ref},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise GitLabError(f"reading {path}@{ref}: {exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise GitLabError(f"reading {path}@{ref} -> HTTP {response.status_code}")
        return response.text

    def codeowners(self, project: str | int, ref: str) -> tuple[str | None, str]:
        """Return ``(text, path)`` for the first CODEOWNERS location that exists."""
        for path in CODEOWNERS_PATHS:
            text = self.raw_file(project, path, ref)
            if text is not None:
                return text, path
        return None, CODEOWNERS_PATHS[1]

    # ------------------------------------------------------------- merge requests

    def merge_request(self, project: str | int, iid: int) -> dict:
        return self.get(f"/projects/{self._ref(project)}/merge_requests/{iid}")

    def merge_request_diffs(self, project: str | int, iid: int) -> tuple[list[str], bool]:
        """Changed paths (both sides of renames) and a truncation flag."""
        paths: list[str] = []
        truncated = False
        for diff in self.paginate(f"/projects/{self._ref(project)}/merge_requests/{iid}/diffs"):
            if not isinstance(diff, dict):
                continue
            if diff.get("collapsed") or diff.get("too_large"):
                truncated = True
            for key in ("new_path", "old_path"):
                path = diff.get(key)
                if path and path not in paths:
                    paths.append(path)
        return paths, truncated

    def approvals(self, project: str | int, iid: int) -> list[str]:
        data = self.get(f"/projects/{self._ref(project)}/merge_requests/{iid}/approvals") or {}
        return [
            entry.get("user", {}).get("username")
            for entry in data.get("approved_by", [])
            if entry.get("user", {}).get("username")
        ]

    def add_labels(self, project: str | int, iid: int, labels: list[str]) -> None:
        if not labels:
            return
        self._request(
            "PUT",
            f"/projects/{self._ref(project)}/merge_requests/{iid}",
            body={"add_labels": ",".join(labels)},
        )

    def notes(self, project: str | int, iid: int) -> list[dict]:
        return list(self.paginate(f"/projects/{self._ref(project)}/merge_requests/{iid}/notes"))

    def add_note(self, project: str | int, iid: int, body: str) -> None:
        self._request(
            "POST",
            f"/projects/{self._ref(project)}/merge_requests/{iid}/notes",
            body={"body": body},
        )

    def find_merge_request_for_commit(
        self, project: str | int, sha: str, target_branch: str | None = None
    ) -> dict | None:
        """Map a post-merge pipeline's commit back to its MR (§6.2)."""
        params = {"state": "merged", "order_by": "updated_at", "sort": "desc", "per_page": 50}
        if target_branch:
            params["target_branch"] = target_branch
        for mr in self.paginate(f"/projects/{self._ref(project)}/merge_requests", params):
            if sha in (mr.get("merge_commit_sha"), mr.get("squash_commit_sha")):
                return mr

        # Fallback: the commits API, which a job token is also allowed to call.
        try:
            related = self.get(
                f"/projects/{self._ref(project)}/repository/commits/{quote(sha, safe='')}/merge_requests"
            )
        except GitLabError as exc:  # pragma: no cover - network dependent
            log.warning("commit→MR lookup failed: %s", exc)
            return None
        for entry in related or []:
            iid = entry.get("iid")
            if iid:
                return self.merge_request(project, iid)
        return None

    # -------------------------------------------------------------------- rosters

    def group_members(self, group: str) -> set[str]:
        """All members of a group, including inherited ones, cached per run."""
        if group in self._members:
            return self._members[group]
        names = {
            member.get("username")
            for member in self.paginate(f"/groups/{quote(group, safe='')}/members/all")
            if member.get("username")
        }
        self._members[group] = names
        return names

    # ------------------------------------------------------------------ pipelines

    def pipeline_jobs(self, project: str | int, pipeline_id: str | int) -> list[dict]:
        return list(self.paginate(f"/projects/{self._ref(project)}/pipelines/{pipeline_id}/jobs"))

    def commit_authors(
        self, project: str | int, path: str = "", ref: str = "", limit: int = 100
    ) -> dict[str, int]:
        """Recent authors touching ``path``, as ``{email: commit_count}``.

        Used only by the bootstrap script to *suggest* owners. The commits API returns
        author e-mail rather than a username (the username field is not part of the
        commit object), so suggestions are matched against rosters by the caller and
        always reviewed by a human.
        """
        params: dict[str, str | int] = {"per_page": 100}
        if path:
            params["path"] = path
        if ref:
            params["ref_name"] = ref

        counts: dict[str, int] = {}
        for index, commit in enumerate(
            self.paginate(f"/projects/{self._ref(project)}/repository/commits", params)
        ):
            if index >= limit:
                break
            email = (commit.get("author_email") or "").lower()
            if email:
                counts[email] = counts.get(email, 0) + 1
        return counts

    # -------------------------------------------------------- writes for rollout

    def create_commit(
        self,
        project: str | int,
        branch: str,
        *,
        start_branch: str,
        actions: list[dict],
        message: str,
    ) -> dict:
        return self._request(
            "POST",
            f"/projects/{self._ref(project)}/repository/commits",
            body={
                "branch": branch,
                "start_branch": start_branch,
                "commit_message": message,
                "actions": actions,
            },
        )

    def create_merge_request(
        self,
        project: str | int,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str = "",
        remove_source_branch: bool = True,
    ) -> dict:
        return self._request(
            "POST",
            f"/projects/{self._ref(project)}/merge_requests",
            body={
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": title,
                "description": description,
                "remove_source_branch": remove_source_branch,
            },
        )


def to_merge_request(data: dict, *, project_path: str = "") -> MergeRequest:
    """Convert the API representation into the engine's ``MergeRequest``."""
    created = data.get("created_at") or ""
    merged = data.get("merged_at")
    reference = data.get("project_path") or data.get("references", {}).get("full", "").split("!")[0]
    return MergeRequest(
        project_id=int(data.get("project_id") or 0),
        project_path=reference or project_path,
        iid=int(data.get("iid") or 0),
        title=data.get("title") or "",
        url=data.get("web_url") or "",
        author_username=(data.get("author") or {}).get("username", ""),
        author_name=(data.get("author") or {}).get("name", ""),
        state=data.get("state") or "",
        draft=bool(data.get("draft") or data.get("work_in_progress")),
        source_branch=data.get("source_branch") or "",
        target_branch=data.get("target_branch") or "",
        created_at=_parse(created) or datetime.now(),
        labels=frozenset(data.get("labels") or []),
        sha=data.get("sha"),
        merged_at=_parse(merged),
    )


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
