"""DynamoDB tables, S3 buckets and CloudWatch log groups.

Three services, three different reasons a template cannot tell you the cost.

**DynamoDB** can, unusually, be priced from the template — but only in provisioned mode,
where the capacity units are declared. In on-demand mode the same table costs whatever
the traffic dictates. The two models differ by orders of magnitude at the same workload,
which is why an unresolved ``BillingMode`` is an unknown rather than a default: guessing
wrong here is not a rounding error.

**S3** declares nothing about what will be stored in it. A bucket is a namespace.

**CloudWatch Logs** hides the most common surprise in this list. A log group with no
``RetentionInDays`` never expires, so its storage grows without bound and no monthly
figure exists at all — not a large one, not an unknown volume, but a genuinely
unbounded quantity. That is reported as such, because "we cannot put a number on this"
is the actionable finding.
"""

from __future__ import annotations

from decimal import Decimal

from cost_gate.config.usage import ResolvedDriver
from cost_gate.domain.cost import Assumption
from cost_gate.domain.enums import Confidence, EstimateType, ValueProvenance
from cost_gate.domain.resources import NormalizedResource
from cost_gate.domain.values import Resolved, Unresolved
from cost_gate.estimators.base import (
    DimensionEstimate,
    EstimationContext,
    RuntimeBasis,
    unknown,
)
from cost_gate.estimators.compute import as_decimal
from cost_gate.pricing.keys import PriceKey

__all__ = [
    "CloudWatchAlarmEstimator",
    "CloudWatchLogsEstimator",
    "DynamoDbTableEstimator",
    "S3BucketEstimator",
]

DYNAMODB_SERVICE = "AmazonDynamoDB"
S3_SERVICE = "AmazonS3"
CLOUDWATCH_SERVICE = "AmazonCloudWatch"

DAYS_PER_MONTH = Decimal("30.4")
"""Used only to convert a retention window into a steady-state retained volume."""


def _driver_assumption(driver: ResolvedDriver, resource: NormalizedResource) -> Assumption:
    """Record a usage figure and where it came from."""
    return Assumption(
        subject=driver.name,
        value=str(driver.quantity.expected),
        provenance=driver.provenance,
        detail=driver.detail,
        resource=resource.key,
    )


class DynamoDbTableEstimator:
    """``AWS::DynamoDB::Table``."""

    resource_types = ("AWS::DynamoDB::Table",)
    service = DYNAMODB_SERVICE

    DEFAULT_BILLING_MODE = "PROVISIONED"
    """CloudFormation's default when ``BillingMode`` is omitted."""

    DEFAULT_TABLE_CLASS = "STANDARD"

    def estimate(
        self, resource: NormalizedResource, context: EstimationContext
    ) -> tuple[DimensionEstimate, ...]:
        """Price capacity or requests, plus storage."""
        declared = resource.property_value("BillingMode")
        if isinstance(declared, Unresolved):
            return (
                unknown(
                    self.service,
                    "BillingMode",
                    missing="BillingMode",
                    reason=(
                        "the billing mode is not knowable before deployment, and on-demand "
                        "and provisioned capacity differ by orders of magnitude at the same "
                        f"workload: {declared.reason}"
                    ),
                    remedy="supply the parameter it depends on with --parameters",
                ),
                self._storage(resource, context),
            )

        mode = str(declared.value) if isinstance(declared, Resolved) else self.DEFAULT_BILLING_MODE
        assumptions: tuple[Assumption, ...] = ()
        if declared is None:
            assumptions = (
                Assumption(
                    subject="BillingMode",
                    value=self.DEFAULT_BILLING_MODE,
                    provenance=ValueProvenance.BUILTIN_DEFAULT,
                    detail="CloudFormation defaults an unspecified table to provisioned capacity",
                    resource=resource.key,
                ),
            )

        if mode == "PAY_PER_REQUEST":
            capacity = self._on_demand(resource, context)
        else:
            capacity = self._provisioned(resource, context, assumptions)
        return (*capacity, self._storage(resource, context))

    def _provisioned(
        self,
        resource: NormalizedResource,
        context: EstimationContext,
        assumptions: tuple[Assumption, ...],
    ) -> tuple[DimensionEstimate, ...]:
        """Price declared capacity units.

        The one case in this module where the template really does say what it costs.
        """
        hours, runtime, reason = context.runtime_hours(resource, RuntimeBasis.ALWAYS_ON)
        estimates: list[DimensionEstimate] = []
        for property_name, dimension, label in (
            ("ReadCapacityUnits", "ReadCapacityUnit-Hours", "read"),
            ("WriteCapacityUnits", "WriteCapacityUnit-Hours", "write"),
        ):
            units = as_decimal(resource.property_value("ProvisionedThroughput", property_name))
            if units is None:
                estimates.append(
                    unknown(
                        self.service,
                        dimension,
                        missing=property_name,
                        reason=(
                            f"a provisioned table must declare {property_name}, and this "
                            "template does not resolve it"
                        ),
                        remedy=f"set ProvisionedThroughput.{property_name}",
                        unit=f"{label[0].upper()}CU-Hr",
                    )
                )
                continue
            estimates.append(
                context.priced(
                    service=self.service,
                    dimension=dimension,
                    key=PriceKey(
                        service=self.service,
                        dimension=dimension,
                        region=context.region,
                        attributes={"billingMode": "PROVISIONED"},
                    ),
                    quantity=units * Decimal(hours),
                    estimate_type=EstimateType.FIXED,
                    confidence=Confidence.MEDIUM,
                    confidence_reasons=(
                        f"{units} provisioned {label} capacity units, resolved from the template",
                        "provisioned capacity is charged whether or not it is used",
                        reason,
                    ),
                    assumptions=(*assumptions, runtime),
                )
            )
        return tuple(estimates)

    def _on_demand(
        self, resource: NormalizedResource, context: EstimationContext
    ) -> tuple[DimensionEstimate, ...]:
        """Price request units, which depend entirely on traffic."""
        estimates: list[DimensionEstimate] = []
        for driver_name, dimension, unit in (
            ("dynamodb_read_requests_per_month", "ReadRequestUnits", "RRU"),
            ("dynamodb_write_requests_per_month", "WriteRequestUnits", "WRU"),
        ):
            driver = context.driver(driver_name, resource)
            if driver is None:
                estimates.append(
                    context.volume_unknown(
                        self.service,
                        dimension,
                        driver=driver_name,
                        resource=resource,
                        why=(
                            "an on-demand table is charged per request, and no request "
                            "volume is configured"
                        ),
                        unit=unit,
                    )
                )
                continue
            estimates.append(
                context.priced(
                    service=self.service,
                    dimension=dimension,
                    key=PriceKey(
                        service=self.service,
                        dimension=dimension,
                        region=context.region,
                        attributes={"billingMode": "PAY_PER_REQUEST"},
                    ),
                    quantity=driver.quantity.expected,
                    quantity_low=driver.quantity.minimum,
                    quantity_high=driver.quantity.maximum,
                    estimate_type=EstimateType.USAGE_BASED,
                    confidence=Confidence.MEDIUM,
                    confidence_reasons=(
                        "published per-request-unit rate",
                        f"assumes {driver.quantity.expected} units/month from the {driver.detail}",
                    ),
                    assumptions=(_driver_assumption(driver, resource),),
                )
            )
        return tuple(estimates)

    def _storage(
        self, resource: NormalizedResource, context: EstimationContext
    ) -> DimensionEstimate:
        driver = context.driver("dynamodb_storage_gb", resource)
        if driver is None:
            return context.volume_unknown(
                self.service,
                "Storage-GB-Month",
                driver="dynamodb_storage_gb",
                resource=resource,
                why="no stored volume is configured, and a table is charged per GB-month",
                unit="GB-Mo",
            )
        return context.priced(
            service=self.service,
            dimension="Storage-GB-Month",
            key=PriceKey(
                service=self.service,
                dimension="Storage-GB-Month",
                region=context.region,
                attributes={"tableClass": self.DEFAULT_TABLE_CLASS},
            ),
            quantity=driver.quantity.expected,
            quantity_low=driver.quantity.minimum,
            quantity_high=driver.quantity.maximum,
            estimate_type=EstimateType.USAGE_BASED,
            confidence=Confidence.MEDIUM,
            confidence_reasons=(
                "published per-GB-month rate for the standard table class",
                f"assumes {driver.quantity.expected} GB from the {driver.detail}",
            ),
            assumptions=(_driver_assumption(driver, resource),),
        )


class S3BucketEstimator:
    """``AWS::S3::Bucket``.

    A bucket declares almost nothing about its cost: storage class is a property of the
    objects, not the bucket, and volume is a property of what gets written. Everything
    here therefore comes from the usage profile or is reported unknown.
    """

    resource_types = ("AWS::S3::Bucket",)
    service = S3_SERVICE

    DEFAULT_STORAGE_CLASS = "STANDARD"

    def estimate(
        self, resource: NormalizedResource, context: EstimationContext
    ) -> tuple[DimensionEstimate, ...]:
        """Price stored volume and request counts."""
        return (
            self._storage(resource, context),
            self._requests(resource, context, "s3_put_requests_per_month", "PutRequests"),
            self._requests(resource, context, "s3_get_requests_per_month", "GetRequests"),
        )

    def _storage(
        self, resource: NormalizedResource, context: EstimationContext
    ) -> DimensionEstimate:
        driver = context.driver("storage_gb", resource)
        if driver is None:
            return context.volume_unknown(
                self.service,
                "Storage-GB-Month",
                driver="storage_gb",
                resource=resource,
                why=(
                    "a bucket is a namespace; how much is stored in it is a property of "
                    "what gets written, not of the template"
                ),
                unit="GB-Mo",
            )
        return context.priced(
            service=self.service,
            dimension="Storage-GB-Month",
            key=PriceKey(
                service=self.service,
                dimension="Storage-GB-Month",
                region=context.region,
                attributes={"storageClass": self.DEFAULT_STORAGE_CLASS},
            ),
            quantity=driver.quantity.expected,
            quantity_low=driver.quantity.minimum,
            quantity_high=driver.quantity.maximum,
            estimate_type=EstimateType.TIERED,
            confidence=Confidence.LOW,
            confidence_reasons=(
                "published first-tier standard storage rate",
                f"assumes {driver.quantity.expected} GB from the {driver.detail}",
                "assumes standard storage; the class is a property of the objects, and a "
                "lifecycle policy moving them elsewhere would change this materially",
            ),
            assumptions=(
                _driver_assumption(driver, resource),
                Assumption(
                    subject="storageClass",
                    value=self.DEFAULT_STORAGE_CLASS,
                    provenance=ValueProvenance.BUILTIN_DEFAULT,
                    detail="storage class is set per object, and the template does not say",
                    resource=resource.key,
                ),
            ),
        )

    def _requests(
        self,
        resource: NormalizedResource,
        context: EstimationContext,
        driver_name: str,
        dimension: str,
    ) -> DimensionEstimate:
        driver = context.driver(driver_name, resource)
        if driver is None:
            return context.volume_unknown(
                self.service,
                dimension,
                driver=driver_name,
                resource=resource,
                why="no request volume is configured, and S3 charges per request",
                unit="Request",
            )
        return context.priced(
            service=self.service,
            dimension=dimension,
            key=PriceKey(
                service=self.service,
                dimension=dimension,
                region=context.region,
                attributes={"storageClass": self.DEFAULT_STORAGE_CLASS},
            ),
            quantity=driver.quantity.expected,
            quantity_low=driver.quantity.minimum,
            quantity_high=driver.quantity.maximum,
            estimate_type=EstimateType.USAGE_BASED,
            confidence=Confidence.MEDIUM,
            confidence_reasons=(
                "published per-request rate for standard storage",
                f"assumes {driver.quantity.expected} requests/month from the {driver.detail}",
            ),
            assumptions=(_driver_assumption(driver, resource),),
        )


class CloudWatchLogsEstimator:
    """``AWS::Logs::LogGroup``.

    Ingestion is usually the dominant charge and the hardest to predict: log volume
    varies by four orders of magnitude between applications, so there is no default
    worth applying.

    Retained storage is where the interesting finding lives. A log group with no
    ``RetentionInDays`` keeps everything forever, so there is no steady-state monthly
    volume to price — the quantity is unbounded, not merely unknown. Saying so is more
    useful than any number would be.
    """

    resource_types = ("AWS::Logs::LogGroup",)
    service = CLOUDWATCH_SERVICE

    DEFAULT_LOG_GROUP_CLASS = "STANDARD"

    def estimate(
        self, resource: NormalizedResource, context: EstimationContext
    ) -> tuple[DimensionEstimate, ...]:
        """Price ingestion and retained storage."""
        declared_class = resource.property_value("LogGroupClass")
        log_class = (
            str(declared_class.value)
            if isinstance(declared_class, Resolved)
            else self.DEFAULT_LOG_GROUP_CLASS
        )
        ingestion_driver = context.driver("log_ingestion_gb", resource)
        return (
            self._ingestion(resource, context, log_class, ingestion_driver),
            self._retained(resource, context, ingestion_driver),
        )

    def _ingestion(
        self,
        resource: NormalizedResource,
        context: EstimationContext,
        log_class: str,
        driver: ResolvedDriver | None,
    ) -> DimensionEstimate:
        if driver is None:
            return context.volume_unknown(
                self.service,
                "Logs-Ingestion-GB",
                driver="log_ingestion_gb",
                resource=resource,
                why=(
                    "log volume varies by four orders of magnitude between applications, "
                    "and ingestion is usually the dominant CloudWatch Logs charge"
                ),
                unit="GB",
            )
        return context.priced(
            service=self.service,
            dimension="Logs-Ingestion-GB",
            key=PriceKey(
                service=self.service,
                dimension="Logs-Ingestion-GB",
                region=context.region,
                attributes={"logGroupClass": log_class},
            ),
            quantity=driver.quantity.expected,
            quantity_low=driver.quantity.minimum,
            quantity_high=driver.quantity.maximum,
            estimate_type=EstimateType.USAGE_BASED,
            confidence=Confidence.MEDIUM,
            confidence_reasons=(
                f"published per-GB ingestion rate for a {log_class} log group",
                f"assumes {driver.quantity.expected} GB/month from the {driver.detail}",
            ),
            assumptions=(_driver_assumption(driver, resource),),
            missing=f"{log_class} log ingestion rate",
        )

    def _retained(
        self,
        resource: NormalizedResource,
        context: EstimationContext,
        ingestion: ResolvedDriver | None,
    ) -> DimensionEstimate:
        """Price retained log storage, or report that it has no bound."""
        declared = resource.property_value("RetentionInDays")
        if isinstance(declared, Unresolved):
            return unknown(
                self.service,
                "Logs-Storage-GB-Month",
                missing="RetentionInDays",
                reason=f"the retention period is not knowable before deployment: {declared.reason}",
                remedy="supply the parameter it depends on with --parameters",
                unit="GB-Mo",
            )

        retention = as_decimal(declared)
        if retention is None:
            return unknown(
                self.service,
                "Logs-Storage-GB-Month",
                missing="RetentionInDays",
                reason=(
                    "this log group has no retention period, so logs are kept forever and "
                    "stored volume grows without bound; there is no steady-state monthly "
                    "figure to report, however much is ingested"
                ),
                remedy=(
                    "set RetentionInDays on the log group; unbounded retention is the most "
                    "common source of quietly growing CloudWatch charges"
                ),
                unit="GB-Mo",
            )

        if ingestion is None:
            return context.volume_unknown(
                self.service,
                "Logs-Storage-GB-Month",
                driver="log_ingestion_gb",
                resource=resource,
                why=(
                    f"retained volume depends on ingestion, which is not configured; "
                    f"with {retention}-day retention the steady state is roughly "
                    "ingestion scaled to the retention window"
                ),
                unit="GB-Mo",
            )

        retained = ingestion.quantity.expected * retention / DAYS_PER_MONTH
        return context.priced(
            service=self.service,
            dimension="Logs-Storage-GB-Month",
            key=PriceKey(
                service=self.service,
                dimension="Logs-Storage-GB-Month",
                region=context.region,
            ),
            quantity=retained,
            estimate_type=EstimateType.USAGE_BASED,
            confidence=Confidence.LOW,
            confidence_reasons=(
                "published per-GB-month archived storage rate",
                f"steady state of roughly {retained} GB from {ingestion.quantity.expected} "
                f"GB/month retained for {retention} days",
                "assumes a steady ingestion rate; a growing application retains more",
            ),
            assumptions=(
                _driver_assumption(ingestion, resource),
                Assumption(
                    subject="retained_volume",
                    value=str(retained),
                    provenance=ValueProvenance.BUILTIN_DEFAULT,
                    detail=(
                        f"derived from {ingestion.quantity.expected} GB/month ingestion and "
                        f"{retention}-day retention, assuming a steady rate"
                    ),
                    resource=resource.key,
                ),
            ),
        )


class CloudWatchAlarmEstimator:
    """``AWS::CloudWatch::Alarm``.

    Unusually for CloudWatch, this one is knowable. An alarm is a flat monthly charge
    with no usage component at all, and the template says which rate applies: a
    ``Period`` below sixty seconds selects high resolution, which costs three times as
    much. That is worth surfacing at review time rather than on a bill.

    Alarms are individually trivial - ten cents a month - and a monitoring build-out
    that adds two hundred of them is not. Leaving them unknown understated a cost the
    template fully determines, which is the opposite of this project's usual failure
    mode and just as dishonest.
    """

    resource_types = ("AWS::CloudWatch::Alarm",)
    service = CLOUDWATCH_SERVICE

    HIGH_RESOLUTION_BELOW_SECONDS = 60
    """A Period under a minute makes the alarm high-resolution. AWS documents 10 and 30
    as the supported values; the comparison is written as a threshold so an unexpected
    value is classified rather than ignored."""

    def estimate(
        self, resource: NormalizedResource, context: EstimationContext
    ) -> tuple[DimensionEstimate, ...]:
        """Price one alarm-month at whichever resolution the template selects."""
        period = as_decimal(resource.property_value("Period"))
        assumptions: tuple[Assumption, ...]

        if period is None:
            # Standard is the defensible default: high resolution has to be asked for
            # explicitly, and a Period is a service configuration rather than a usage
            # volume - the kind of default this project does allow, provided it says so.
            resolution = "STANDARD"
            confidence = Confidence.MEDIUM
            assumptions = (
                Assumption(
                    subject="resolution",
                    value="STANDARD",
                    provenance=ValueProvenance.BUILTIN_DEFAULT,
                    detail=(
                        "no resolvable Period, so standard resolution is assumed; a "
                        "high-resolution alarm costs three times as much"
                    ),
                    resource=resource.key,
                ),
            )
            reasons = (
                "flat monthly charge per alarm, with no usage component",
                "resolution assumed standard because Period could not be resolved",
            )
        else:
            high = period < self.HIGH_RESOLUTION_BELOW_SECONDS
            resolution = "HIGH" if high else "STANDARD"
            confidence = Confidence.HIGH
            assumptions = ()
            reasons = (
                "flat monthly charge per alarm, with no usage component",
                f"Period of {period}s selects {'high' if high else 'standard'} resolution",
            )

        return (
            context.priced(
                service=self.service,
                dimension="Alarm-Month",
                key=PriceKey(
                    service=self.service,
                    dimension="Alarm-Month",
                    region=context.region,
                    attributes={"resolution": resolution},
                ),
                quantity=Decimal(1),
                estimate_type=EstimateType.FIXED,
                confidence=confidence,
                confidence_reasons=reasons,
                assumptions=assumptions,
            ),
        )
