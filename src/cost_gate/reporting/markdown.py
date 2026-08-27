"""The pull-request report.

Constraints this renderer works under, which shape most of its decisions:

* **A GitHub comment has a hard 65,536-character limit.** A large change must not
  produce a comment that fails to post, so the body is budgeted: the summary is always
  present, details are collapsed, and long tables are truncated with a visible marker
  and a pointer to the full artifact. Truncation is never silent — a reader must know
  they are looking at a summary.
* **Every value from a template is untrusted.** Logical IDs, tag values and intrinsic
  expressions all pass through :mod:`cost_gate.reporting.escaping`.
* **The reader is a developer in a hurry.** The decision and its reasons come first,
  then the number, then the evidence. Anything a reviewer would not read on the way to
  approving or rejecting goes inside a collapsed section.

The footer always states where the prices came from. A report built on unverified
rates that does not say so is the failure mode this whole project exists to avoid.
"""

from __future__ import annotations

from collections.abc import Sequence

from cost_gate.config.money_value import format_percent
from cost_gate.domain.artifact import AnalysisArtifact
from cost_gate.domain.cost import CostComponent
from cost_gate.domain.decision import BudgetEvaluation, PolicyEvaluation
from cost_gate.domain.enums import GateResult
from cost_gate.reporting.escaping import code, escape_markdown, table_cell

__all__ = ["COMMENT_MARKER", "MAX_COMMENT_BYTES", "render_markdown"]

COMMENT_MARKER = "<!-- cost-gate:report:v1 -->"
"""Hidden marker used to find and update this comment instead of posting a new one."""

MAX_COMMENT_BYTES = 60_000
"""Budget for the whole body, below GitHub's 65,536 limit with room to spare."""

MAX_TABLE_ROWS = 15

_HEADLINES: dict[GateResult, tuple[str, str]] = {
    GateResult.PASS: ("✅", "Passed"),
    GateResult.WARN: ("⚠️", "Passed with warnings"),
    GateResult.REQUIRE_APPROVAL: ("🛑", "Approval required"),
    GateResult.BLOCK: ("⛔", "Blocked"),
    GateResult.ERROR: ("❌", "Error — no trustworthy answer"),
}


def render_markdown(artifact: AnalysisArtifact, artifact_hint: str = "") -> str:
    """Render the pull-request report."""
    sections = [
        COMMENT_MARKER,
        _headline(artifact),
        _reasons(artifact),
        _totals(artifact),
        _unknowns(artifact),
        _movers(artifact),
        _budgets(artifact.decision.budget_evaluations),
        _policies(artifact.decision.policy_evaluations),
        _assumptions(artifact),
        _recommendations(artifact),
        _footer(artifact, artifact_hint),
    ]
    body = "\n\n".join(section for section in sections if section)
    return _fit(body, artifact_hint)


# ---------------------------------------------------------------------------


def _headline(artifact: AnalysisArtifact) -> str:
    icon, label = _HEADLINES[artifact.decision.result]
    delta = artifact.cost.totals.monthly_delta.signed_display()
    heading = f"## {icon} AWS Cost-Aware Deployment Gate — {label}"
    summary = f"**Estimated monthly change: {delta}**"
    if artifact.decision.required_approver_groups:
        approvers = ", ".join(code(group) for group in artifact.decision.required_approver_groups)
        summary += f"\n\nApproval required from: {approvers}"
    return f"{heading}\n\n{summary}"


def _reasons(artifact: AnalysisArtifact) -> str:
    if not artifact.decision.reasons:
        return ""
    lines = [f"- {escape_markdown(reason.text, 300)}" for reason in artifact.decision.reasons]
    return "**Why**\n\n" + "\n".join(lines)


def _totals(artifact: AnalysisArtifact) -> str:
    totals = artifact.cost.totals
    rows = [
        ("Current estimate", str(totals.current_monthly)),
        ("Proposed estimate", str(totals.proposed_monthly)),
        ("Monthly change", totals.monthly_delta.signed_display()),
        ("— fixed", totals.fixed_delta.signed_display()),
        ("— usage-based", totals.usage_based_delta.signed_display()),
        ("Unknown components", str(totals.unknown_component_count)),
        ("Confidence", artifact.confidence.value),
    ]
    if not totals.one_time.is_zero:
        rows.insert(3, ("One-time cost", str(totals.one_time)))

    table = ["| | |", "|---|---:|"]
    table += [f"| {label} | {value} |" for label, value in rows]
    changes = artifact.changes
    note = (
        f"{changes.added} added · {changes.removed} removed · {changes.modified} modified · "
        f"{changes.replaced} replaced · {changes.unchanged_count} unchanged"
    )
    return "\n".join(table) + f"\n\n<sub>{note}</sub>"


def _unknowns(artifact: AnalysisArtifact) -> str:
    """List what could not be established. Never collapsed when it is non-empty."""
    unknown = artifact.cost.unknown_components()
    if not unknown:
        return ""
    lines = [
        f"**{len(unknown)} cost(s) could not be established.** These are not included in "
        "the totals above, and are not zero.",
        "",
    ]
    for component in unknown[:MAX_TABLE_ROWS]:
        missing = component.unknown_inputs[0] if component.unknown_inputs else None
        detail = escape_markdown(missing.reason, 200) if missing else "no reason recorded"
        lines.append(
            f"- {code(str(component.resource))} {code(component.pricing_dimension)} — {detail}"
        )
    if len(unknown) > MAX_TABLE_ROWS:
        lines.append(f"- …and {len(unknown) - MAX_TABLE_ROWS} more (see the JSON artifact)")
    return "\n".join(lines)


def _movers(artifact: AnalysisArtifact) -> str:
    increases = artifact.cost.largest_increases()
    savings = artifact.cost.largest_savings()
    if not increases and not savings:
        return ""
    parts: list[str] = []
    if increases:
        parts.append(_component_table("Largest increases", increases))
    if savings:
        parts.append(_component_table("Largest savings", savings))
    return _collapsed("Cost breakdown", "\n\n".join(parts))


def _component_table(title: str, components: Sequence[CostComponent]) -> str:
    rows = ["| Resource | Dimension | Change | Confidence |", "|---|---|---:|---|"]
    for component in components:
        delta = (
            component.monthly_delta.signed_display()
            if component.monthly_delta is not None
            else "unknown"
        )
        rows.append(
            f"| {table_cell(str(component.resource))} "
            f"| {table_cell(component.pricing_dimension)} "
            f"| {delta} | {component.confidence.value} |"
        )
    return f"**{title}**\n\n" + "\n".join(rows)


def _budgets(evaluations: Sequence[BudgetEvaluation]) -> str:
    if not evaluations:
        return ""
    rows = [
        "| Budget | Scope | Estimated | Limit | Utilisation | Basis |",
        "|---|---|---:|---:|---:|---|",
    ]
    for evaluation in evaluations:
        scope = ", ".join(f"{k}={v}" for k, v in evaluation.scope_matched.items()) or "all"
        utilisation = (
            f"{format_percent(evaluation.utilization_percent)}%"
            if evaluation.utilization_percent is not None
            else "—"
        )
        rows.append(
            f"| {table_cell(evaluation.budget_id)} | {table_cell(scope)} "
            f"| {evaluation.estimated_infrastructure_proposed} "
            f"| {evaluation.monthly_limit or '—'} | {utilisation} "
            f"| {table_cell(evaluation.basis)} |"
        )
    note = (
        "\n\n<sub>Estimates come from the templates in this change, not from your bill. "
        "A basis of `estimate` means utilisation was measured against the template "
        "estimate alone; `actual+delta` means reported actual spend plus this change."
        "</sub>"
    )
    return _collapsed("Budget impact", "\n".join(rows) + note)


def _policies(evaluations: Sequence[PolicyEvaluation]) -> str:
    if not evaluations:
        return ""
    rows = ["| Policy | Result | Action | What was compared |", "|---|---|---|---|"]
    for evaluation in evaluations:
        inputs = "; ".join(f"{k}={v}" for k, v in evaluation.evaluated_inputs.items())
        rows.append(
            f"| {table_cell(evaluation.policy_id)} "
            f"| {'matched' if evaluation.matched else 'not matched'} "
            f"| {evaluation.action.value if evaluation.action else '—'} "
            f"| {table_cell(inputs, 200)} |"
        )
    note = (
        "\n\n<sub>Rules that did not match are listed with the values they compared, so "
        "that “why did this not catch it?” is answerable.</sub>"
    )
    return _collapsed("Policy results", "\n".join(rows) + note)


def _assumptions(artifact: AnalysisArtifact) -> str:
    assumptions = artifact.cost.assumptions
    if not assumptions:
        return ""
    rows = ["| Assumption | Value | Source | Why |", "|---|---|---|---|"]
    for assumption in assumptions[:MAX_TABLE_ROWS]:
        rows.append(
            f"| {table_cell(assumption.subject)} | {table_cell(assumption.value)} "
            f"| {assumption.provenance.value} | {table_cell(assumption.detail, 160)} |"
        )
    if len(assumptions) > MAX_TABLE_ROWS:
        rows.append(f"| …{len(assumptions) - MAX_TABLE_ROWS} more | | | |")
    return _collapsed("Assumptions", "\n".join(rows))


def _recommendations(artifact: AnalysisArtifact) -> str:
    """Patterns worth looking at.

    Collapsed, because advice is not a finding: a reader skimming for the verdict
    should not have to scroll past opinions to reach it. Each entry states the cost
    being incurred and the condition under which acting on it is right - never a
    saving, which the tool cannot know.
    """
    found = artifact.recommendations.recommendations
    if not found:
        return ""
    rows = []
    for item in found:
        amount = (
            str(item.addressable_monthly)
            if item.addressable_monthly is not None
            else "not established"
        )
        rows.append(f"**{escape_markdown(item.title, 160)}**")
        rows.append("")
        rows.append(f"Currently costing: {amount}")
        rows.append("")
        rows.append(escape_markdown(item.detail, 400))
        rows.append("")
        rows.append(f"*{escape_markdown(item.condition, 400)}*")
        rows.append("")
    body = "\n".join(rows).rstrip()
    body += (
        "\n\nThese are patterns worth checking, not instructions. Each states the "
        "cost being incurred now and what must be true for the change to be right; "
        "none of them is a promised saving."
    )
    return _collapsed(f"Worth a look ({len(found)})", body)


def _footer(artifact: AnalysisArtifact, artifact_hint: str) -> str:
    pricing = artifact.pricing
    lines = [
        "---",
        f"<sub>{escape_markdown(pricing.disclaimer, 300)}</sub>",
        f"<sub>Hours convention: {artifact.monthly_hours} h/month · "
        f"region {escape_markdown(artifact.region, 40)} · run "
        f"{escape_markdown(artifact.run_id, 40)}</sub>",
        "<sub>An estimate from Infrastructure as Code is not a prediction of your bill: "
        "it excludes actual usage, Savings Plans and Reserved Instance coverage, "
        "enterprise discounts, credits, taxes, and every resource created outside this "
        "repository.</sub>",
    ]
    if artifact_hint:
        lines.append(f"<sub>Full detail: {escape_markdown(artifact_hint, 200)}</sub>")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------


def _collapsed(title: str, body: str) -> str:
    return f"<details>\n<summary>{title}</summary>\n\n{body}\n\n</details>"


def _fit(body: str, artifact_hint: str) -> str:
    """Keep the body inside the comment budget, saying so if it had to be cut."""
    if len(body.encode("utf-8")) <= MAX_COMMENT_BYTES:
        return body
    notice = (
        "\n\n---\n\n> **This report was truncated to fit a pull-request comment.** "
        "The complete analysis is in the JSON artifact"
    )
    notice += f" ({escape_markdown(artifact_hint, 200)})." if artifact_hint else "."
    budget = MAX_COMMENT_BYTES - len(notice.encode("utf-8"))
    trimmed = body.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    return trimmed + notice
