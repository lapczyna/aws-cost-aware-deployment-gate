# GitHub integration

## 1. Shape of the integration

The gate ships as three things:

1. A **composite action** at `.github/actions/cost-gate/` that installs the package, runs the
   analysis, writes the job summary and uploads artifacts.
2. A **pull-request workflow** (`cost-gate.yml`) that runs the action with no privileges.
3. A **comment workflow** (`cost-gate-comment.yml`) that turns the artifact into exactly one
   pull-request comment.

Consumers can use the action alone if they prefer to wire their own workflows.

## 2. Workflow 1: analysis (untrusted)

```yaml
name: cost-gate
on:
  pull_request:
    paths: ["infrastructure/**", "examples/**", "policies/**", "config/**"]

permissions: {}

concurrency:
  group: cost-gate-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  analyze:
    runs-on: ubuntu-latest
    permissions:
      contents: read          # nothing else; no secrets are referenced anywhere in this job
    steps:
      - uses: actions/checkout@<sha>          # v4.x
        with:
          fetch-depth: 0                      # baseline revision must be reachable
      - uses: ./.github/actions/cost-gate
        with:
          baseline-ref: ${{ github.event.pull_request.base.sha }}
          config: config/cost-gate.yaml
          fail-on: block
```

Key properties:

* `permissions: {}` at workflow level, `contents: read` on the job. A fork PR gets a read-only
  token regardless, but being explicit means the same guarantee holds for branch PRs.
* **No `secrets` context is referenced in this job at all.** This is the property that makes it
  safe to run `cdk synth` on pull-request code.
* `fetch-depth: 0` because the baseline template is produced by synthesising the base revision
  from a `git worktree`.
* `concurrency` cancels superseded runs so a rapid series of pushes does not produce a queue of
  contradictory reports.

The action ends by setting outputs (`decision`, `monthly-delta`, `unknown-count`) and exiting
with the gate exit code, which is what branch protection observes.

## 3. Workflow 2: commenting (trusted, no PR code)

```yaml
name: cost-gate-comment
on:
  workflow_run:
    workflows: ["cost-gate"]
    types: [completed]

permissions: {}

jobs:
  comment:
    runs-on: ubuntu-latest
    if: github.event.workflow_run.event == 'pull_request'
    permissions:
      pull-requests: write
      actions: read
    steps:
      - uses: actions/checkout@<sha>          # base branch only, never PR head
      - name: Download report artifact
        uses: actions/download-artifact@<sha>
        with:
          run-id: ${{ github.event.workflow_run.id }}
          github-token: ${{ github.token }}
          name: cost-gate-report
      - name: Validate and post
        run: python scripts/post_comment.py --artifact-dir . --run-id ${{ github.event.workflow_run.id }}
```

`scripts/post_comment.py` treats the artifact as hostile:

1. Refuse files above a size cap.
2. Validate `report.json` against `schemas/report.schema.json`.
3. Read the PR number from the artifact **and** cross-check it against the `workflow_run`
   payload; mismatch aborts.
4. Re-render Markdown from the validated JSON rather than trusting `report.md`, passing every
   template-derived string through the escaper.
5. Upsert the comment.

Re-rendering from JSON rather than posting the uploaded Markdown is deliberate: it means the
comment body is produced by trusted code from validated data, so a crafted `report.md` cannot
reach a comment.

## 4. Comment idempotency

Every comment carries a hidden marker:

```html
<!-- cost-gate:report:v1 -->
```

The algorithm: list comments on the pull request, find the first authored by the workflow
identity whose body contains the marker, `PATCH` it if found, `POST` otherwise. The result is
one comment that updates in place across pushes, instead of a wall of stale reports.

Two details that matter in practice:

* Listing is paginated; a long-lived pull request can exceed one page and the marker may be on
  page three.
* A comment can be deleted by a human between runs, so `PATCH` must tolerate a 404 and fall
  back to `POST`.

Both are covered by unit tests against a fake API client.

## 5. Job summary

The same rendered Markdown is written to `$GITHUB_STEP_SUMMARY`, which works identically for
forks and requires no permissions at all. This is the fallback path: even where commenting is
impossible or disabled, the report is always one click away from the checks tab.

## 6. Report size management

GitHub comments have a hard limit (65,536 characters), and a large infrastructure change can
produce a long report. The Markdown renderer applies a budget:

* Always shown: decision, reasons, totals, budget impact, confidence, unknown count.
* Collapsed in `<details>`: full component table, non-matching policies, assumptions.
* Truncated with an explicit marker: component tables beyond N rows, sorted by absolute delta
  so the material items survive.
* Always present: a pointer to the complete JSON artifact.

Truncation is stated in the body, never silent — a reviewer must know they are looking at a
summary.

## 7. Branch protection and approvals

Recommended repository configuration:

1. Require the `cost-gate / analyze` check on the default branch.
2. For `REQUIRE_APPROVAL` (exit 10) the check fails, so merging is blocked until the change is
   revised or an authorised approval is recorded.
3. Record approvals through a **protected GitHub environment** (for example `finops-approval`)
   with required reviewers matching the `approver_group` in the policy. A small workflow with
   `workflow_dispatch` targets that environment; approving the deployment records who approved
   and when.
4. Deployment jobs consume the recorded decision artifact rather than re-running the analysis,
   so the reviewed artifact is the authorising artifact.

GitHub environments are the mechanism here because they produce an auditable approval record
tied to a team, which a comment reaction or a label cannot.

## 8. Fork behaviour

| Capability | Same-repo PR | Fork PR |
|---|---|---|
| Analysis runs | yes | yes |
| Job summary | yes | yes |
| Artifact upload | yes | yes |
| PR comment | yes | yes (via workflow 2) |
| AWS access | none | none |
| Secrets | none | none |

Fork pull requests get the full experience because the privileged half never needs their code.

## 9. Dogfooding

The repository runs the gate against its own optional CDK infrastructure
(`infrastructure/`), so every pull request that touches it publishes a real cost report. This
is both a continuous end-to-end test and the sample output used in documentation — which is
why samples in this repository are generated artifacts rather than hand-written illustrations.
