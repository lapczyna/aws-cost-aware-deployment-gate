"""A two-stack CDK application, used to demonstrate analysing CDK changes.

This app is synthesised, never deployed. There is no account behind it.

The ``growth`` context flag selects between the two versions of the application, so
that both are built from *the same constructs*. Two separate apps would produce two
unrelated sets of logical IDs, and would demonstrate nothing about the matching problem
that makes CDK interesting to a cost tool:

    cdk synth --context growth=false    # the baseline
    cdk synth --context growth=true     # the proposal

Environments are hard-coded to us-east-1 because the bundled pricing catalog covers
that region, and because an app that resolves its region from the ambient environment
synthesises differently on different machines, which would defeat the golden files.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import Tags
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticache as elasticache
from aws_cdk import aws_rds as rds
from constructs import Construct

REGION = "us-east-1"
ACCOUNT = "000000000000"
"""A placeholder. Never a real account ID, in this repository or any fixture."""


class NetworkStack(cdk.Stack):
    """The VPC, and whether anything in it can reach the internet.

    In the baseline the private subnets are *isolated*: no NAT Gateway, no egress, and
    nothing billed by the hour. The proposal gives them egress, which is the single
    most common way a development environment acquires a permanent cost that no one
    reviewed as a cost.
    """

    def __init__(self, scope: Construct, identifier: str, *, growth: bool, **kwargs) -> None:
        super().__init__(scope, identifier, **kwargs)

        subnet_configuration = [
            ec2.SubnetConfiguration(
                name="Private",
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                if growth
                else ec2.SubnetType.PRIVATE_ISOLATED,
                cidr_mask=24,
            )
        ]
        if growth:
            subnet_configuration.insert(
                0,
                ec2.SubnetConfiguration(
                    name="Public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                ),
            )

        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            ip_addresses=ec2.IpAddresses.cidr("10.0.0.0/16"),
            max_azs=2,
            # One NAT Gateway rather than one per availability zone: the point is to
            # show the cost appearing at all, not to make it look worse than it is.
            nat_gateways=1 if growth else 0,
            subnet_configuration=subnet_configuration,
        )


class WorkloadStack(cdk.Stack):
    """The application itself: compute, a database, and optionally a cache.

    Every resource here is a cross-stack reference to the network stack, which is worth
    exercising: CDK renders those as ``Fn::ImportValue``, and an unresolvable import is
    exactly the kind of thing a cost tool must report as unknown rather than guess.
    """

    def __init__(
        self,
        scope: Construct,
        identifier: str,
        *,
        vpc: ec2.IVpc,
        growth: bool,
        **kwargs,
    ) -> None:
        super().__init__(scope, identifier, **kwargs)

        # The subnet type has to be named explicitly, because it differs between the
        # two versions and CDK's default selection would fail against the baseline's
        # isolated subnets. This is the kind of coupling that makes "just change one
        # flag" harder in real apps than it looks.
        placement = ec2.SubnetSelection(
            subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            if growth
            else ec2.SubnetType.PRIVATE_ISOLATED
        )

        ec2.Instance(
            self,
            "Api",
            vpc=vpc,
            vpc_subnets=placement,
            instance_type=ec2.InstanceType("t3.large" if growth else "t3.small"),
            machine_image=ec2.MachineImage.generic_linux({REGION: "ami-0demo00000000000"}),
        )

        rds.DatabaseInstance(
            self,
            "Database",
            vpc=vpc,
            vpc_subnets=placement,
            engine=rds.DatabaseInstanceEngine.postgres(version=rds.PostgresEngineVersion.VER_16_3),
            instance_type=ec2.InstanceType("t3.medium" if growth else "t3.small"),
            allocated_storage=100,
            storage_type=rds.StorageType.GP3,
            multi_az=growth,
            backup_retention=cdk.Duration.days(7),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        if growth:
            # Deliberately a type the tool does not price. A CDK app that only ever
            # used supported resources would give a misleading impression of coverage.
            subnet_group = elasticache.CfnSubnetGroup(
                self,
                "CacheSubnets",
                description="Session cache",
                subnet_ids=vpc.select_subnets(subnet_type=placement.subnet_type).subnet_ids,
            )
            cache = elasticache.CfnCacheCluster(
                self,
                "Cache",
                cache_node_type="cache.t3.micro",
                engine="redis",
                num_cache_nodes=1,
                cache_subnet_group_name=subnet_group.ref,
            )
            cache.add_dependency(subnet_group)


def build(app: cdk.App) -> None:
    """Define the stacks."""
    # Context arrives as a string from the command line and as a bool from cdk.json,
    # so both have to be accepted. Defaulting to the baseline means a bare `cdk synth`
    # produces the "before" picture.
    raw = app.node.try_get_context("growth")
    growth = raw is True or (isinstance(raw, str) and raw.lower() in {"true", "1", "yes"})

    environment = cdk.Environment(account=ACCOUNT, region=REGION)
    network = NetworkStack(app, "PaymentsNetwork", growth=growth, env=environment)
    workload = WorkloadStack(
        app, "PaymentsWorkload", vpc=network.vpc, growth=growth, env=environment
    )

    # Attribution the gate's budgets and policies match on. Tagging at the app level
    # means every resource carries it, which is the arrangement worth demonstrating.
    for stack in (network, workload):
        Tags.of(stack).add("Environment", "development")
        Tags.of(stack).add("Application", "payments")
    workload.add_dependency(network)


app = cdk.App()
build(app)
app.synth()
