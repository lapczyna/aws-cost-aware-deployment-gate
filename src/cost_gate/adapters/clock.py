"""Time, behind a port.

Reports must be byte-identical for identical input, and a timestamp read directly from
the operating system makes that impossible to test. Every part of the tool that needs
the current time or a run identifier asks a :class:`Clock`, and the golden-file tests
inject a fixed one.

This is the whole reason ``adapters`` exists as a layer: the clock is an external system
just as much as the AWS API is.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "FixedClock", "SystemClock"]


@runtime_checkable
class Clock(Protocol):
    """A source of the current time and of run identifiers."""

    def now(self) -> datetime:
        """The current instant, timezone-aware."""
        ...

    def run_id(self) -> str:
        """An identifier correlating every artifact from one execution."""
        ...


class SystemClock:
    """The real clock. Used everywhere except tests."""

    def __init__(self) -> None:
        """Fix the run identifier once, so every artifact from one run agrees."""
        self._run_id = uuid.uuid4().hex[:12]

    def now(self) -> datetime:
        """The current UTC instant."""
        return datetime.now(tz=UTC)

    def run_id(self) -> str:
        """The identifier for this execution."""
        return self._run_id


@dataclass
class FixedClock:
    """A clock that never moves. Used by golden-file tests."""

    instant: datetime = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    identifier: str = "fixedrun0001"

    def now(self) -> datetime:
        """The fixed instant."""
        return self.instant

    def run_id(self) -> str:
        """The fixed identifier."""
        return self.identifier
