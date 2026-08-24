"""AWS cost-aware deployment gate.

Estimates the monthly AWS cost impact of an infrastructure change and evaluates it
against version-controlled budgets and policies, producing an explainable decision.
"""

from __future__ import annotations

from importlib import metadata

__all__ = ["__version__"]


def _resolve_version() -> str:
    """Return the installed distribution version, or a placeholder when not installed."""
    try:
        return metadata.version("aws-cost-aware-deployment-gate")
    except metadata.PackageNotFoundError:  # pragma: no cover - only when run from a source tree
        return "0.0.0+unknown"


__version__: str = _resolve_version()
