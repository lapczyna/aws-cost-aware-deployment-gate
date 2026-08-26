"""The console report.

Two properties matter beyond "it prints something":

* everything goes to **stderr**, so that ``--format json`` can be redirected without
  the human-readable report contaminating the machine-readable one;
* an unknown is never printed as a number, in any renderer.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from cost_gate.domain.enums import GateResult
from cost_gate.reporting.console import render_console
from tests.factories import artifact_with, component, decision_with

pytestmark = pytest.mark.unit


def rendered(artifact, *, verbose: bool = False) -> str:
    """Render to a fixed-width buffer so wrapping cannot vary by terminal."""
    console = Console(file=__import__("io").StringIO(), width=100, no_color=True)
    render_console(artifact, console, verbose=verbose)
    return console.file.getvalue()


class TestTheVerdictIsVisible:
    @pytest.mark.parametrize(
        "result",
        [GateResult.PASS, GateResult.WARN, GateResult.REQUIRE_APPROVAL, GateResult.BLOCK],
    )
    def test_the_result_is_printed(self, result):
        output = rendered(artifact_with(decision=decision_with(result=result)))
        assert result.value in output

    def test_the_monthly_delta_is_printed(self):
        artifact = artifact_with(components=[component(logical_id="Nat", delta="32.40")])
        assert "32.40" in rendered(artifact)


class TestUnknownsAreVisibleHereToo:
    def test_an_unknown_resource_is_named(self):
        artifact = artifact_with(
            components=[component(logical_id="Mystery", unknown="instance type")]
        )
        assert "Mystery" in rendered(artifact)

    def test_the_reason_is_given(self):
        artifact = artifact_with(
            components=[component(logical_id="Mystery", unknown="instance type")]
        )
        assert "instance type" in rendered(artifact)

    def test_an_unknown_is_not_printed_as_a_number(self):
        artifact = artifact_with(
            components=[component(logical_id="Mystery", unknown="instance type")]
        )
        output = rendered(artifact)
        unknown_block = output[output.index("Mystery") :][:200]
        assert "$0.00" not in unknown_block


class TestStreamDiscipline:
    def test_nothing_is_written_to_stdout(self, capsys):
        # `cost-gate analyze --format json > report.json` must produce a parseable
        # file, which it cannot if the console report shares the stream.
        render_console(
            artifact_with(components=[component(logical_id="Nat", delta="32.40")]),
            Console(stderr=True, no_color=True),
            verbose=False,
        )
        assert capsys.readouterr().out == ""


class TestVerbosity:
    def test_assumptions_are_hidden_by_default(self):
        # The default report has to fit on a screen or it will not be read.
        artifact = artifact_with(components=[component(logical_id="Nat", delta="32.40")])
        assert len(rendered(artifact)) <= len(rendered(artifact, verbose=True))

    def test_verbose_shows_the_confidence_reasoning(self):
        artifact = artifact_with(components=[component(logical_id="Nat", delta="32.40")])
        assert "published hourly rate" in rendered(artifact, verbose=True)
