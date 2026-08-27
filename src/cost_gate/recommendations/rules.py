"""The rules themselves.

Each one answers three questions in order, and stops at the first "no":

1. Is the pattern present in this change? (from the template, not from guesswork)
2. What is it currently costing? (from the components the estimators produced)
3. Under what condition would acting on it be right?

Question three is the one that keeps this honest. Every rule below has a real answer to
it, and a proposed rule that cannot answer it does not belong here — which is why
"this instance looks oversized" is absent. Right-sizing needs utilisation data, a
template carries none, and a recommendation to downsize a machine that turns out to be
busy is worse than silence.

Rules never look at whether the change was *added* or already existed. A NAT Gateway that
has been there for two years costs exactly as much as one added today, and a reader
reviewing a pull request that touches the stack is the person best placed to notice.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from cost_gate.domain.cost import CostComponent, CostReport
from cost_gate.domain.decision import Evidence
from cost_gate.domain.enums import Confidence
from cost_gate.domain.money import Money
from cost_gate.domain.recommendations import Recommendation
from cost_gate.domain.resources import NormalizedResource, ResourceKey
from cost_gate.domain.values import Resolved

__all__ = ["RecommendationFacts", "Rule", "default_rules"]


@dataclass(frozen=True)
class RecommendationFacts:
    """Everything a rule may look at.

    A fixed set, like the policy predicates and for the same reason: a rule that could
    reach anywhere would make the advice impossible to describe or to test.
    """

    resources: tuple[NormalizedResource, ...]
    report: CostReport
    environment: str | None = None

    def of_type(self, *resource_types: str) -> tuple[NormalizedResource, ...]:
        """Resources of any of the given types, in a stable order."""
        wanted = set(resource_types)
        return tuple(
            sorted(
                (r for r in self.resources if r.resource_type in wanted),
                key=lambda r: r.key.sort_key,
            )
        )

    def cost_of(self, key: ResourceKey, *dimensions: str) -> Money | None:
        """What a resource is costing, across the named dimensions.

        Returns ``None`` when nothing could be established — in which case the
        recommendation still applies, it simply cannot say how much is at stake.
        """
        wanted = set(dimensions)
        amounts = [
            component.proposed_monthly
            for component in self.report.components
            if component.resource == key
            and component.pricing_dimension in wanted
            and component.proposed_monthly is not None
        ]
        return sum(amounts[1:], start=amounts[0]) if amounts else None

    def components_for(self, key: ResourceKey) -> tuple[CostComponent, ...]:
        """Every component belonging to a resource."""
        return tuple(c for c in self.report.components if c.resource == key)


Rule = object
"""A callable taking :class:`RecommendationFacts` and yielding recommendations."""


def _evidence(resource: NormalizedResource, description: str) -> Evidence:
    return Evidence(description=description, resource=resource.key, source=resource.source)


def nat_gateway_endpoints(facts: RecommendationFacts) -> Iterable[Recommendation]:
    """NAT Gateways, and the endpoints that sometimes replace them.

    The canonical example of advice that must not overstate. Gateway endpoints for S3 and
    DynamoDB carry no hourly charge, so a gateway whose traffic is entirely to those two
    services is pure overhead. A gateway that also reaches the public internet is not,
    and nothing in a template says which this is.
    """
    for resource in facts.of_type("AWS::EC2::NatGateway"):
        cost = facts.cost_of(resource.key, "NatGateway-Hours", "NatGateway-Bytes")
        yield Recommendation(
            rule_id="nat-gateway-endpoints",
            title=f"{resource.key.logical_id} is charged by the hour whether or not traffic flows",
            detail=(
                "A NAT Gateway accrues an hourly charge for as long as it exists, plus a "
                "per-gigabyte charge for what passes through it. VPC gateway endpoints "
                "for S3 and DynamoDB carry neither."
            ),
            condition=(
                "Applies only if the traffic through this gateway is destined for S3 and "
                "DynamoDB alone. If anything behind it reaches the public internet, or "
                "any AWS service without a gateway endpoint, the gateway is doing work "
                "endpoints cannot. Check the flow logs before acting."
            ),
            addressable_monthly=cost,
            resource=resource.key,
            evidence=(_evidence(resource, "NAT Gateway declared in this stack"),),
            confidence=Confidence.HIGH,
        )


def always_on_development_compute(facts: RecommendationFacts) -> Iterable[Recommendation]:
    """Compute in a non-production environment, billed around the clock.

    The usage profile already models a working-hours schedule as an *assumption*. This
    points out that the assumption is not a control: nothing in the template stops the
    instance running at the weekend.
    """
    if facts.environment in (None, "production"):
        return
    for resource in facts.of_type("AWS::EC2::Instance", "AWS::RDS::DBInstance"):
        cost = facts.cost_of(resource.key, "InstanceHours")
        yield Recommendation(
            rule_id="always-on-non-production-compute",
            title=f"{resource.key.logical_id} runs continuously in {facts.environment}",
            detail=(
                "Non-production compute is often idle outside working hours. A schedule "
                "in the usage profile changes what this tool assumes; it does not change "
                "what runs. Stopping the instance is what changes the bill."
            ),
            condition=(
                "Applies only if the workload tolerates being stopped. Anything holding "
                "state in instance storage, running a long batch, or serving a shared "
                "environment other teams depend on does not."
            ),
            addressable_monthly=cost,
            resource=resource.key,
            evidence=(_evidence(resource, f"declared in the {facts.environment} environment"),),
            confidence=Confidence.MEDIUM,
        )


def log_retention_unbounded(facts: RecommendationFacts) -> Iterable[Recommendation]:
    """Log groups that keep everything forever.

    The strongest rule here, because the precondition is nearly always satisfied and the
    fact comes straight from the template. A log group with no ``RetentionInDays`` has no
    steady-state size at all: its cost is unbounded rather than merely unknown.
    """
    for resource in facts.of_type("AWS::Logs::LogGroup"):
        if resource.property_value("RetentionInDays") is not None:
            continue
        yield Recommendation(
            rule_id="unbounded-log-retention",
            title=f"{resource.key.logical_id} retains logs indefinitely",
            detail=(
                "With no RetentionInDays, a log group keeps everything forever. Storage "
                "grows for as long as the application runs, so there is no steady monthly "
                "figure to estimate - the cost is unbounded rather than unknown."
            ),
            condition=(
                "Applies unless a regulatory or contractual obligation requires "
                "indefinite retention, in which case a lifecycle policy to cheaper "
                "storage is the alternative worth considering."
            ),
            # Deliberately no amount. The whole point is that there is not one.
            addressable_monthly=None,
            resource=resource.key,
            evidence=(_evidence(resource, "no RetentionInDays declared"),),
            confidence=Confidence.HIGH,
        )


def idle_public_ipv4(facts: RecommendationFacts) -> Iterable[Recommendation]:
    """Elastic IPs, charged whether or not anything uses them.

    Every allocated public IPv4 address is billed now, attached or not, which surprises
    people often enough to be worth stating plainly.
    """
    for resource in facts.of_type("AWS::EC2::EIP"):
        cost = facts.cost_of(resource.key, "PublicIPv4-Hours")
        yield Recommendation(
            rule_id="public-ipv4-address",
            title=f"{resource.key.logical_id} is charged while allocated, attached or not",
            detail=(
                "Every public IPv4 address carries an hourly charge from the moment it is "
                "allocated. An address kept for a machine that no longer exists costs the "
                "same as one in active use."
            ),
            condition=(
                "Applies only if the address is not required. A stable address referenced "
                "by DNS, an allowlist held by a third party, or a firewall rule elsewhere "
                "is doing a job that releasing it would break."
            ),
            addressable_monthly=cost,
            resource=resource.key,
            evidence=(_evidence(resource, "Elastic IP declared in this stack"),),
            confidence=Confidence.HIGH,
        )


def dynamodb_capacity_mode(facts: RecommendationFacts) -> Iterable[Recommendation]:
    """Provisioned DynamoDB capacity, which is paid for whether used or not."""
    for resource in facts.of_type("AWS::DynamoDB::Table"):
        mode = resource.property_value("BillingMode")
        if not isinstance(mode, Resolved) or str(mode.value) != "PROVISIONED":
            continue
        cost = facts.cost_of(resource.key, "ReadCapacityUnit-Hours", "WriteCapacityUnit-Hours")
        yield Recommendation(
            rule_id="dynamodb-capacity-mode",
            title=f"{resource.key.logical_id} uses provisioned capacity",
            detail=(
                "Provisioned capacity is charged continuously at the level configured, "
                "regardless of how much of it is used. On-demand is charged per request "
                "and costs nothing while the table is idle."
            ),
            condition=(
                "Applies only if the traffic is low, spiky or unpredictable. A table with "
                "steady high throughput is cheaper provisioned, often substantially, so "
                "this is a trade rather than an improvement."
            ),
            addressable_monthly=cost,
            resource=resource.key,
            evidence=(_evidence(resource, "BillingMode is PROVISIONED"),),
            confidence=Confidence.MEDIUM,
        )


def redundant_load_balancers(facts: RecommendationFacts) -> Iterable[Recommendation]:
    """More than one load balancer in a stack.

    Each carries its own hourly charge, and several small services behind one balancer
    with host or path rules is a common consolidation. Not always right - separate
    balancers give separate blast radii - so the condition carries real weight.
    """
    balancers = facts.of_type("AWS::ElasticLoadBalancingV2::LoadBalancer")
    minimum = 2
    if len(balancers) < minimum:
        return
    amounts = [facts.cost_of(b.key, "LoadBalancer-Hours") for b in balancers]
    known = [a for a in amounts if a is not None]
    total = sum(known[1:], start=known[0]) if known else None
    yield Recommendation(
        rule_id="redundant-load-balancers",
        title=f"{len(balancers)} load balancers, each with its own hourly charge",
        detail=(
            "An Application Load Balancer is charged by the hour before any traffic "
            "reaches it. Several small services can often share one, routed by host or "
            "path rules, at a single hourly charge."
        ),
        condition=(
            "Applies only if the services can share a failure domain and a certificate. "
            "Separate balancers give separate blast radii and independent scaling, which "
            "is sometimes exactly what was wanted."
        ),
        addressable_monthly=total,
        resource=None,
        evidence=tuple(_evidence(b, "load balancer declared in this change") for b in balancers),
        confidence=Confidence.MEDIUM,
    )


def eks_control_plane(facts: RecommendationFacts) -> Iterable[Recommendation]:
    """EKS control planes, charged per cluster per hour."""
    clusters = facts.of_type("AWS::EKS::Cluster")
    minimum = 2
    if len(clusters) < minimum:
        return
    amounts = [facts.cost_of(c.key, "Cluster-Hours") for c in clusters]
    known = [a for a in amounts if a is not None]
    yield Recommendation(
        rule_id="eks-control-plane-count",
        title=f"{len(clusters)} EKS control planes, each charged by the hour",
        detail=(
            "The control plane is charged per cluster per hour, before any node joins. "
            "Namespaces within one cluster carry no such charge."
        ),
        condition=(
            "Applies only if the workloads can share a cluster. Separate clusters are the "
            "right answer where isolation is a compliance boundary, where control plane "
            "versions must differ, or where a noisy neighbour would matter."
        ),
        addressable_monthly=sum(known[1:], start=known[0]) if known else None,
        resource=None,
        evidence=tuple(_evidence(c, "EKS cluster declared in this change") for c in clusters),
        confidence=Confidence.MEDIUM,
    )


def gp2_volumes(facts: RecommendationFacts) -> Iterable[Recommendation]:
    """gp2 volumes, which gp3 supersedes at a lower price for the same capacity."""
    for resource in facts.of_type("AWS::EC2::Volume"):
        volume_type = resource.property_value("VolumeType")
        if not isinstance(volume_type, Resolved) or str(volume_type.value) != "gp2":
            continue
        cost = facts.cost_of(resource.key, "Storage-GB-Month")
        yield Recommendation(
            rule_id="gp2-volume-type",
            title=f"{resource.key.logical_id} uses gp2 storage",
            detail=(
                "gp3 is the current general-purpose volume type. It is priced lower per "
                "gigabyte than gp2 and decouples IOPS from capacity, so throughput no "
                "longer requires over-provisioning space."
            ),
            condition=(
                "Applies to essentially all gp2 volumes; the modification is online and "
                "does not detach the volume. Check that the baseline gp3 performance "
                "meets the workload, since gp2 IOPS scale with size."
            ),
            addressable_monthly=cost,
            resource=resource.key,
            evidence=(_evidence(resource, "VolumeType is gp2"),),
            confidence=Confidence.HIGH,
        )


def default_rules() -> tuple[Rule, ...]:
    """Every rule, in the order their findings are reported.

    Ordered by how confidently the condition can be judged from a template alone, so the
    most actionable advice is read first — not by cost, which would put the biggest
    number at the top regardless of whether anyone can act on it.
    """
    return (
        log_retention_unbounded,
        gp2_volumes,
        idle_public_ipv4,
        nat_gateway_endpoints,
        dynamodb_capacity_mode,
        redundant_load_balancers,
        eks_control_plane,
        always_on_development_compute,
    )


def _zero() -> Decimal:
    """Kept for clarity at call sites that compare against nothing."""
    return Decimal(0)
