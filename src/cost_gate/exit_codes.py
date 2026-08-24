"""Process exit codes.

The exit code is the machine-readable form of a gate decision: it is what branch
protection and downstream deployment jobs observe. The mapping is part of the public
contract of this tool and is documented in ``docs/policy-engine.md``.

``ERROR`` deliberately fails rather than passing. A gate that opens when it is
confused is not a gate.
"""

from __future__ import annotations

from enum import IntEnum

__all__ = ["ExitCode"]


class ExitCode(IntEnum):
    """Exit status returned by the ``cost-gate`` command."""

    PASS = 0
    """No policy matched, or only advisory policies matched."""

    REQUIRE_APPROVAL = 10
    """A policy requires an authorised approval before the change may proceed."""

    BLOCK = 20
    """A policy forbids the change."""

    ERROR = 30
    """The tool could not produce a trustworthy answer: invalid configuration,
    an unreadable template, a provider failure, or a failed reconciliation check."""

    USAGE = 64
    """Invalid command-line usage. Matches the BSD ``sysexits.h`` convention."""
