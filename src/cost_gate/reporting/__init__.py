"""Console, JSON and Markdown renderers.

Three audiences, one artifact: a developer at a terminal, a machine consuming the JSON,
and a reviewer reading a pull-request comment.

Two rules cut across all three. Every value that originated in a template is escaped
before rendering (:mod:`cost_gate.reporting.escaping`), because those values reach a
GitHub comment. And nothing is rendered until the report has been reconciled
(:mod:`cost_gate.reporting.reconcile`) — a report that does not add up is worse than no
report, because it looks authoritative and is wrong.
"""

from __future__ import annotations

from cost_gate.reporting.console import render_console
from cost_gate.reporting.escaping import code, escape_markdown, table_cell, truncate
from cost_gate.reporting.json_report import render_json, write_json
from cost_gate.reporting.markdown import COMMENT_MARKER, MAX_COMMENT_BYTES, render_markdown
from cost_gate.reporting.reconcile import reconcile_artifact, reconcile_report

__all__ = [
    "COMMENT_MARKER",
    "MAX_COMMENT_BYTES",
    "code",
    "escape_markdown",
    "reconcile_artifact",
    "reconcile_report",
    "render_console",
    "render_json",
    "render_markdown",
    "table_cell",
    "truncate",
    "write_json",
]
