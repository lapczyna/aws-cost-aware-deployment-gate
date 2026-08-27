"""Configuration that loads cleanly and cannot do anything.

The loader rejects configuration that is *wrong*. These checks find configuration that is
merely **inert**: a rule that can never fire, a threshold that can never be crossed, a
scope that quietly falls back to defaults. None of it is invalid, so none of it belongs in
the loader — and all of it looks, to whoever wrote it, like a decision that has been
recorded.

That is the same argument the policy engine makes about a rule that never matches, and
the feedback loop makes about a usage override that matches nothing. A control providing
false assurance is worse than no control, because someone is relying on it.

Kept out of the default path deliberately. These are judgements about intent rather than
correctness, and a check that is occasionally wrong must be something a person opts into
rather than something that fails their build.

There are deliberately only two checks. A third — thresholds on a budget with no monthly
limit — was written and then deleted, because the loader already rejects that with a
better message than this module could give (``config/budgets.py``). A lint that can never
fire is the exact thing this module exists to find, and shipping one here would have been
funny rather than defensible.
"""

from __future__ import annotations

from dataclasses import dataclass

from cost_gate.config.root import LoadedConfig
from cost_gate.domain.enums import Confidence

__all__ = ["Finding", "strict_findings"]


@dataclass(frozen=True)
class Finding:
    """Something that loads but cannot take effect."""

    location: str
    """Where it is, precise enough to edit: file and path within it."""

    problem: str
    remedy: str

    def render(self) -> str:
        """One line for a terminal."""
        return f"{self.location}: {self.problem}"


def strict_findings(loaded: LoadedConfig) -> tuple[Finding, ...]:
    """Find configuration that is valid and inert.

    Returns them in a stable order, so two runs over one file produce identical output.
    """
    findings: list[Finding] = []
    findings.extend(_unprofiled_environments(loaded))
    findings.extend(_vacuous_conditions(loaded))
    return tuple(sorted(findings, key=lambda finding: (finding.location, finding.problem)))


def _unprofiled_environments(loaded: LoadedConfig) -> list[Finding]:
    """Environments a rule targets but the usage profile does not describe.

    Estimates for such an environment fall back to ``defaults`` without complaint. That
    is the right runtime behaviour — refusing to estimate would be worse — but a policy
    written specifically for ``staging`` while the usage profile knows only
    ``production`` and ``development`` is being evaluated against numbers nobody chose
    for staging.
    """
    if loaded.usage is None:
        return []
    known = set(loaded.usage.environments)
    if not known:
        return []

    findings = []
    referenced: list[tuple[str, str]] = []
    if loaded.policies is not None:
        for policy in loaded.policies.policies:
            for environment in policy.scope.environments or ():
                referenced.append((f"policies/{policy.id}/scope", environment))
    if loaded.budgets is not None:
        for budget in loaded.budgets.budgets:
            if budget.scope.environment is not None:
                referenced.append((f"budgets/{budget.id}/scope", budget.scope.environment))

    for location, environment in referenced:
        if environment not in known:
            findings.append(
                Finding(
                    location=location,
                    problem=(
                        f"targets environment {environment!r}, which the usage profile "
                        f"does not describe (it knows {', '.join(sorted(known))})"
                    ),
                    remedy=(
                        f"add a {environment!r} entry to the usage profile, or correct the spelling"
                    ),
                )
            )
    return findings


def _vacuous_conditions(loaded: LoadedConfig) -> list[Finding]:
    """Conditions that are true of every change.

    ``confidence_at_most: HIGH`` is the one that occurs in practice. ``HIGH`` is the top
    of the confidence lattice, so the condition holds for every report ever produced —
    which makes it a no-op inside ``all_of`` and makes the whole policy unconditional
    inside ``any_of``. Either way it is not doing what its author expected.
    """
    if loaded.policies is None:
        return []

    findings = []
    for policy in loaded.policies.policies:
        for path, ceiling in _confidence_ceilings(policy.when, f"policies/{policy.id}/when"):
            if ceiling is Confidence.HIGH:
                findings.append(
                    Finding(
                        location=path,
                        problem=(
                            "confidence_at_most: HIGH is true of every change, because "
                            "HIGH is the highest confidence there is"
                        ),
                        remedy=(
                            "use MEDIUM or LOW to mean 'the tool was not sure', or "
                            "remove the condition"
                        ),
                    )
                )
    return findings


def _confidence_ceilings(condition: object, path: str) -> list[tuple[str, Confidence]]:
    """Every ``confidence_at_most`` in a condition tree, with where it was found.

    Walks the combinators rather than looking only at the top level: the vacuous case is
    most likely to hide inside an ``all_of``, where it silently contributes nothing.
    """
    found: list[tuple[str, Confidence]] = []
    ceiling = getattr(condition, "confidence_at_most", None)
    if isinstance(ceiling, Confidence):
        found.append((path, ceiling))

    for combinator in ("all_of", "any_of"):
        children = getattr(condition, combinator, None) or ()
        for index, child in enumerate(children):
            found.extend(_confidence_ceilings(child, f"{path}/{combinator}[{index}]"))

    # Written as `not:` in YAML; the field is `negate`, because `not` is a keyword.
    negated = getattr(condition, "negate", None)
    if negated is not None:
        found.extend(_confidence_ceilings(negated, f"{path}/not"))
    return found
