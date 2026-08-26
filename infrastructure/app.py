"""Optional AWS infrastructure for the cost gate.

**This is never deployed.** There is no account behind this repository, nothing here
obtains AWS credentials, and `scripts/check_workflows.py` fails the build if anything
tries. It exists to be synthesised, asserted against, and — the point of the exercise —
analysed by the gate itself.

What it would do, if someone chose to deploy it:

* keep dated **pricing snapshots** in S3, so a report can be reproduced months later
  against the rates it actually used;
* record **predictions** in DynamoDB, so the estimate a pull request produced can later
  be compared against the bill (Phase 17);
* **refresh the catalog** on a schedule, via a Lambda that holds read-only access to
  the Price List API and write access to one bucket prefix;
* watch its own spend with an **AWS Budget** and alarm on the refresher failing.

Two design constraints run through all of it.

**It must cost nothing at idle.** A demonstration that is expensive to leave running is
a demonstration nobody leaves running, and a cost tool whose own infrastructure is
wasteful is not a good advertisement. So: on-demand DynamoDB rather than provisioned
capacity, S3 with lifecycle rules rather than unbounded retention, Lambda on a schedule
rather than anything always-on, and no NAT Gateway anywhere — the exact shape this tool
spends its time warning people about.

**Its permissions must be boring.** The refresher can read public price lists and write
to one prefix of one bucket. It cannot read the prediction table, delete a snapshot, or
reach anything else. Least privilege is easy to claim and easy to check here, because
there is so little to grant.

Estimated cost and a teardown procedure are in `docs/infrastructure.md`.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import Duration, RemovalPolicy, Tags
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cloudwatch_actions
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from constructs import Construct

REGION = "us-east-1"
ACCOUNT = "000000000000"
"""A placeholder. Never a real account ID, here or in any fixture."""

MONTHLY_BUDGET_USD = 5
"""What this whole arrangement is expected to cost. If it ever exceeds this, something
is wrong with the design rather than with the budget."""

SNAPSHOT_PREFIX = "catalogs/"
"""The only prefix the refresher may write to."""


class StorageStack(cdk.Stack):
    """Where pricing snapshots and prediction records live.

    Both are cheap by construction rather than by discipline: the bucket expires old
    snapshots on a lifecycle rule, and the table is on-demand so an idle month costs
    only what is stored.
    """

    def __init__(self, scope: Construct, identifier: str, **kwargs) -> None:
        super().__init__(scope, identifier, **kwargs)

        self.snapshots = s3.Bucket(
            self,
            "PricingSnapshots",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            # A snapshot is only useful while a report that used it might still be
            # examined. Keeping them forever turns a small bucket into a slowly growing
            # bill nobody notices - which is the failure mode this project is about.
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-old-snapshots",
                    enabled=True,
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.INFREQUENT_ACCESS,
                            transition_after=Duration.days(30),
                        )
                    ],
                    expiration=Duration.days(365),
                    noncurrent_version_expiration=Duration.days(30),
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                )
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.predictions = dynamodb.Table(
            self,
            "Predictions",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            # On-demand, not provisioned. The write rate is one record per analysed
            # pull request, which is far below the point where provisioned capacity
            # becomes cheaper, and provisioning would mean paying while idle.
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            # Predictions age out on their own. Accuracy data older than a year says
            # more about a previous version of the tool than about this one.
            time_to_live_attribute="expires_at",
            removal_policy=RemovalPolicy.RETAIN,
        )


class RefreshStack(cdk.Stack):
    """The scheduled job that refreshes the pricing catalog.

    The IAM here is the part worth reading. The function may read public price lists and
    write to one prefix of one bucket. It cannot read predictions, delete a snapshot, or
    touch anything else in the account.
    """

    def __init__(
        self,
        scope: Construct,
        identifier: str,
        *,
        snapshots: s3.IBucket,
        **kwargs,
    ) -> None:
        super().__init__(scope, identifier, **kwargs)

        # Retention is explicit. A log group left on "never expire" is one of the
        # commonest ways a serverless system acquires a bill nobody budgeted for, and
        # it would be embarrassing for this tool in particular to make that mistake.
        log_group = logs.LogGroup(
            self,
            "RefreshLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.function = lambda_.Function(
            self,
            "RefreshCatalog",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            # Inline rather than a bundled asset: bundling needs Docker or esbuild,
            # which would make synthesis non-deterministic and stop the committed
            # templates from being comparable. The real refresher is
            # `cost-gate pricing refresh`; this is the scheduling around it.
            code=lambda_.Code.from_inline(
                "def handler(event, context):\n"
                '    """Placeholder. The refresh logic lives in cost_gate.pricing."""\n'
                "    raise NotImplementedError(\n"
                '        "deploy this with the cost-gate package bundled as a layer"\n'
                "    )\n"
            ),
            memory_size=512,
            timeout=Duration.minutes(5),
            log_group=log_group,
            environment={
                "SNAPSHOT_BUCKET": snapshots.bucket_name,
                "SNAPSHOT_PREFIX": SNAPSHOT_PREFIX,
            },
        )

        # Read-only, and only the two calls a catalog refresh actually makes. The
        # Price List API has no resource-level permissions, so the resource is "*" and
        # the narrowing has to come from the action list.
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["pricing:GetProducts", "pricing:DescribeServices"],
                resources=["*"],
            )
        )
        # Write, but only under one prefix, and no delete. A refresher that can remove
        # snapshots can destroy the audit trail it exists to create.
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["s3:PutObject"],
                resources=[snapshots.arn_for_objects(f"{SNAPSHOT_PREFIX}*")],
            )
        )

        events.Rule(
            self,
            "WeeklyRefresh",
            # Weekly, not daily. List prices change rarely, every run writes a snapshot,
            # and a schedule tighter than the data changes is just cost.
            schedule=events.Schedule.cron(minute="0", hour="3", week_day="MON"),
            targets=[targets.LambdaFunction(self.function, retry_attempts=2)],
        )


class ObservabilityStack(cdk.Stack):
    """Watching the refresher, and watching the spend.

    A budget on infrastructure whose purpose is watching budgets is not a joke: this is
    the same argument the tool makes to its users, and applying it here is the cheapest
    way to find out whether the advice is any good.
    """

    def __init__(
        self,
        scope: Construct,
        identifier: str,
        *,
        refresher: lambda_.IFunction,
        notification_email: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, identifier, **kwargs)

        self.alarms = sns.Topic(self, "CostGateAlarms", display_name="cost-gate alarms")

        # A failed refresh is not urgent, but a silently failed refresh means the
        # catalog quietly goes stale and every later report is confidently wrong.
        failures = cloudwatch.Alarm(
            self,
            "RefreshFailed",
            metric=refresher.metric_errors(period=Duration.hours(1), statistic="Sum"),
            threshold=1,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description=(
                "The pricing refresher failed. Reports will use the previous catalog, "
                "which is safe but ages."
            ),
        )
        failures.add_alarm_action(cloudwatch_actions.SnsAction(self.alarms))

        budgets.CfnBudget(
            self,
            "MonthlyBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_type="COST",
                time_unit="MONTHLY",
                budget_name="cost-gate-infrastructure",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=MONTHLY_BUDGET_USD, unit="USD"),
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        comparison_operator="GREATER_THAN",
                        notification_type="ACTUAL",
                        threshold=80,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            address=notification_email, subscription_type="EMAIL"
                        )
                    ],
                )
            ],
        )


def build(app: cdk.App) -> None:
    """Define the stacks."""
    environment = cdk.Environment(account=ACCOUNT, region=REGION)
    # A placeholder, overridable by context. Never a real address in a committed
    # template: synthesised output is public, and an email in it is an email harvested.
    notification_email = app.node.try_get_context("notificationEmail") or (
        "cost-gate-alarms@example.invalid"
    )

    storage = StorageStack(app, "CostGateStorage", env=environment)
    refresh = RefreshStack(app, "CostGateRefresh", snapshots=storage.snapshots, env=environment)
    observability = ObservabilityStack(
        app,
        "CostGateObservability",
        refresher=refresh.function,
        notification_email=notification_email,
        env=environment,
    )

    for stack in (storage, refresh, observability):
        Tags.of(stack).add("Environment", "production")
        Tags.of(stack).add("Application", "cost-gate")
        Tags.of(stack).add("Team", "payments-platform")

    refresh.add_stack_dependency(storage)
    observability.add_stack_dependency(refresh)


app = cdk.App()
build(app)
app.synth()
