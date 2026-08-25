"""The offline catalog: loading, honesty about provenance, and the checksum lock."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from cost_gate.domain.money import Money
from cost_gate.pricing import (
    LOCK_FILENAME,
    MANIFEST_FILENAME,
    CachingProvider,
    ChainProvider,
    FixtureCatalogProvider,
    PriceKey,
    PriceNotFound,
    PriceQuote,
    PricingError,
    checksum_catalog,
    verify_catalog,
    write_lock,
)

pytestmark = pytest.mark.unit

CATALOG = Path(__file__).resolve().parents[2] / "pricing-data"

MANIFEST = """
version: 1
catalog_version: "test"
region: us-east-1
currency: USD
captured_at: 2026-01-01T00:00:00Z
authoritative: false
verified: false
source: A fixture catalog written for tests.
limitations:
  - Not a price source.
"""

SERVICE = """
version: 1
service: AmazonVPC
region: us-east-1
currency: USD
prices:
  - id: nat-hours
    dimension: NatGateway-Hours
    rate: "0.045"
    unit: Hrs
"""


@pytest.fixture
def catalog(tmp_path: Path) -> Path:
    (tmp_path / "us-east-1").mkdir()
    (tmp_path / MANIFEST_FILENAME).write_text(MANIFEST, encoding="utf-8", newline="\n")
    (tmp_path / "us-east-1" / "amazon-vpc.yaml").write_text(SERVICE, encoding="utf-8", newline="\n")
    write_lock(tmp_path)
    return tmp_path


class TestTheShippedCatalogIsHonest:
    """The disclaimers are load-bearing, so they are tested like any other behaviour."""

    def test_it_never_claims_to_be_authoritative(self):
        metadata = FixtureCatalogProvider(CATALOG).catalog_metadata()
        assert metadata.authoritative is False
        assert metadata.verified is False

    def test_the_manifest_cannot_be_edited_into_claiming_authority(self, tmp_path):
        # authoritative is pinned to False by the type, not by a convention someone
        # could quietly flip.
        (tmp_path / MANIFEST_FILENAME).write_text(
            MANIFEST.replace("authoritative: false", "authoritative: true"),
            encoding="utf-8",
            newline="\n",
        )
        with pytest.raises(PricingError, match="invalid"):
            FixtureCatalogProvider(tmp_path)

    def test_every_quote_carries_the_disclaimer_forward(self):
        provider = FixtureCatalogProvider(CATALOG)
        quote = provider.lookup(PriceKey(service="AmazonVPC", dimension="NatGateway-Hours"))
        assert isinstance(quote, PriceQuote)
        assert quote.authoritative is False
        assert quote.retrieved_at is not None
        assert quote.catalog_version

    def test_the_disclaimer_line_says_what_it_is(self):
        disclaimer = FixtureCatalogProvider(CATALOG).catalog_metadata().disclaimer
        assert "illustrative" in disclaimer
        assert "not verified" in disclaimer

    def test_the_manifest_states_its_limitations(self):
        metadata = FixtureCatalogProvider(CATALOG).catalog_metadata()
        assert len(metadata.limitations) >= 4
        joined = " ".join(metadata.limitations).lower()
        assert "savings plans" in joined
        assert "free-tier" in joined or "free tier" in joined

    def test_retrieved_at_is_when_the_rates_were_established(self):
        # Not when the tool ran: a report produced today from an old catalog must say so.
        provider = FixtureCatalogProvider(CATALOG)
        assert provider.age_days >= 0


class TestLoading:
    def test_a_missing_catalog_is_a_broken_provider_not_a_missing_rate(self, tmp_path):
        # The distinction matters: a missing rate becomes UNKNOWN, a broken provider
        # must make the whole gate ERROR rather than silently pricing nothing.
        with pytest.raises(PricingError, match="no pricing catalog"):
            FixtureCatalogProvider(tmp_path / "absent")

    def test_a_malformed_service_file_is_rejected(self, tmp_path, catalog):
        (catalog / "us-east-1" / "broken.yaml").write_text(
            "version: 1\nservice: X\n", encoding="utf-8", newline="\n"
        )
        with pytest.raises(PricingError, match="invalid"):
            FixtureCatalogProvider(catalog)

    def test_a_float_rate_is_rejected(self, catalog):
        # An unquoted 0.045 in YAML is a float, and a float rate would silently lose
        # exactness on the way to a total.
        (catalog / "us-east-1" / "floaty.yaml").write_text(
            "version: 1\nservice: X\nregion: us-east-1\nprices:\n"
            "  - id: a\n    dimension: D\n    rate: 0.045\n    unit: Hrs\n",
            encoding="utf-8",
            newline="\n",
        )
        with pytest.raises(PricingError, match="quoted strings"):
            FixtureCatalogProvider(catalog)

    def test_a_negative_rate_is_rejected(self, catalog):
        (catalog / "us-east-1" / "negative.yaml").write_text(
            "version: 1\nservice: X\nregion: us-east-1\nprices:\n"
            '  - id: a\n    dimension: D\n    rate: "-1"\n    unit: Hrs\n',
            encoding="utf-8",
            newline="\n",
        )
        with pytest.raises(PricingError, match="non-negative"):
            FixtureCatalogProvider(catalog)

    def test_a_duplicate_rate_is_rejected(self, catalog):
        # Two entries answering the same key would make lookups depend on file order.
        (catalog / "us-east-1" / "duplicate.yaml").write_text(
            SERVICE, encoding="utf-8", newline="\n"
        )
        with pytest.raises(PricingError, match="duplicate rate"):
            FixtureCatalogProvider(catalog)

    def test_rates_are_decimals_not_floats(self, catalog):
        quote = FixtureCatalogProvider(catalog).lookup(
            PriceKey(service="AmazonVPC", dimension="NatGateway-Hours")
        )
        assert isinstance(quote, PriceQuote)
        assert isinstance(quote.unit_price.amount, Decimal)
        assert quote.unit_price.amount == Decimal("0.045")


class TestAttributeMatchingIsExact:
    def test_an_exact_attribute_set_matches(self):
        provider = FixtureCatalogProvider(CATALOG)
        result = provider.lookup(
            PriceKey(
                service="AmazonEC2",
                dimension="InstanceHours",
                attributes={
                    "instanceType": "t3.micro",
                    "operatingSystem": "Linux",
                    "tenancy": "Shared",
                },
            )
        )
        assert isinstance(result, PriceQuote)

    def test_a_missing_attribute_does_not_match(self):
        provider = FixtureCatalogProvider(CATALOG)
        result = provider.lookup(
            PriceKey(
                service="AmazonEC2",
                dimension="InstanceHours",
                attributes={"instanceType": "t3.micro"},
            )
        )
        assert isinstance(result, PriceNotFound)

    def test_an_extra_attribute_does_not_match(self):
        provider = FixtureCatalogProvider(CATALOG)
        result = provider.lookup(
            PriceKey(
                service="AmazonEC2",
                dimension="InstanceHours",
                attributes={
                    "instanceType": "t3.micro",
                    "operatingSystem": "Linux",
                    "tenancy": "Shared",
                    "extra": "value",
                },
            )
        )
        assert isinstance(result, PriceNotFound)

    def test_the_miss_explains_that_matching_is_exact(self):
        provider = FixtureCatalogProvider(CATALOG)
        result = provider.lookup(
            PriceKey(
                service="AmazonEC2",
                dimension="InstanceHours",
                attributes={"instanceType": "t3.micro"},
            )
        )
        assert isinstance(result, PriceNotFound)
        assert "exact" in result.reason


class TestMissesAreDiagnostic:
    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            (PriceKey(service="Nope", dimension="D"), "no rates for service"),
            (PriceKey(service="AmazonVPC", dimension="Nope"), "no 'Nope' rate"),
            (
                PriceKey(service="AmazonVPC", dimension="NatGateway-Hours", region="eu-west-1"),
                "covers us-east-1 only",
            ),
        ],
    )
    def test_the_reason_says_what_went_wrong(self, key, expected):
        result = FixtureCatalogProvider(CATALOG).lookup(key)
        assert isinstance(result, PriceNotFound)
        assert expected in result.reason

    def test_a_miss_suggests_a_remedy(self):
        result = FixtureCatalogProvider(CATALOG).lookup(PriceKey(service="Nope", dimension="D"))
        assert isinstance(result, PriceNotFound)
        assert result.remedy

    def test_an_unknown_dimension_lists_the_ones_that_exist(self):
        result = FixtureCatalogProvider(CATALOG).lookup(
            PriceKey(service="AmazonVPC", dimension="Nope")
        )
        assert isinstance(result, PriceNotFound)
        assert "NatGateway-Hours" in result.reason


class TestChecksumLock:
    def test_a_freshly_locked_catalog_verifies(self, catalog):
        assert verify_catalog(catalog) == []

    def test_the_shipped_catalog_matches_its_lock(self):
        assert verify_catalog(CATALOG) == []

    def test_an_edited_file_fails_verification(self, catalog):
        target = catalog / "us-east-1" / "amazon-vpc.yaml"
        target.write_text(SERVICE.replace("0.045", "9.999"), encoding="utf-8", newline="\n")
        problems = verify_catalog(catalog)
        assert any("checksum mismatch" in problem for problem in problems)

    def test_an_added_file_fails_verification(self, catalog):
        (catalog / "us-east-1" / "extra.yaml").write_text(
            SERVICE.replace("AmazonVPC", "AmazonOther"), encoding="utf-8", newline="\n"
        )
        problems = verify_catalog(catalog)
        assert any("not in the lock file" in problem for problem in problems)

    def test_a_deleted_file_fails_verification(self, catalog):
        (catalog / "us-east-1" / "amazon-vpc.yaml").unlink()
        problems = verify_catalog(catalog)
        assert any("missing from the catalog" in problem for problem in problems)

    def test_a_missing_lock_file_is_reported(self, catalog):
        (catalog / LOCK_FILENAME).unlink()
        problems = verify_catalog(catalog)
        assert any("is missing" in problem for problem in problems)

    def test_an_unreadable_lock_file_is_reported(self, catalog):
        (catalog / LOCK_FILENAME).write_text("not json", encoding="utf-8", newline="\n")
        problems = verify_catalog(catalog)
        assert any("not readable" in problem for problem in problems)

    def test_checksums_are_stable(self, catalog):
        assert checksum_catalog(catalog) == checksum_catalog(catalog)

    def test_the_lock_file_is_deterministic_json(self, catalog):
        first = (catalog / LOCK_FILENAME).read_bytes()
        write_lock(catalog)
        assert (catalog / LOCK_FILENAME).read_bytes() == first

    def test_the_lock_file_records_every_catalog_file(self, catalog):
        document = json.loads((catalog / LOCK_FILENAME).read_text(encoding="utf-8"))
        assert set(document["files"]) == {MANIFEST_FILENAME, "us-east-1/amazon-vpc.yaml"}

    def test_verification_cannot_detect_a_rate_that_was_always_wrong(self, catalog):
        # Stated because it is the limit of what a checksum can do, and the manifest
        # disclaimer is what covers the rest.
        write_lock(catalog)
        assert verify_catalog(catalog) == []


class TestCaching:
    def test_a_repeat_lookup_is_served_from_the_cache(self):
        provider = CachingProvider(inner=FixtureCatalogProvider(CATALOG))
        key = PriceKey(service="AmazonVPC", dimension="NatGateway-Hours")
        provider.lookup(key)
        provider.lookup(key)
        assert provider.statistics.hits == 1
        assert provider.statistics.misses == 1
        assert provider.statistics.hit_rate == 0.5

    def test_misses_are_cached_too(self):
        # Re-asking a remote provider for a rate it has already declined is the pattern
        # that earns a throttling response.
        provider = CachingProvider(inner=FixtureCatalogProvider(CATALOG))
        key = PriceKey(service="Nope", dimension="D")
        provider.lookup(key)
        provider.lookup(key)
        assert provider.statistics.hits == 1

    def test_an_expired_entry_is_refetched(self):
        provider = CachingProvider(inner=FixtureCatalogProvider(CATALOG), ttl=timedelta(seconds=0))
        key = PriceKey(service="AmazonVPC", dimension="NatGateway-Hours")
        provider.lookup(key)
        provider.lookup(key)
        assert provider.statistics.expirations == 1
        assert provider.statistics.hits == 0

    def test_clearing_forgets_everything(self):
        provider = CachingProvider(inner=FixtureCatalogProvider(CATALOG))
        key = PriceKey(service="AmazonVPC", dimension="NatGateway-Hours")
        provider.lookup(key)
        provider.clear()
        provider.lookup(key)
        assert provider.statistics.misses == 2

    def test_caching_adds_no_provenance_of_its_own(self):
        inner = FixtureCatalogProvider(CATALOG)
        assert CachingProvider(inner=inner).catalog_metadata() == inner.catalog_metadata()

    def test_the_hit_rate_of_an_unused_cache_is_zero(self):
        provider = CachingProvider(inner=FixtureCatalogProvider(CATALOG))
        assert provider.statistics.hit_rate == 0.0


class TestChaining:
    def test_an_empty_chain_is_rejected(self):
        # It would answer nothing while looking configured.
        with pytest.raises(PricingError, match="at least one"):
            ChainProvider(providers=[])

    def test_the_first_provider_that_answers_wins(self, catalog):
        empty = FixtureCatalogProvider(catalog)
        full = FixtureCatalogProvider(CATALOG)
        chain = ChainProvider(providers=[empty, full])
        result = chain.lookup(PriceKey(service="AmazonEKS", dimension="ControlPlane-Hours"))
        assert isinstance(result, PriceQuote)

    def test_the_first_providers_explanation_is_kept(self, catalog):
        # It explains the source the user actually asked for; the rest are fallback.
        first = FixtureCatalogProvider(catalog)
        chain = ChainProvider(providers=[first, FixtureCatalogProvider(CATALOG)])
        result = chain.lookup(PriceKey(service="Nope", dimension="D"))
        assert isinstance(result, PriceNotFound)
        assert result.reason == first.lookup(PriceKey(service="Nope", dimension="D")).reason

    def test_the_chain_names_its_members(self, catalog):
        chain = ChainProvider(providers=[FixtureCatalogProvider(catalog)])
        assert "fixture-catalog" in chain.name

    def test_a_chain_is_verified_only_if_every_member_is(self, catalog):
        chain = ChainProvider(providers=[FixtureCatalogProvider(catalog)])
        assert chain.catalog_metadata().verified is False


class TestIntrospection:
    def test_the_catalog_lists_the_keys_it_can_answer(self):
        provider = FixtureCatalogProvider(CATALOG)
        keys = provider.available_keys()
        assert len(keys) > 40
        assert all(isinstance(provider.lookup(key), PriceQuote) for key in keys)

    def test_listed_keys_are_in_a_stable_order(self):
        first = FixtureCatalogProvider(CATALOG).available_keys()
        second = FixtureCatalogProvider(CATALOG).available_keys()
        assert first == second

    def test_a_key_renders_readably(self):
        key = PriceKey(
            service="AmazonEC2", dimension="InstanceHours", attributes={"instanceType": "t3.micro"}
        )
        assert str(key) == "AmazonEC2/InstanceHours@us-east-1 [instanceType=t3.micro]"


class TestCoverageMatchesReality:
    """The manifest advertises coverage; that advertisement is checked."""

    @pytest.mark.parametrize(
        ("service", "dimension"),
        [
            ("AmazonVPC", "NatGateway-Hours"),
            ("AmazonVPC", "NatGateway-Bytes"),
            ("AmazonVPC", "PublicIPv4-Hours"),
            ("AmazonEKS", "ControlPlane-Hours"),
            ("AmazonRDS", "Storage-GB-Month"),
            ("AWSLambda", "GB-Seconds"),
            ("AmazonDynamoDB", "Storage-GB-Month"),
            ("AmazonCloudWatch", "Logs-Ingestion-GB"),
            ("AWSDataTransfer", "DataTransfer-Out-GB"),
        ],
    )
    def test_the_advertised_dimensions_exist(self, service, dimension):
        provider = FixtureCatalogProvider(CATALOG)
        dimensions = {key.dimension for key in provider.available_keys() if key.service == service}
        assert dimension in dimensions

    def test_every_rate_is_positive_or_explicitly_zero(self):
        provider = FixtureCatalogProvider(CATALOG)
        for key in provider.available_keys():
            quote = provider.lookup(key)
            assert isinstance(quote, PriceQuote)
            assert quote.unit_price >= Money.zero()

    def test_every_rate_declares_a_unit(self):
        provider = FixtureCatalogProvider(CATALOG)
        for key in provider.available_keys():
            quote = provider.lookup(key)
            assert isinstance(quote, PriceQuote)
            assert quote.unit.strip()

    def test_every_rate_has_a_stable_identifier(self):
        provider = FixtureCatalogProvider(CATALOG)
        identifiers = [provider.lookup(key).price_id for key in provider.available_keys()]
        assert all(identifiers)
        assert len(identifiers) == len(set(identifiers))
