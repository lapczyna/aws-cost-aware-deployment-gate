# Security policy

## Reporting a vulnerability

Report vulnerabilities privately through GitHub Security Advisories:
**Security → Report a vulnerability** on this repository. Do not open a public issue.

Please include: affected version or commit, a description of the impact, and reproduction
steps. Expect an acknowledgement within a few days. This is a personal portfolio project, not
a commercially supported product, so there is no formal SLA — but reports are taken seriously
and fixed or documented.

## Supported versions

The `main` branch is the only supported version while the project is pre-1.0.

## Security model

The full threat model is in [docs/security.md](docs/security.md). In summary:

* The default analysis path is **fully offline** and requires no AWS credentials.
* Workflows that execute pull-request code (including `cdk synth`) hold **no secrets and no
  write token**. Commenting happens in a separate `workflow_run` workflow that never checks out
  pull-request code.
* **`pull_request_target` is prohibited** in this repository and CI fails if it appears.
* AWS access, where used at all, is via GitHub OIDC with a repository-scoped trust policy and
  read-only pricing permissions.
* Policies are data evaluated by a closed predicate grammar. No configuration file can cause
  code execution.
* Templates are parsed with a `SafeLoader` subclass under size, depth and node-count limits.
* All template-derived strings are escaped before reaching a pull-request comment.

## Out of scope

* A malicious maintainer, or anyone with write access to the default branch. Branch protection
  and required reviews are the control, and they are configured outside this repository.
* Compromise of the GitHub Actions platform or its runners.
* Accuracy of the pricing catalog. The checksum lock detects tampering, not staleness; the
  catalog is explicitly labelled non-authoritative.
* Cost incurred by resources created outside this repository's infrastructure code.

## What this project will never do

* Commit credentials, real AWS account identifiers, or private pricing data.
* Deploy AWS resources from a `push` or `pull_request` trigger.
* Execute code supplied through a configuration or policy file.
