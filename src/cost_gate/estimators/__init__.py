"""Per-service cost estimators, registered by AWS resource type.

An estimator prices a resource **state**, never a change (ADR 0003). The engine calls
each one twice — for the baseline and the proposal — under one usage profile and one
pricing snapshot, then subtracts. Estimators therefore never reason about additions or
removals and cannot get a sign wrong.

Coverage lives in the registry, and `cost-gate list-supported-resources` reads it
directly, so the tool cannot overstate what it can price.
"""

from __future__ import annotations

from cost_gate.estimators.base import (
    DimensionEstimate,
    EstimationContext,
    Estimator,
    RuntimeBasis,
    unknown,
)
from cost_gate.estimators.compute import (
    EbsVolumeEstimator,
    Ec2InstanceEstimator,
    EksClusterEstimator,
)
from cost_gate.estimators.database import RdsInstanceEstimator
from cost_gate.estimators.engine import estimate_graphs, estimate_resource
from cost_gate.estimators.network import (
    ElasticIpEstimator,
    LoadBalancerEstimator,
    NatGatewayEstimator,
)
from cost_gate.estimators.registry import (
    COST_FREE_TYPES,
    EstimatorRegistry,
    UnsupportedResourceEstimator,
    default_registry,
)
from cost_gate.estimators.serverless import (
    ApiGatewayEstimator,
    LambdaFunctionEstimator,
    RestApiEstimator,
)
from cost_gate.estimators.storage import (
    CloudWatchAlarmEstimator,
    CloudWatchLogsEstimator,
    DynamoDbTableEstimator,
    S3BucketEstimator,
)

__all__ = [
    "COST_FREE_TYPES",
    "ApiGatewayEstimator",
    "CloudWatchAlarmEstimator",
    "CloudWatchLogsEstimator",
    "DimensionEstimate",
    "DynamoDbTableEstimator",
    "EbsVolumeEstimator",
    "Ec2InstanceEstimator",
    "EksClusterEstimator",
    "ElasticIpEstimator",
    "EstimationContext",
    "Estimator",
    "EstimatorRegistry",
    "LambdaFunctionEstimator",
    "LoadBalancerEstimator",
    "NatGatewayEstimator",
    "RdsInstanceEstimator",
    "RestApiEstimator",
    "RuntimeBasis",
    "S3BucketEstimator",
    "UnsupportedResourceEstimator",
    "default_registry",
    "estimate_graphs",
    "estimate_resource",
    "unknown",
]
