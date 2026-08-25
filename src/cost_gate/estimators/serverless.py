"""Lambda functions and API Gateway APIs.

These are the purest usage-driven services: the template says the thing exists and
almost nothing about what it will cost. A Lambda function that is never invoked costs
nothing; the same function under heavy load can dominate a bill. There is no honest way
to bridge that gap from a template alone, so the estimator asks the usage profile and
reports an explicit unknown when the profile is silent.

The distinction that runs through this module:

* **Service defaults are defensible.** Lambda's memory size defaults to 128 MB because
  AWS says so, not because this tool guessed. Applying it is reporting a fact.
* **Usage volumes are not.** "How many invocations per month" has no defensible answer,
  and inventing one produces exactly the confident-looking fiction this project exists
  to avoid.
"""

from __future__ import annotations

from decimal import Decimal

from cost_gate.config.usage import ResolvedDriver
from cost_gate.domain.cost import Assumption
from cost_gate.domain.enums import Confidence, EstimateType
from cost_gate.domain.resources import NormalizedResource
from cost_gate.domain.values import Resolved, Unresolved
from cost_gate.estimators.base import DimensionEstimate, EstimationContext, unknown
from cost_gate.estimators.compute import as_decimal
from cost_gate.pricing.keys import PriceKey

__all__ = ["ApiGatewayEstimator", "LambdaFunctionEstimator", "RestApiEstimator"]

LAMBDA_SERVICE = "AWSLambda"
API_GATEWAY_SERVICE = "AmazonApiGateway"

MILLISECONDS_PER_SECOND = Decimal(1000)
MB_PER_GB = Decimal(1024)


class LambdaFunctionEstimator:
    """``AWS::Lambda::Function``.

    Two dimensions, both usage-driven: a per-request charge and a per-GB-second charge
    for billed duration. Duration is the harder one — it depends on what the code does,
    which a template cannot describe — so it stays unknown until a profile supplies it,
    even when the invocation count is known.
    """

    resource_types = ("AWS::Lambda::Function",)
    service = LAMBDA_SERVICE

    DEFAULT_MEMORY_MB = Decimal(128)
    """Lambda's own default. A service default, not a guess by this tool."""

    DEFAULT_ARCHITECTURE = "x86_64"

    def estimate(
        self, resource: NormalizedResource, context: EstimationContext
    ) -> tuple[DimensionEstimate, ...]:
        """Price requests and billed duration."""
        architecture = self._architecture(resource)
        if isinstance(architecture, DimensionEstimate):
            return (architecture,)

        invocations = context.driver("invocations_per_month", resource)
        return (
            self._requests(resource, context, architecture, invocations),
            self._duration(resource, context, architecture, invocations),
        )

    def _architecture(self, resource: NormalizedResource) -> str | DimensionEstimate:
        declared = resource.property_value("Architectures", 0)
        if isinstance(declared, Unresolved):
            return unknown(
                self.service,
                "Requests",
                missing="Architectures",
                reason=(
                    "the architecture is not knowable before deployment, and arm64 is "
                    f"charged at a different rate: {declared.reason}"
                ),
                remedy="supply the parameter it depends on with --parameters",
                unit="Request",
            )
        if isinstance(declared, Resolved) and isinstance(declared.value, str):
            return declared.value
        return self.DEFAULT_ARCHITECTURE

    def _requests(
        self,
        resource: NormalizedResource,
        context: EstimationContext,
        architecture: str,
        invocations: ResolvedDriver | None,
    ) -> DimensionEstimate:
        if invocations is None:
            return context.volume_unknown(
                self.service,
                "Requests",
                driver="invocations_per_month",
                resource=resource,
                why=(
                    "no invocation count is configured; a function that is never called "
                    "costs nothing and the same function under load can dominate a bill"
                ),
                unit="Request",
            )
        quantity = invocations.quantity
        return context.priced(
            service=self.service,
            dimension="Requests",
            key=PriceKey(
                service=self.service,
                dimension="Requests",
                region=context.region,
                attributes={"architecture": architecture},
            ),
            quantity=quantity.expected,
            quantity_low=quantity.minimum,
            quantity_high=quantity.maximum,
            estimate_type=EstimateType.USAGE_BASED,
            confidence=Confidence.MEDIUM,
            confidence_reasons=(
                "published per-request rate",
                f"assumes {quantity.expected} invocations/month from the {invocations.detail}",
            ),
            assumptions=(
                Assumption(
                    subject="invocations_per_month",
                    value=str(quantity.expected),
                    provenance=invocations.provenance,
                    detail=invocations.detail,
                    resource=resource.key,
                ),
            ),
        )

    def _duration(
        self,
        resource: NormalizedResource,
        context: EstimationContext,
        architecture: str,
        invocations: ResolvedDriver | None,
    ) -> DimensionEstimate:
        """Price billed duration, which needs both a count and a duration."""
        duration = context.driver("average_duration_ms", resource)
        if invocations is None or duration is None:
            missing = "invocations_per_month" if invocations is None else "average_duration_ms"
            return context.volume_unknown(
                self.service,
                "GB-Seconds",
                driver=missing,
                resource=resource,
                why=(
                    "billed duration needs both an invocation count and an average "
                    "duration; how long the code runs is a property of the code, not of "
                    "the template"
                ),
                unit="GB-Second",
            )

        memory = self._memory(resource, context)
        memory_gb = memory / MB_PER_GB
        seconds = duration.quantity.expected / MILLISECONDS_PER_SECOND
        count = invocations.quantity.expected

        return context.priced(
            service=self.service,
            dimension="GB-Seconds",
            key=PriceKey(
                service=self.service,
                dimension="GB-Seconds",
                region=context.region,
                attributes={"architecture": architecture},
            ),
            quantity=count * seconds * memory_gb,
            estimate_type=EstimateType.USAGE_BASED,
            confidence=Confidence.LOW,
            confidence_reasons=(
                "published per-GB-second rate",
                f"assumes {count} invocations at {duration.quantity.expected} ms and {memory} MB",
                "billed duration is rounded up per invocation, which this does not model",
            ),
            assumptions=(
                Assumption(
                    subject="average_duration_ms",
                    value=str(duration.quantity.expected),
                    provenance=duration.provenance,
                    detail=duration.detail,
                    resource=resource.key,
                ),
            ),
        )

    def _memory(self, resource: NormalizedResource, context: EstimationContext) -> Decimal:
        """Read the configured memory, falling back to Lambda's own default."""
        declared = as_decimal(resource.property_value("MemorySize"))
        if declared is not None:
            return declared
        override = context.driver("allocated_memory_mb", resource)
        if override is not None:
            return override.quantity.expected
        return self.DEFAULT_MEMORY_MB


class _ApiRequestEstimator:
    """Shared behaviour for the two API Gateway resource types."""

    service = API_GATEWAY_SERVICE
    api_type = "HTTP"

    def _requests(
        self, resource: NormalizedResource, context: EstimationContext, api_type: str
    ) -> DimensionEstimate:
        requests = context.driver("requests_per_month", resource)
        if requests is None:
            return context.volume_unknown(
                self.service,
                "Requests",
                driver="requests_per_month",
                resource=resource,
                why="no request volume is configured, and an API is charged per request",
                unit="Request",
            )
        quantity = requests.quantity
        return context.priced(
            service=self.service,
            dimension="Requests",
            key=PriceKey(
                service=self.service,
                dimension="Requests",
                region=context.region,
                attributes={"apiType": api_type},
            ),
            quantity=quantity.expected,
            quantity_low=quantity.minimum,
            quantity_high=quantity.maximum,
            estimate_type=EstimateType.TIERED,
            confidence=Confidence.MEDIUM,
            confidence_reasons=(
                f"published first-tier per-request rate for a {api_type} API",
                f"assumes {quantity.expected} requests/month from the {requests.detail}",
                "priced at the first tier; real pricing steps down at high volume, so "
                "this overstates a busy API",
            ),
            assumptions=(
                Assumption(
                    subject="requests_per_month",
                    value=str(quantity.expected),
                    provenance=requests.provenance,
                    detail=requests.detail,
                    resource=resource.key,
                ),
            ),
            missing=f"{api_type} API per-request rate",
        )


class ApiGatewayEstimator(_ApiRequestEstimator):
    """``AWS::ApiGatewayV2::Api`` — HTTP and WebSocket APIs."""

    resource_types = ("AWS::ApiGatewayV2::Api",)

    def estimate(
        self, resource: NormalizedResource, context: EstimationContext
    ) -> tuple[DimensionEstimate, ...]:
        """Price API requests, if the protocol has a catalogued rate."""
        declared = resource.property_value("ProtocolType")
        if isinstance(declared, Unresolved):
            return (
                unknown(
                    self.service,
                    "Requests",
                    missing="ProtocolType",
                    reason=(
                        "the protocol is not knowable before deployment, and HTTP and "
                        f"WebSocket APIs are charged differently: {declared.reason}"
                    ),
                    remedy="supply the parameter it depends on with --parameters",
                    unit="Request",
                ),
            )
        protocol = declared.value if isinstance(declared, Resolved) else "HTTP"
        return (self._requests(resource, context, str(protocol)),)


class RestApiEstimator(_ApiRequestEstimator):
    """``AWS::ApiGateway::RestApi``.

    REST APIs are charged several times the HTTP API rate for the same request count,
    which is worth surfacing when someone adds one to a development stack.
    """

    resource_types = ("AWS::ApiGateway::RestApi",)

    def estimate(
        self, resource: NormalizedResource, context: EstimationContext
    ) -> tuple[DimensionEstimate, ...]:
        """Price API requests."""
        return (self._requests(resource, context, "REST"),)
