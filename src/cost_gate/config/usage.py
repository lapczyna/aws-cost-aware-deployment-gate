"""Usage profiles: the assumptions that turn a template into a cost.

A template says a Lambda function exists. It does not say how many times it will be
invoked, and no amount of parsing will reveal that. Usage profiles are where a team
records what it expects, in version control, so the assumption is reviewable rather
than buried in an estimator.

Two things make this module more than a settings bag.

**A closed driver vocabulary.** Driver names are model fields, not free-form keys, so
``invocations_per_month`` misspelled as ``invocation_per_month`` is rejected at load
time. A silently ignored driver would mean an estimate quietly falling back to a
default while the user believed they had configured it.

**Explicit precedence with provenance.** :meth:`UsageProfileConfig.resolve` returns not
only a value but where it came from, so the report can say "220 h/month, from usage
profile `development`" instead of presenting an assumption as a fact.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cost_gate.domain.enums import ValueProvenance
from cost_gate.domain.schedule import (
    DEFAULT_MONTHLY_HOURS,
    MAX_MONTHLY_HOURS,
    ScheduleError,
    WeeklySchedule,
)

__all__ = [
    "DRIVER_NAMES",
    "EnvironmentUsage",
    "Quantity",
    "ResolvedDriver",
    "UsageDrivers",
    "UsageProfileConfig",
]


class Quantity(BaseModel):
    """A usage figure, optionally expressed as a range.

    Accepts either a scalar (``200000``) or a mapping
    (``{min: 5000, expected: 20000, max: 100000}``). A range is what lets a report show
    a span instead of inventing a precision the user never claimed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    expected: Decimal
    minimum: Decimal | None = Field(default=None, alias="min")
    maximum: Decimal | None = Field(default=None, alias="max")

    @model_validator(mode="before")
    @classmethod
    def _accept_scalar(cls, value: Any) -> Any:
        if isinstance(value, (int, str, Decimal)) and not isinstance(value, bool):
            return {"expected": value}
        if isinstance(value, float):
            raise ValueError(
                'usage figures must not be floats; write 1_500_000 or "1500000.5" so '
                "the value stays exact"
            )
        return value

    @model_validator(mode="after")
    def _ordered_and_non_negative(self) -> Self:
        if self.expected < 0:
            raise ValueError("usage figures must not be negative")
        if self.minimum is not None and self.minimum > self.expected:
            raise ValueError("min must not exceed expected")
        if self.maximum is not None and self.maximum < self.expected:
            raise ValueError("max must not be below expected")
        return self

    @property
    def has_range(self) -> bool:
        """Whether bounds were supplied alongside the expected value."""
        return self.minimum is not None or self.maximum is not None


class UsageDrivers(BaseModel):
    """The closed set of usage figures an estimator may consume.

    Adding a driver is a code change, deliberately: the same reasoning as the policy
    predicate vocabulary in ADR 0006. A configuration file cannot introduce a driver
    that no estimator reads.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    # Compute
    requests_per_month: Quantity | None = None
    invocations_per_month: Quantity | None = None
    average_duration_ms: Quantity | None = None
    allocated_memory_mb: Quantity | None = None

    # Storage
    storage_gb: Quantity | None = None
    s3_get_requests_per_month: Quantity | None = None
    s3_put_requests_per_month: Quantity | None = None

    # Database
    dynamodb_read_requests_per_month: Quantity | None = None
    dynamodb_write_requests_per_month: Quantity | None = None
    dynamodb_storage_gb: Quantity | None = None

    # Network
    outbound_data_gb: Quantity | None = None
    inter_az_data_gb: Quantity | None = None
    nat_processed_gb: Quantity | None = None

    # Observability
    log_ingestion_gb: Quantity | None = None
    log_retention_days: Quantity | None = None

    def get(self, name: str) -> Quantity | None:
        """Look up a driver by name, returning ``None`` when it is not set."""
        if name not in DRIVER_NAMES:
            raise KeyError(
                f"unknown usage driver {name!r}; known drivers are "
                f"{', '.join(sorted(DRIVER_NAMES))}"
            )
        value = getattr(self, name)
        return value if isinstance(value, Quantity) else None


DRIVER_NAMES: frozenset[str] = frozenset(UsageDrivers.model_fields)
"""Every driver an estimator may ask for. Used to reject typos and to document coverage."""


class EnvironmentUsage(BaseModel):
    """Runtime and usage assumptions for one environment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    monthly_hours: int | None = None
    """Hours per month the resources are expected to run. Mutually exclusive with
    :attr:`schedule`."""

    schedule: str | None = None
    """A weekly schedule such as ``Mon-Fri 08:00-20:00``, converted deterministically."""

    expected_lifetime_hours: int | None = None
    """For ephemeral environments: the total expected lifetime, used instead of a
    monthly figure."""

    drivers: UsageDrivers = Field(default_factory=UsageDrivers)

    @model_validator(mode="before")
    @classmethod
    def _lift_inline_drivers(cls, value: Any) -> Any:
        """Allow drivers to be written inline rather than nested under ``drivers``.

        Both forms are accepted because the nested form is clearer for large profiles
        and the inline form is what people write first. Unknown keys are still
        rejected: an inline key that is not a known driver fails here rather than
        being silently discarded.
        """
        if not isinstance(value, dict):
            return value
        structural = {"monthly_hours", "schedule", "expected_lifetime_hours", "drivers"}
        inline = {key: item for key, item in value.items() if key not in structural}
        if not inline:
            return value
        unknown = sorted(set(inline) - DRIVER_NAMES)
        if unknown:
            raise ValueError(
                f"unknown key(s) {', '.join(unknown)}; known usage drivers are "
                f"{', '.join(sorted(DRIVER_NAMES))}"
            )
        merged = {key: item for key, item in value.items() if key in structural}
        existing = merged.get("drivers") or {}
        if not isinstance(existing, dict):
            raise ValueError("drivers must be a mapping")
        merged["drivers"] = {**existing, **inline}
        return merged

    @model_validator(mode="after")
    def _hours_are_unambiguous(self) -> Self:
        if self.monthly_hours is not None and self.schedule is not None:
            raise ValueError(
                "set either monthly_hours or schedule, not both; two sources for the "
                "same number is how reports come to disagree with themselves"
            )
        if self.monthly_hours is not None and not 0 < self.monthly_hours <= MAX_MONTHLY_HOURS:
            raise ValueError(
                f"monthly_hours must be between 1 and {MAX_MONTHLY_HOURS} "
                f"(received {self.monthly_hours})"
            )
        if self.schedule is not None:
            try:
                WeeklySchedule.parse(self.schedule)
            except ScheduleError as exc:
                raise ValueError(str(exc)) from exc
        return self

    def resolved_monthly_hours(
        self, default: int = DEFAULT_MONTHLY_HOURS
    ) -> tuple[int, ValueProvenance, str]:
        """Return monthly hours, its provenance, and a sentence explaining it."""
        if self.monthly_hours is not None:
            return (
                self.monthly_hours,
                ValueProvenance.CONFIG_ENVIRONMENT,
                "monthly hours set explicitly in the usage profile",
            )
        if self.schedule is not None:
            parsed = WeeklySchedule.parse(self.schedule)
            hours = parsed.monthly_hours(default)
            return (
                hours,
                ValueProvenance.CONFIG_ENVIRONMENT,
                f"{hours} h/month derived from schedule {parsed.expression!r} "
                f"using {default} h/month",
            )
        return (
            default,
            ValueProvenance.BUILTIN_DEFAULT,
            f"no runtime configured; assuming continuous operation at {default} h/month",
        )


class ResolvedDriver(BaseModel):
    """A driver value together with where it came from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    quantity: Quantity
    provenance: ValueProvenance
    detail: str = ""


class UsageProfileConfig(BaseModel):
    """A version-controlled set of usage assumptions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    defaults: EnvironmentUsage = Field(default_factory=EnvironmentUsage)
    environments: dict[str, EnvironmentUsage] = Field(default_factory=dict)
    resource_overrides: dict[str, EnvironmentUsage] = Field(default_factory=dict)
    """Keyed by logical ID. Overrides the environment profile for one resource."""

    def environment(self, name: str | None) -> EnvironmentUsage | None:
        """Return the profile for an environment, if one is defined."""
        if name is None:
            return None
        return self.environments.get(name)

    def resolve(
        self,
        driver: str,
        *,
        environment: str | None = None,
        logical_id: str | None = None,
    ) -> ResolvedDriver | None:
        """Resolve one driver, applying the documented precedence.

        Precedence, most specific first:

        1. ``resource_overrides[logical_id]`` — ``CONFIG_RESOURCE_OVERRIDE``
        2. ``environments[environment]`` — ``CONFIG_ENVIRONMENT``
        3. ``defaults`` — ``CONFIG_ENVIRONMENT``

        Returns ``None`` when no source supplies the driver. The caller then decides
        between a documented built-in default (dropping confidence to ``LOW``) and an
        unknown — a decision that belongs to the estimator, because only it knows
        whether a defensible default exists. Guessing a log volume, for example, is
        exactly the false precision this project exists to avoid.
        """
        if driver not in DRIVER_NAMES:
            raise KeyError(
                f"unknown usage driver {driver!r}; known drivers are "
                f"{', '.join(sorted(DRIVER_NAMES))}"
            )

        candidates: list[tuple[EnvironmentUsage | None, ValueProvenance, str]] = [
            (
                self.resource_overrides.get(logical_id) if logical_id else None,
                ValueProvenance.CONFIG_RESOURCE_OVERRIDE,
                f"resource override for {logical_id}",
            ),
            (
                self.environment(environment),
                ValueProvenance.CONFIG_ENVIRONMENT,
                f"usage profile for environment {environment!r}",
            ),
            (self.defaults, ValueProvenance.CONFIG_ENVIRONMENT, "usage profile defaults"),
        ]
        for source, provenance, detail in candidates:
            if source is None:
                continue
            quantity = source.drivers.get(driver)
            if quantity is not None:
                return ResolvedDriver(
                    name=driver, quantity=quantity, provenance=provenance, detail=detail
                )
        return None

    def monthly_hours(
        self,
        *,
        environment: str | None = None,
        logical_id: str | None = None,
        default: int = DEFAULT_MONTHLY_HOURS,
    ) -> tuple[int, ValueProvenance, str]:
        """Resolve monthly runtime hours with the same precedence as drivers."""
        override = self.resource_overrides.get(logical_id) if logical_id else None
        if override is not None and (
            override.monthly_hours is not None or override.schedule is not None
        ):
            hours, _, detail = override.resolved_monthly_hours(default)
            return hours, ValueProvenance.CONFIG_RESOURCE_OVERRIDE, detail

        profile = self.environment(environment)
        if profile is not None and (
            profile.monthly_hours is not None or profile.schedule is not None
        ):
            return profile.resolved_monthly_hours(default)

        if self.defaults.monthly_hours is not None or self.defaults.schedule is not None:
            return self.defaults.resolved_monthly_hours(default)

        return EnvironmentUsage().resolved_monthly_hours(default)
