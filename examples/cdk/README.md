# Example CDK application

A two-stack application used to demonstrate analysing CDK changes. It exists to be
*synthesised*, not deployed — there is no account behind it and no bootstrap stack.

The `growth` context flag selects between the two versions of the application:

```bash
cdk synth --context growth=false    # the baseline
cdk synth --context growth=true     # the proposal
```

One flag rather than two directories, because the interesting question is what happens
to *the same constructs* when they change. Two unrelated apps would produce two
unrelated sets of logical IDs and demonstrate nothing about matching.

## What the proposal changes

| Construct | Baseline | Proposal |
|---|---|---|
| `Network/Vpc` | One private isolated subnet | Adds a public subnet and a NAT Gateway |
| `Workload/Api` | `t3.small` | `t3.large` |
| `Workload/Database` | Single-AZ `t3.small` | Multi-AZ `t3.medium` |
| `Workload/Cache` | absent | An ElastiCache cluster the tool cannot price |

## Synthesising it

```bash
pip install -e ".[cdk]"          # installs aws-cdk-lib
npm install -g aws-cdk           # the CLI

cost-gate cdk snapshot --app examples/cdk --context growth=false --out build/baseline
cost-gate cdk snapshot --app examples/cdk --context growth=true  --out build/proposed
cost-gate analyze --baseline build/baseline --proposed build/proposed \
  --config examples/config/cost-gate.yaml --environment development
```

Templates synthesised from this app are committed under `synthesized/`, so the test
suite can exercise real CDK output without needing Node installed. Regenerate them with
`python scripts/dev.py synth`.

## Why the availability zones are pinned in `cdk.json`

A stack with a concrete account and region makes the CDK CLI *look up* the region's
availability zones, which needs AWS credentials. Committing the answer as context
avoids that, and makes synthesis reproducible: an app that discovers its AZs produces
different templates depending on who runs it and when.

This is also the `cdk.context.json` drift problem in miniature. Cached context makes
synthesis deterministic, and stale cached context makes it deterministically wrong.
A cost estimate inherits whichever it is — see [domain model](../../docs/domain-model.md).
