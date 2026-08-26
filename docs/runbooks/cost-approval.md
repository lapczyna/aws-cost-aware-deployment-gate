# Runbook: approving a cost-gated change

For the person who has been asked to approve a deployment the cost gate held.

## What you are being asked

The gate returned `REQUIRE_APPROVAL`. That is not "the tool is unsure" — it is a policy
someone wrote saying this class of change needs a named group to agree before it ships.
The pull-request comment says which policy fired and why.

You are agreeing to **a specific change**, identified by a fingerprint. That fingerprint
covers the resources, the estimated cost, the verdict, the policies that matched, and
what the tool could not establish. If any of those move, your approval stops applying
and the deployment stops again. You do not have to watch for that.

## Before you approve

1. **Read the unknowns first.** They are never collapsed in the report. A change with
   four unknown costs is not a change with a known cost of `$X`; it is a change costing
   at least `$X`. Ask whether the unknown parts are the expensive parts.
2. **Check the basis.** The budget table says `estimate` or `actual+delta`. An estimate
   from Infrastructure as Code is not the bill: it excludes usage, discounts, Savings
   Plans, credits, taxes, and everything created outside the repository.
3. **Look at the assumptions.** `-v` output and the report's assumptions section give
   the hours convention, the usage profile, and where each figure came from. A
   `BUILTIN_DEFAULT` provenance means nobody chose that number for this application.
4. **Ask whether the cost is intended, not whether it is small.** A NAT Gateway at
   ~$33/month is not much; a NAT Gateway nobody decided to add is a different problem.

## Approving

Approve the `…-cost-approval` environment on the deployment run. GitHub records who you
are; the workflow records what you approved.

The deployment job then runs:

```bash
cost-gate approval check \
  --report report.json \
  --approved-fingerprint <the fingerprint from the analysis job> \
  --approver-group platform-architecture
```

It exits `0` only if the fingerprint matches **and** you belong to a group the policy
named. Exit `10` means the approval is missing, stale or unauthorised; exit `20` means
the change was refused outright.

## What you cannot approve

A `BLOCK` is not approvable, by design. A block that a click can remove is a warning
wearing a blocking label, and the distinction is worth keeping. If a blocked change
should ship, change the policy — that leaves a diff, a reviewer, and a record.

An `ERROR` is not approvable either. The tool could not produce a trustworthy answer,
so there is nothing to agree to. Fix the analysis first.

## If the approval goes stale

You will see:

```
the approval is for a different change (a1b2c3…)
  the approval was granted for 9f8e7d…, but this change fingerprints as a1b2c3…
```

This is working correctly. Something changed after you approved — usually a further
commit. Re-read the current report and approve again if it is still fine.

## Rolling back

The gate does not deploy and cannot roll back. It is a decision record, and that is
what it is useful for afterwards:

1. `cost-gate explain-decision --report <the artifact from the deploy run>` shows every
   rule considered, including those that did not fire. "Why did this not get caught?"
   is answerable.
2. `cost-gate explain-estimate --report … --resource <LogicalId>` shows how one
   resource's figure was reached, with its assumptions and confidence.
3. Compare the estimate against the actual bill once billing data catches up — costs
   take up to 24 hours to appear, and tag-based attribution only applies from the moment
   a tag is activated. A large discrepancy is worth turning into a fixture.

If a deployment introduced an unexpected recurring cost, the useful question is not
"why did the estimate differ" but "was this resource in the report at all". An unknown
that turned out to be expensive is a candidate for a new estimator; a resource that was
missing entirely is a parser bug.

## Who should be reviewers

Configure the `…-cost-approval` environments in repository settings so their reviewers
match the `approver_group` values in `policies.yaml`. Today those are:

| Group | Approves |
|---|---|
| `platform-architecture` | Architectural cost commitments — NAT Gateways, load balancers, clusters |
| `finops` | Budget thresholds and large deltas |

The tool checks group membership as an *entitlement*, and trusts the CI system on
identity. Authenticating a person is GitHub's job; doing it again, worse, here would be
a downgrade.
