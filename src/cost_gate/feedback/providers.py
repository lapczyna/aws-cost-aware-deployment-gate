"""Where observed cost comes from.

Two implementations behind one protocol, and the default is the offline one — the same
arrangement as pricing. A feedback loop that only works with AWS credentials is a
feedback loop that is never exercised in CI, and this machinery is exactly the kind
whose edge cases (an incomplete window, an untagged resource, a change never deployed)
matter more than its happy path.

The Cost Explorer adapter is optional and lives behind the ``aws`` extra. It is worth
noting what it costs to call: Cost Explorer charges **per request**, which makes an
accidental loop over every prediction a line item on the very bill this tool is meant
to keep an eye on. The adapter therefore batches by window rather than querying per
record.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from pydantic import ValidationError

from cost_gate.config.loader import BoundedSafeLoader, load_bounded_yaml
from cost_gate.domain.money import Money
from cost_gate.feedback.records import (
    Comparability,
    Observation,
    PredictionRecord,
    ServiceObservation,
)

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from collections.abc import Sequence

__all__ = [
    "BILLING_LAG_HOURS",
    "FixtureObservationProvider",
    "ObservationError",
    "ObservationProvider",
    "settled_window",
]

BILLING_LAG_HOURS: Final = 24
"""How far behind cost data runs.

AWS documents up to 24 hours for Cost Explorer. A month is not final for several days
after it ends, which is why :func:`settled_window` also refuses the current month.
"""

MAX_FIXTURE_BYTES: Final = 5 * 1024 * 1024


class ObservationError(Exception):
    """Observed cost could not be retrieved.

    Distinct from an observation that exists but is not comparable. This means the
    lookup failed; that means the lookup succeeded and the answer cannot be used.
    """


class ObservationProvider(Protocol):
    """Where observed cost comes from."""

    def observe(self, record: PredictionRecord) -> Observation | None:
        """Return what was billed for a prediction, or ``None`` if nothing is known.

        ``None`` means "no data for this change", which is different from an
        ``Observation`` carrying a non-comparable status: the latter means data exists
        and cannot honestly be used.
        """
        ...


def settled_window(reference: datetime, deployed_at: datetime | None) -> Comparability:
    """Decide whether a billing window can be compared yet.

    Two independent reasons it may not be:

    * **the data has not arrived.** Cost data lags by up to 24 hours, so a deployment
      from this morning has no bill yet;
    * **the month is not whole.** A monthly estimate describes a steady month. A change
      deployed on the 20th produces ten days of billing, and scaling that up assumes a
      steadiness that a just-deployed system rarely has.

    Returns ``COMPARABLE`` only when neither applies.
    """
    if deployed_at is None:
        return Comparability.NOT_DEPLOYED
    if reference - deployed_at < timedelta(hours=BILLING_LAG_HOURS):
        return Comparability.BILLING_INCOMPLETE
    if (deployed_at.year, deployed_at.month) == (reference.year, reference.month):
        return Comparability.PARTIAL_MONTH
    return Comparability.COMPARABLE


def tags_were_active(record: PredictionRecord) -> bool:
    """Whether cost allocation tags covered the whole billing window.

    Tags apply from activation forward and are never backfilled, so a resource deployed
    before its tags were activated has untagged history that attribution cannot see.
    The observed figure would then be genuinely lower than the truth, and comparing it
    would make the tool look better than it is.
    """
    if record.tags_activated_on is None or record.deployed_at is None:
        return True
    return record.tags_activated_on <= record.deployed_at.date()


class FixtureObservationProvider:
    """Observations from a checked-in YAML file.

    The default, and the only one exercised in CI. Its fixtures deliberately include the
    awkward cases — an incomplete window, an untagged resource, a change never deployed,
    a service billed that was never predicted — because those are what the accuracy
    arithmetic has to handle correctly and what a happy-path fixture would never reach.
    """

    source = "fixture"

    def __init__(self, path: Path) -> None:
        """Load observations from ``path``.

        Raises:
            ObservationError: if the file is missing, oversized or malformed.
        """
        if not path.is_file():
            raise ObservationError(f"no observation fixture at {path}")
        size = path.stat().st_size
        if size > MAX_FIXTURE_BYTES:
            raise ObservationError(f"{path} is {size} bytes; the maximum is {MAX_FIXTURE_BYTES}")

        try:
            document = load_bounded_yaml(path.read_text(encoding="utf-8"), BoundedSafeLoader)
        except ValueError as exc:
            raise ObservationError(f"{path} is not valid YAML: {exc}") from exc
        if not isinstance(document, dict):
            raise ObservationError(f"{path} must contain a mapping at the top level")

        self._observations: dict[str, Observation] = {}
        for entry in document.get("observations") or []:
            try:
                observation = Observation.model_validate(entry)
            except ValidationError as exc:
                raise ObservationError(f"invalid observation in {path}: {exc}") from exc
            if observation.fingerprint in self._observations:
                raise ObservationError(f"{path} has two observations for {observation.fingerprint}")
            self._observations[observation.fingerprint] = observation

    def observe(self, record: PredictionRecord) -> Observation | None:
        """Look up the observation for a prediction."""
        return self._observations.get(record.fingerprint)

    def __len__(self) -> int:
        """How many observations were loaded."""
        return len(self._observations)


class CostExplorerObservationProvider:
    """Observed cost from the AWS Cost Explorer API.

    Optional, and never used by the default path. Requires ``boto3`` (the ``aws`` extra)
    and credentials with ``ce:GetCostAndUsage``.

    **Cost Explorer charges per request.** A naive implementation querying once per
    prediction would put a line item on the bill this tool exists to watch, so results
    are fetched once per window and matched to records afterwards.
    """

    source = "cost-explorer"

    def __init__(
        self,
        client: object,
        *,
        tag_key: str = "Application",
        reference: datetime | None = None,
    ) -> None:
        """Wrap a boto3 ``ce`` client.

        The client is injected rather than constructed so this can be tested with
        botocore's ``Stubber`` and never needs credentials in CI.
        """
        self._client = client
        self._tag_key = tag_key
        self._reference = reference or datetime.now(tz=UTC)
        self._cache: dict[tuple[date, date], dict[str, dict[str, str]]] = {}

    def observe(self, record: PredictionRecord) -> Observation | None:
        """Return what was billed for a prediction.

        Raises:
            ObservationError: if the API call fails. A failed lookup is not an
                observation of zero, and treating it as one would flatter the tool.
        """
        if record.application is None:
            return None

        status = settled_window(self._reference, record.deployed_at)
        if not status.is_comparable:
            return self._excluded(record, status)
        if not tags_were_active(record):
            return self._excluded(record, Comparability.TAGS_NOT_ACTIVE)

        window = self._window(record)
        try:
            grouped = self._fetch(window)
        except Exception as exc:  # botocore raises a wide, undocumented family
            raise ObservationError(f"Cost Explorer lookup failed: {type(exc).__name__}") from exc

        attributed = grouped.get(record.application)
        if not attributed:
            return self._excluded(record, Comparability.UNATTRIBUTED)
        return self._build(record, window, attributed)

    # The remaining methods are deliberately small; the arithmetic lives in accuracy.py.

    def _window(self, record: PredictionRecord) -> tuple[date, date]:
        """The calendar month in which the change was deployed.

        Raises:
            ObservationError: if the record has no deployment time. Unreachable via
                ``observe``, which checks first - but written as a real check rather
                than an assertion, because assertions vanish under ``python -O`` and
                this one guards an arithmetic that would otherwise crash obscurely.
        """
        if record.deployed_at is None:
            raise ObservationError(f"{record.fingerprint} has no deployment time")
        start = record.deployed_at.date().replace(day=1)
        following = (start + timedelta(days=32)).replace(day=1)
        return start, following

    def _fetch(self, window: tuple[date, date]) -> dict[str, dict[str, str]]:
        """One API call per window, cached. See the note about per-request charges."""
        if window in self._cache:
            return self._cache[window]
        response = self._client.get_cost_and_usage(  # type: ignore[attr-defined]
            TimePeriod={"Start": window[0].isoformat(), "End": window[1].isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[
                {"Type": "TAG", "Key": self._tag_key},
                {"Type": "DIMENSION", "Key": "SERVICE"},
            ],
        )
        parsed: dict[str, dict[str, str]] = {}
        for period in response.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                keys = group.get("Keys", [])
                expected_keys = 2
                if len(keys) != expected_keys:
                    continue
                application = keys[0].split("$", 1)[-1]
                amount = group["Metrics"]["UnblendedCost"]["Amount"]
                parsed.setdefault(application, {})[keys[1]] = amount
        self._cache[window] = parsed
        return parsed

    def _build(
        self,
        record: PredictionRecord,
        window: tuple[date, date],
        attributed: dict[str, str],
    ) -> Observation:
        """Assemble an observation from grouped Cost Explorer results."""
        services = tuple(
            ServiceObservation(
                service=service,
                monthly_amount=Money(amount=Decimal(amount), currency=record.currency),
            )
            for service, amount in sorted(attributed.items())
        )
        total = sum((item.monthly_amount.amount for item in services), start=Decimal(0))
        return Observation(
            fingerprint=record.fingerprint,
            observed_at=self._reference,
            window_start=window[0],
            window_end=window[1],
            observed_monthly_amount=Money(amount=total, currency=record.currency),
            services=services,
            source=self.source,
            # Cost Explorer is the account's own billing data, so unlike the pricing
            # fixtures this genuinely is authoritative - for what it measures.
            authoritative=True,
        )

    def _excluded(self, record: PredictionRecord, status: Comparability) -> Observation:
        """An observation that exists but cannot honestly be compared."""
        return Observation(
            fingerprint=record.fingerprint,
            observed_at=self._reference,
            window_start=self._reference.date(),
            window_end=self._reference.date(),
            observed_monthly_amount=Money(amount=Decimal(0), currency=record.currency),
            comparability=status,
            detail=_EXCLUSION_DETAIL[status],
            source=self.source,
            authoritative=True,
        )


_EXCLUSION_DETAIL: Final[dict[Comparability, str]] = {
    Comparability.NOT_DEPLOYED: "the change was analysed but never deployed",
    Comparability.BILLING_INCOMPLETE: (
        f"cost data lags by up to {BILLING_LAG_HOURS} hours and has not settled"
    ),
    Comparability.PARTIAL_MONTH: (
        "the change was deployed in the current month, so the observed figure covers "
        "part of a month while the prediction describes a whole one"
    ),
    Comparability.TAGS_NOT_ACTIVE: (
        "cost allocation tags were activated after deployment, so these resources have "
        "untagged history that attribution cannot see"
    ),
    Comparability.UNATTRIBUTED: (
        "no cost carried this application's tag; usually a tagging gap rather than a zero bill"
    ),
    Comparability.RESOURCES_DRIFTED: "what is running is no longer what was predicted",
}


def observations_for(
    provider: ObservationProvider, records: Sequence[PredictionRecord]
) -> list[tuple[PredictionRecord, Observation]]:
    """Pair each prediction with its observation, dropping those with no data at all.

    A record with no observation is not an exclusion — nothing was measured, so there is
    nothing to report about it. Exclusions are pairs where data exists and cannot be
    used, and those are counted.
    """
    pairs = []
    for record in records:
        observation = provider.observe(record)
        if observation is not None:
            pairs.append((record, observation))
    return pairs
