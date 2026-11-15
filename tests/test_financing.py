"""Rent cover: the monthly mortgage payment, and how much of it the rent pays."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scout import analysis as analysis_mod
from scout.analysis import ListingAnalysis
from scout.config import load_criteria_set
from scout.dashboard import render
from scout.dedupe import Store
from scout.dvf import MarketStats
from scout.financing import Financing, financing_from_raw, rent_cover
from scout.notify_telegram import format_listing
from scout.pipeline import _verdict_from_row

from test_profiles import BASE_YAML, listing, write_criteria

FINANCING_YAML = """
financing:
  interest_rate_pct: 3.5
  duration_years: 20
  loan_pct_of_price: 100
  insurance_rate_pct: 0.30
"""

TERMS = Financing(interest_rate_pct=3.5, duration_years=20)


# ------------------------------------------------------------------ the maths


def test_monthly_payment_matches_the_standard_annuity():
    # 200 000 € at 3.5% over 20 years is 1 159.92 €/month in any bank simulator.
    assert TERMS.monthly_payment(200_000) == pytest.approx(1159.92, abs=0.01)


def test_insurance_is_charged_on_the_initial_capital():
    insured = Financing(3.5, 20, insurance_rate_pct=0.30)
    # 0.30% of 200 000 € a year is 50 €/month on top of the instalment.
    assert insured.monthly_payment(200_000) == pytest.approx(1209.92, abs=0.01)


def test_down_payment_shrinks_the_loan():
    ninety = Financing(3.5, 20, loan_pct_of_price=90)
    assert ninety.loan_amount(200_000) == 180_000
    assert ninety.monthly_payment(200_000) == pytest.approx(1159.92 * 0.9, abs=0.01)


def test_zero_rate_is_capital_only():
    assert Financing(0, 20).monthly_payment(240_000) == pytest.approx(1000)


def test_rent_cover_fields():
    assert rent_cover(200_000, 1160, TERMS) == {
        "monthly_payment_eur": 1160,
        "rent_cover_pct": 100.0,
    }
    # No rent estimate: the payment is still worth showing.
    assert rent_cover(200_000, None, TERMS)["rent_cover_pct"] is None
    assert rent_cover(200_000, None, TERMS)["monthly_payment_eur"] == 1160
    # Nothing to compute without terms or a price.
    empty = {"monthly_payment_eur": None, "rent_cover_pct": None}
    assert rent_cover(200_000, 1000, None) == empty
    assert rent_cover(None, 1000, TERMS) == empty


@pytest.mark.parametrize(
    "raw",
    [
        {"interest_rate_pct": 3.5, "duration_years": 0},
        {"interest_rate_pct": -1, "duration_years": 20},
        {"interest_rate_pct": 3.5, "duration_years": 20, "loan_pct_of_price": 0},
    ],
)
def test_nonsense_terms_are_rejected(raw):
    with pytest.raises(ValueError):
        financing_from_raw(raw)


# --------------------------------------------------------------------- config


def test_financing_loads_from_criteria_and_is_optional(tmp_path):
    without = load_criteria_set(write_criteria(tmp_path, BASE_YAML)).base
    assert without.financing is None

    with_terms = load_criteria_set(
        write_criteria(tmp_path, BASE_YAML + FINANCING_YAML)
    ).base
    assert with_terms.financing == Financing(3.5, 20, 100, 0.30)


def test_a_profile_can_override_the_loan_terms(tmp_path):
    text = (
        BASE_YAML
        + FINANCING_YAML
        + """
profiles:
  - name: "short-loan"
    financing:
      duration_years: 15
  - name: "base-terms"
"""
    )
    short, base = load_criteria_set(write_criteria(tmp_path, text)).profiles
    assert short.financing.duration_years == 15
    assert short.financing.interest_rate_pct == 3.5   # inherited
    assert base.financing.duration_years == 20


# ---------------------------------------------------------- analysis + output


@pytest.fixture
def criteria(tmp_path):
    return load_criteria_set(write_criteria(tmp_path, BASE_YAML + FINANCING_YAML)).base


def market() -> MarketStats:
    return MarketStats("69003", 4100.0, 4250.0, 42, "dvf")


def test_the_prompt_carries_the_goal_and_each_listing_payment(criteria):
    system = analysis_mod._criteria_context(criteria)
    assert "rent_cover" in system or "Rent cover" in system
    assert "3.5% over 20 years" in system

    contents = analysis_mod._listing_context(listing(), market(), None, criteria)
    assert "Monthly mortgage payment at the investor's terms: 1,210 €" in contents


def test_no_financing_leaves_the_prompt_as_it_was(tmp_path):
    plain = load_criteria_set(write_criteria(tmp_path, BASE_YAML)).base
    assert "Financing" not in analysis_mod._criteria_context(plain)
    assert "mortgage" not in analysis_mod._listing_context(listing(), market(), None, plain)


def test_the_verdict_carries_payment_and_cover(criteria):
    parsed = ListingAnalysis(estimated_monthly_rent=968, score=60, analysis="…")
    client = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **_: SimpleNamespace(parsed=parsed))
    )
    verdict = analysis_mod.analyse(client, listing(), market(), None, criteria)
    assert verdict["monthly_payment_eur"] == 1210
    assert verdict["rent_cover_pct"] == 80.0


def test_telegram_shows_the_cover(tmp_path, criteria):
    with Store(tmp_path / "db.sqlite3") as store:
        key = store.insert(listing(), ["default"])
        row = store.get(key, "default")

    covered = format_listing(row, {"score": 80, "monthly_payment_eur": 1210, "rent_cover_pct": 104.0})
    assert "✅ Rent covers 104% of the 1 210 €/mo mortgage" in covered
    short = format_listing(row, {"score": 60, "monthly_payment_eur": 1210, "rent_cover_pct": 80.0})
    assert "❌ Rent covers 80%" in short
    assert "🏦" not in format_listing(row, {"score": 60})


def test_stored_verdicts_get_the_cover_without_reanalysis(tmp_path, criteria):
    """Derived, not stored: existing rows pick it up from today's loan terms."""
    with Store(tmp_path / "db.sqlite3") as store:
        key = store.insert(listing(), ["default"])
        store.save_analysis(key, {"score": 60, "estimated_monthly_rent": 968}, "default")
        row = store.get(key, "default")

        assert _verdict_from_row(row, criteria.financing)["rent_cover_pct"] == 80.0
        assert _verdict_from_row(row)["rent_cover_pct"] is None

        criteria_set = load_criteria_set(
            write_criteria(tmp_path, BASE_YAML + FINANCING_YAML)
        )
        out = render(store, tmp_path / "index.html", criteria_set=criteria_set)
    html = out.read_text(encoding="utf-8")
    assert "Rent cover" in html
    assert "80%" in html and "1 210 €/mo" in html.replace(" ", " ").replace(",", " ")
