"""Analysing CDK output.

Split in two on purpose:

* the default tests read the **committed** templates under
  ``examples/cdk/synthesized/``, so they need neither Node nor ``aws-cdk-lib`` and run
  in milliseconds;
* the ``cdk`` marker tests run a real ``cdk synth`` and check that the committed
  templates still match what the app produces.

Without the second group the committed templates would slowly become fiction. Without
the first, the whole suite would depend on a Node toolchain to test a Python library
that never imports one.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from cost_gate.adapters.cdk import (
    CdkError,
    copy_templates,
    find_cdk_executable,
    stack_name,
    synthesize,
)
from cost_gate.adapters.clock import FixedClock
from cost_gate.config import load_config
from cost_gate.diff.matching import match_resources
from cost_gate.domain.enums import MatchMethod
from cost_gate.parsers import load_graph
from cost_gate.pipeline import AnalysisRequest, run_analysis

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "examples" / "cdk"
BASELINE = APP / "synthesized" / "baseline"
PROPOSED = APP / "synthesized" / "proposed"


def analyse():
    """Run the gate over the committed CDK templates."""
    return run_analysis(
        AnalysisRequest(
            baseline=BASELINE,
            proposed=PROPOSED,
            config=load_config(ROOT / "examples" / "config" / "cost-gate.yaml"),
            environment="development",
            application="payments",
            catalog=ROOT / "pricing-data",
            clock=FixedClock(),
            tool_version="0.1.0",
        )
    )


class TestSynthesisedTemplatesLoad:
    def test_both_snapshots_are_multi_stack(self):
        # A single-stack example would not exercise the part of matching that is scoped
        # per stack, which is where CDK problems actually show up.
        assert len(list(BASELINE.glob("*.json"))) == 2
        assert len(list(PROPOSED.glob("*.json"))) == 2

    def test_stacks_are_named_after_their_files(self):
        graph = load_graph(PROPOSED)
        assert {"PaymentsNetwork", "PaymentsWorkload"} == {r.key.stack for r in graph.resources}

    def test_every_resource_carries_a_construct_path(self):
        # This is what makes CDK tractable. If synthesis ever stops emitting
        # aws:cdk:path, matching silently degrades to logical IDs and every hash change
        # becomes a phantom delete and create.
        graph = load_graph(PROPOSED)
        assert graph.resources
        assert all(resource.construct_path for resource in graph.resources)

    def test_the_metadata_resource_is_not_reported_as_unknown(self):
        # CDK puts one in every stack. Reporting it would put noise at the top of every
        # report, which is how a reader learns to skip the unknowns section.
        artifact = analyse()
        unknown_ids = {c.resource.logical_id for c in artifact.cost.components if c.is_unknown}
        assert not any("CDKMetadata" in identifier for identifier in unknown_ids)


class TestMatching:
    def test_resources_are_matched_by_construct_path(self):
        matches = match_resources(load_graph(BASELINE), load_graph(PROPOSED))
        methods = {match.method for match in matches.matches}
        assert methods == {MatchMethod.CONSTRUCT_PATH}

    def test_the_database_is_modified_rather_than_replaced_wholesale(self):
        # The instance class and multi-AZ both change. If matching failed this would
        # appear as a delete plus a create, and the delta would be nonsense.
        artifact = analyse()
        assert artifact.changes.modified > 0
        assert artifact.changes.removed == 0


class TestTheAnalysis:
    def test_the_growth_change_costs_money(self):
        artifact = analyse()
        assert artifact.cost.totals.monthly_delta.amount > 0

    def test_the_nat_gateway_is_found(self):
        artifact = analyse()
        dimensions = {c.pricing_dimension for c in artifact.cost.components}
        assert "NatGateway-Hours" in dimensions

    def test_unsupported_types_are_visible_rather_than_ignored(self):
        # A real CDK app pulls in resources this tool does not price - Secrets Manager,
        # custom resources, ElastiCache. Saying so is the honest answer; quietly
        # omitting them would understate the change.
        artifact = analyse()
        assert artifact.cost.totals.unknown_component_count > 0
        assert artifact.decision.unknowns.inputs

    def test_no_real_account_id_appears_in_the_templates(self):
        # Synthesised templates are committed, so anything the app resolved at synth
        # time is now public. The only twelve-digit sequence allowed is the placeholder.
        for template in list(BASELINE.glob("*.json")) + list(PROPOSED.glob("*.json")):
            found = set(re.findall(r"\b\d{12}\b", template.read_text(encoding="utf-8")))
            assert found <= {"000000000000"}, f"{template.name} contains {found}"


class TestCopyingTemplates:
    def test_templates_are_renamed_after_their_stack(self, tmp_path):
        source = tmp_path / "assembly"
        source.mkdir()
        template = source / "MyStack.template.json"
        template.write_text('{"Resources": {}}', encoding="utf-8")
        written = copy_templates([template], tmp_path / "out")
        assert written == [tmp_path / "out" / "MyStack.json"]

    def test_the_stack_name_survives_the_copy(self):
        # The diff engine scopes matching to a stack, so losing the name here would
        # break pairing across the two snapshots.
        assert stack_name(Path("PaymentsWorkload.template.json")) == "PaymentsWorkload"

    def test_invalid_json_is_refused(self, tmp_path):
        source = tmp_path / "assembly"
        source.mkdir()
        template = source / "Broken.template.json"
        template.write_text("{not json", encoding="utf-8")
        with pytest.raises(CdkError, match="not valid JSON"):
            copy_templates([template], tmp_path / "out")

    def test_an_oversized_template_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cost_gate.adapters.cdk.MAX_TEMPLATE_BYTES", 10)
        source = tmp_path / "assembly"
        source.mkdir()
        template = source / "Big.template.json"
        template.write_text('{"Resources": {}}' + " " * 100, encoding="utf-8")
        with pytest.raises(CdkError, match="maximum"):
            copy_templates([template], tmp_path / "out")

    def test_copied_templates_use_unix_line_endings(self, tmp_path):
        source = tmp_path / "assembly"
        source.mkdir()
        template = source / "MyStack.template.json"
        template.write_text('{\n  "Resources": {}\n}', encoding="utf-8", newline="\n")
        written = copy_templates([template], tmp_path / "out")
        assert b"\r\n" not in written[0].read_bytes()


class TestSynthesisFailures:
    def test_a_directory_without_cdk_json_is_refused(self, tmp_path):
        if find_cdk_executable() is None:
            pytest.skip("the CDK CLI is not installed")
        with pytest.raises(CdkError, match=r"cdk\.json"):
            synthesize(tmp_path, tmp_path / "out")


@pytest.mark.cdk
class TestRealSynthesis:
    """Opt-in: these run a real ``cdk synth``. Enable with ``-m cdk``."""

    @pytest.fixture(autouse=True)
    def _requires_toolchain(self):
        if find_cdk_executable() is None:
            pytest.skip("the CDK CLI is not installed")
        if shutil.which("node") is None:
            pytest.skip("Node is not installed")
        pytest.importorskip("aws_cdk", reason="install the [cdk] extra")

    def synth(self, tmp_path: Path, growth: str) -> Path:
        assembly = tmp_path / f"assembly-{growth}"
        templates = synthesize(APP, assembly, context={"growth": growth})
        return Path(copy_templates(templates, tmp_path / growth)[0]).parent

    def test_the_committed_baseline_matches_a_fresh_synth(self, tmp_path):
        # Without this the committed templates would slowly become fiction.
        produced = self.synth(tmp_path, "false")
        for expected in sorted(BASELINE.glob("*.json")):
            actual = produced / expected.name
            assert actual.is_file(), f"{expected.name} was not synthesised"
            assert json.loads(actual.read_text("utf-8")) == json.loads(
                expected.read_text("utf-8")
            ), f"{expected.name} differs; regenerate with `python scripts/dev.py synth`"

    def test_the_committed_proposal_matches_a_fresh_synth(self, tmp_path):
        produced = self.synth(tmp_path, "true")
        for expected in sorted(PROPOSED.glob("*.json")):
            actual = produced / expected.name
            assert actual.is_file(), f"{expected.name} was not synthesised"
            assert json.loads(actual.read_text("utf-8")) == json.loads(
                expected.read_text("utf-8")
            ), f"{expected.name} differs; regenerate with `python scripts/dev.py synth`"

    def test_synthesis_is_deterministic(self, tmp_path):
        first = self.synth(tmp_path / "a", "true")
        second = self.synth(tmp_path / "b", "true")
        for template in sorted(first.glob("*.json")):
            assert json.loads(template.read_text("utf-8")) == json.loads(
                (second / template.name).read_text("utf-8")
            )
