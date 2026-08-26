r"""The machine-readable artifact.

Two properties matter more than anything else here.

**Stability.** The document carries ``schema_version``, and a consumer pins it. The
privileged half of the GitHub integration reads this artifact as untrusted input and
validates it against a schema before rendering a comment, so a change to its shape is a
change to a security boundary, not just a convenience.

**Determinism.** Money is serialised as strings and field order is fixed by the model,
so two runs over the same input produce byte-identical files. That is what makes golden
tests meaningful, and what lets a reviewer diff two reports and see only what actually
changed.
"""

from __future__ import annotations

from pathlib import Path

from cost_gate.domain.artifact import AnalysisArtifact

__all__ = ["render_json", "write_json"]


def render_json(artifact: AnalysisArtifact, indent: int = 2) -> str:
    """Serialise an artifact deterministically."""
    return artifact.model_dump_json(indent=indent, by_alias=True) + "\n"


def write_json(artifact: AnalysisArtifact, path: Path, indent: int = 2) -> Path:
    r"""Write the artifact, with explicit newlines.

    ``newline="\n"`` is required rather than cosmetic: development happens on Windows
    with ``core.autocrlf`` enabled, and a golden-file comparison would otherwise fail on
    line endings alone.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_json(artifact, indent), encoding="utf-8", newline="\n")
    return path
