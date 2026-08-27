"""Generate docs/demo-scenarios.md from the scenarios themselves.

Written by a script rather than by hand so that the documented list cannot drift from
the scenarios that actually exist. Regenerate with `python scripts/dev.py docs`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path("src").resolve()))

from cost_gate.demo import load_scenarios

PREAMBLE = """# Demonstration scenarios

{count} changes, each chosen because it shows something the others do not. All of them
run offline: no AWS account, no credentials, no network access.

```bash
python scripts/dev.py demo              # run them all
cost-gate demo --list                   # see what each one shows
cost-gate demo --scenario nat-gateway-development --report
```

Prices come from the bundled fixture catalog. They are hand-curated, capture-dated and
**not authoritative** — see [pricing sources](pricing-sources.md).

## How a scenario asserts

Each scenario directory holds two CloudFormation snapshots and a `scenario.yaml` that
states what the gate ought to do with them:

```yaml
expect:
  result: REQUIRE_APPROVAL
  exit_code: 10
  delta: increase
  unknowns: some
  matched_policies: [nat-gateway-in-development]
  approver_groups: [platform-architecture]
```

Those expectations are **written by hand, before the tool is run**. That distinction is
the whole point. An expectation recorded from the tool's own output asserts only that
the tool agrees with itself, which it always does, including when it is wrong.

There are two mechanisms here and they do different jobs:

| Mechanism | Catches | Written by |
|---|---|---|
| `scenario.yaml` | Wrong behaviour | A person, stating intent |
| `tests/golden/scenarios/*.md` | Unintended change | The tool |

Conflating them is how a test suite quietly stops testing anything. The expectations are
deliberately stated at the level of intent — "this should increase costs", not "this
should cost $32.85" — because an assertion that has to be edited every time the pricing
fixtures are refreshed has stopped being an assertion. Exact figures live in the golden
reports, where a change shows up as a reviewable diff.

A scenario also names the policies it expects to fire. Reaching the right verdict by the
wrong route is not the same as being right, and it will not stay right.

## The scenarios

"""

FOOTER = """
## Adding one

A new scenario earns its place by showing something none of the existing ones does.

1. `examples/scenarios/<id>/` with `baseline.yaml`, `proposed.yaml` and `scenario.yaml`.
2. Write the expectation first, from what you believe should happen.
3. `cost-gate demo --scenario <id>`.
4. If the tool disagrees, work out which of you is wrong before touching either. The
   scenarios in this directory found two genuine bugs that way, and one of the
   expectations turned out to be based on a misunderstanding of how EKS is billed.
5. `python scripts/dev.py golden --update`, and read the generated report.

Referenced configuration paths are confined to the directory holding the config file,
because they come from a file a pull request can edit. A scenario needing its own rules
carries its own complete configuration — see `budget-exhausted`.
"""


_NUMERALS = {
    12: "Twelve",
    13: "Thirteen",
    14: "Fourteen",
    15: "Fifteen",
    16: "Sixteen",
    17: "Seventeen",
    18: "Eighteen",
    19: "Nineteen",
    20: "Twenty",
}


def _spell(count: int) -> str:
    """Spell a small count, so the opening sentence reads as prose.

    Computed rather than written down: a generated document that hardcodes a number
    it could derive is one that goes stale, which defeats the point of generating
    it. This one did - it said seventeen after an eighteenth scenario was added.
    """
    return _NUMERALS.get(count, str(count))


def main() -> None:
    rows = []
    sections = []
    for scenario, _directory in load_scenarios(Path("examples/scenarios")):
        expect = scenario.expect
        rows.append(
            f"| [`{scenario.identifier}`](#{scenario.identifier}) "
            f"| {expect.result.value} | {expect.exit_code} | {scenario.title} |"
        )
        details = [f"### {scenario.identifier}", "", f"**{scenario.title}**", ""]
        details.append(" ".join(scenario.demonstrates.split()))
        details.append("")
        facts = [
            f"- Expects **{expect.result.value}**, exit code `{expect.exit_code}`",
            f"- Estimated monthly cost: {expect.delta.value}",
        ]
        if expect.unknowns.value == "some":
            facts.append("- Includes at least one cost the tool cannot establish")
        else:
            facts.append("- Every cost is established")
        if expect.matched_policies:
            joined = ", ".join(f"`{policy}`" for policy in expect.matched_policies)
            facts.append(f"- Policies that must fire: {joined}")
        if expect.approver_groups:
            joined = ", ".join(f"`{group}`" for group in expect.approver_groups)
            facts.append(f"- Approval required from: {joined}")
        facts.append(f"- Environment: `{scenario.environment or 'unset'}`")
        details.extend(facts)
        details.append("")
        details.append(
            f"[Generated report](../tests/golden/scenarios/{scenario.identifier}.md)"
            if expect.result.value != "ERROR"
            else "_No report: this scenario expects the analysis to fail._"
        )
        details.append("")
        sections.append("\n".join(details))

    table = [
        "| Scenario | Expects | Exit | Shows |",
        "|---|---|---:|---|",
        *rows,
    ]
    # str.replace rather than str.format: the preamble contains YAML braces
    # that format() would try to interpret as fields.
    preamble = PREAMBLE.replace("{count}", _spell(len(rows)))
    document = preamble + "\n".join(table) + "\n\n" + "\n".join(sections) + FOOTER
    Path("docs/demo-scenarios.md").write_text(document, encoding="utf-8", newline="\n")
    print(f"wrote docs/demo-scenarios.md ({len(rows)} scenarios)")


if __name__ == "__main__":
    main()
