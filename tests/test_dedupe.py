"""Dedup and persistence behaviour, against a temporary SQLite file."""

from __future__ import annotations

import json

import pytest

from scout.dedupe import Store
from scout.parsers.base import Listing, listing_key


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.sqlite3") as s:
        yield s


def make_listing(**overrides) -> Listing:
    base = dict(
        site="leboncoin",
        external_id="2412345678",
        url="https://www.leboncoin.fr/ad/ventes_immobilieres/2412345678",
        title="Appartement T3",
        price_eur=239_000,
        surface_m2=68.0,
        rooms=3,
        city="Lyon",
        postal_code="69003",
    )
    base.update(overrides)
    return Listing(**base)


def test_unknown_listing_is_new_then_known(store):
    listing = make_listing()
    assert store.is_known(listing) is False
    store.insert(listing)
    assert store.is_known(listing) is True


def test_second_run_finds_no_new_listings(store):
    """The core requirement: re-processing the same alert changes nothing."""
    listings = [make_listing(), make_listing(external_id="999", url="https://x/999")]
    for listing in listings:
        store.insert(listing)

    new = [l for l in listings if not store.is_known(l)]
    assert new == []


def test_same_flat_on_another_site_is_deduped_by_fuzzy_key(store):
    store.insert(make_listing())

    reposted = make_listing(
        site="seloger",
        external_id="87654321",
        url="https://www.seloger.com/annonces/87654321.htm",
        price_eur=237_000,  # small price drop
    )
    assert store.is_known(reposted) is True


def test_a_genuinely_different_flat_is_not_deduped(store):
    store.insert(make_listing())
    other = make_listing(
        site="seloger",
        external_id="87654321",
        url="https://www.seloger.com/annonces/87654321.htm",
        price_eur=310_000,
        surface_m2=95.0,
    )
    assert store.is_known(other) is False


def test_insert_is_idempotent(store):
    listing = make_listing()
    store.insert(listing)
    store.insert(listing)
    assert len(store.all_listings()) == 1


def test_price_per_sqm_is_stored(store):
    key = store.insert(make_listing())
    assert store.get(key)["price_per_sqm"] == pytest.approx(3514.7, abs=0.1)


def test_save_analysis_round_trips(store):
    key = store.insert(make_listing())
    store.save_analysis(
        key,
        {
            "dvf_median_per_sqm": 4100.0,
            "dvf_sample_size": 42,
            "price_vs_dvf_pct": -14.3,
            "estimated_monthly_rent": 1020,
            "gross_yield_pct": 5.1,
            "tension_locative": "high",
            "dpe": "C",
            "red_flags": ["bail en cours"],
            "score": 74,
            "analysis": "Priced below the DVF median.",
            "enrichment_source": "email_only",
        },
    )

    row = store.get(key)
    assert row["score"] == 74
    assert row["dvf_sample_size"] == 42
    assert json.loads(row["red_flags"]) == ["bail en cours"]
    assert row["enrichment_source"] == "email_only"


def test_status_defaults_to_new_and_survives_analysis(store):
    key = store.insert(make_listing())
    assert store.get(key)["status"] == "New"
    store.save_analysis(key, {"score": 80})
    assert store.get(key)["status"] == "New"


def test_notified_flag_prevents_duplicate_pushes(store):
    key = store.insert(make_listing())
    assert store.get(key)["notified"] == 0
    store.mark_notified(key)
    assert store.get(key)["notified"] == 1


def test_listings_sort_by_score(store):
    low = store.insert(make_listing(external_id="1", url="https://x/1"))
    high = store.insert(make_listing(external_id="2", url="https://x/2", price_eur=1))
    store.save_analysis(low, {"score": 40})
    store.save_analysis(high, {"score": 90})
    assert [r["key"] for r in store.all_listings()] == [high, low]


def test_dvf_cache_round_trips(store):
    assert store.get_dvf("69003") is None
    store.put_dvf("69003", 4100.0, 4250.0, 42)
    cached = store.get_dvf("69003")
    assert cached["median_per_sqm"] == 4100.0
    assert cached["sample_size"] == 42

    store.put_dvf("69003", 4200.0, 4300.0, 50)
    assert store.get_dvf("69003")["median_per_sqm"] == 4200.0


def test_run_history_is_recorded(store):
    store.record_run(3, 12, 4, 2)
    runs = store.last_runs()
    assert runs[0]["emails_seen"] == 3
    assert runs[0]["listings_new"] == 4
