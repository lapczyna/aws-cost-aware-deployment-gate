# ADR 0007 — Separate untrusted analysis from privileged commenting

* Status: Accepted
* Date: 2026-08-24

## Context

The gate must post its report to a pull request, including pull requests from forks. Posting a
comment requires `pull-requests: write`. Fork pull requests receive a read-only `GITHUB_TOKEN`,
so a `pull_request`-triggered workflow cannot comment on them.

The complication specific to this project: analysing a CDK change requires `cdk synth`, which
**imports and executes `app.py` from the pull request**. Analysis is arbitrary code execution
of untrusted input.

Options:

1. `pull_request` only. Job summary and artifact, no comment on forks.
2. `pull_request_target`, checking out the PR head. Comments work everywhere.
3. Two workflows: `pull_request` for analysis, `workflow_run` for commenting.

## Decision

Option 3. `cost-gate.yml` runs on `pull_request` with `contents: read`, no secrets and no OIDC,
and uploads the report as an artifact. `cost-gate-comment.yml` runs on `workflow_run`, holds
`pull-requests: write`, never checks out pull-request code, and posts the comment after
validating the artifact.

`pull_request_target` is prohibited in this repository, and a CI check greps for it.

## Rationale

* Option 2 is the well-known critical misconfiguration: `pull_request_target` grants a write
  token and secret access while running in the base-branch context; checking out the PR head
  then executes attacker-controlled code with those privileges. Given that `cdk synth` executes
  code unconditionally, this would be an unauthenticated remote code execution path into the
  repository and its cloud credentials.
* Option 1 is safe but degrades the experience for exactly the contributors — external ones —
  who most need the report explained.
* Option 3 keeps the two capabilities apart: the job that runs untrusted code holds nothing
  worth stealing, and the job that holds a token never runs untrusted code. The only thing that
  crosses the boundary is data.

## Consequences

* The artifact is untrusted input to the privileged workflow and must be handled as such:
  size cap, schema validation, PR-number cross-check against the `workflow_run` payload, and
  Markdown re-rendered by trusted code from validated JSON rather than posting the uploaded
  `report.md`.
* Comments appear a few seconds after the check completes, because a second workflow must
  start. Acceptable.
* `workflow_run` workflows only run from the default branch, so changes to the commenting logic
  take effect after merge, not in the pull request that proposes them. This is a security
  property, not a limitation, and is documented for contributors who find it confusing.
* The job summary remains the primary, permission-free delivery channel; commenting is an
  enhancement layered on top rather than the only path to the report.
