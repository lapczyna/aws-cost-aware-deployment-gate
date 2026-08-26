"""What was predicted, what was observed, and whether the two can fairly be compared.

The third of those is the part that matters, and it is the reason this module is bigger
than it looks like it should be.

An estimate produced from Infrastructure as Code and a figure from a bill are not the
same kind of thing, and subtracting one from the other is only meaningful when a list of
conditions holds. When they do not hold, the honest answer is to exclude the comparison
and say why — exactly as an unknown cost is excluded from a total and named. A
"98% accurate" headline computed over comparisons that were never valid is worse than no
figure at all, because somebody will act on it.

:class:`Comparability` enumerates the reasons a pair cannot be compared. Every one of
them is a real property of AWS billing rather than a defensive shrug:

* **billing lag** — cost data is up to 24 hours behind, and a month is not final until
  several days after it ends;
* **tag activation** — cost allocation tags only apply from the moment they are
  activated, so a resource tagged today has untagged history;
* **partial month** — a deployment mid-month produces a bill for part of a month, which
  is not what a monthly estimate describes;
* **drift** — resources changed after deployment, so the thing billed is no longer the
  thing predicted;
* **unattributed** — shared costs, support, taxes and credits do not carry the tags the
  attribution relies on.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cost_gate.domain.money import Currency, Money

__all__ = [
    "PREDICTION_SCHEMA_VERSION",
    "Comparability",
    "Observation",
    "PredictionRecord",
    "ServiceObservation",
    "ServicePrediction",
]

PREDICTION_SCHEMA_VERSION = "1"


class Comparability(StrEnum):
    """Whether a prediction and an observation may honestly be compared."""

    COMPARABLE = "comparable"
    """Every condition holds. The difference means something."""

    BILLING_INCOMPLETE = "billing_incomplete"
    """The window has not settled. Cost data lags by up to 24 hours, and a month is not
    final for several days after it ends."""

    PARTIAL_MONTH = "partial_month"
    """The change was deployed mid-window, so the observation covers part of a month
    while the prediction describes a whole one."""

    TAGS_NOT_ACTIVE = "tags_not_active"
    """Cost allocation tags were activated after the deployment, so the resources have
    untagged history and the observed figure is missing some of their cost."""

    RESOURCES_DRIFTED = "resources_drifted"
    """What is running is no longer what was predicted — a later change, a manual edit,
    or an autoscaling group that did its job."""

    UNATTRIBUTED = "unattributed"
    """No cost could be attributed to these resources at all. Usually a tagging gap
    rather than a zero bill, and treating it as zero would flatter the tool."""

    NOT_DEPLOYED = "not_deployed"
    """The change was analysed but never merged or deployed. There is nothing to
    compare, and counting it as a perfect prediction would be absurd."""

    @property
    def is_comparable(self) -> bool:
        """Whether this pair contributes to accuracy figures."""
        return self is Comparability.COMPARABLE


class ServicePrediction(BaseModel):
    """What was predicted for one AWS service.

    Broken down per service because that is the granularity at which a bias is
    actionable: "the tool underestimates RDS" leads somewhere, "the tool is 12% out"
    does not.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    service: str
    monthly_delta: Money
    unknown_component_count: int = 0
    """How many costs the tool could not establish for this service. A service whose
    prediction was mostly unknown is not a service whose estimate can be scored."""


class ServiceObservation(BaseModel):
    """What was actually billed for one AWS service, as far as anyone can tell."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    service: str
    monthly_amount: Money
    attributed: bool = True
    """Whether this figure could be attributed to the change's resources. An
    unattributed service is reported, not silently dropped."""


class PredictionRecord(BaseModel):
    """A prediction, recorded at the moment a change was approved or merged.

    Identified by the same fingerprint the approval mechanism uses, so a prediction and
    the approval that authorised it describe provably the same change.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = PREDICTION_SCHEMA_VERSION
    fingerprint: str
    """From :func:`cost_gate.approvals.decision_fingerprint`."""

    recorded_at: datetime
    environment: str | None = None
    application: str | None = None
    region: str = "us-east-1"
    currency: Currency = Currency.USD

    predicted_monthly_delta: Money
    unknown_component_count: int = 0
    services: tuple[ServicePrediction, ...] = ()

    deployed_at: datetime | None = None
    """When the change reached the account. ``None`` means it never did, which makes
    the prediction uncomparable rather than perfect."""

    tags_activated_on: date | None = None
    """When cost allocation tags were activated. Anything billed before this date is
    invisible to attribution."""

    @model_validator(mode="after")
    def _service_deltas_reconcile(self) -> Self:
        """The per-service breakdown must add up to the total.

        Not a formality: the breakdown is what makes an accuracy figure actionable, and
        a breakdown that does not sum to the total is one nobody can reason from.
        """
        if not self.services:
            return self
        total = sum(
            (prediction.monthly_delta.amount for prediction in self.services), start=Decimal(0)
        )
        if total != self.predicted_monthly_delta.amount:
            raise ValueError(
                f"per-service predictions sum to {total}, but the recorded total is "
                f"{self.predicted_monthly_delta.amount}"
            )
        return self


class Observation(BaseModel):
    """What the billing data says, and whether it can be trusted for this comparison."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fingerprint: str
    observed_at: datetime
    window_start: date
    window_end: date

    observed_monthly_amount: Money
    services: tuple[ServiceObservation, ...] = ()

    comparability: Comparability = Comparability.COMPARABLE
    detail: str = ""
    """Why, when the pair is not comparable. Required, because an exclusion nobody can
    explain looks like the tool hiding a result it did not like."""

    source: str = "fixture"
    """Which provider produced this. Recorded so a report can never present a
    demonstration figure as though it came from a bill."""

    authoritative: bool = False
    """``False`` for the bundled fixtures. The same discipline as pricing provenance:
    illustrative data must say so wherever it is shown."""

    @model_validator(mode="after")
    def _exclusions_are_explained(self) -> Self:
        """An excluded comparison must say why."""
        if not self.comparability.is_comparable and not self.detail:
            raise ValueError(
                f"an observation excluded as {self.comparability.value} must explain why"
            )
        if self.window_end < self.window_start:
            raise ValueError("the observation window ends before it starts")
        return self


class PredictionStore(BaseModel):
    """A file of prediction records.

    A file rather than a database, because the point of Phase 17 is the arithmetic and
    the honesty around it, not the persistence. ``infrastructure/`` models the DynamoDB
    table this would become.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = 1
    predictions: tuple[PredictionRecord, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _fingerprints_are_unique(self) -> Self:
        """One record per change.

        Two records for one fingerprint would double-count that change in every
        accuracy figure derived from the store.
        """
        seen = [record.fingerprint for record in self.predictions]
        duplicates = sorted({name for name in seen if seen.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate prediction fingerprints: {', '.join(duplicates)}")
        return self
