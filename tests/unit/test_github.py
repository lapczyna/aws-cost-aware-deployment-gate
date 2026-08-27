"""Posting a report to a pull request.

This is the privileged half of the integration, acting on data produced by a job that
ran code from a pull request anyone can open. Most of these tests are about what it
refuses to do.

Everything runs against a fake API. A test that needs a live repository is a test
nobody runs, and this logic is exactly the kind that has to be exercised on every
change.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from cost_gate.adapters.github import (
    MAX_ARTIFACT_BYTES,
    Comment,
    CommentOutcome,
    GitHubError,
    find_existing_comment,
    load_untrusted_artifact,
    resolve_pull_request,
    upsert_comment,
)
from cost_gate.adapters.github_http import GitHubHttpApi, validate_repository
from cost_gate.domain.artifact import ARTIFACT_SCHEMA_VERSION
from cost_gate.reporting import render_json
from cost_gate.reporting.markdown import COMMENT_MARKER, render_markdown
from tests.factories import artifact_with, component

pytestmark = pytest.mark.unit

BOT = "github-actions[bot]"


class FakeApi:
    """Everything the integration needs from GitHub, in memory."""

    def __init__(
        self,
        *,
        comments: list[Comment] | None = None,
        pulls: dict[str, list[int]] | None = None,
    ) -> None:
        self.comments = list(comments or [])
        self.pulls = pulls or {}
        self.created: list[tuple[int, str]] = []
        self.updated: list[tuple[int, str]] = []
        self.deleted: set[int] = set()
        self._next_id = 1000

    def pull_requests_for_commit(self, sha: str) -> list[int]:
        return list(self.pulls.get(sha.lower(), []))

    def list_comments(self, pull_request: int) -> list[Comment]:
        return [c for c in self.comments if c.identifier not in self.deleted]

    def create_comment(self, pull_request: int, body: str) -> int:
        self._next_id += 1
        self.created.append((pull_request, body))
        self.comments.append(Comment(identifier=self._next_id, body=body, author=BOT))
        return self._next_id

    def update_comment(self, comment_id: int, body: str) -> bool:
        if comment_id in self.deleted:
            return False
        self.updated.append((comment_id, body))
        for index, existing in enumerate(self.comments):
            if existing.identifier == comment_id:
                self.comments[index] = replace(existing, body=body)
        return True


@pytest.fixture
def report(tmp_path: Path) -> Path:
    """A valid artifact on disk."""
    path = tmp_path / "report.json"
    artifact = artifact_with(components=[component(logical_id="Nat", delta="32.40")])
    path.write_text(render_json(artifact), encoding="utf-8", newline="\n")
    return path


class TestTheArtifactIsUntrusted:
    def test_a_valid_report_loads(self, report):
        assert load_untrusted_artifact(report).schema_version == ARTIFACT_SCHEMA_VERSION

    def test_a_missing_report_is_refused(self, tmp_path):
        with pytest.raises(GitHubError, match="no report"):
            load_untrusted_artifact(tmp_path / "absent.json")

    def test_an_oversized_report_is_refused_without_being_parsed(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cost_gate.adapters.github.MAX_ARTIFACT_BYTES", 100)
        path = tmp_path / "big.json"
        path.write_text("{}" + " " * 500, encoding="utf-8")
        with pytest.raises(GitHubError, match="maximum"):
            load_untrusted_artifact(path)

    def test_the_size_cap_is_generous_enough_for_a_real_report(self, report):
        assert report.stat().st_size < MAX_ARTIFACT_BYTES

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ("not json at all", "not valid JSON"),
            ("[]", "not a cost-gate report"),
            ("null", "not a cost-gate report"),
            ('{"schema_version": "1"}', "declares schema version"),
            (
                '{"schema_version": "' + ARTIFACT_SCHEMA_VERSION + '"}',
                "not a valid cost-gate report",
            ),
        ],
    )
    def test_malformed_payloads_are_refused(self, tmp_path, payload, expected):
        # Each fails for a different reason and the message says which. A test demanding
        # one message for all of them would have to be loosened every time the checks
        # get more specific, which is the wrong direction.
        path = tmp_path / "bad.json"
        path.write_text(payload, encoding="utf-8")
        with pytest.raises(GitHubError, match=expected):
            load_untrusted_artifact(path)

    def test_an_unknown_field_is_refused(self, tmp_path, report):
        # extra="forbid" on the model is what turns a smuggled field into a rejection
        # rather than a value that quietly rides along.
        payload = json.loads(report.read_text("utf-8"))
        payload["surprise"] = "hello"
        path = tmp_path / "extra.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(GitHubError, match="not a valid cost-gate report"):
            load_untrusted_artifact(path)

    def test_the_version_is_checked_before_the_model(self, tmp_path):
        # The order matters and was wrong. AnalysisArtifact forbids unknown fields, so a
        # document from a newer tool fails validation on the field it added and never
        # reaches a version check placed afterwards - which turned a one-line diagnosis
        # into a bare ValidationError in a workflow log.
        path = tmp_path / "newer.json"
        path.write_text(
            json.dumps({"schema_version": "99", "a_field_from_the_future": True}),
            encoding="utf-8",
        )
        with pytest.raises(GitHubError, match="declares schema version"):
            load_untrusted_artifact(path)

    def test_the_version_message_explains_why_it_cannot_be_read(self, tmp_path):
        path = tmp_path / "newer.json"
        path.write_text(json.dumps({"schema_version": "99"}), encoding="utf-8")
        with pytest.raises(GitHubError, match="unknown fields are refused"):
            load_untrusted_artifact(path)

    def test_a_different_schema_version_is_refused(self, tmp_path, report):
        payload = json.loads(report.read_text("utf-8"))
        payload["schema_version"] = "99"
        path = tmp_path / "future.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(GitHubError, match="schema version"):
            load_untrusted_artifact(path)

    def test_the_error_does_not_echo_the_payload(self, tmp_path):
        # The message may reach a log many people can read, and the payload is written
        # by whoever opened the pull request.
        marker = "SUPER-SECRET-MARKER-9127"
        path = tmp_path / "bad.json"
        path.write_text(f'{{"schema_version": "{marker}"', encoding="utf-8")
        with pytest.raises(GitHubError) as caught:
            load_untrusted_artifact(path)
        assert marker not in str(caught.value)


class TestTheCommentBodyIsRebuilt:
    def test_the_body_comes_from_the_validated_artifact(self, report):
        # Not from the uploaded report.md. This is the property that makes a crafted
        # Markdown file unable to reach a comment at all.
        body = render_markdown(load_untrusted_artifact(report))
        assert body.startswith(COMMENT_MARKER)

    def test_a_hostile_logical_id_cannot_inject_markup(self, tmp_path):
        artifact = artifact_with(
            components=[component(logical_id="<img src=x onerror=alert(1)>", delta="1.00")]
        )
        path = tmp_path / "hostile.json"
        path.write_text(render_json(artifact), encoding="utf-8", newline="\n")
        body = render_markdown(load_untrusted_artifact(path))
        assert "<img" not in body.replace(COMMENT_MARKER, "")


class TestResolvingThePullRequest:
    SHA = "0f5c9e2a1b3d4f6a8c0e2b4d6f8a0c2e4b6d8f0a"

    def test_the_pull_request_comes_from_the_head_commit(self):
        api = FakeApi(pulls={self.SHA: [42]})
        assert resolve_pull_request(api, self.SHA) == 42

    def test_a_report_claiming_another_pull_request_is_refused(self):
        # The number in the artifact is written by a job that ran pull-request code, so
        # a malicious change could name someone else's pull request and have its cost
        # analysis posted there.
        api = FakeApi(pulls={self.SHA: [42]})
        with pytest.raises(GitHubError, match="Refusing to comment"):
            resolve_pull_request(api, self.SHA, claimed=7)

    def test_a_matching_claim_is_accepted(self):
        api = FakeApi(pulls={self.SHA: [42]})
        assert resolve_pull_request(api, self.SHA, claimed=42) == 42

    def test_an_unknown_commit_is_refused(self):
        with pytest.raises(GitHubError, match="no open pull request"):
            resolve_pull_request(FakeApi(), self.SHA)

    def test_an_ambiguous_commit_is_refused_rather_than_guessed(self):
        # Posting to the wrong one puts a cost analysis on an unrelated change.
        api = FakeApi(pulls={self.SHA: [42, 43]})
        with pytest.raises(GitHubError, match="refusing to guess"):
            resolve_pull_request(api, self.SHA)

    @pytest.mark.parametrize(
        "sha",
        [
            "",
            "not-a-sha",
            "../../etc/passwd",
            "42; rm -rf /",
            "0f5c9e2a1b3d4f6a8c0e2b4d6f8a0c2e4b6d8f0z",
        ],
    )
    def test_anything_that_is_not_a_commit_sha_is_refused(self, sha):
        with pytest.raises(GitHubError, match="not a commit SHA"):
            resolve_pull_request(FakeApi(), sha)


class TestUpsert:
    def body(self, text: str = "the report") -> str:
        return f"{COMMENT_MARKER}\n\n{text}"

    def test_the_first_run_creates_a_comment(self):
        api = FakeApi()
        outcome, _ = upsert_comment(api, 42, self.body(), marker=COMMENT_MARKER, author=BOT)
        assert outcome is CommentOutcome.CREATED
        assert len(api.created) == 1

    def test_the_second_run_updates_it_in_place(self):
        # Otherwise every push leaves another stale report behind, and the pull request
        # becomes unreadable.
        api = FakeApi()
        upsert_comment(api, 42, self.body("first"), marker=COMMENT_MARKER, author=BOT)
        outcome, _ = upsert_comment(api, 42, self.body("second"), marker=COMMENT_MARKER, author=BOT)
        assert outcome is CommentOutcome.UPDATED
        assert len(api.created) == 1
        assert len(api.updated) == 1

    def test_ten_runs_leave_exactly_one_comment(self):
        api = FakeApi()
        for index in range(10):
            upsert_comment(api, 42, self.body(str(index)), marker=COMMENT_MARKER, author=BOT)
        assert len([c for c in api.comments if COMMENT_MARKER in c.body]) == 1

    def test_a_deleted_comment_is_replaced_rather_than_lost(self):
        # Somebody can delete it between the listing and the edit. Losing the report
        # over that race would be worse than posting a new one.
        api = FakeApi()
        _, comment_id = upsert_comment(api, 42, self.body(), marker=COMMENT_MARKER, author=BOT)
        api.deleted.add(comment_id)
        outcome, _ = upsert_comment(api, 42, self.body("again"), marker=COMMENT_MARKER, author=BOT)
        assert outcome is CommentOutcome.CREATED

    def test_a_human_comment_carrying_the_marker_is_not_overwritten(self):
        # A contributor quoting the report, or pasting the marker deliberately, must
        # not have their comment silently replaced by the gate.
        api = FakeApi(
            comments=[Comment(identifier=1, body=f"look: {COMMENT_MARKER}", author="a-contributor")]
        )
        outcome, _ = upsert_comment(api, 42, self.body(), marker=COMMENT_MARKER, author=BOT)
        assert outcome is CommentOutcome.CREATED
        assert api.comments[0].body == f"look: {COMMENT_MARKER}"

    def test_other_comments_are_left_alone(self):
        api = FakeApi(comments=[Comment(identifier=1, body="looks good to me", author="reviewer")])
        upsert_comment(api, 42, self.body(), marker=COMMENT_MARKER, author=BOT)
        assert api.comments[0].body == "looks good to me"

    def test_a_body_without_the_marker_is_refused(self):
        # The next run could not find it, so it would post a duplicate. Failing here is
        # better than starting to accumulate them.
        with pytest.raises(GitHubError, match="marker"):
            upsert_comment(FakeApi(), 42, "no marker here", marker=COMMENT_MARKER, author=BOT)

    def test_the_marker_is_found_even_when_it_is_not_the_first_comment(self):
        api = FakeApi(
            comments=[
                Comment(identifier=index, body=f"comment {index}", author="reviewer")
                for index in range(1, 30)
            ]
        )
        api.comments.append(Comment(identifier=99, body=self.body("old"), author=BOT))
        outcome, comment_id = upsert_comment(
            api, 42, self.body("new"), marker=COMMENT_MARKER, author=BOT
        )
        assert (outcome, comment_id) == (CommentOutcome.UPDATED, 99)


class TestFindingTheExistingComment:
    def test_nothing_is_found_on_an_empty_pull_request(self):
        assert find_existing_comment(FakeApi(), 42, marker=COMMENT_MARKER) is None

    def test_the_author_filter_can_be_disabled(self):
        api = FakeApi(comments=[Comment(identifier=1, body=COMMENT_MARKER, author="someone")])
        assert find_existing_comment(api, 42, marker=COMMENT_MARKER) is not None


class TestRepositoryValidation:
    @pytest.mark.parametrize("repository", ["owner/name", "a-b.c/d_e-f", "Org123/repo.name"])
    def test_ordinary_repositories_are_accepted(self, repository):
        assert validate_repository(repository) == repository

    @pytest.mark.parametrize(
        "repository",
        [
            "owner/name/../../other",
            "owner",
            "owner/name?query=1",
            "../etc/passwd",
            "owner/name#fragment",
            "",
            "owner/na me",
        ],
    )
    def test_anything_that_could_redirect_the_url_is_refused(self, repository):
        # This value comes from a workflow input and is interpolated into an API path.
        with pytest.raises(GitHubError):
            validate_repository(repository)

    def test_a_client_without_a_token_is_refused(self):
        with pytest.raises(GitHubError, match="token"):
            GitHubHttpApi("owner/name", "")

    @pytest.mark.parametrize(
        "api_root",
        ["file:///etc/passwd", "ftp://example.invalid", "gopher://example.invalid", "/local/path"],
    )
    def test_a_non_http_api_root_is_refused(self, api_root):
        # urlopen honours file://, so an unchecked api_root would turn an API client
        # into a file reader - in a job holding a write token.
        with pytest.raises(GitHubError, match="http"):
            GitHubHttpApi("owner/name", "token", api_root=api_root)

    def test_an_https_api_root_is_accepted(self):
        assert GitHubHttpApi("owner/name", "token").repository == "owner/name"
