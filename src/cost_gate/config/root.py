"""The root configuration file.

``cost-gate.yaml`` ties the pieces together: which region and currency to price in,
which environment and application a change belongs to, and where the usage, budget and
policy files live.

Referenced paths are resolved relative to the configuration file and confined to its
directory, because those paths come from a file a pull request can edit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cost_gate.config.budgets import BudgetsConfig
from cost_gate.config.loader import load_model, resolve_within
from cost_gate.config.policies import PoliciesConfig
from cost_gate.config.usage import UsageProfileConfig
from cost_gate.domain.money import Currency
from cost_gate.domain.resources import ResourceContext
from cost_gate.domain.schedule import DEFAULT_MONTHLY_HOURS, MAX_MONTHLY_HOURS

__all__ = ["LoadedConfig", "PricingConfig", "RootConfig", "load_config"]


class PricingConfig(BaseModel):
    """Which pricing provider to use and where its data lives."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Literal["fixtures", "aws", "chain"] = "fixtures"
    """``fixtures`` is the default and the only one that works offline. ``chain`` falls
    back between providers and must be requested explicitly: an implicit fallback would
    let a failed lookup quietly become a stale price (ADR 0005)."""

    catalog: str = "pricing-data"
    cache_dir: str | None = None
    ttl_hours: int = 24

    @model_validator(mode="after")
    def _positive_ttl(self) -> Self:
        if self.ttl_hours <= 0:
            raise ValueError("ttl_hours must be positive")
        return self


class RootConfig(BaseModel):
    """The contents of ``cost-gate.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    region: str = "us-east-1"
    currency: Currency = Currency.USD
    monthly_hours: int = DEFAULT_MONTHLY_HOURS
    """The hours-per-month convention. Printed in every report so a reader never has to
    guess whether 730, 720 or 744 was used."""

    environment: str | None = None
    application: str | None = None
    team: str | None = None
    cost_centre: str | None = None

    usage_profile: str | None = None
    budgets: str | None = None
    policies: str | None = None

    pricing: PricingConfig = Field(default_factory=PricingConfig)

    @model_validator(mode="after")
    def _sane_hours(self) -> Self:
        if not 0 < self.monthly_hours <= MAX_MONTHLY_HOURS:
            raise ValueError(
                f"monthly_hours must be between 1 and {MAX_MONTHLY_HOURS} "
                f"(received {self.monthly_hours})"
            )
        return self

    def context(self) -> ResourceContext:
        """The default attribution applied to resources that carry no tags of their own."""
        return ResourceContext(
            environment=self.environment,
            application=self.application,
            team=self.team,
            cost_centre=self.cost_centre,
        )


class LoadedConfig(BaseModel):
    """A root configuration together with the documents it references."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root: RootConfig
    source: str
    usage: UsageProfileConfig | None = None
    budgets: BudgetsConfig | None = None
    policies: PoliciesConfig | None = None
    catalog_path: str = ""

    @property
    def monthly_hours(self) -> int:
        """The hours-per-month convention in force."""
        return self.root.monthly_hours


def load_config(path: Path, *, allow_missing_references: bool = False) -> LoadedConfig:
    """Load ``cost-gate.yaml`` and every file it references.

    Args:
        path: the root configuration file.
        allow_missing_references: when true, a referenced file that does not exist is
            skipped instead of raising. Used by ``validate-config`` so that a partially
            written configuration still reports every other problem it has.

    Raises:
        ConfigError: naming the file and the path within it for every problem found.
    """
    root_path = path.resolve()
    root = load_model(RootConfig, root_path)
    base = root_path.parent

    usage: UsageProfileConfig | None = None
    if root.usage_profile is not None:
        usage_path = resolve_within(base, root.usage_profile)
        if usage_path.is_file() or not allow_missing_references:
            usage = load_model(UsageProfileConfig, usage_path)

    budgets: BudgetsConfig | None = None
    if root.budgets is not None:
        budgets_path = resolve_within(base, root.budgets)
        if budgets_path.is_file() or not allow_missing_references:
            budgets = load_model(BudgetsConfig, budgets_path)

    policies: PoliciesConfig | None = None
    if root.policies is not None:
        policies_path = resolve_within(base, root.policies)
        if policies_path.is_file() or not allow_missing_references:
            policies = load_model(PoliciesConfig, policies_path)

    # The pricing catalog is deliberately *not* confined to the configuration
    # directory: the usual arrangement points at a catalog elsewhere in the repository,
    # or at the copy bundled with the installed package. It is read-only data validated
    # against a strict schema by the pricing layer, so a path pointing somewhere
    # unexpected produces a load failure rather than leaking the file's contents.
    # Configuration *includes* are a different matter and stay confined, because they
    # change how the gate behaves.
    catalog_path = ""
    if root.pricing.catalog:
        candidate = Path(root.pricing.catalog)
        catalog_path = str(candidate if candidate.is_absolute() else (base / candidate).resolve())

    return LoadedConfig(
        root=root,
        source=str(root_path),
        usage=usage,
        budgets=budgets,
        policies=policies,
        catalog_path=catalog_path,
    )
