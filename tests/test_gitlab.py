"""The thin GitLab client's plumbing: error normalisation and redirect safety.

Transport failures must surface as :class:`GitLabError`, because every caller's
degrade-gracefully path is written against that type (§11).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import requests

from ownership_bot.gitlab import GitLab, GitLabError, to_merge_request


class StubResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.content = b"x" if payload is not None else b""

    def json(self):  # noqa: ANN201
        return self._payload


class StubSession(requests.Session):
    """A session that returns a canned response (or raises) without the network."""

    def __init__(self, response=None, error: Exception | None = None) -> None:
        super().__init__()
        self._response = response
        self._error = error
        self.requests: list[dict] = []

    def request(self, method, url, **kwargs):  # type: ignore[override]  # noqa: ANN001
        self.requests.append({"method": method, "url": url, **kwargs})
        if self._error is not None:
            raise self._error
        return self._response


def make_client(**kwargs) -> tuple[GitLab, StubSession]:
    client = GitLab("https://gitlab.example.com", "token")
    session = StubSession(**kwargs)
    client.session = session
    return client, session


@pytest.mark.parametrize(
    "error",
    [requests.ConnectionError("refused"), requests.Timeout("slow")],
)
def test_transport_errors_become_gitlab_errors(error):
    client, _session = make_client(error=error)

    with pytest.raises(GitLabError):
        client.get("/projects/1")


def test_http_error_status_is_a_gitlab_error():
    client, _session = make_client(response=StubResponse(500, text="boom"))

    with pytest.raises(GitLabError, match="HTTP 500"):
        client.get("/projects/1")


def test_redirects_are_not_followed():
    """A 3xx must be an error, never followed with the token attached."""
    client, session = make_client(response=StubResponse(302))

    with pytest.raises(GitLabError):
        client.get("/projects/1")

    assert session.requests[0]["allow_redirects"] is False


def test_to_merge_request_defaults_created_at_to_an_aware_datetime():
    """A naive fallback would make ``age_minutes`` raise on an aware ``now``."""
    mr = to_merge_request({"iid": 1, "author": {"username": "x"}})

    assert mr.created_at.tzinfo is not None
    assert mr.age_minutes(datetime.now(UTC)) >= 0
