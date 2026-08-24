"""Runtime schedules.

Assuming every resource runs continuously overstates development and test environments,
often by a factor of three. A schedule lets a profile say what it actually expects:

    development:
      schedule: "Mon-Fri 08:00-20:00"

Two design points are worth noticing.

**Determinism.** Weekly hours are converted to monthly hours with a fixed factor of
``730 / 168`` weeks per month, not by counting the weekdays in the current calendar
month. A report must not change because it was run in a month with five Mondays.

**A schedule is a statement of intent, not a control.** Declaring working hours does
not stop an instance running at the weekend. Everything derived from a schedule is
recorded as an assumption, and the recommendation engine suggests actually implementing
the schedule when it sees always-on development compute.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Self

from pydantic import BaseModel, ConfigDict

__all__ = ["DEFAULT_MONTHLY_HOURS", "ScheduleError", "TimeWindow", "WeeklySchedule"]

DEFAULT_MONTHLY_HOURS: Final = 730
"""The AWS convention: 365 x 24 / 12. Configurable, and printed in every report."""

MAX_MONTHLY_HOURS: Final = 744
"""Hours in the longest possible month (31 x 24). A larger figure is a typo."""

HOURS_PER_WEEK: Final = Decimal(168)
HOURS_PER_DAY: Final = 24
MINUTES_PER_HOUR: Final = 60

_DAYS: Final = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_SEGMENT = re.compile(
    r"^\s*(?P<days>[A-Za-z]{3}(?:\s*-\s*[A-Za-z]{3})?(?:\s*,\s*[A-Za-z]{3})*)\s+"
    r"(?P<start>\d{1,2}:\d{2})\s*-\s*(?P<end>\d{1,2}:\d{2})\s*$"
)
MAX_SEGMENTS: Final = 14
"""Cap on schedule complexity. Schedules come from configuration a pull request can
edit, so the parser is bounded rather than open-ended."""


class ScheduleError(ValueError):
    """Raised when a schedule expression cannot be parsed."""


class TimeWindow(BaseModel):
    """A recurring window on a set of weekdays."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    days: tuple[str, ...]
    start_minute: int
    end_minute: int

    @property
    def hours_per_week(self) -> Decimal:
        """Hours covered by this window across a week."""
        minutes = (self.end_minute - self.start_minute) * len(self.days)
        return Decimal(minutes) / Decimal(60)


class WeeklySchedule(BaseModel):
    """A weekly availability schedule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expression: str
    windows: tuple[TimeWindow, ...]

    @classmethod
    def parse(cls, expression: str) -> Self:
        """Parse an expression such as ``Mon-Fri 08:00-20:00; Sat 09:00-13:00``.

        Raises:
            ScheduleError: if the expression is malformed, empty, spans midnight, or
                exceeds the complexity cap.
        """
        segments = [segment for segment in expression.split(";") if segment.strip()]
        if not segments:
            raise ScheduleError(f"schedule is empty: {expression!r}")
        if len(segments) > MAX_SEGMENTS:
            raise ScheduleError(
                f"schedule has {len(segments)} segments; the maximum is {MAX_SEGMENTS}"
            )

        windows: list[TimeWindow] = []
        for segment in segments:
            match = _SEGMENT.match(segment)
            if match is None:
                raise ScheduleError(
                    f"cannot parse schedule segment {segment.strip()!r}; expected a form "
                    'such as "Mon-Fri 08:00-20:00"'
                )
            days = _parse_days(match.group("days"))
            start = _parse_time(match.group("start"))
            end = _parse_time(match.group("end"))
            if end <= start:
                raise ScheduleError(
                    f"schedule segment {segment.strip()!r} ends at or before it starts; "
                    "windows spanning midnight must be written as two segments"
                )
            windows.append(TimeWindow(days=days, start_minute=start, end_minute=end))
        return cls(expression=expression.strip(), windows=tuple(windows))

    @property
    def hours_per_week(self) -> Decimal:
        """Total scheduled hours in a week.

        Overlapping windows are summed rather than merged. Overlap in a schedule is
        almost always a configuration mistake, and summing makes it visible as an
        implausible number instead of hiding it behind a union.
        """
        total = Decimal(0)
        for window in self.windows:
            total += window.hours_per_week
        return total

    def monthly_hours(self, hours_per_month: int = DEFAULT_MONTHLY_HOURS) -> int:
        """Convert to whole monthly hours using a fixed weeks-per-month factor."""
        weeks_per_month = Decimal(hours_per_month) / HOURS_PER_WEEK
        hours = self.hours_per_week * weeks_per_month
        return int(hours.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    def __str__(self) -> str:
        """Render the original expression."""
        return self.expression


def _parse_days(spec: str) -> tuple[str, ...]:
    """Expand ``Mon-Fri`` or ``Mon,Wed`` into a tuple of day names."""
    spec = spec.upper().replace(" ", "")
    if "-" in spec:
        start_name, _, end_name = spec.partition("-")
        start, end = _day_index(start_name), _day_index(end_name)
        if start > end:
            raise ScheduleError(
                f"day range {spec!r} runs backwards; ranges that wrap the week must be "
                "written as two segments"
            )
        return _DAYS[start : end + 1]
    return tuple(dict.fromkeys(_DAYS[_day_index(name)] for name in spec.split(",")))


def _day_index(name: str) -> int:
    try:
        return _DAYS.index(name.upper())
    except ValueError:
        raise ScheduleError(f"unknown day {name!r}; expected one of {', '.join(_DAYS)}") from None


def _parse_time(value: str) -> int:
    hours_text, _, minutes_text = value.partition(":")
    hours, minutes = int(hours_text), int(minutes_text)
    if (
        not 0 <= hours <= HOURS_PER_DAY
        or not 0 <= minutes < MINUTES_PER_HOUR
        or (hours == HOURS_PER_DAY and minutes > 0)
    ):
        raise ScheduleError(f"invalid time {value!r}")
    return hours * MINUTES_PER_HOUR + minutes
