"""The whole analysis, in one function.

Everything the previous phases built, joined up:

    parse -> diff -> estimate -> budgets -> policies -> decide -> reconcile

Keeping this in one place, above the CLI, means the pull-request integration, the demo
command and any future caller run *exactly* the same pipeline. A GitHub action that
assembled the steps itself would eventually drift from what the CLI does, and the two
would disagree about the same change.

Failures are separated deliberately:

* a broken **provider or configuration** produces ``ERROR`` — the tool cannot answer;
* an unresolvable **value** produces an unknown — the tool answers, and says what it
  could not establish.

Conflating them would let a misconfigured catalog turn every cost into an unknown and
still report success.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from cost_gate.adapters.clock import Clock, SystemClock
from cost_gate.budgets import budget_policy_evaluations, evaluate_budgets
from cost_gate.config.root import LoadedConfig
from cost_gate.config.usage import UsageProfileConfig
from cost_gate.diff import compare
from cost_gate.diff.matching import match_resources
from cost_gate.domain.artifact import AnalysisArtifact, ChangeSummary, PricingProvenance
from cost_gate.domain.resources import ResourceContext
from cost_gate.estimators import EstimationContext, default_registry, estimate_graphs
from cost_gate.parsers import TemplateError, load_graph
from cost_gate.parsers.normalize import DEFAULT_SINGLE_STACK
from cost_gate.policies import PolicyFacts, build_decision, evaluate_policies
from cost_gate.pricing import PricingError, PricingProvider
from cost_gate.pricing.selection import build_provider
from cost_gate.recommendations import RecommendationFacts, recommend
from cost_gate.reporting.reconcile import reconcile_artifact

__all__ = ["AnalysisError", "AnalysisRequest", "run_analysis"]


class AnalysisError(Exception):
    """The analysis could not be performed at all.

    Distinct from a report full of unknowns. This means no trustworthy answer exists,
    and the caller must exit ``ERROR`` rather than render anything.
    """

    def __init__(self, messages: list[str]) -> None:
        """Record every reason, not just the first."""
        self.messages = messages
        super().__init__("; ".join(messages))


@dataclass(frozen=True)
class AnalysisRequest:
    """Everything one run needs."""

    baseline: Path
    proposed: Path
    config: LoadedConfig | None = None
    region: str | None = None
    environment: str | None = None
    application: str | None = None
    parameters: dict[str, str] = field(default_factory=dict)
    catalog: Path | None = None
    clock: Clock = field(default_factory=SystemClock)
    tool_version: str = ""

    def resolved_region(self) -> str:
        """The region to price in."""
        if self.region:
            return self.region
        return self.config.root.region if self.config else "us-east-1"

    def resolved_context(self) -> ResourceContext:
        """Default attribution for resources that carry no tags of their own."""
        base = self.config.root.context() if self.config else ResourceContext()
        return ResourceContext(
            environment=self.environment or base.environment,
            application=self.application or base.application,
            team=base.team,
            cost_centre=base.cost_centre,
        )


def run_analysis(request: AnalysisRequest) -> AnalysisArtifact:
    """Run the pipeline end to end.

    Raises:
        AnalysisError: if templates cannot be read, the pricing catalog cannot be
            loaded, or the finished report does not reconcile.
    """
    context = request.resolved_context()
    region = request.resolved_region()
    monthly_hours = request.config.monthly_hours if request.config else 730
    usage = (
        request.config.usage
        if request.config and request.config.usage
        else UsageProfileConfig(version=1)
    )

    # Both sides must agree on the stack name or nothing can pair across them: a
    # file named baseline.yaml and one named proposed.yaml would otherwise describe
    # two different stacks, and every resource would look deleted and recreated.
    single_files = request.baseline.is_file() and request.proposed.is_file()
    shared_stack = DEFAULT_SINGLE_STACK if single_files else None

    try:
        baseline = load_graph(
            request.baseline,
            stack_name=shared_stack,
            region=region,
            supplied_parameters=request.parameters,
            default_context=context,
        )
        proposed = load_graph(
            request.proposed,
            stack_name=shared_stack,
            region=region,
            supplied_parameters=request.parameters,
            default_context=context,
        )
    except TemplateError as exc:
        raise AnalysisError([exc.render()]) from exc

    catalog = request.catalog or (
        Path(request.config.catalog_path)
        if request.config and request.config.catalog_path
        else None
    )
    # `fixtures` unless the configuration says otherwise. Selecting `aws` is a
    # deliberate act that needs boto3 and credentials, and build_provider raises rather
    # than falling back - a silent fallback would let somebody believe they were pricing
    # against live rates when they were not.
    kind = request.config.root.pricing.provider if request.config else "fixtures"
    try:
        provider: PricingProvider = build_provider(kind, catalog=catalog)
    except PricingError as exc:
        raise AnalysisError([str(exc)]) from exc

    matches = match_resources(baseline, proposed)
    changes = compare(baseline, proposed)

    estimation = EstimationContext(
        provider=provider,
        usage=usage,
        region=region,
        monthly_hours=monthly_hours,
        environment=context.environment,
    )
    cost = estimate_graphs(baseline, proposed, estimation, default_registry(), matches)

    contexts = {
        resource.key: resource.context
        for graph in (baseline, proposed)
        for resource in graph.resources
    }
    budgets_config = request.config.budgets if request.config else None
    policies_config = request.config.policies if request.config else None

    budget_evaluations = evaluate_budgets(budgets_config, cost, contexts, context)
    facts = PolicyFacts(
        report=cost,
        changes=changes,
        budgets=budget_evaluations,
        region=region,
        environment=context.environment,
        application=context.application,
    )
    evaluations = evaluate_policies(policies_config, facts) + budget_policy_evaluations(
        budgets_config, budget_evaluations
    )
    decision = build_decision(evaluations, budget_evaluations, cost.totals, cost.unknowns)

    # An override nobody's resources match is a decision that never took effect.
    identities = [
        (resource.key.logical_id, resource.construct_path)
        for graph in (baseline, proposed)
        for resource in graph.resources
    ]
    warnings = tuple(
        f"usage override {key!r} matched no resource in this change"
        for key in usage.unmatched_overrides(identities)
    )

    # Advice, computed from the proposed state rather than from the change: a NAT
    # Gateway that has been there for two years costs the same as one added today, and
    # whoever is reviewing a change to the stack is best placed to notice.
    advice = recommend(
        RecommendationFacts(
            resources=tuple(proposed.resources),
            report=cost,
            environment=context.environment,
        )
    )

    metadata = provider.catalog_metadata()
    artifact = AnalysisArtifact(
        tool_version=request.tool_version,
        generated_at=request.clock.now(),
        run_id=request.clock.run_id(),
        region=region,
        currency=str(metadata.currency),
        monthly_hours=monthly_hours,
        environment=context.environment,
        application=context.application,
        pricing=PricingProvenance(
            provider=metadata.provider,
            catalog_version=metadata.version,
            region=metadata.region,
            captured_at=metadata.captured_at,
            authoritative=metadata.authoritative,
            verified=metadata.verified,
            disclaimer=metadata.disclaimer,
            limitations=metadata.limitations,
        ),
        changes=ChangeSummary.of(changes),
        decision=decision,
        cost=cost,
        recommendations=advice,
        warnings=warnings,
    )

    problems = reconcile_artifact(artifact)
    if problems:
        # Printing a report that does not add up is worse than refusing to print one.
        raise AnalysisError(["the report failed its own reconciliation checks", *problems])
    return artifact
