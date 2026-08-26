"""Posting a report to a pull request, treating everything it came from as hostile.

This module runs in the **privileged** half of the integration: the workflow that calls
it holds a token with ``pull-requests: write``. The report it posts was produced by the
*unprivileged* half, running code from a pull request anyone can open. Everything
crossing that boundary is attacker-controlled data.

Three defences, in order of importance:

1. **The comment body is re-rendered here**, from validated JSON, by the same escaper
   every other report goes through. The uploaded ``report.md`` is never posted. A
   crafted Markdown file therefore cannot reach a comment at all.
2. **The pull request is identified by commit SHA, not by the number in the artifact.**
   The number is attacker-controlled: a malicious pull request could name someone
   else's and have the comment posted there. The ``workflow_run`` event's ``head_sha``
   is set by GitHub, so resolving the pull request from it is the only trustworthy
   route. (Note that ``workflow_run.pull_requests`` is *empty* for forks, which is
   precisely the case this integration exists to support, so it cannot be used.)
3. **The artifact is size-capped and schema-validated** before anything reads it.
   ``AnalysisArtifact`` forbids unknown fields, so a payload carrying extra keys is
   rejected rather than partially trusted.

The HTTP client lives behind :class:`GitHubApi` so the logic here can be tested against
a fake. A test that needs a live repository is a test nobody runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from pydantic import ValidationError

from cost_gate.domain.artifact import ARTIFACT_SCHEMA_VERSION, AnalysisArtifact

__all__ = [
    "MAX_ARTIFACT_BYTES",
    "MAX_COMMENT_PAGES",
    "Comment",
    "CommentOutcome",
    "GitHubApi",
    "GitHubError",
    "load_untrusted_artifact",
    "resolve_pull_request",
    "upsert_comment",
]

MAX_ARTIFACT_BYTES: Final = 5 * 1024 * 1024
"""Refuse a report larger than this without parsing it.

Generous next to a real report, which is tens of kilobytes. It exists so that an
artifact crafted to exhaust memory is rejected by a `stat` call rather than by the JSON
parser.
"""

MAX_COMMENT_PAGES: Final = 20
"""How far to page through a pull request's comments looking for ours.

A long-lived pull request can carry hundreds of comments and the marker may be on page
three. A cap stops a pathological thread turning into an unbounded API loop.
"""


class GitHubError(Exception):
    """The integration refused to act, or the API did not cooperate."""


@dataclass(frozen=True)
class Comment:
    """One issue comment, reduced to what the upsert needs."""

    identifier: int
    body: str
    author: str


class CommentOutcome(StrEnum):
    """What the upsert did. Reported so a workflow log says which happened."""

    CREATED = "created"
    UPDATED = "updated"


class GitHubApi(Protocol):
    """The slice of the GitHub API this integration uses.

    Deliberately tiny. A protocol this small can be faked completely in a test, which
    is the only way the comment logic gets exercised without a live repository.
    """

    def pull_requests_for_commit(self, sha: str) -> list[int]:
        """Pull request numbers whose head is ``sha``."""
        ...

    def list_comments(self, pull_request: int) -> list[Comment]:
        """Every issue comment on a pull request, oldest first."""
        ...

    def create_comment(self, pull_request: int, body: str) -> int:
        """Post a new comment, returning its id."""
        ...

    def update_comment(self, comment_id: int, body: str) -> bool:
        """Edit an existing comment. ``False`` means it no longer exists."""
        ...


def load_untrusted_artifact(path: Path) -> AnalysisArtifact:
    """Load a report produced by an untrusted job.

    Raises:
        GitHubError: if the file is missing, oversized, not valid JSON, does not match
            the artifact schema, or was produced by a different schema version.
    """
    if not path.is_file():
        raise GitHubError(f"no report at {path}")

    size = path.stat().st_size
    if size > MAX_ARTIFACT_BYTES:
        raise GitHubError(f"report is {size} bytes; the maximum is {MAX_ARTIFACT_BYTES}")

    try:
        artifact = AnalysisArtifact.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, ValueError, UnicodeDecodeError) as exc:
        # Deliberately not echoing the payload into the error: it is attacker-controlled
        # and this message may reach a log a lot of people can read.
        raise GitHubError(
            f"{path.name} is not a valid cost-gate report: {type(exc).__name__}"
        ) from exc

    if artifact.schema_version != ARTIFACT_SCHEMA_VERSION:
        raise GitHubError(
            f"report uses schema version {artifact.schema_version!r}, but this tool "
            f"produces {ARTIFACT_SCHEMA_VERSION!r}"
        )
    return artifact


def resolve_pull_request(api: GitHubApi, head_sha: str, *, claimed: int | None = None) -> int:
    """Find the pull request a report belongs to, from its head commit.

    ``head_sha`` comes from the ``workflow_run`` event and is set by GitHub, so it is
    the one piece of routing information the untrusted job cannot influence.

    ``claimed`` is the number the artifact says it belongs to. It is checked against the
    resolved one rather than trusted: a mismatch means the artifact is trying to steer
    the comment somewhere it does not belong, and the run is refused rather than
    silently corrected.

    Raises:
        GitHubError: if no pull request has that head, or the artifact claims a
            different one.
    """
    if not head_sha or not all(character in "0123456789abcdefABCDEF" for character in head_sha):
        raise GitHubError(f"{head_sha!r} is not a commit SHA")

    candidates = api.pull_requests_for_commit(head_sha)
    if not candidates:
        raise GitHubError(f"no open pull request has {head_sha[:12]} as its head commit")
    if len(candidates) > 1:
        # Two pull requests can share a head commit. Guessing which one the report
        # belongs to would be a coin flip, and the wrong choice posts someone's cost
        # analysis onto an unrelated change.
        raise GitHubError(
            f"{head_sha[:12]} is the head of {len(candidates)} pull requests "
            f"({', '.join(str(number) for number in sorted(candidates))}); refusing to guess"
        )

    resolved = candidates[0]
    if claimed is not None and claimed != resolved:
        raise GitHubError(
            f"the report claims pull request #{claimed}, but {head_sha[:12]} belongs to "
            f"#{resolved}. Refusing to comment on a pull request the report is not about"
        )
    return resolved


def find_existing_comment(
    api: GitHubApi,
    pull_request: int,
    *,
    marker: str,
    author: str | None = None,
) -> Comment | None:
    """Find the comment this integration previously posted, if any.

    Matching on a hidden marker rather than on position or wording means the comment
    survives being edited by a human and is not confused with somebody quoting the
    report.

    ``author`` narrows the search to the workflow identity. Without it, a contributor
    who pasted the marker into their own comment could have the gate overwrite it.
    """
    for comment in api.list_comments(pull_request):
        if marker in comment.body and (author is None or comment.author == author):
            return comment
    return None


def upsert_comment(
    api: GitHubApi,
    pull_request: int,
    body: str,
    *,
    marker: str,
    author: str | None = None,
) -> tuple[CommentOutcome, int]:
    """Post the report, or update the one already there.

    One comment that changes in place, rather than a wall of stale reports accumulating
    with every push.

    Returns:
        What happened, and the comment's id.

    Raises:
        GitHubError: if the comment could not be posted.
    """
    if marker not in body:
        # Without the marker the next run cannot find this comment, and would post a
        # second one. Better to fail here than to start accumulating duplicates.
        raise GitHubError("refusing to post a comment that does not carry the marker")

    existing = find_existing_comment(api, pull_request, marker=marker, author=author)
    if existing is not None and api.update_comment(existing.identifier, body):
        return CommentOutcome.UPDATED, existing.identifier
    # Either there was nothing to update, or somebody deleted it between the listing
    # and the edit. Posting a new one is the right recovery for the second case: the
    # alternative is losing the report over a race.
    return CommentOutcome.CREATED, api.create_comment(pull_request, body)
