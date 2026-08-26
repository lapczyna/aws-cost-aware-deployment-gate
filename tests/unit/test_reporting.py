"""What a report must never do.

Most of these assert absences: a number that is not shown, a section that is not
collapsed, a total that does not silently include an unknown. Those are the failures
that would make the tool confidently wrong, which is worse than it being unavailable.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from cost_gate.domain.enums import Confidence, EstimateType, GateResult, Severity
from cost_gate.domain.money import Money
from cost_gate.reporting.markdown import (
    COMMENT_MARKER,
    MAX_COMMENT_BYTES,
    _fit,
    render_markdown,
)
from cost_gate.reporting.reconcile import reconcile_artifact, reconcile_report
from tests.factories import (
    artifact_with,
    component,
    cost_report,
    decision_with,
    reason,
    usd,
)

pytestmark = pytest.mark.unit


class TestUnknownsAreNeverHidden:
    def test_an_unknown_component_is_named_in_the_report(self):
        artifact = artifact_with(
            components=[component(logical_id="Mystery", unknown="instance type")]
        )
        assert "Mystery" in render_markdown(artifact)

    def test_the_unknown_section_is_not_collapsed(self):
        # Everything else may be folded into <details>. This may not: a reader who
        # skims must still see that the estimate is incomplete.
        artifact = artifact_with(
            components=[component(logical_id="Mystery", unknown="instance type")]
        )
        markdown = render_markdown(artifact)
        _head, _, tail = markdown.partition("could not be established")
        assert "<details>" not in tail.split("\n\n")[0]
        assert "could not be established" in markdown

    def test_an_unknown_is_not_rendered_as_a_zero(self):
        artifact = artifact_with(
            components=[component(logical_id="Mystery", unknown="instance type")]
        )
        markdown = render_markdown(artifact)
        assert "$0.00" not in markdown.split("could not be established")[1][:400]

    def test_a_total_beside_unknowns_is_marked_as_a_lower_bound(self):
        artifact = artifact_with(
            components=[
                component(logical_id="Nat", delta="32.40"),
                component(logical_id="Mystery", unknown="instance type"),
            ]
        )
        markdown = render_markdown(artifact)
        assert "at least" in markdown or "excludes" in markdown


class TestTheCommentCanBeFoundAgain:
    def test_the_marker_is_present_so_a_comment_can_be_updated(self):
        # Without a stable marker every push posts a new comment.
        assert COMMENT_MARKER in render_markdown(artifact_with())

    def test_the_marker_comes_first(self):
        assert render_markdown(artifact_with()).startswith(COMMENT_MARKER)


class TestSizeIsBounded:
    def test_a_huge_change_still_fits_in_a_comment(self):
        artifact = artifact_with(
            components=[component(logical_id=f"Resource{i}", delta="1.00") for i in range(600)]
        )
        rendered = render_markdown(artifact)
        assert len(rendered.encode("utf-8")) <= MAX_COMMENT_BYTES

    def test_the_unknown_count_is_never_truncated_even_when_the_list_is(self):
        # The enumeration may be cut. The number may not: "3 costs unknown" and
        # "600 costs unknown" are different decisions, and the reader needs the second.
        artifact = artifact_with(
            components=[
                component(logical_id=f"Resource{i}", unknown="instance type") for i in range(600)
            ]
        )
        rendered = render_markdown(artifact)
        assert "600 cost(s) could not be established" in rendered
        assert "and 585 more" in rendered

    def test_an_oversized_body_is_cut_with_a_visible_notice(self):
        # _fit is the last line of defence; GitHub rejects the comment outright above
        # its own limit, so a silent overrun would lose the whole report.
        oversized = "x" * (MAX_COMMENT_BYTES + 5000)
        fitted = _fit(oversized, "build/report.json")
        assert len(fitted.encode("utf-8")) <= MAX_COMMENT_BYTES
        assert "truncated" in fitted
        # Escaped, because the path came from the command line.
        assert "report" in fitted.rsplit("artifact", 1)[1]

    def test_a_hostile_logical_id_cannot_inject_markup(self):
        artifact = artifact_with(
            components=[component(logical_id="<img src=x onerror=alert(1)>", delta="1.00")]
        )
        rendered = render_markdown(artifact).replace(COMMENT_MARKER, "")
        assert "<img" not in rendered


class TestTheHeadlineMatchesTheDecision:
    @pytest.mark.parametrize(
        ("result", "headline"),
        [
            (GateResult.PASS, "Passed"),
            (GateResult.WARN, "Passed with warnings"),
            (GateResult.REQUIRE_APPROVAL, "Approval required"),
            (GateResult.BLOCK, "Blocked"),
        ],
    )
    def test_the_verdict_is_in_the_heading(self, result, headline):
        # The heading is what a reviewer reads; it must not require interpreting an
        # enum name or scrolling to a table.
        artifact = artifact_with(decision=decision_with(result=result))
        assert headline in render_markdown(artifact).splitlines()[2]

    def test_every_reason_is_shown(self):
        artifact = artifact_with(
            decision=decision_with(
                result=GateResult.BLOCK,
                reasons=[
                    reason("the delta exceeds the production budget", Severity.CRITICAL),
                    reason("three resources could not be priced", Severity.HIGH),
                ],
            )
        )
        rendered = render_markdown(artifact)
        assert "exceeds the production budget" in rendered
        assert "could not be priced" in rendered


class TestProvenanceIsAlwaysStated:
    def test_the_disclaimer_survives_into_the_report(self):
        # A reader must never mistake this for a quote.
        assert "illustrative" in render_markdown(artifact_with()).lower()

    def test_the_capture_date_is_shown(self):
        # Rates six months old are a different claim from rates captured last week, and
        # nobody opens the JSON artifact to find out which. Backslashes are stripped
        # because how the date is escaped is not what this test is about.
        assert "2026-01-15" in render_markdown(artifact_with()).replace(chr(92), "")

    def test_the_monthly_hours_convention_is_shown(self):
        assert "730" in render_markdown(artifact_with())


class TestReconciliation:
    def test_a_report_whose_totals_match_its_components_passes(self):
        report = cost_report([component(logical_id="Nat", delta="32.40")])
        assert reconcile_report(report) == []

    def test_a_tampered_total_is_caught(self):
        # This is the check that would catch an estimator quietly dropping a component.
        report = cost_report([component(logical_id="Nat", delta="32.40")])
        broken = report.model_copy(
            update={
                "totals": report.totals.model_copy(
                    update={"monthly_delta": Money(amount=Decimal("99.99"), currency="USD")}
                )
            }
        )
        assert reconcile_report(broken)

    def test_an_unknown_counted_as_known_is_caught(self):
        report = cost_report([component(logical_id="Mystery", unknown="instance type")])
        broken = report.model_copy(
            update={"totals": report.totals.model_copy(update={"unknown_component_count": 0})}
        )
        assert reconcile_report(broken)

    def test_current_plus_delta_equals_proposed(self):
        report = cost_report(
            [component(logical_id="Db", current="10.00", proposed="25.00", delta="15.00")]
        )
        assert reconcile_report(report) == []

    def test_a_delta_that_does_not_bridge_the_two_states_is_caught(self):
        report = cost_report(
            [component(logical_id="Db", current="10.00", proposed="25.00", delta="15.00")]
        )
        wrong = report.components[0].model_copy(update={"monthly_delta": usd("1.00")})
        broken = report.model_copy(update={"components": [wrong]})
        assert reconcile_report(broken)

    def test_the_artifact_check_includes_the_report_check(self):
        artifact = artifact_with(components=[component(logical_id="Nat", delta="32.40")])
        assert reconcile_artifact(artifact) == []


class TestEstimateTypesAreDistinguished:
    def test_a_usage_based_estimate_is_labelled_as_such(self):
        # A reader must be able to tell "this will cost $32" from "this could cost $32
        # if the traffic assumption holds".
        artifact = artifact_with(
            components=[
                component(
                    logical_id="Function",
                    delta="4.00",
                    estimate_type=EstimateType.USAGE_BASED,
                    confidence=Confidence.LOW,
                )
            ]
        )
        rendered = render_markdown(artifact)
        assert "usage" in rendered.lower()
