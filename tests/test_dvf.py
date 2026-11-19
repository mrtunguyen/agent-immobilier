"""DVF aggregation logic, exercised on synthetic rows — no network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.dvf import (
    CACHE_MAX_AGE_DAYS,
    MarketStats,
    _cache_is_fresh,
    _mutation_price_per_sqm,
)

TYPES = {"Appartement", "Maison"}


def row(**overrides) -> dict[str, str]:
    base = {
        "id_mutation": "2024-1",
        "nature_mutation": "Vente",
        "valeur_fonciere": "280000",
        "type_local": "Appartement",
        "surface_reelle_bati": "70",
        "code_postal": "69003",
    }
    base.update(overrides)
    return base


def test_simple_sale():
    assert _mutation_price_per_sqm([row()], TYPES) == pytest.approx(4000.0)


def test_multi_lot_sale_sums_surfaces_but_counts_price_once():
    """One sale spanning two lots must not be double-counted."""
    rows = [
        row(surface_reelle_bati="70"),
        row(surface_reelle_bati="30"),
    ]
    # 280 000 € for 100 m² total, not 280 000 € per lot.
    assert _mutation_price_per_sqm(rows, TYPES) == pytest.approx(2800.0)


def test_dependency_rows_are_ignored_in_the_surface():
    """A cellar sold with the flat shouldn't dilute the price per m²."""
    rows = [
        row(type_local="Appartement", surface_reelle_bati="70"),
        row(type_local="Dépendance", surface_reelle_bati="12"),
    ]
    assert _mutation_price_per_sqm(rows, TYPES) == pytest.approx(4000.0)


def test_mixed_sale_with_a_commercial_unit_is_rejected():
    """A flat sold together with a shop can't be attributed to either."""
    rows = [
        row(type_local="Appartement", surface_reelle_bati="70"),
        row(type_local="Local industriel. commercial ou assimilé", surface_reelle_bati="40"),
    ]
    assert _mutation_price_per_sqm(rows, TYPES) is None


def test_non_sale_mutations_are_rejected():
    assert _mutation_price_per_sqm([row(nature_mutation="Echange")], TYPES) is None


def test_zero_or_missing_values_are_rejected():
    assert _mutation_price_per_sqm([row(valeur_fonciere="")], TYPES) is None
    assert _mutation_price_per_sqm([row(valeur_fonciere="0")], TYPES) is None
    assert _mutation_price_per_sqm([row(surface_reelle_bati="")], TYPES) is None


def test_tiny_surfaces_are_rejected():
    """Parking spaces and cellars mis-typed as flats would skew the median."""
    assert _mutation_price_per_sqm([row(surface_reelle_bati="4")], TYPES) is None


def test_outlier_prices_are_rejected():
    # 280 000 € for 300 m² in central Lyon is a data error, not a bargain.
    assert _mutation_price_per_sqm([row(surface_reelle_bati="900")], TYPES) is None
    # And an implausibly high figure is a mis-keyed valeur_fonciere.
    assert (
        _mutation_price_per_sqm(
            [row(valeur_fonciere="99000000", surface_reelle_bati="70")], TYPES
        )
        is None
    )


def test_house_only_search_excludes_flats():
    assert _mutation_price_per_sqm([row(type_local="Appartement")], {"Maison"}) is None


def test_empty_input():
    assert _mutation_price_per_sqm([], TYPES) is None


# ------------------------------------------------------------------ cache TTL


def test_fresh_cache_is_reused():
    recent = datetime.now(timezone.utc) - timedelta(days=1)
    assert _cache_is_fresh(recent.isoformat()) is True


def test_stale_cache_is_refetched():
    old = datetime.now(timezone.utc) - timedelta(days=CACHE_MAX_AGE_DAYS + 1)
    assert _cache_is_fresh(old.isoformat()) is False


def test_missing_or_malformed_timestamp_is_not_fresh():
    assert _cache_is_fresh(None) is False
    assert _cache_is_fresh("not a date") is False


# ------------------------------------------------- sample-size confidence


def stats(sample_size: int, min_comparable: int, median=4100.0, **kw):
    return MarketStats("93100", median, 4200.0, sample_size, "dvf",
                       min_comparable, kw.get("fallback"))


def test_a_sample_at_or_above_the_threshold_is_confident():
    assert stats(5, 5).is_confident is True
    assert stats(3131, 5).is_confident is True


def test_a_thin_sample_is_not_confident_but_keeps_its_median():
    """Three real sales are still data — flagged, not discarded."""
    thin = stats(3, 5)
    assert thin.is_confident is False
    assert thin.median_per_sqm == 4100.0
    assert thin.sample_size == 3


def test_a_missing_median_is_never_confident():
    assert MarketStats("93100", None, None, 0, "unavailable", 5).is_confident is False


def test_one_sale_still_counts_when_no_threshold_is_configured():
    """min_comparable_transactions: 0 disables the check, not the data."""
    assert stats(1, 0).is_confident is True
    assert stats(0, 0).is_confident is False


def test_the_criteria_fallback_is_never_treated_as_confident():
    """A hand-typed estimate is not a sample of recorded sales."""
    guess = MarketStats("93100", 5500.0, None, 0, "criteria_fallback", 5)
    assert guess.is_confident is False


# ------------------------------------------------- caching around failures
#
# market_stats against a mocked network: a lookup that failed must never be
# remembered as "no sales here", and an empty answer must not stick for months.

import httpx

from scout.config import load_criteria_set
from scout.dedupe import Store
from scout.dvf import EMPTY_CACHE_MAX_AGE_DAYS, market_stats

from test_profiles import BASE_YAML, write_criteria

CSV_HEADER = "id_mutation,nature_mutation,valeur_fonciere,type_local,surface_reelle_bati,code_postal\n"
CSV_ONE_SALE = CSV_HEADER + "2024-1,Vente,280000,Appartement,70,69003\n"   # 4 000 €/m²


def _client(geo_status=200, year_status=200, failing_years=()):
    def handler(request: httpx.Request) -> httpx.Response:
        if "geo.api.gouv.fr" in request.url.host:
            if geo_status != 200:
                return httpx.Response(geo_status)
            return httpx.Response(200, json=[{"code": "69383"}])
        year = int(request.url.path.split("/")[4])
        if year in failing_years:
            return httpx.Response(500)
        if year_status == 404:
            return httpx.Response(404)
        return httpx.Response(200, text=CSV_ONE_SALE)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def criteria(tmp_path):
    return load_criteria_set(write_criteria(tmp_path, BASE_YAML)).base


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "db.sqlite3") as s:
        yield s


def test_a_failed_geo_lookup_is_not_cached(store, criteria):
    stats = market_stats("69003", store, criteria, _client(geo_status=503))
    assert stats.source == "unavailable"
    assert store.get_dvf("69003") is None          # retried next run


def test_a_genuine_no_sales_answer_is_cached(store, criteria):
    stats = market_stats("69003", store, criteria, _client(year_status=404))
    assert stats.source == "unavailable"
    assert store.get_dvf("69003")["sample_size"] == 0


def test_an_empty_cache_entry_is_retried_after_a_day(store, criteria):
    store.put_dvf("69003", None, None, 0)
    stale = datetime.now(timezone.utc) - timedelta(days=EMPTY_CACHE_MAX_AGE_DAYS + 1)
    store.conn.execute(
        "UPDATE dvf_cache SET fetched_at = ? WHERE cache_key = '69003'",
        (stale.isoformat(),),
    )

    stats = market_stats("69003", store, criteria, _client())
    assert stats.source == "dvf"
    assert stats.median_per_sqm == 4000.0
    assert store.get_dvf("69003")["median_per_sqm"] == 4000.0


def test_a_real_median_is_kept_for_the_full_cache_period(store, criteria):
    store.put_dvf("69003", 4000.0, 4000.0, 12)
    week_old = datetime.now(timezone.utc) - timedelta(days=7)
    store.conn.execute(
        "UPDATE dvf_cache SET fetched_at = ? WHERE cache_key = '69003'",
        (week_old.isoformat(),),
    )
    # Any network call would fail: the cached median must be served as is.
    stats = market_stats("69003", store, criteria, _client(geo_status=503))
    assert stats.source == "dvf_cache"
    assert stats.median_per_sqm == 4000.0


def test_a_partial_sample_is_used_but_not_cached(store, criteria):
    this_year = datetime.now(timezone.utc).year
    stats = market_stats(
        "69003", store, criteria, _client(failing_years={this_year - 1})
    )
    assert stats.source == "dvf"
    assert stats.median_per_sqm == 4000.0
    assert store.get_dvf("69003") is None
