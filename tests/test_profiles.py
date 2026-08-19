"""Multiple search profiles: loading, matching, storage, and rendering.

Offline like the rest of the suite — profile YAML is written to tmp_path and the
store runs against a temporary SQLite file.
"""

from __future__ import annotations

import sqlite3

import pytest

from scout.config import (
    DEFAULT_PROFILE_NAME,
    load_criteria,
    load_criteria_set,
)
from scout.dashboard import render
from scout.dedupe import Store
from scout.notify_telegram import format_listing
from scout.parsers.base import Listing
from scout.pipeline import matching_profiles, passes_hard_filters

BASE_YAML = """
budget:
  max_price_eur: 250000
  min_price_eur: 50000

target_cities:
  - name: "Lyon"
    postal_codes: ["69001", "69003"]
    avg_rent_per_sqm_eur: 15.5
  - name: "Villeurbanne"
    postal_codes: ["69100"]
    avg_rent_per_sqm_eur: 14.0

min_surface_m2: 20
min_rooms: 1

yield_thresholds:
  min_gross_yield_pct: 5.5
  target_gross_yield_pct: 7.0

dpe:
  acceptable_classes: ["A", "B", "C", "D"]

red_flag_keywords:
  - "travaux importants"

scoring:
  score_threshold_notify: 70
  weight_yield: 0.5

models:
  # Deliberately two different ids: the tests assert each reaches the right call.
  parsing_model: "gemini-3.5-flash-lite"
  analysis_model: "gemini-3.6-flash"

pipeline:
  max_listings_per_run: 200
"""

TWO_PROFILES_YAML = (
    BASE_YAML
    + """
profiles:
  - name: "studio-cashflow"
    label: "Studio · cashflow"
    budget:
      max_price_eur: 150000
    min_surface_m2: 18
    yield_thresholds:
      min_gross_yield_pct: 7.0
    cities: ["Lyon", "Villeurbanne"]

  - name: "family-t3"
    budget:
      max_price_eur: 380000
    min_surface_m2: 60
    min_rooms: 3
    scoring:
      score_threshold_notify: 80
    cities: ["Lyon"]
"""
)


def write_criteria(tmp_path, text: str):
    path = tmp_path / "criteria.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def listing(**overrides) -> Listing:
    base = dict(
        site="leboncoin",
        external_id="1",
        url="https://x/1",
        title="Appartement",
        price_eur=200_000,
        surface_m2=50.0,
        rooms=2,
        city="Lyon",
        postal_code="69003",
    )
    base.update(overrides)
    return Listing(**base)


# ------------------------------------------------------------------- loading


def test_no_profiles_block_yields_one_default_profile(tmp_path):
    cset = load_criteria_set(write_criteria(tmp_path, BASE_YAML))
    assert cset.names == [DEFAULT_PROFILE_NAME]
    assert cset.profiles[0] is cset.base


def test_profiles_are_loaded_in_order(tmp_path):
    cset = load_criteria_set(write_criteria(tmp_path, TWO_PROFILES_YAML))
    assert cset.names == ["studio-cashflow", "family-t3"]
    assert len(cset) == 2


def test_profile_overrides_win_and_the_rest_is_inherited(tmp_path):
    cset = load_criteria_set(write_criteria(tmp_path, TWO_PROFILES_YAML))
    studio = cset.get("studio-cashflow")

    assert studio.max_price_eur == 150_000          # overridden
    assert studio.min_gross_yield_pct == 7.0        # overridden
    assert studio.min_rooms == 1                    # inherited from the base
    assert studio.dpe_acceptable_classes == ["A", "B", "C", "D"]
    assert studio.score_threshold_notify == 70      # base default


def test_nested_override_keeps_untouched_siblings(tmp_path):
    """Setting budget.max_price_eur must not wipe budget.min_price_eur."""
    cset = load_criteria_set(write_criteria(tmp_path, TWO_PROFILES_YAML))
    assert cset.get("studio-cashflow").min_price_eur == 50_000
    assert cset.get("family-t3").target_gross_yield_pct == 7.0


def test_profile_can_raise_its_own_notify_threshold(tmp_path):
    cset = load_criteria_set(write_criteria(tmp_path, TWO_PROFILES_YAML))
    assert cset.get("family-t3").score_threshold_notify == 80


def test_cities_shorthand_restricts_target_cities(tmp_path):
    cset = load_criteria_set(write_criteria(tmp_path, TWO_PROFILES_YAML))
    assert [c.name for c in cset.get("family-t3").target_cities] == ["Lyon"]
    assert cset.get("family-t3").matches_location("69100", None) is False
    assert cset.get("studio-cashflow").matches_location("69100", None) is True


def test_lists_replace_rather_than_extend(tmp_path):
    yaml_text = (
        BASE_YAML
        + """
profiles:
  - name: "strict"
    red_flag_keywords: ["bail en cours"]
"""
    )
    cset = load_criteria_set(write_criteria(tmp_path, yaml_text))
    assert cset.get("strict").red_flag_keywords == ["bail en cours"]


def test_label_falls_back_to_the_name(tmp_path):
    cset = load_criteria_set(write_criteria(tmp_path, TWO_PROFILES_YAML))
    assert cset.get("studio-cashflow").display_name == "Studio · cashflow"
    assert cset.get("family-t3").display_name == "family-t3"


def test_disabled_profile_is_skipped(tmp_path):
    yaml_text = (
        BASE_YAML
        + """
profiles:
  - name: "on"
  - name: "off"
    enabled: false
"""
    )
    assert load_criteria_set(write_criteria(tmp_path, yaml_text)).names == ["on"]


def test_all_profiles_disabled_is_an_error(tmp_path):
    yaml_text = BASE_YAML + """
profiles:
  - name: "off"
    enabled: false
"""
    with pytest.raises(ValueError, match="disabled"):
        load_criteria_set(write_criteria(tmp_path, yaml_text))


def test_duplicate_profile_names_are_rejected(tmp_path):
    yaml_text = BASE_YAML + """
profiles:
  - name: "same"
  - name: "same"
"""
    with pytest.raises(ValueError, match="duplicate"):
        load_criteria_set(write_criteria(tmp_path, yaml_text))


def test_run_level_keys_in_a_profile_are_rejected(tmp_path):
    """Models and DVF settings are shared, so a per-profile value would be a lie."""
    yaml_text = BASE_YAML + """
profiles:
  - name: "cheap"
    models:
      analysis_model: "gemini-3.5-flash"
"""
    with pytest.raises(ValueError, match="run-level"):
        load_criteria_set(write_criteria(tmp_path, yaml_text))


def test_unknown_city_in_a_profile_is_rejected(tmp_path):
    yaml_text = BASE_YAML + """
profiles:
  - name: "elsewhere"
    cities: ["Bordeaux"]
"""
    with pytest.raises(ValueError, match="Bordeaux"):
        load_criteria_set(write_criteria(tmp_path, yaml_text))


def test_select_narrows_and_rejects_unknown_names(tmp_path):
    cset = load_criteria_set(write_criteria(tmp_path, TWO_PROFILES_YAML))
    assert cset.select(["family-t3"]).names == ["family-t3"]
    with pytest.raises(ValueError, match="unknown profile"):
        cset.select(["nope"])


def test_load_criteria_still_returns_the_base(tmp_path):
    """The single-Criteria entry point stays valid for run-level settings."""
    base = load_criteria(write_criteria(tmp_path, TWO_PROFILES_YAML))
    assert base.max_price_eur == 250_000
    assert base.analysis_model == "gemini-3.6-flash"


# ------------------------------------------------------------------ matching


def test_a_listing_matches_only_the_profile_it_fits(tmp_path):
    cset = load_criteria_set(write_criteria(tmp_path, TWO_PROFILES_YAML))

    studio = listing(price_eur=130_000, surface_m2=22.0, rooms=1)
    assert [p.name for p in matching_profiles(studio, cset)] == ["studio-cashflow"]

    family = listing(price_eur=370_000, surface_m2=68.0, rooms=3)
    assert [p.name for p in matching_profiles(family, cset)] == ["family-t3"]


def test_an_overlapping_listing_matches_both_profiles(tmp_path):
    cset = load_criteria_set(write_criteria(tmp_path, TWO_PROFILES_YAML))
    overlap = listing(price_eur=145_000, surface_m2=62.0, rooms=3)
    assert [p.name for p in matching_profiles(overlap, cset)] == [
        "studio-cashflow",
        "family-t3",
    ]


def test_a_listing_no_profile_wants_matches_nothing(tmp_path):
    cset = load_criteria_set(write_criteria(tmp_path, TWO_PROFILES_YAML))
    assert matching_profiles(listing(price_eur=900_000), cset) == []


def test_hard_filters_still_work_per_profile(tmp_path):
    cset = load_criteria_set(write_criteria(tmp_path, TWO_PROFILES_YAML))
    t3 = listing(price_eur=370_000, surface_m2=68.0, rooms=3)
    assert passes_hard_filters(t3, cset.get("family-t3")) is True
    assert passes_hard_filters(t3, cset.get("studio-cashflow")) is False


# ------------------------------------------------------------------- storage


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.sqlite3") as s:
        yield s


def test_one_listing_gets_one_verdict_per_profile(store):
    store.insert(listing(), ["studio-cashflow", "family-t3"])
    assert store.counts_by_profile() == {"family-t3": 1, "studio-cashflow": 1}
    assert len(store.all_listings()) == 2


def test_verdicts_are_scored_independently(store):
    key = store.insert(listing(), ["studio-cashflow", "family-t3"])
    store.save_analysis(key, {"score": 40, "analysis": "thin yield"}, "studio-cashflow")
    store.save_analysis(key, {"score": 88, "analysis": "good hold"}, "family-t3")

    assert store.get(key, "studio-cashflow")["score"] == 40
    assert store.get(key, "family-t3")["score"] == 88
    assert store.get(key, "family-t3")["analysis"] == "good hold"


def test_notifying_one_profile_leaves_the_other_pending(store):
    key = store.insert(listing(), ["a", "b"])
    store.mark_notified(key, "a")
    assert store.get(key, "a")["notified"] == 1
    assert store.get(key, "b")["notified"] == 0


def test_notion_page_ids_are_per_profile(store):
    key = store.insert(listing(), ["a", "b"])
    store.set_notion_page_id(key, "page-a", "a")
    assert store.get(key, "a")["notion_page_id"] == "page-a"
    assert store.get(key, "b")["notion_page_id"] is None


def test_dedupe_is_profile_blind(store):
    """A second profile must not re-open a listing already in the database."""
    first = listing()
    store.insert(first, ["a"])
    assert store.is_known(listing()) is True


def test_all_listings_can_be_filtered_to_one_profile(store):
    key = store.insert(listing(), ["a", "b"])
    store.save_analysis(key, {"score": 70}, "a")
    rows = store.all_listings(profile="a")
    assert [r["profile"] for r in rows] == ["a"]
    assert rows[0]["score"] == 70


def test_profiles_seen_lists_what_is_in_the_database(store):
    store.insert(listing(), ["b", "a"])
    assert store.profiles_seen() == ["a", "b"]


def test_insert_defaults_to_the_default_profile(store):
    key = store.insert(listing())
    assert store.get(key)["profile"] == DEFAULT_PROFILE_NAME


# ----------------------------------------------------------------- migration


PRE_PROFILE_SCHEMA = """
CREATE TABLE listings (
    key TEXT PRIMARY KEY, fuzzy_key TEXT, site TEXT NOT NULL, external_id TEXT,
    url TEXT NOT NULL, title TEXT, price_eur INTEGER, surface_m2 REAL,
    rooms INTEGER, city TEXT, postal_code TEXT, description TEXT,
    photo_url TEXT, provenance TEXT, enrichment_source TEXT,
    price_per_sqm REAL, dvf_median_per_sqm REAL, dvf_sample_size INTEGER,
    price_vs_dvf_pct REAL, estimated_monthly_rent INTEGER, gross_yield_pct REAL,
    tension_locative TEXT, dpe TEXT, red_flags TEXT, score INTEGER,
    analysis TEXT, status TEXT NOT NULL DEFAULT 'New',
    notified INTEGER NOT NULL DEFAULT 0, notion_page_id TEXT,
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
"""


def test_a_pre_profile_database_is_migrated_onto_the_default_profile(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(PRE_PROFILE_SCHEMA)
    conn.execute(
        """
        INSERT INTO listings (key, site, url, title, price_eur, surface_m2,
                              score, analysis, status, notified, notion_page_id,
                              enrichment_source, first_seen_at, last_seen_at)
        VALUES ('k1','leboncoin','https://x/1','T3',239000,68.0,
                74,'Priced below the DVF median.','Interested',1,'page-1',
                'email_only','2026-01-01T00:00:00+00:00','2026-01-02T00:00:00+00:00')
        """
    )
    conn.commit()
    conn.close()

    with Store(path) as store:
        row = store.get("k1")
        assert row["profile"] == DEFAULT_PROFILE_NAME
        assert row["score"] == 74
        assert row["status"] == "Interested"
        assert row["notified"] == 1
        assert row["notion_page_id"] == "page-1"
        assert row["enrichment_source"] == "email_only"
        assert row["title"] == "T3"
        assert row["last_seen_at"] == "2026-01-02T00:00:00+00:00"
        # Analysis columns are gone from listings; verdicts owns them now.
        columns = {r[1] for r in store.conn.execute("PRAGMA table_info(listings)")}
        assert "score" not in columns


def test_migration_is_not_re_run_on_an_already_migrated_database(tmp_path):
    path = tmp_path / "db.sqlite3"
    with Store(path) as store:
        key = store.insert(listing(), ["a"])
        store.save_analysis(key, {"score": 55}, "a")
    with Store(path) as store:
        assert store.get(key, "a")["score"] == 55


# ------------------------------------------------------------- presentation


def test_telegram_message_names_the_profile(tmp_path):
    with Store(tmp_path / "db.sqlite3") as store:
        key = store.insert(listing(), ["studio-cashflow"])
        row = store.get(key, "studio-cashflow")

    body = format_listing(row, {"score": 78}, profile="Studio · cashflow")
    assert "Studio · cashflow" in body
    # Omitted when a single profile is active, so nothing changes for one search.
    assert "🎯" not in format_listing(row, {"score": 78})


def test_dashboard_shows_a_profile_column_and_filter(tmp_path):
    cset = load_criteria_set(write_criteria(tmp_path, TWO_PROFILES_YAML))
    with Store(tmp_path / "db.sqlite3") as store:
        key = store.insert(
            listing(price_eur=145_000, surface_m2=62.0, rooms=3),
            ["studio-cashflow", "family-t3"],
        )
        store.save_analysis(key, {"score": 75, "analysis": "solid yield"}, "studio-cashflow")
        store.save_analysis(key, {"score": 75, "analysis": "thin for a hold"}, "family-t3")
        out = render(store, tmp_path / "index.html", criteria_set=cset)

    html = out.read_text(encoding="utf-8")
    assert 'id="profileFilter"' in html
    assert "Studio · cashflow" in html
    assert "family-t3" in html
    # Same score, different bars: 75 clears studio's 70 but not family's 80.
    assert html.count('data-profile="studio-cashflow"') == 1
    assert "Above notify bar (profile bar)" in html


def test_dashboard_hides_the_profile_column_for_a_single_profile(tmp_path):
    cset = load_criteria_set(write_criteria(tmp_path, BASE_YAML))
    with Store(tmp_path / "db.sqlite3") as store:
        store.insert(listing())
        out = render(store, tmp_path / "index.html", criteria_set=cset)

    html = out.read_text(encoding="utf-8")
    assert 'id="profileFilter"' not in html
    assert "Above notify bar (70)" in html


def test_dashboard_marks_a_retired_profile(tmp_path):
    """Verdicts from a profile you deleted stay visible, flagged as history."""
    cset = load_criteria_set(write_criteria(tmp_path, TWO_PROFILES_YAML))
    with Store(tmp_path / "db.sqlite3") as store:
        store.insert(listing(), ["old-search"])
        out = render(store, tmp_path / "index.html", criteria_set=cset)

    html = out.read_text(encoding="utf-8")
    assert 'class="tag retired"' in html
    assert "old-search" in html


# --------------------------------------------------------------- the run loop


def test_run_produces_one_verdict_per_matched_profile(tmp_path, monkeypatch):
    """The wiring end to end: shared work once, judgement once per profile."""
    from scout import analysis as analysis_mod
    from scout import dashboard as dashboard_mod
    from scout import dvf, enrich, pipeline
    from scout.config import Settings
    from scout.dvf import MarketStats

    cset = load_criteria_set(write_criteria(tmp_path, TWO_PROFILES_YAML))
    both = listing(url="https://x/both", price_eur=145_000, surface_m2=62.0, rooms=3)
    studio_only = listing(
        external_id="2", url="https://x/studio", price_eur=120_000,
        surface_m2=25.0, rooms=1,
    )

    fetches: list[str] = []
    scored: list[tuple[str, str]] = []

    monkeypatch.setattr(pipeline, "_gemini_client", lambda settings: object())
    monkeypatch.setattr(pipeline, "collect_emails", lambda settings, limit: ([], []))
    monkeypatch.setattr(
        pipeline, "extract_listings", lambda emails, criteria, client: [both, studio_only]
    )
    monkeypatch.setattr(
        dvf,
        "market_stats",
        lambda pc, store, criteria, http: MarketStats(pc or "", 4100.0, 4200.0, 42, "dvf"),
    )
    monkeypatch.setattr(
        enrich,
        "fetch_listing_page",
        lambda url, timeout, retries: fetches.append(url) or "page text",
    )
    monkeypatch.setattr(
        analysis_mod,
        "analyse",
        lambda client, lst, market, page, criteria: (
            scored.append((lst.url, criteria.name))
            or {"score": 90, "analysis": f"judged for {criteria.name}"}
        ),
    )
    monkeypatch.setattr(dashboard_mod, "render", lambda *a, **k: tmp_path / "index.html")

    settings = Settings(None, None, "key", None, None, None, None)
    with Store(tmp_path / "run.sqlite3") as store:
        stats = pipeline.run(settings, cset, store, dry_run=True)

        assert stats.listings_new == 2
        assert stats.analysed == 3          # one listing matched both profiles
        assert stats.per_profile == {"studio-cashflow": 2, "family-t3": 1}

        # The page fetch and DVF lookup are per listing, not per verdict.
        assert fetches == ["https://x/both", "https://x/studio"]
        assert sorted(scored) == [
            ("https://x/both", "family-t3"),
            ("https://x/both", "studio-cashflow"),
            ("https://x/studio", "studio-cashflow"),
        ]
        assert store.counts_by_profile() == {"family-t3": 1, "studio-cashflow": 2}


def test_selecting_one_profile_does_not_retire_the_others(tmp_path):
    """`--profile` narrows what runs, not what the dashboard reports on."""
    cset = load_criteria_set(write_criteria(tmp_path, TWO_PROFILES_YAML))
    narrowed = cset.select(["studio-cashflow"])
    assert narrowed.names == ["studio-cashflow"]
    assert [p.name for p in narrowed.all_profiles] == ["studio-cashflow", "family-t3"]

    with Store(tmp_path / "db.sqlite3") as store:
        store.insert(listing(), ["family-t3"])
        out = render(store, tmp_path / "index.html", criteria_set=narrowed)

    assert 'class="tag retired"' not in out.read_text(encoding="utf-8")
