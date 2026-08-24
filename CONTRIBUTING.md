# Contributing

## Setup

```bash
git clone https://github.com/lapczyna/aws-cost-aware-deployment-gate.git
cd aws-cost-aware-deployment-gate

python -m venv .venv
# Windows:            .venv\Scripts\activate
# macOS / Linux:      source .venv/bin/activate

python scripts/dev.py install
cost-gate --version
```

Python 3.12 or newer is required. No AWS account, credentials or network access are needed
for development or for the test suite.

## Running tasks

`scripts/dev.py` is the canonical definition of every task. The `Makefile` delegates to it, so
`make test` and `python scripts/dev.py test` do exactly the same thing — use whichever suits
your machine (`make` is not installed by default on Windows).

```bash
python scripts/dev.py --list          # show every task

python scripts/dev.py format          # apply formatting and import order
python scripts/dev.py lint            # ruff
python scripts/dev.py typecheck       # mypy --strict
python scripts/dev.py imports         # architecture contracts (import-linter)
python scripts/dev.py test            # fast unit and property tests
python scripts/dev.py test-all        # everything except the opt-in CDK suite
python scripts/dev.py security        # bandit + pip-audit
python scripts/dev.py all             # the full gate, as CI runs it
```

Run `python scripts/dev.py all` before opening a pull request. CI runs the same tasks.

## Non-negotiable rules

These are enforced by tests, linting or CI, and a change that violates one will not merge.

| Rule | Why | Enforced by |
|---|---|---|
| Money is `Decimal`, never `float` | Reconciliation must be exact | Review, property tests |
| Unknown cost is `None` on a visible component, never `0` | Zero is a lie that looks safe | Property tests (ADR 0002) |
| `domain/` imports no `boto3`, `typer` or GitHub code | Keeps the domain testable and portable | import-linter |
| YAML is loaded with a `SafeLoader` subclass only | Deserialisation is remote code execution | CI grep, Bandit |
| `pull_request_target` is never used | Executes untrusted code with a write token | CI grep (ADR 0007) |
| Third-party actions are pinned to a full commit SHA | Tags are mutable | CI grep |
| No real AWS account IDs, ARNs or credentials in fixtures | Portfolio repository is public | Review, secret scanning |
| Reports are deterministic: same input, identical bytes | Golden tests and reproducible demos | e2e determinism test |
| No fabricated pricing | Credibility of the whole project | Review; catalog carries provenance |

## Tests

| Directory | Marker | Contents |
|---|---|---|
| `tests/unit` | `unit` | Fast, isolated tests of one component |
| `tests/property` | `property` | Hypothesis tests of invariants |
| `tests/contract` | `contract` | Pricing provider conformance (no network) |
| `tests/integration` | `integration` | Multi-component wiring |
| `tests/e2e` | `e2e` | CLI against checked-in fixtures, offline |
| — | `cdk` | Opt-in tests that shell out to a real `cdk synth`; excluded by default |

No test may require AWS credentials or network access. AWS interactions are tested with
botocore's `Stubber` against the adapter interface.

Golden files are compared byte-for-byte. Write them with `newline="\n"`; `.gitattributes`
marks them `-text` so Git does not translate line endings on Windows.

## Commits

* One focused change per commit, with a message explaining *why*.
* Conventional prefixes: `feat:`, `fix:`, `docs:`, `test:`, `build:`, `ci:`, `refactor:`,
  `infra:`.
* Do not force-push a shared branch or rewrite published history.

## Adding support for an AWS resource type

1. Add pricing entries to `pricing-data/<region>/<service>.yaml` and update
   `manifest.yaml` coverage plus `catalog.lock.json`.
2. Add the property metadata (`cost_relevant`, `replacement`) for the type.
3. Implement the estimator, pricing a resource *state* — never a change (ADR 0003).
4. Populate `assumptions`, `confidence_reasons` and `unknown_inputs` on every component. A
   component whose confidence cannot be explained is not finished.
5. Add a table-driven unit test, including a case where a pricing-relevant property is
   unresolved and the result must be `UNKNOWN`.
6. Register the estimator, and confirm it appears in `cost-gate list-supported-resources`.
7. Update `docs/roadmap.md`.

## Architecture decisions

Anything expensive to reverse gets an ADR in `docs/adr/`. ADRs are immutable once accepted:
supersede, do not edit. See the [template](docs/adr/README.md#template).

## Reporting security issues

See [SECURITY.md](SECURITY.md). Do not open a public issue for a vulnerability.
