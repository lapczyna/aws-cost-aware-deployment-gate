"""The workflows' security properties, asserted rather than described.

The privilege split is the whole design: one workflow runs pull-request code and holds
nothing, the other holds a write token and never runs pull-request code. Both halves
are safe alone and dangerous combined, which makes this exactly the kind of arrangement
that erodes under a well-meaning edit. Documentation does not stop that; a failing test
does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
ACTIONS = ROOT / ".github" / "actions"

ANALYSIS = WORKFLOWS / "cost-gate.yml"
COMMENT = WORKFLOWS / "cost-gate-comment.yml"
DEPLOY = WORKFLOWS / "deploy-example.yml"


def load(path: Path) -> dict[str, Any]:
    """Parse a workflow.

    YAML 1.1 reads a bare ``on:`` key as the boolean ``True``, which is a genuine trap:
    ``document["on"]`` returns nothing and a check written that way silently passes.
    """
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if True in document:
        document["on"] = document.pop(True)
    return document


def steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job.get("steps") or [] if isinstance(step, dict)]


class TestTheAnalysisWorkflowHoldsNothing:
    def test_it_runs_on_pull_request(self):
        assert "pull_request" in load(ANALYSIS)["on"]

    def test_it_grants_no_permissions_at_the_workflow_level(self):
        assert load(ANALYSIS)["permissions"] == {}

    def test_its_job_can_only_read_contents(self):
        # This job runs code from the pull request. Anything more than read access here
        # is an arbitrary-code-execution path into the repository.
        for job in load(ANALYSIS)["jobs"].values():
            assert job.get("permissions") == {"contents": "read"}

    def test_it_references_no_secrets_anywhere(self):
        # The property that makes it safe to run `cdk synth` on pull-request code. A
        # textual check is the right one: any use at all, however indirect, is a defect.
        assert "secrets." not in ANALYSIS.read_text(encoding="utf-8")

    def test_it_cancels_superseded_runs(self):
        # Otherwise a rapid series of pushes produces a queue of contradictory reports
        # racing each other to comment.
        assert load(ANALYSIS)["concurrency"]["cancel-in-progress"] is True

    def test_it_posts_nothing(self):
        text = ANALYSIS.read_text(encoding="utf-8")
        assert "cost-gate comment" not in text
        assert "GITHUB_TOKEN" not in text


class TestTheCommentWorkflowRunsNoPullRequestCode:
    def test_it_runs_on_workflow_run(self):
        assert "workflow_run" in load(COMMENT)["on"]

    def test_it_grants_no_permissions_at_the_workflow_level(self):
        assert load(COMMENT)["permissions"] == {}

    def test_its_job_holds_exactly_the_three_permissions_it_needs(self):
        # This assertion previously demanded a set that could not work: without
        # `contents: read` the checkout fails, and GitHub reports it as "Repository not
        # found" rather than a permission error. A structural test can pin a permission
        # set; only running it proves the set is sufficient.
        for job in load(COMMENT)["jobs"].values():
            assert job.get("permissions") == {
                "contents": "read",
                "pull-requests": "write",
                "actions": "read",
            }

    def test_it_cannot_write_anything_but_pull_request_comments(self):
        # The property that actually matters: read access to a public repository's
        # contents grants nothing, while a second write scope would.
        for job in load(COMMENT)["jobs"].values():
            writes = {
                scope for scope, level in (job.get("permissions") or {}).items() if level == "write"
            }
            assert writes == {"pull-requests"}

    def test_it_never_checks_out_the_triggering_run(self):
        # The hazard this whole arrangement exists to avoid: checking out
        # workflow_run.head_sha here puts the contributor's code in the job holding the
        # write token, which is `pull_request_target` spelled differently.
        for job in load(COMMENT)["jobs"].values():
            for step in steps(job):
                if "actions/checkout" not in str(step.get("uses", "")):
                    continue
                ref = str((step.get("with") or {}).get("ref", ""))
                assert "workflow_run.head" not in ref

    def test_its_checkout_does_not_persist_credentials(self):
        for job in load(COMMENT)["jobs"].values():
            for step in steps(job):
                if "actions/checkout" in str(step.get("uses", "")):
                    assert (step.get("with") or {}).get("persist-credentials") is False

    def test_it_only_acts_on_pull_request_runs(self):
        for job in load(COMMENT)["jobs"].values():
            assert "pull_request" in str(job.get("if", ""))

    def test_it_installs_the_tool_from_the_trusted_checkout(self):
        # The comment body must be rendered by code from the base branch, not by code
        # the pull request supplied.
        assert "pip install --quiet ." in COMMENT.read_text(encoding="utf-8")

    def test_it_passes_the_head_sha_from_the_event(self):
        # The one piece of routing information the untrusted job cannot influence.
        assert "github.event.workflow_run.head_sha" in COMMENT.read_text(encoding="utf-8")


class TestNeitherWorkflowUsesTheProhibitedTrigger:
    @pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")))
    def test_no_workflow_uses_pull_request_target(self, path):
        # ADR 0007. Also enforced by scripts/check_workflows.py in `dev.py all`; both
        # exist because this is the single mistake that would undo the whole design.
        assert "pull_request_target" not in load(path)["on"]


class TestActionsArePinned:
    @pytest.mark.parametrize(
        "path",
        sorted(WORKFLOWS.glob("*.yml")) + sorted(ACTIONS.rglob("*.yml")),
    )
    def test_every_third_party_action_is_pinned_to_a_sha(self, path):
        # A tag is mutable, so `@v4` means trusting whoever can move it, on every run.
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip().removeprefix("- ")
            if not stripped.startswith("uses:"):
                continue
            # The trailing "# v5.4.0" comment is the convention that keeps a pinned
            # SHA readable, so it has to be stripped before checking the revision.
            reference = stripped.split(":", 1)[1].split("#")[0].strip()
            if reference.startswith("."):
                continue  # a local composite action, which is this repository's own code
            _, _, revision = reference.partition("@")
            assert len(revision) == 40, f"{path.name}: {reference} is not pinned"
            assert all(character in "0123456789abcdef" for character in revision)


class TestTheCompositeAction:
    def test_it_exists_and_parses(self):
        assert (ACTIONS / "cost-gate" / "action.yml").is_file()
        assert load(ACTIONS / "cost-gate" / "action.yml")["runs"]["using"] == "composite"

    def test_it_never_calls_the_github_api(self):
        # It has to be usable from a job holding no token at all.
        text = (ACTIONS / "cost-gate" / "action.yml").read_text(encoding="utf-8")
        assert "api.github.com" not in text
        assert "cost-gate comment" not in text

    def test_it_uploads_the_report_even_when_the_gate_fails(self):
        # A blocked change is exactly the one whose report someone needs to read.
        action = load(ACTIONS / "cost-gate" / "action.yml")
        upload = [
            step
            for step in action["runs"]["steps"]
            if "upload-artifact" in str(step.get("uses", ""))
        ]
        assert upload, "the action does not upload a report at all"
        assert upload[0].get("if") == "always()"

    def test_it_exposes_the_decision_as_an_output(self):
        outputs = load(ACTIONS / "cost-gate" / "action.yml")["outputs"]
        assert {"result", "monthly-delta", "unknown-count", "exit-code"} <= set(outputs)


class TestNothingDeploysByAccident:
    """This repository has no AWS account and must not be able to acquire one.

    The example deployment workflow is inert by construction rather than by convention.
    These tests are what make that claim checkable, and they are the reason enabling a
    real deployment has to be a reviewable diff instead of a quiet edit.
    """

    @pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")))
    def test_no_workflow_obtains_aws_credentials(self, path):
        text = path.read_text(encoding="utf-8")
        assert "aws-actions/configure-aws-credentials" not in text
        assert "role-to-assume" not in text

    @pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")))
    def test_no_aws_variable_is_fed_from_a_secret(self, path):
        # ci.yml deliberately sets AWS_* to poison values so an accidental SDK call
        # fails loudly. That is the opposite of configuring a credential, so the check
        # is about where the value comes from, not about the variable name.
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("AWS_"):
                assert "secrets." not in line
                assert "vars." not in line

    @pytest.mark.parametrize(
        "path", [p for p in sorted(WORKFLOWS.glob("*.yml")) if "deploy" in p.stem]
    )
    def test_a_deployment_workflow_never_runs_automatically(self, path):
        # A deployment must be something a person asks for, on a revision they name.
        triggers = set(load(path)["on"])
        assert not triggers & {"push", "pull_request", "schedule"}

    def test_the_example_deployment_deploys_nothing(self):
        assert "deploys nothing" in DEPLOY.read_text(encoding="utf-8")


class TestTheDeploymentOrdering:
    def test_approval_waits_on_the_analysis(self):
        jobs = load(DEPLOY)["jobs"]
        assert "analyse" in jobs["approve"]["needs"]

    def test_deployment_waits_on_both(self):
        assert set(load(DEPLOY)["jobs"]["deploy"]["needs"]) == {"analyse", "approve"}

    def test_approval_uses_a_protected_environment(self):
        # The environment is what actually holds the job until a reviewer acts; the
        # condition alone would only skip it.
        assert "cost-approval" in load(DEPLOY)["jobs"]["approve"]["environment"]

    def test_approval_is_only_requested_when_the_gate_asked_for_it(self):
        # A gate people have to wave through routinely is a gate they stop reading.
        condition = load(DEPLOY)["jobs"]["approve"]["if"]
        assert "REQUIRE_APPROVAL" in condition

    def test_a_blocked_change_never_reaches_the_deploy_job(self):
        condition = load(DEPLOY)["jobs"]["deploy"]["if"]
        assert "!= 'BLOCK'" in condition
        assert "!= 'ERROR'" in condition

    def test_a_failed_approval_blocks_deployment(self):
        # `always()` is needed because `approve` is skipped when no approval was
        # required, but it must not let a *failed* or *cancelled* approval through.
        condition = load(DEPLOY)["jobs"]["deploy"]["if"]
        assert "needs.approve.result == 'success'" in condition
        assert "needs.approve.result == 'skipped'" in condition

    def test_the_deployment_verifies_the_approval_matches_the_change(self):
        # The environment approved a job. This checks that what was approved is what is
        # about to be deployed.
        assert "cost-gate approval check" in DEPLOY.read_text(encoding="utf-8")

    def test_deployments_of_one_environment_do_not_interleave(self):
        concurrency = load(DEPLOY)["concurrency"]
        assert "inputs.environment" in concurrency["group"]
        # Interrupting a deployment half-way is worse than queueing behind one.
        assert concurrency["cancel-in-progress"] is False
