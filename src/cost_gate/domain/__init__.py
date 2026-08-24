"""Pure domain types. Imports nothing outside the standard library and pydantic.

The dependency rule is enforced mechanically by import-linter: nothing here may import
``cost_gate.adapters``, ``cost_gate.cli``, ``boto3`` or ``typer``. That is what keeps
the interesting logic testable without a cloud account and portable to a different
delivery mechanism.

Start with :mod:`cost_gate.domain.money` and :mod:`cost_gate.domain.values`; the rest
follows from how those two model exactness and ignorance.
"""

from __future__ import annotations

from cost_gate.domain.changes import ChangeSet, PropertyDelta, ResourceChange
from cost_gate.domain.cost import (
    Assumption,
    CostComponent,
    CostReport,
    CostTotals,
    PricingSourceRef,
    UnknownInput,
    UnknownSummary,
)
from cost_gate.domain.decision import (
    BudgetEvaluation,
    Evidence,
    GateDecision,
    PolicyEvaluation,
    Reason,
    combine_results,
)
from cost_gate.domain.enums import (
    ChangeOperation,
    Confidence,
    CostCategory,
    EstimateType,
    GateResult,
    IntrinsicKind,
    MatchMethod,
    PolicyAction,
    PurchaseOption,
    Severity,
    ValueProvenance,
    most_specific_provenance,
)
from cost_gate.domain.money import (
    Currency,
    Money,
    add_or_unknown,
    subtract_or_unknown,
    sum_known,
)
from cost_gate.domain.resources import (
    NormalizedResource,
    ResourceContext,
    ResourceGraph,
    ResourceKey,
    SourceLocation,
    property_path,
)
from cost_gate.domain.values import PropertyValue, Resolved, ResourceRef, Unresolved

__all__ = [
    "Assumption",
    "BudgetEvaluation",
    "ChangeOperation",
    "ChangeSet",
    "Confidence",
    "CostCategory",
    "CostComponent",
    "CostReport",
    "CostTotals",
    "Currency",
    "EstimateType",
    "Evidence",
    "GateDecision",
    "GateResult",
    "IntrinsicKind",
    "MatchMethod",
    "Money",
    "NormalizedResource",
    "PolicyAction",
    "PolicyEvaluation",
    "PricingSourceRef",
    "PropertyDelta",
    "PropertyValue",
    "PurchaseOption",
    "Reason",
    "Resolved",
    "ResourceChange",
    "ResourceContext",
    "ResourceGraph",
    "ResourceKey",
    "ResourceRef",
    "Severity",
    "SourceLocation",
    "UnknownInput",
    "UnknownSummary",
    "Unresolved",
    "ValueProvenance",
    "add_or_unknown",
    "combine_results",
    "most_specific_provenance",
    "property_path",
    "subtract_or_unknown",
    "sum_known",
]
