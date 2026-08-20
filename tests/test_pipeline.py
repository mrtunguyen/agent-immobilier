"""Config loading, hard filters, and dashboard rendering — all offline.

Behavioural tests run against a fixture in tmp_path, never the real
criteria.yaml: editing your own budget or target cities must not turn the suite
red. The one test that does read criteria.yaml only asserts it is coherent, and
names nothing that a change of search would invalidate.
"""

from __future__ import annotations

import pytest

from scout.config import load_criteria, load_criteria_set
from scout.dashboard import render
from scout.dedupe import Store
from scout.parsers.base import Listing
from scout.pipeline import passes_hard_filters

FIXTURE_YAML = """
budget:
  max_price_eur: 250000

target_cities:
  - name: "Lyon"
    postal_codes: ["69001", "69003"]
    avg_rent_per_sqm_eur: 15.5

min_surface_m2: 20
min_rooms: 1

scoring:
  score_threshold_notify: 70
"""


@pytest.fixture
def criteria(tmp_path):
    path = tmp_path / "criteria.yaml"
    path.write_text(FIXTURE_YAML, encoding="utf-8")
    return load_criteria_set(path).base


def test_the_real_criteria_file_is_coherent():
    """Guards the shipped criteria.yaml without pinning it to one search."""
    c = load_criteria()
    assert c.max_price_eur is None or c.max_price_eur > 0
    assert c.target_cities, "criteria.yaml must define at least one target city"
    assert all(city.postal_codes for city in c.target_cities)
    assert c.analysis_model and c.parsing_model
    assert 0 <= c.score_threshold_notify <= 100


def test_city_lookup_by_postal_code(criteria):
    assert criteria.city_for_postal_code("69003").name == "Lyon"
    assert criteria.city_for_postal_code("75001") is None
    assert criteria.city_for_postal_code(None) is None


def test_location_matching_falls_back_to_city_name(criteria):
    assert criteria.matches_location("69003", None) is True
    assert criteria.matches_location(None, "Lyon 3e") is True
    assert criteria.matches_location(None, "Marseille") is False


def good_listing(**overrides) -> Listing:
    base = dict(
        url="https://x/1", price_eur=200_000, surface_m2=50.0, rooms=2,
        postal_code="69003", city="Lyon",
    )
    base.update(overrides)
    return Listing(**base)


def test_matching_listing_passes(criteria):
    assert passes_hard_filters(good_listing(), criteria) is True


def test_over_budget_is_rejected(criteria):
    assert passes_hard_filters(good_listing(price_eur=900_000), criteria) is False


def test_too_small_is_rejected(criteria):
    assert passes_hard_filters(good_listing(surface_m2=12.0), criteria) is False


def test_wrong_city_is_rejected(criteria):
    listing = good_listing(postal_code="13001", city="Marseille")
    assert passes_hard_filters(listing, criteria) is False


def test_unknown_location_is_kept_for_the_model_to_judge(criteria):
    """Missing data shouldn't silently drop a listing before analysis."""
    listing = good_listing(postal_code=None, city=None)
    assert passes_hard_filters(listing, criteria) is True


def test_dashboard_renders_with_no_listings(tmp_path):
    with Store(tmp_path / "db.sqlite3") as store:
        out = render(store, tmp_path / "index.html")
    html = out.read_text(encoding="utf-8")
    assert "No listings yet" in html


def test_dashboard_renders_analysed_listing(tmp_path):
    with Store(tmp_path / "db.sqlite3") as store:
        key = store.insert(
            Listing(
                site="leboncoin",
                external_id="1",
                url="https://www.leboncoin.fr/ad/ventes_immobilieres/1",
                title="Appartement T3",
                price_eur=239_000,
                surface_m2=68.0,
                rooms=3,
                city="Lyon",
                postal_code="69003",
            )
        )
        store.save_analysis(
            key,
            {
                "score": 82,
                "gross_yield_pct": 6.2,
                "dvf_median_per_sqm": 4100.0,
                "dvf_sample_size": 42,
                "price_vs_dvf_pct": -14.3,
                "red_flags": ["rez-de-chaussée"],
                "analysis": "Priced below the DVF median for the 69003.",
                "enrichment_source": "email_only",
            },
        )
        out = render(store, tmp_path / "index.html")

    html = out.read_text(encoding="utf-8")
    assert "Appartement T3" in html
    assert "82" in html
    assert "-14%" in html
    assert "DVF 4 100" in html
    assert "rez-de-chaussée" in html


def test_dashboard_escapes_listing_titles(tmp_path):
    """Titles come from third-party emails — they must not inject markup."""
    with Store(tmp_path / "db.sqlite3") as store:
        store.insert(
            Listing(
                site="x", external_id="1", url="https://x/1",
                title="<script>alert(1)</script>", price_eur=1000,
            )
        )
        out = render(store, tmp_path / "index.html")
    html = out.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_dashboard_flags_a_thin_dvf_sample(tmp_path):
    """A median from 3 sales must not look as solid as one from 3,000."""
    cset = load_criteria_set(_write(tmp_path, FIXTURE_YAML + "\ndvf:\n  min_comparable_transactions: 5\n"))
    with Store(tmp_path / "db.sqlite3") as store:
        thin = store.insert(Listing(site="x", external_id="1", url="https://x/1",
                                    price_eur=200_000, surface_m2=50.0))
        solid = store.insert(Listing(site="x", external_id="2", url="https://x/2",
                                     price_eur=210_000, surface_m2=50.0))
        store.save_analysis(thin, {"score": 80, "dvf_median_per_sqm": 4100.0,
                                   "dvf_sample_size": 3})
        store.save_analysis(solid, {"score": 79, "dvf_median_per_sqm": 4100.0,
                                    "dvf_sample_size": 3131})
        out = render(store, tmp_path / "index.html", criteria_set=cset)

    html = out.read_text(encoding="utf-8")
    assert "n=3 ⚠" in html
    assert "n=3131 ⚠" not in html
    assert "note thin" in html


def _write(tmp_path, text: str):
    path = tmp_path / "criteria.yaml"
    path.write_text(text, encoding="utf-8")
    return path
