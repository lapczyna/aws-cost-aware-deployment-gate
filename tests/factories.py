"""Builders for domain objects, so tests state only what they are about.

A test that constructs a fully-populated ``AnalysisArtifact`` inline buries its own
point under thirty lines of scaffolding, and every field added later breaks it. These
helpers supply defensible defaults and let each test override the one thing it asserts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from cost_gate.domain.artifact import AnalysisArtifact, ChangeSummary, PricingProvenance
from cost_gate.domain.cost import (
    CostComponent,
    CostReport,
    CostTotals,
    UnknownInput,
    UnknownSummary,
)
from cost_gate.domain.decision import GateDecision, PolicyEvaluation, Reason
from cost_gate.domain.enums import (
    Confidence,
    EstimateType,
    GateResult,
    PolicyAction,
    Severity,
)
from cost_gate.domain.money import Money
from cost_gate.domain.resources import ResourceKey

__all__ = [
    "artifact_with",
    "component",
    "cost_report",
    "decision_with",
    "provenance",
    "reason",
    "usd",
]

CAPTURED_AT = datetime(2026, 1, 15, tzinfo=UTC)
GENERATED_AT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def usd(amount: str) -> Money:
    """A dollar amount, from a string so no float ever enters a test."""
    return Money(amount=Decimal(amount), currency="USD")


def component(
    *,
    logical_id: str,
    stack: str = "stack",
    current: str = "0.00",
    proposed: str | None = None,
    delta: str | None = None,
    unknown: str | None = None,
    service: str = "AmazonEC2",
    dimension: str = "NatGateway-Hours",
    estimate_type: EstimateType = EstimateType.FIXED,
    confidence: Confidence = Confidence.HIGH,
) -> CostComponent:
    """One cost component.

    Passing ``unknown`` produces a component whose cost could not be established: all
    three money fields stay ``None`` and an :class:`UnknownInput` explains why. That is
    the shape the rest of the system must handle, so it must be easy to build.
    """
    key = ResourceKey(stack=stack, logical_id=logical_id)
    if unknown is not None:
        return CostComponent(
            component_id=f"{stack}/{logical_id}/{dimension}",
            service=service,
            resource=key,
            pricing_dimension=dimension,
            region="us-east-1",
            estimate_type=EstimateType.UNKNOWN,
            confidence=Confidence.UNKNOWN,
            unknown_inputs=(
                UnknownInput(
                    name=unknown,
                    reason=f"{unknown} could not be resolved from the template",
                    remedy="supply the value with --parameters",
                ),
            ),
        )

    current_money = usd(current)
    if delta is not None and proposed is None:
        proposed_money = current_money + usd(delta)
    else:
        proposed_money = usd(proposed) if proposed is not None else current_money
    return CostComponent(
        component_id=f"{stack}/{logical_id}/{dimension}",
        service=service,
        resource=key,
        pricing_dimension=dimension,
        region="us-east-1",
        unit="Hrs",
        quantity=Decimal("730"),
        current_monthly=current_money,
        proposed_monthly=proposed_money,
        monthly_delta=proposed_money - current_money,
        estimate_type=estimate_type,
        confidence=confidence,
        confidence_reasons=("published hourly rate; quantity fully resolved",),
    )


def cost_report(components: list[CostComponent]) -> CostReport:
    """A report whose totals are derived from its components, as in production."""
    unknown_components = [c for c in components if c.is_unknown]
    # A component does not carry its resource type; the estimation engine derives that
    # from the graphs it priced. Tests that care about the type assert on the summary.
    return CostReport(
        components=tuple(components),
        totals=CostTotals.from_components(components),
        unknowns=UnknownSummary(
            component_count=len(unknown_components),
            resource_types=("AWS::EC2::NatGateway",) if unknown_components else (),
            inputs=tuple(i for c in unknown_components for i in c.unknown_inputs),
        ),
        region="us-east-1",
    )


def reason(text: str, severity: Severity = Severity.MEDIUM) -> Reason:
    """One line of the explanation."""
    return Reason(text=text, severity=severity)


def decision_with(
    *,
    result: GateResult = GateResult.PASS,
    totals: CostTotals | None = None,
    reasons: list[Reason] | None = None,
) -> GateDecision:
    """A decision, defaulting to the uninteresting case.

    ``GateDecision`` refuses a result its own policy evaluations do not imply, so a
    non-``PASS`` result needs a matched policy to justify it. That validator is the
    reason a test cannot simply assert a verdict into existence — which is the point of
    having it.
    """
    evaluations: tuple[PolicyEvaluation, ...] = ()
    if result is not GateResult.PASS:
        evaluations = (
            PolicyEvaluation(
                policy_id="test-policy",
                matched=True,
                action=PolicyAction(result.value),
                reason="a policy matched in a test",
                approver_group=("finops" if result is GateResult.REQUIRE_APPROVAL else None),
            ),
        )
    return GateDecision(
        result=result,
        totals=totals or CostTotals.from_components([]),
        policy_evaluations=evaluations,
        required_approver_groups=tuple(e.approver_group for e in evaluations if e.approver_group),
        reasons=tuple(reasons or []),
    )


def provenance() -> PricingProvenance:
    """Fixture-catalog provenance, including the disclaimer the report must carry."""
    return PricingProvenance(
        provider="fixture-catalog",
        catalog_version="2026.01",
        region="us-east-1",
        captured_at=CAPTURED_AT,
        authoritative=False,
        verified=True,
        # Shaped like CatalogMetadata.disclaimer, which is what actually carries the
        # capture date to a reader. A prettier invented string here would let the
        # report drop the date without any test noticing.
        disclaimer=(
            "pricing: fixture-catalog · v2026.01 · captured 2026-01-15 · "
            "illustrative list prices, not authoritative"
        ),
        limitations=("no discounts, credits, taxes or Savings Plans are modelled",),
    )


def artifact_with(
    *,
    components: list[CostComponent] | None = None,
    decision: GateDecision | None = None,
    changes: ChangeSummary | None = None,
) -> AnalysisArtifact:
    """A complete artifact built around whichever part the test cares about.

    The decision's totals are taken from the cost report unless the caller supplies a
    decision of their own, so the default artifact reconciles.
    """
    report = cost_report(components or [])
    return AnalysisArtifact(
        tool_version="0.1.0",
        generated_at=GENERATED_AT,
        run_id="fixedrun0001",
        region="us-east-1",
        monthly_hours=730,
        environment="development",
        application="payments",
        pricing=provenance(),
        changes=changes or ChangeSummary(added=len(components or [])),
        decision=decision
        or GateDecision(
            result=GateResult.PASS,
            totals=report.totals,
            unknowns=report.unknowns,
        ),
        cost=report,
    )
