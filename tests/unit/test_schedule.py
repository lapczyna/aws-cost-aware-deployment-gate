"""Runtime schedules must be deterministic and refuse ambiguity."""

from __future__ import annotations

from decimal import Decimal

import pytest

from cost_gate.domain.schedule import (
    DEFAULT_MONTHLY_HOURS,
    MAX_SEGMENTS,
    ScheduleError,
    WeeklySchedule,
)

pytestmark = pytest.mark.unit


class TestParsing:
    def test_a_working_week(self):
        schedule = WeeklySchedule.parse("Mon-Fri 08:00-20:00")
        assert schedule.hours_per_week == Decimal(60)  # 5 days x 12 hours

    def test_multiple_segments_are_separated_by_semicolons(self):
        schedule = WeeklySchedule.parse("Mon-Fri 08:00-20:00; Sat 09:00-13:00")
        assert schedule.hours_per_week == Decimal(64)

    def test_a_day_list_is_accepted(self):
        assert WeeklySchedule.parse("Mon,Wed,Fri 09:00-17:00").hours_per_week == Decimal(24)

    def test_case_and_whitespace_are_tolerated(self):
        assert WeeklySchedule.parse("  mon - fri  08:00 - 20:00 ").hours_per_week == Decimal(60)

    def test_duplicate_days_in_a_list_are_collapsed(self):
        assert WeeklySchedule.parse("Mon,Mon 09:00-17:00").hours_per_week == Decimal(8)


class TestConversionIsDeterministic:
    def test_monthly_hours_use_a_fixed_weeks_per_month_factor(self):
        # 60 h/week x (730 / 168) = 260.7, rounded half up.
        assert WeeklySchedule.parse("Mon-Fri 08:00-20:00").monthly_hours() == 261

    def test_the_result_does_not_depend_on_the_calendar(self):
        # Nothing in the conversion reads a clock, so the same expression always gives
        # the same number. A report must not change because it ran in a month with
        # five Mondays.
        schedule = WeeklySchedule.parse("Mon-Fri 08:00-20:00")
        assert {schedule.monthly_hours() for _ in range(50)} == {261}

    def test_continuous_operation_reproduces_the_monthly_convention(self):
        continuous = WeeklySchedule.parse("Mon-Sun 00:00-24:00")
        assert continuous.hours_per_week == Decimal(168)
        assert continuous.monthly_hours() == DEFAULT_MONTHLY_HOURS

    def test_the_convention_is_configurable(self):
        continuous = WeeklySchedule.parse("Mon-Sun 00:00-24:00")
        assert continuous.monthly_hours(720) == 720

    def test_a_development_schedule_is_far_below_continuous(self):
        # The point of the feature: assuming 730 h for a working-hours environment
        # overstates it roughly threefold.
        assert WeeklySchedule.parse("Mon-Fri 08:00-20:00").monthly_hours() < 300


class TestRejectsAmbiguity:
    @pytest.mark.parametrize(
        "expression",
        [
            "",
            "   ",
            "Mon-Fri",
            "08:00-20:00",
            "Mon-Fri 08:00",
            "Funday 08:00-20:00",
            "Mon-Fri 25:00-26:00",
            "Mon-Fri 08:70-09:00",
        ],
    )
    def test_malformed_expressions_are_rejected(self, expression):
        with pytest.raises(ScheduleError):
            WeeklySchedule.parse(expression)

    def test_a_window_ending_before_it_starts_is_rejected(self):
        with pytest.raises(ScheduleError, match="spanning midnight"):
            WeeklySchedule.parse("Mon-Fri 20:00-08:00")

    def test_a_zero_length_window_is_rejected(self):
        with pytest.raises(ScheduleError, match="ends at or before"):
            WeeklySchedule.parse("Mon 09:00-09:00")

    def test_a_backwards_day_range_is_rejected(self):
        with pytest.raises(ScheduleError, match="runs backwards"):
            WeeklySchedule.parse("Fri-Mon 08:00-20:00")

    def test_complexity_is_bounded(self):
        # Schedules come from configuration a pull request can edit.
        expression = "; ".join(["Mon 09:00-10:00"] * (MAX_SEGMENTS + 1))
        with pytest.raises(ScheduleError, match="maximum"):
            WeeklySchedule.parse(expression)

    def test_an_unknown_day_names_the_valid_ones(self):
        with pytest.raises(ScheduleError, match="MON, TUE"):
            WeeklySchedule.parse("Xyz 08:00-20:00")


class TestOverlapIsVisibleNotHidden:
    def test_overlapping_windows_are_summed(self):
        # Overlap is almost always a configuration mistake. Summing surfaces it as an
        # implausible number instead of hiding it behind a silent union.
        schedule = WeeklySchedule.parse("Mon 08:00-20:00; Mon 09:00-17:00")
        assert schedule.hours_per_week == Decimal(20)
