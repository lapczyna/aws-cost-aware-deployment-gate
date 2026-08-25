"""Errors that point at the problem.

A validation message that says "1 validation error for UsageProfileConfig" and then
prints a Python type name is a message the user cannot act on. Every error raised here
names the file, the path within it, the offending value, and — where the cause is a
closed vocabulary such as a driver name — the permitted alternatives.

:class:`DocumentError` is shared by configuration loading and template parsing, so both
report problems the same way.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Self

from pydantic import ValidationError

__all__ = ["ConfigError", "DocumentError", "DocumentIssue", "from_validation_error"]

_MAX_VALUE_LENGTH = 120


class DocumentIssue:
    """One problem with one part of one document."""

    def __init__(self, path: str, message: str, value: object = None) -> None:
        """Record a problem at a JSON-Pointer-style path within a document."""
        self.path = path or "/"
        self.message = message
        self.value = value

    def __str__(self) -> str:
        """Render as ``/path: message (received ...)``."""
        rendered = f"{self.path}: {self.message}"
        if self.value is not None:
            text = repr(self.value)
            if len(text) > _MAX_VALUE_LENGTH:
                text = text[: _MAX_VALUE_LENGTH - 1] + "…"
            rendered += f" (received {text})"
        return rendered


class DocumentError(Exception):
    """A document could not be loaded or was invalid.

    Carries every issue found rather than only the first, so that a user fixing a file
    does not have to rediscover the next problem on each run.
    """

    label = "document"

    def __init__(self, source: Path | str, issues: Sequence[DocumentIssue]) -> None:
        """Build an error for one document."""
        self.source = str(source)
        self.issues = tuple(issues)
        super().__init__(self.render())

    def render(self) -> str:
        """Render every issue, one per line, prefixed by the document."""
        header = f"invalid {self.label} in {self.source}"
        if not self.issues:
            return header
        body = "\n".join(f"  {issue}" for issue in self.issues)
        return f"{header}:\n{body}"

    @classmethod
    def single(cls, source: Path | str, message: str, path: str = "") -> Self:
        """Build an error with one issue."""
        return cls(source, [DocumentIssue(path, message)])


class ConfigError(DocumentError):
    """A configuration file could not be loaded or was invalid."""

    label = "configuration"


# Retained for readability at call sites that build issues directly.
ConfigIssue = DocumentIssue


def _pointer(location: Iterable[object]) -> str:
    """Render a pydantic error location as a JSON Pointer.

    Pydantic includes union-discriminator tags such as ``function-after[...]`` in some
    locations; these are dropped because they describe the validator, not the document,
    and would send a user looking for a key that does not exist in their file.
    """
    tokens: list[str] = []
    for part in location:
        text = str(part)
        if text.startswith(("function-", "constrained-", "tagged-union")):
            continue
        tokens.append(text.replace("~", "~0").replace("/", "~1"))
    return "/" + "/".join(tokens) if tokens else "/"


def from_validation_error(
    source: Path | str,
    error: ValidationError,
    error_type: type[DocumentError] = ConfigError,
) -> DocumentError:
    """Convert a pydantic :class:`ValidationError` into a user-facing error."""
    issues: list[DocumentIssue] = []
    for detail in error.errors():
        message = detail.get("msg", "invalid value")
        if detail.get("type") == "extra_forbidden":
            message = (
                "unknown key; this configuration uses a closed vocabulary, so a typo is "
                "rejected here rather than silently ignored"
            )
        issues.append(
            DocumentIssue(
                path=_pointer(detail.get("loc", ())),
                message=message,
                value=detail.get("input"),
            )
        )
    return error_type(source, issues)
