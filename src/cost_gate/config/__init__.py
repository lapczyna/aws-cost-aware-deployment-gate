"""Loading and validation of user configuration (usage, budgets, policies).

Configuration is treated as semi-trusted input: it lives in the repository, but a pull
request can change it. Every model uses ``extra="forbid"`` so that a misspelled key is
rejected at load time rather than silently ignored — a setting the user believes is in
force but which nothing reads is worse than no setting at all.

Budget and policy configuration arrives in Phase 9, alongside the engine that evaluates
it.
"""

from __future__ import annotations

from cost_gate.config.errors import ConfigError, ConfigIssue
from cost_gate.config.loader import load_model, load_yaml_file, resolve_within
from cost_gate.config.root import LoadedConfig, PricingConfig, RootConfig, load_config
from cost_gate.config.schema import SCHEMA_VERSION, exported_schemas, write_schemas
from cost_gate.config.usage import (
    DRIVER_NAMES,
    EnvironmentUsage,
    Quantity,
    ResolvedDriver,
    UsageDrivers,
    UsageProfileConfig,
)

__all__ = [
    "DRIVER_NAMES",
    "SCHEMA_VERSION",
    "ConfigError",
    "ConfigIssue",
    "EnvironmentUsage",
    "LoadedConfig",
    "PricingConfig",
    "Quantity",
    "ResolvedDriver",
    "RootConfig",
    "UsageDrivers",
    "UsageProfileConfig",
    "exported_schemas",
    "load_config",
    "load_model",
    "load_yaml_file",
    "resolve_within",
    "write_schemas",
]
