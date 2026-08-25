"""Template parsing errors."""

from __future__ import annotations

from cost_gate.config.errors import DocumentError, DocumentIssue

__all__ = ["TemplateError", "TemplateIssue"]

TemplateIssue = DocumentIssue


class TemplateError(DocumentError):
    """A CloudFormation template could not be read or was structurally invalid.

    Structural problems only. A template that parses but contains values the tool cannot
    resolve is *not* an error: unresolved values are a normal, expected outcome and are
    reported as unknowns rather than as failures.
    """

    label = "template"
