"""Turning prediction-observation pairs into something an estimator author can act on.

Three rules govern what this module will and will not report.

**Signed error, not absolute.** A tool that is 20% high on everything and a tool that is
20% high on half and 20% low on the other half have the same mean absolute error and
completely different problems. The first has a systematic bias worth fixing; the second
is noisy. Only the signed distribution distinguishes them.

**A distribution, not a number.** "94% accurate" invites a reader to apply that
confidence to the next estimate they see, which is precisely what the number does not
support. A median with a p10 to p90 spread says what is actually known.

**Per service.** "The tool underestimates RDS storage" leads somewhere. "The tool is 12%
out" leads nowhere.

And one rule about what is counted at all: a pair that cannot honestly be compared is
excluded and named, never quietly folded in. The list of exclusions is part of the
report, because an accuracy figure computed over a filtered population is only
meaningful alongside the filter.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final

from pydantic import BaseModel, ConfigDict

from cost_gate.domain.money import Money
from cost_gate.feedback.records import Comparability, Observation, PredictionRecord

__all__ = [
    "BIAS_THRESHOLD_PERCENT",
    "MINIMUM_COMPARISONS",
    "AccuracyReport",
    "Comparison",
    "ServiceAccuracy",
    "compare",
    "summarise",
]

MINIMUM_COMPARISONS: Final = 5
"""Below this, no distribution is reported.

Three comparisons produce a median, a p10 and a p90 that are all the same one or two
numbers. Presenting that as a distribution would be the sort of false precision this
project exists to avoid, so the report says how many pairs it had instead.
"""

_HUNDRED: Final = Decimal(100)

BIAS_THRESHOLD_PERCENT: Final = Decimal(5)
"""Below this, an error is called noise rather than a direction.

Five per cent is well inside the spread that shared costs, discounts and partial
months introduce on their own, so calling anything smaller a bias would be reading
signal into billing noise."""


class Comparison(BaseModel):
    """One prediction set against one observation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fingerprint: str
    comparability: Comparability
    detail: str = ""

    predicted: Money
    observed: Money
    error: Money
    """Observed minus predicted. **Positive means the tool underestimated**, which is
    the direction that costs somebody money and therefore the direction worth having
    the sign point at."""

    error_percent: Decimal | None = None
    """``None`` when the prediction was zero. Dividing by it would produce either an
    infinity or a silent zero, and neither is a percentage."""

    @property
    def counted(self) -> bool:
        """Whether this pair contributes to the distribution."""
        return self.comparability.is_comparable


class ServiceAccuracy(BaseModel):
    """How the tool does on one service."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    service: str
    comparisons: int
    median_error_percent: Decimal | None = None
    predicted_total: Money
    observed_total: Money

    @property
    def bias(self) -> str:
        """A word for the direction, or an admission that there is not enough data."""
        if self.median_error_percent is None:
            return "unknown"
        if self.median_error_percent > BIAS_THRESHOLD_PERCENT:
            return "underestimates"
        if self.median_error_percent < -BIAS_THRESHOLD_PERCENT:
            return "overestimates"
        return "no clear bias"


class AccuracyReport(BaseModel):
    """What the feedback loop found, with everything it had to exclude."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    comparisons: tuple[Comparison, ...] = ()
    counted: int = 0
    excluded: dict[str, int] = {}
    """Exclusion reason to count. Reported prominently: an accuracy figure over a
    filtered population means nothing without the filter."""

    median_error_percent: Decimal | None = None
    p10_error_percent: Decimal | None = None
    p90_error_percent: Decimal | None = None

    services: tuple[ServiceAccuracy, ...] = ()
    authoritative: bool = False
    """``False`` when any observation came from fixtures. Illustrative data says so."""

    @property
    def has_distribution(self) -> bool:
        """Whether enough comparable pairs existed to describe a spread."""
        return self.counted >= MINIMUM_COMPARISONS

    @property
    def headline(self) -> str:
        """One sentence that does not overclaim."""
        if not self.has_distribution:
            return (
                f"{self.counted} comparable prediction(s) - too few to describe a "
                f"distribution (at least {MINIMUM_COMPARISONS} are needed)"
            )
        if self.median_error_percent is None:
            # Enough comparable pairs, but every one of them predicted zero, so none
            # has a percentage error. Rarer than it sounds - a change that only removes
            # resources predicts a negative delta, but one that changes nothing
            # chargeable predicts exactly nothing.
            return (
                f"{self.counted} comparisons, none with a percentage error: every "
                "prediction was zero"
            )
        direction = "under" if self.median_error_percent > 0 else "over"
        return (
            f"median error {self.median_error_percent:+}% "
            f"({direction}-estimating) across {self.counted} comparisons"
        )


def _percent(predicted: Decimal, observed: Decimal) -> Decimal | None:
    """Error as a percentage of the prediction, or ``None`` if that is meaningless."""
    if predicted == 0:
        # A prediction of zero has no percentage error. Reporting 0% would say the tool
        # was right, and reporting infinity would poison every aggregate it entered.
        return None
    try:
        return ((observed - predicted) / predicted * _HUNDRED).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, ZeroDivisionError):  # pragma: no cover - guarded above
        return None


def compare(record: PredictionRecord, observation: Observation) -> Comparison:
    """Set one prediction against one observation.

    A comparison is produced even when the pair is not comparable, carrying the reason.
    Excluding it silently would leave a reader unable to tell a tool with few
    observations from one with many bad ones.

    Raises:
        ValueError: if the two describe different changes. That is a programming error
            in whatever paired them, not a data quality problem to be tolerated.
    """
    if record.fingerprint != observation.fingerprint:
        raise ValueError(
            f"prediction {record.fingerprint} paired with observation {observation.fingerprint}"
        )

    predicted = record.predicted_monthly_delta
    observed = observation.observed_monthly_amount
    return Comparison(
        fingerprint=record.fingerprint,
        comparability=observation.comparability,
        detail=observation.detail,
        predicted=predicted,
        observed=observed,
        error=observed - predicted,
        error_percent=_percent(predicted.amount, observed.amount),
    )


def _quantile(values: list[Decimal], fraction: Decimal) -> Decimal | None:
    """Nearest-rank quantile.

    Nearest-rank rather than interpolated: with a handful of comparisons, interpolating
    between two observations invents a value that nothing measured. Every number this
    module reports should be one that actually happened.
    """
    if not values:
        return None
    ordered = sorted(values)
    index = int((fraction * (len(ordered) - 1)).to_integral_value(rounding=ROUND_HALF_UP))
    return ordered[max(0, min(index, len(ordered) - 1))]


def _service_accuracy(
    pairs: list[tuple[PredictionRecord, Observation]],
) -> tuple[ServiceAccuracy, ...]:
    """Accuracy per service, over comparable pairs only."""
    predicted: dict[str, list[Money]] = defaultdict(list)
    observed: dict[str, list[Money]] = defaultdict(list)
    errors: dict[str, list[Decimal]] = defaultdict(list)

    for record, observation in pairs:
        if not observation.comparability.is_comparable:
            continue
        billed = {
            item.service: item.monthly_amount for item in observation.services if item.attributed
        }
        for prediction in record.services:
            actual = billed.get(prediction.service)
            if actual is None:
                # Predicted but never billed under this service. That is a finding, but
                # it is an attribution finding rather than an estimator error, so it
                # does not enter the distribution.
                continue
            predicted[prediction.service].append(prediction.monthly_delta)
            observed[prediction.service].append(actual)
            percent = _percent(prediction.monthly_delta.amount, actual.amount)
            if percent is not None:
                errors[prediction.service].append(percent)

    results = []
    for service in sorted(predicted):
        results.append(
            ServiceAccuracy(
                service=service,
                comparisons=len(predicted[service]),
                median_error_percent=_quantile(errors[service], Decimal("0.5")),
                predicted_total=sum(predicted[service][1:], start=predicted[service][0]),
                observed_total=sum(observed[service][1:], start=observed[service][0]),
            )
        )
    return tuple(results)


def summarise(pairs: list[tuple[PredictionRecord, Observation]]) -> AccuracyReport:
    """Build the accuracy report for a set of prediction-observation pairs."""
    comparisons = tuple(compare(record, observation) for record, observation in pairs)

    excluded: dict[str, int] = defaultdict(int)
    for comparison in comparisons:
        if not comparison.counted:
            excluded[comparison.comparability.value] += 1

    percentages = [
        comparison.error_percent
        for comparison in comparisons
        if comparison.counted and comparison.error_percent is not None
    ]
    counted = sum(1 for comparison in comparisons if comparison.counted)
    enough = counted >= MINIMUM_COMPARISONS

    return AccuracyReport(
        comparisons=comparisons,
        counted=counted,
        excluded=dict(sorted(excluded.items())),
        # Withheld below the threshold rather than computed and caveated. A number on
        # the page gets quoted; a sentence explaining why there is no number does not.
        median_error_percent=_quantile(percentages, Decimal("0.5")) if enough else None,
        p10_error_percent=_quantile(percentages, Decimal("0.1")) if enough else None,
        p90_error_percent=_quantile(percentages, Decimal("0.9")) if enough else None,
        services=_service_accuracy(pairs),
        authoritative=bool(pairs) and all(o.authoritative for _, o in pairs),
    )
