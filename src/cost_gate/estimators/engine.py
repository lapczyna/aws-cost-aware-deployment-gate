"""Turning two resource graphs into a :class:`~cost_gate.domain.cost.CostReport`.

The engine prices **every resource in both graphs**, not only the changed ones. That is
what makes the report's ``current`` and ``proposed`` totals mean "the cost of the
infrastructure this template describes" rather than "the cost of the bits that moved" —
and budgets are evaluated against the former.

Pairing across the two graphs reuses the identity matching from
:mod:`cost_gate.diff.matching`, because a CDK resource can keep its identity while
changing its logical ID (ADR 0004). Pricing a renamed database as a deletion plus a
creation would be just as wrong here as in the diff.

Within a matched pair, dimensions are paired by name:

* present on both sides — a genuine before and after;
* present only after — the resource or dimension is new, so ``current`` is
  ``Money.zero()``. It did not exist; that is a real zero, not an unknown;
* present only before — likewise, ``proposed`` is ``Money.zero()``;
* unknown on either side — the delta is unknown, and the component says so.

Deltas are never computed independently: the ``CostComponent`` validator requires
``delta == proposed - current``, so an arithmetic slip fails construction rather than
producing a report that does not add up.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from cost_gate.diff.matching import MatchResult, match_resources
from cost_gate.domain.cost import (
    Assumption,
    CostComponent,
    CostReport,
    CostTotals,
    UnknownInput,
    UnknownSummary,
)
from cost_gate.domain.enums import Confidence, EstimateType
from cost_gate.domain.money import Money
from cost_gate.domain.resources import NormalizedResource, ResourceGraph, ResourceKey
from cost_gate.estimators.base import DimensionEstimate, EstimationContext, Estimator
from cost_gate.estimators.registry import EstimatorRegistry, default_registry

__all__ = ["estimate_graphs", "estimate_resource"]


def estimate_resource(
    resource: NormalizedResource | None,
    context: EstimationContext,
    registry: EstimatorRegistry,
) -> dict[str, DimensionEstimate]:
    """Price one resource state, keyed by dimension.

    A ``None`` resource yields nothing: that state does not exist, and the engine
    represents non-existence as a zero on the other side rather than as an absence here.
    """
    if resource is None:
        return {}
    estimator: Estimator = registry.for_type(resource.resource_type)  # type: ignore[assignment]
    return {dimension.dimension: dimension for dimension in estimator.estimate(resource, context)}


def _component_id(key: ResourceKey, dimension: str) -> str:
    return f"{key}#{dimension}"


def _merge(
    before: DimensionEstimate | None, after: DimensionEstimate | None
) -> tuple[tuple[str, ...], tuple[Assumption, ...], tuple[UnknownInput, ...]]:
    """Combine the explanations from both sides, without duplicating them."""
    reasons: list[str] = []
    assumptions: list[Assumption] = []
    unknowns: list[UnknownInput] = []
    for side in (before, after):
        if side is None:
            continue
        for reason in side.confidence_reasons:
            if reason not in reasons:
                reasons.append(reason)
        for assumption in side.assumptions:
            if assumption not in assumptions:
                assumptions.append(assumption)
        for unknown_input in side.unknown_inputs:
            if unknown_input not in unknowns:
                unknowns.append(unknown_input)
    return tuple(reasons), tuple(assumptions), tuple(unknowns)


def _build_component(
    key: ResourceKey,
    dimension: str,
    before: DimensionEstimate | None,
    after: DimensionEstimate | None,
    region: str,
) -> CostComponent:
    """Pair one dimension's two states into a component."""
    present = after or before
    if present is None:  # pragma: no cover - the caller only passes real pairs
        raise ValueError(f"{key}#{dimension}: neither state was estimated")

    reasons, assumptions, unknowns = _merge(before, after)

    # A side that was not estimated at all means the resource did not exist then, which
    # is a real zero. A side that was estimated but could not be priced is unknown.
    current = Money.zero() if before is None else before.monthly
    proposed = Money.zero() if after is None else after.monthly

    # Checked directly rather than through a flag, so that the type narrows for the
    # arithmetic below: a delta is only ever computed where both sides are known.
    if current is None or proposed is None:
        if not unknowns:
            unknowns = (
                UnknownInput(
                    name=dimension,
                    reason="the cost of this dimension could not be established",
                ),
            )
        return CostComponent(
            component_id=_component_id(key, dimension),
            service=present.service,
            resource=key,
            pricing_dimension=dimension,
            region=region,
            unit=present.unit,
            purchase_option=present.purchase_option,
            quantity=after.quantity if after is not None else None,
            current_monthly=current,
            proposed_monthly=proposed,
            monthly_delta=None,
            estimate_type=EstimateType.UNKNOWN,
            confidence=Confidence.UNKNOWN,
            assumptions=assumptions,
            unknown_inputs=unknowns,
            pricing_source=present.pricing_source,
        )

    confidences = [side.confidence for side in (before, after) if side is not None]
    return CostComponent(
        component_id=_component_id(key, dimension),
        service=present.service,
        resource=key,
        pricing_dimension=dimension,
        region=region,
        unit=present.unit,
        purchase_option=present.purchase_option,
        quantity=after.quantity if after is not None else None,
        current_monthly=current,
        proposed_monthly=proposed,
        monthly_delta=proposed - current,
        one_time=after.one_time if after is not None else None,
        low=after.low if after is not None else None,
        high=after.high if after is not None else None,
        estimate_type=present.estimate_type,
        confidence=min(confidences),
        confidence_reasons=reasons,
        assumptions=assumptions,
        pricing_source=present.pricing_source,
    )


def _pair(
    key: ResourceKey,
    before: Mapping[str, DimensionEstimate],
    after: Mapping[str, DimensionEstimate],
    region: str,
) -> list[CostComponent]:
    """Build a component for every dimension either state mentions."""
    return [
        _build_component(key, dimension, before.get(dimension), after.get(dimension), region)
        for dimension in sorted(set(before) | set(after))
    ]


def estimate_graphs(
    baseline: ResourceGraph,
    proposed: ResourceGraph,
    context: EstimationContext,
    registry: EstimatorRegistry | None = None,
    matches: MatchResult | None = None,
) -> CostReport:
    """Price both snapshots and derive the difference.

    Args:
        baseline: the current infrastructure.
        proposed: the infrastructure after the change.
        context: pricing provider, usage profile, region and hours convention.
        registry: estimators to use. Defaults to the shipped set.
        matches: a precomputed pairing, to avoid matching twice when the caller has
            already built a change set.
    """
    table = registry or default_registry()
    pairing = matches or match_resources(baseline, proposed)

    components: list[CostComponent] = []

    for match in pairing.matches:
        components.extend(
            _pair(
                match.after.key,
                estimate_resource(match.before, context, table),
                estimate_resource(match.after, context, table),
                context.region,
            )
        )

    for removed in pairing.removed:
        components.extend(
            _pair(removed.key, estimate_resource(removed, context, table), {}, context.region)
        )

    for added in pairing.added:
        components.extend(
            _pair(added.key, {}, estimate_resource(added, context, table), context.region)
        )

    components.sort(key=lambda component: component.component_id)
    totals = CostTotals.from_components(components, context.monthly_hours)
    return CostReport(
        components=tuple(components),
        totals=totals,
        unknowns=_summarise_unknowns(components, baseline, proposed),
        assumptions=_collect_assumptions(components),
        region=context.region,
        currency="USD",
    )


def _summarise_unknowns(
    components: Iterable[CostComponent],
    baseline: ResourceGraph,
    proposed: ResourceGraph,
) -> UnknownSummary:
    """Describe what could not be established, and for which resource types."""
    collected = [component for component in components if component.is_unknown]
    types_by_key = {
        resource.key: resource.resource_type
        for graph in (baseline, proposed)
        for resource in graph.resources
    }
    resource_types = sorted(
        {
            types_by_key[component.resource]
            for component in collected
            if component.resource in types_by_key
        }
    )
    inputs: list[UnknownInput] = []
    for component in collected:
        for unknown_input in component.unknown_inputs:
            if unknown_input not in inputs:
                inputs.append(unknown_input)
    return UnknownSummary(
        component_count=len(collected),
        resource_types=tuple(resource_types),
        inputs=tuple(inputs),
    )


def _collect_assumptions(components: Iterable[CostComponent]) -> tuple[Assumption, ...]:
    """Every distinct assumption, in a stable order."""
    seen: list[Assumption] = []
    for component in components:
        for assumption in component.assumptions:
            if assumption not in seen:
                seen.append(assumption)
    return tuple(sorted(seen, key=lambda assumption: assumption.sort_key))
