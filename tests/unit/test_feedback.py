"""Comparing predictions against bills.

Most of these tests are about refusing to report a number. The arithmetic is easy; the
discipline is in knowing when subtracting one figure from another means nothing, and
saying so instead of producing a confident percentage.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from cost_gate.domain.money import Money
from cost_gate.feedback import (
    Comparability,
    FixtureObservationProvider,
    Observation,
    ObservationError,
    PredictionRecord,
    ServiceObservation,
    ServicePrediction,
    compare,
    observations_for,
    settled_window,
    summarise,
)
from cost_gate.feedback.accuracy import MINIMUM_COMPARISONS
from cost_gate.feedback.providers import tags_were_active

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "examples" / "feedback"
NOW = datetime(2026, 3, 2, tzinfo=UTC)


def usd(amount: str) -> Money:
    return Money(amount=Decimal(amount), currency="USD")


def prediction(
    fingerprint: str = "a" * 32,
    predicted: str = "100.00",
    *,
    deployed: datetime | None = datetime(2026, 1, 6, tzinfo=UTC),
    services: tuple[ServicePrediction, ...] = (),
    tags_activated_on: date | None = None,
) -> PredictionRecord:
    return PredictionRecord(
        fingerprint=fingerprint,
        recorded_at=datetime(2026, 1, 6, tzinfo=UTC),
        application="payments",
        predicted_monthly_delta=usd(predicted),
        services=services,
        deployed_at=deployed,
        tags_activated_on=tags_activated_on,
    )


def observation(
    fingerprint: str = "a" * 32,
    observed: str = "100.00",
    *,
    comparability: Comparability = Comparability.COMPARABLE,
    detail: str = "",
    services: tuple[ServiceObservation, ...] = (),
) -> Observation:
    return Observation(
        fingerprint=fingerprint,
        observed_at=NOW,
        window_start=date(2026, 2, 1),
        window_end=date(2026, 3, 1),
        observed_monthly_amount=usd(observed),
        services=services,
        comparability=comparability,
        detail=detail,
    )


class TestARecordMustAddUp:
    def test_per_service_predictions_must_sum_to_the_total(self):
        # The breakdown is what makes an accuracy figure actionable, and one that does
        # not sum to the total is one nobody can reason from.
        with pytest.raises(ValidationError, match="sum to"):
            prediction(
                predicted="100.00",
                services=(
                    ServicePrediction(service="AmazonEC2", monthly_delta=usd("60.00")),
                    ServicePrediction(service="AmazonRDS", monthly_delta=usd("30.00")),
                ),
            )

    def test_a_matching_breakdown_is_accepted(self):
        record = prediction(
            predicted="90.00",
            services=(
                ServicePrediction(service="AmazonEC2", monthly_delta=usd("60.00")),
                ServicePrediction(service="AmazonRDS", monthly_delta=usd("30.00")),
            ),
        )
        assert len(record.services) == 2

    def test_no_breakdown_is_allowed(self):
        assert prediction().services == ()


class TestAnExclusionMustBeExplained:
    def test_an_excluded_observation_without_a_reason_is_rejected(self):
        # An exclusion nobody can explain looks like the tool hiding a result it did
        # not like.
        with pytest.raises(ValidationError, match="explain why"):
            observation(comparability=Comparability.UNATTRIBUTED)

    def test_an_explained_exclusion_is_accepted(self):
        excluded = observation(
            comparability=Comparability.UNATTRIBUTED, detail="no cost carried the tag"
        )
        assert excluded.detail

    def test_a_comparable_observation_needs_no_detail(self):
        assert observation().detail == ""

    def test_a_backwards_window_is_rejected(self):
        with pytest.raises(ValidationError, match="ends before it starts"):
            Observation(
                fingerprint="a" * 32,
                observed_at=NOW,
                window_start=date(2026, 3, 1),
                window_end=date(2026, 2, 1),
                observed_monthly_amount=usd("1.00"),
            )


class TestWhenAWindowCanBeCompared:
    def test_a_change_never_deployed_is_not_comparable(self):
        # Counting it as a perfect prediction would be absurd.
        assert settled_window(NOW, None) is Comparability.NOT_DEPLOYED

    def test_a_deployment_from_this_morning_has_no_bill_yet(self):
        assert settled_window(NOW, NOW - timedelta(hours=2)) is Comparability.BILLING_INCOMPLETE

    def test_a_deployment_in_the_current_month_gives_a_partial_month(self):
        # A monthly estimate describes a steady month; ten days of billing scaled up
        # assumes a steadiness a just-deployed system rarely has.
        deployed = datetime(2026, 3, 1, tzinfo=UTC) - timedelta(days=0, hours=48)
        reference = datetime(2026, 3, 20, tzinfo=UTC)
        assert settled_window(reference, datetime(2026, 3, 2, tzinfo=UTC)) in (
            Comparability.PARTIAL_MONTH,
            Comparability.BILLING_INCOMPLETE,
        )
        assert deployed < reference

    def test_a_settled_previous_month_is_comparable(self):
        assert settled_window(NOW, datetime(2026, 1, 6, tzinfo=UTC)) is Comparability.COMPARABLE


class TestTagActivation:
    def test_tags_activated_before_deployment_are_fine(self):
        assert tags_were_active(prediction(tags_activated_on=date(2025, 12, 1)))

    def test_tags_activated_after_deployment_hide_history(self):
        # Tags apply from activation forward and are never backfilled, so the observed
        # figure is genuinely lower than the truth.
        assert not tags_were_active(prediction(tags_activated_on=date(2026, 2, 15)))

    def test_no_recorded_activation_is_assumed_fine(self):
        assert tags_were_active(prediction())


class TestComparingOnePair:
    def test_the_error_is_observed_minus_predicted(self):
        result = compare(prediction(predicted="100.00"), observation(observed="120.00"))
        assert result.error == usd("20.00")

    def test_a_positive_error_means_the_tool_underestimated(self):
        # The direction that costs somebody money, so it is the direction the sign
        # points at.
        result = compare(prediction(predicted="100.00"), observation(observed="120.00"))
        assert result.error_percent == Decimal("20.0")

    def test_a_negative_error_means_the_tool_overestimated(self):
        result = compare(prediction(predicted="100.00"), observation(observed="80.00"))
        assert result.error_percent == Decimal("-20.0")

    def test_a_zero_prediction_has_no_percentage_error(self):
        # 0% would say the tool was right; infinity would poison every aggregate it
        # entered. Neither is a percentage.
        result = compare(prediction(predicted="0.00"), observation(observed="15.00"))
        assert result.error_percent is None
        assert result.error == usd("15.00")

    def test_mismatched_fingerprints_are_a_programming_error(self):
        with pytest.raises(ValueError, match="paired with observation"):
            compare(prediction("a" * 32), observation("b" * 32))

    def test_an_excluded_pair_still_produces_a_comparison(self):
        # Dropping it silently would leave a reader unable to tell a tool with few
        # observations from one with many bad ones.
        result = compare(
            prediction(deployed=None),
            observation(comparability=Comparability.NOT_DEPLOYED, detail="never merged"),
        )
        assert not result.counted
        assert result.detail == "never merged"


class TestTheReportRefusesToOverclaim:
    def pairs(self, count: int) -> list:
        return [
            (
                prediction(f"{index:032d}", predicted="100.00"),
                observation(f"{index:032d}", observed=f"{100 + index}.00"),
            )
            for index in range(count)
        ]

    def test_no_distribution_below_the_threshold(self):
        # Three comparisons produce a median, a p10 and a p90 that are all the same one
        # or two numbers. Presenting that as a distribution would be false precision.
        report = summarise(self.pairs(MINIMUM_COMPARISONS - 1))
        assert report.median_error_percent is None
        assert not report.has_distribution

    def test_the_headline_says_why_there_is_no_number(self):
        report = summarise(self.pairs(2))
        assert "too few" in report.headline

    def test_a_distribution_appears_once_there_is_enough_data(self):
        report = summarise(self.pairs(MINIMUM_COMPARISONS))
        assert report.has_distribution
        assert report.median_error_percent is not None
        assert report.p10_error_percent is not None
        assert report.p90_error_percent is not None

    def test_the_headline_never_claims_an_accuracy_percentage(self):
        # "94% accurate" invites a reader to apply that confidence to the next estimate
        # they see, which is exactly what the number does not support.
        headline = summarise(self.pairs(MINIMUM_COMPARISONS)).headline
        assert "accurate" not in headline.lower()

    def test_quantiles_are_values_that_actually_happened(self):
        # Nearest-rank, not interpolated: with a handful of comparisons, interpolating
        # invents a value that nothing measured.
        report = summarise(self.pairs(MINIMUM_COMPARISONS))
        measured = {
            comparison.error_percent
            for comparison in report.comparisons
            if comparison.error_percent is not None
        }
        assert report.median_error_percent in measured
        assert report.p10_error_percent in measured
        assert report.p90_error_percent in measured


class TestExclusionsAreCounted:
    def test_every_exclusion_reason_is_reported(self):
        # An accuracy figure over a filtered population is only meaningful alongside
        # the filter.
        pairs = [
            (prediction("a" * 32), observation("a" * 32)),
            (
                prediction("b" * 32, deployed=None),
                observation("b" * 32, comparability=Comparability.NOT_DEPLOYED, detail="x"),
            ),
            (
                prediction("c" * 32),
                observation("c" * 32, comparability=Comparability.UNATTRIBUTED, detail="y"),
            ),
        ]
        report = summarise(pairs)
        assert report.counted == 1
        assert report.excluded == {"not_deployed": 1, "unattributed": 1}

    def test_excluded_pairs_do_not_enter_the_distribution(self):
        pairs = [
            (prediction(f"{i:032d}"), observation(f"{i:032d}", observed="100.00"))
            for i in range(MINIMUM_COMPARISONS)
        ]
        pairs.append(
            (
                prediction("z" * 32, predicted="1.00"),
                observation(
                    "z" * 32,
                    observed="99999.00",
                    comparability=Comparability.UNATTRIBUTED,
                    detail="tagging gap",
                ),
            )
        )
        report = summarise(pairs)
        # The wild excluded pair must not drag the distribution anywhere.
        assert report.median_error_percent == Decimal("0.0")


class TestPerServiceAccuracy:
    def build(self, predicted: str, observed: str, service: str = "AmazonS3"):
        return (
            prediction(
                predicted=predicted,
                services=(ServicePrediction(service=service, monthly_delta=usd(predicted)),),
            ),
            observation(
                observed=observed,
                services=(ServiceObservation(service=service, monthly_amount=usd(observed)),),
            ),
        )

    def test_a_service_that_is_underestimated_is_named(self):
        # "The tool underestimates S3" leads somewhere; "the tool is 12% out" does not.
        report = summarise([self.build("10.00", "25.00")])
        assert report.services[0].service == "AmazonS3"
        assert report.services[0].bias == "underestimates"

    def test_a_service_that_is_overestimated_is_named(self):
        report = summarise([self.build("100.00", "50.00")])
        assert report.services[0].bias == "overestimates"

    def test_a_close_estimate_shows_no_bias(self):
        report = summarise([self.build("100.00", "102.00")])
        assert report.services[0].bias == "no clear bias"

    def test_a_service_billed_but_never_predicted_is_not_scored(self):
        # That is an attribution finding rather than an estimator being wrong, so it
        # does not enter the per-service distribution.
        record, observed = self.build("10.00", "10.00")
        observed = observed.model_copy(
            update={
                "services": (
                    *observed.services,
                    ServiceObservation(service="AmazonCloudWatch", monthly_amount=usd("3.00")),
                )
            }
        )
        report = summarise([(record, observed)])
        assert {service.service for service in report.services} == {"AmazonS3"}

    def test_an_unattributed_service_figure_is_not_counted(self):
        record, observed = self.build("10.00", "10.00")
        observed = observed.model_copy(
            update={
                "services": (
                    ServiceObservation(
                        service="AmazonS3", monthly_amount=usd("10.00"), attributed=False
                    ),
                )
            }
        )
        assert summarise([(record, observed)]).services == ()


class TestProvenanceIsCarried:
    def test_fixture_observations_are_never_authoritative(self):
        # The same discipline as pricing provenance: illustrative data says so wherever
        # it is shown.
        report = summarise([(prediction(), observation())])
        assert report.authoritative is False

    def test_an_empty_report_is_not_authoritative_either(self):
        assert summarise([]).authoritative is False


class TestTheFixtureProvider:
    def provider(self) -> FixtureObservationProvider:
        return FixtureObservationProvider(FIXTURES / "observations.yaml")

    def test_it_loads_the_bundled_observations(self):
        assert len(self.provider()) == 8

    def test_a_missing_file_is_an_error(self, tmp_path):
        with pytest.raises(ObservationError, match="no observation fixture"):
            FixtureObservationProvider(tmp_path / "absent.yaml")

    def test_malformed_yaml_is_an_error(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("observations: [1, 2, 3]", encoding="utf-8")
        with pytest.raises(ObservationError):
            FixtureObservationProvider(path)

    def test_duplicate_fingerprints_are_refused(self, tmp_path):
        path = tmp_path / "dupe.yaml"
        entry = (
            "  - fingerprint: {f}\n"
            "    observed_at: 2026-03-02T06:00:00Z\n"
            "    window_start: 2026-02-01\n"
            "    window_end: 2026-03-01\n"
            "    observed_monthly_amount: {{amount: '1.00', currency: USD}}\n"
        ).format(f="a" * 32)
        path.write_text(f"version: 1\nobservations:\n{entry}{entry}", encoding="utf-8")
        with pytest.raises(ObservationError, match="two observations"):
            FixtureObservationProvider(path)

    def test_a_prediction_with_no_observation_yields_nothing(self):
        # Nothing was measured, so there is nothing to report about it - which is
        # different from an exclusion, where data exists and cannot be used.
        assert self.provider().observe(prediction("0" * 32)) is None

    def test_pairing_drops_records_with_no_data(self):
        pairs = observations_for(self.provider(), [prediction("0" * 32)])
        assert pairs == []
