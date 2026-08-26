"""A GitHub API client built on the standard library.

``urllib`` rather than ``requests`` or ``PyGithub``: this runs in a privileged workflow
holding a token with write access to pull requests, and the smallest possible dependency
surface there is worth more than the ergonomics of a nicer client. It is also the only
place in the tool that makes a network call at all, which is easier to state truthfully
when nothing was added to do it.

The logic that decides *what* to post lives in :mod:`cost_gate.adapters.github` behind a
protocol. This module only moves bytes.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Final

from cost_gate.adapters.github import MAX_COMMENT_PAGES, Comment, GitHubError

__all__ = ["GitHubHttpApi", "GitHubNotFoundError", "validate_repository"]

API_ROOT: Final = "https://api.github.com"
PAGE_SIZE: Final = 100
TIMEOUT_SECONDS: Final = 30
MAX_ATTEMPTS: Final = 3
MAX_RESPONSE_BYTES: Final = 10 * 1024 * 1024

_REPOSITORY = re.compile(r"^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$")

_NOT_FOUND: Final = 404
_TOO_MANY_REQUESTS: Final = 429
_SERVER_ERROR: Final = 500


class GitHubNotFoundError(GitHubError):
    """A resource is gone. Separated because a deleted comment is recoverable."""


def validate_repository(repository: str) -> str:
    """Check an ``owner/name`` string before it is interpolated into a URL.

    The repository arrives from workflow inputs. Without this, a value containing
    ``../`` would let a crafted input address a different API endpoint entirely.

    Raises:
        GitHubError: if it is not a plain ``owner/name`` pair.
    """
    if not _REPOSITORY.match(repository):
        raise GitHubError(f"{repository!r} is not a valid owner/name repository")
    return repository


def _validate_api_root(api_root: str) -> str:
    """Insist that the API base is an HTTP(S) URL.

    ``urlopen`` honours ``file://``, so an api_root of ``file:///etc/passwd`` would turn
    an API client into a file reader - and this one runs in a job holding a write token.
    Plain ``http`` is permitted only so a test can point at a local stub; the default is
    HTTPS and nothing in the workflows overrides it.

    Raises:
        GitHubError: for any other scheme.
    """
    parsed = urllib.parse.urlparse(api_root)
    if parsed.scheme not in ("https", "http"):
        raise GitHubError(f"the API root must be an http(s) URL, not {parsed.scheme or 'empty'}")
    return api_root.rstrip("/")


class GitHubHttpApi:
    """Implements :class:`~cost_gate.adapters.github.GitHubApi` over HTTPS."""

    def __init__(self, repository: str, token: str, *, api_root: str = API_ROOT) -> None:
        """Prepare a client.

        Raises:
            GitHubError: if the repository is malformed or the token is empty.
        """
        self.repository = validate_repository(repository)
        if not token:
            raise GitHubError("a GitHub token is required to post a comment")
        self._token = token
        self._api_root = _validate_api_root(api_root)

    # -- transport ---------------------------------------------------------

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        """Make one API call, retrying the failures that are worth retrying.

        Rate limiting and 5xx are transient and retried with a backoff. A 4xx other
        than 429 is a real answer - the token lacks a permission, or the resource is
        gone - and retrying it just delays the error.

        Raises:
            GitHubError: on a non-transient failure, or after the last attempt.
        """
        url = f"{self._api_root}{path}"
        # Checked again at the point of use: _api_root is validated in __init__,
        # and `path` is built here from validated components, so nothing can have
        # changed the scheme in between. Cheap, and it keeps the guarantee local.
        if not url.startswith(("https://", "http://")):
            raise GitHubError("refusing to open a non-http(s) URL")
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=body, method=method)  # noqa: S310
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "cost-gate")
        if body is not None:
            request.add_header("Content-Type", "application/json")

        last: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                # nosec B310 - the scheme is checked in __init__ and again above;
                # file:// and custom schemes cannot reach this call.
                with urllib.request.urlopen(  # noqa: S310  # nosec B310
                    request, timeout=TIMEOUT_SECONDS
                ) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise GitHubError(f"{method} {path} returned an oversized response")
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                if exc.code == _NOT_FOUND:
                    raise GitHubNotFoundError(f"{method} {path} returned 404") from exc
                if exc.code != _TOO_MANY_REQUESTS and exc.code < _SERVER_ERROR:
                    # The token is missing a scope, or the input was rejected. The
                    # response body can quote attacker-controlled content, so only the
                    # status is reported.
                    raise GitHubError(f"{method} {path} failed with HTTP {exc.code}") from exc
                last = exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = exc

            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(2**attempt)

        raise GitHubError(f"{method} {path} failed after {MAX_ATTEMPTS} attempts: {last}")

    def _paged(self, path: str) -> list[Any]:
        """Collect every page of a listing endpoint.

        Paging matters here: a long-lived pull request can exceed one page, and a
        comment the gate posted weeks ago may be on page three. Stopping at the first
        page would silently post a duplicate.
        """
        collected: list[Any] = []
        for page in range(1, MAX_COMMENT_PAGES + 1):
            separator = "&" if "?" in path else "?"
            batch = self._request("GET", f"{path}{separator}per_page={PAGE_SIZE}&page={page}")
            if not isinstance(batch, list) or not batch:
                break
            collected.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
        return collected

    # -- the protocol ------------------------------------------------------

    def pull_requests_for_commit(self, sha: str) -> list[int]:
        """Pull request numbers whose head commit is ``sha``.

        Only open pull requests count. A report arriving for a commit whose pull request
        has already been merged or closed has nowhere useful to go.
        """
        quoted = urllib.parse.quote(sha, safe="")
        found = self._paged(f"/repos/{self.repository}/commits/{quoted}/pulls")
        return [
            int(item["number"])
            for item in found
            if isinstance(item, dict) and item.get("state") == "open" and "number" in item
        ]

    def list_comments(self, pull_request: int) -> list[Comment]:
        """Every issue comment on a pull request, oldest first."""
        raw = self._paged(f"/repos/{self.repository}/issues/{int(pull_request)}/comments")
        comments: list[Comment] = []
        for item in raw:
            if not isinstance(item, dict) or "id" not in item:
                continue
            author = item.get("user") or {}
            comments.append(
                Comment(
                    identifier=int(item["id"]),
                    body=str(item.get("body") or ""),
                    author=str(author.get("login") or ""),
                )
            )
        return comments

    def create_comment(self, pull_request: int, body: str) -> int:
        """Post a new comment, returning its id."""
        created = self._request(
            "POST",
            f"/repos/{self.repository}/issues/{int(pull_request)}/comments",
            {"body": body},
        )
        if not isinstance(created, dict) or "id" not in created:
            raise GitHubError("GitHub accepted the comment but did not return its id")
        return int(created["id"])

    def update_comment(self, comment_id: int, body: str) -> bool:
        """Edit an existing comment.

        Returns ``False`` when it has been deleted since it was listed, so the caller
        can post a new one instead of losing the report over a race.
        """
        try:
            self._request(
                "PATCH",
                f"/repos/{self.repository}/issues/comments/{int(comment_id)}",
                {"body": body},
            )
        except GitHubNotFoundError:
            return False
        return True
