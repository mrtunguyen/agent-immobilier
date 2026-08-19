"""DVF aggregation logic, exercised on synthetic rows — no network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.dvf import CACHE_MAX_AGE_DAYS, _cache_is_fresh, _mutation_price_per_sqm

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
