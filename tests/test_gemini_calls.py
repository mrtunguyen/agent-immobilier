"""The Gemini request shape, checked against the real SDK types, offline.

No network and no API key: a fake client records what the code asked for, and
the assertions are that the request is one the SDK would accept — the field
names are right, the pydantic response schemas convert, and a parsed response
maps back onto our own records. Everything above this layer is stubbed
elsewhere, so this is the only place a provider typo would surface.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.genai import types
from google.genai import _transformers as genai_transformers

from scout import analysis as analysis_mod
from scout.analysis import ListingAnalysis
from scout.config import load_criteria_set
from scout.dvf import MarketStats
from scout.parsers.base import Listing
from scout.parsers.llm_fallback import ExtractedListings, make_fallback

from test_profiles import BASE_YAML, write_criteria


class FakeModels:
    def __init__(self, parsed):
        self.parsed = parsed
        self.calls: list[dict] = []

    def generate_content(self, *, model, contents, config):
        # Keyword-only, matching genai.models.Models.generate_content.
        self.calls.append({"model": model, "contents": contents, "config": config})
        return SimpleNamespace(parsed=self.parsed)


class FakeClient:
    def __init__(self, parsed):
        self.models = FakeModels(parsed)


def listing() -> Listing:
    return Listing(
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


def market() -> MarketStats:
    return MarketStats("69003", 4100.0, 4250.0, 42, "dvf")


# --------------------------------------------------------------- the analysis


@pytest.fixture
def criteria(tmp_path):
    return load_criteria_set(write_criteria(tmp_path, BASE_YAML)).base


def test_analysis_request_is_a_valid_gemini_request(criteria):
    verdict = ListingAnalysis(score=82, analysis="Below the DVF median.")
    client = FakeClient(verdict)

    analysis_mod.analyse(client, listing(), market(), "page text", criteria)

    call = client.models.calls[0]
    assert call["model"] == "gemini-3.6-flash"
    # The SDK validates its own config type, so a renamed field fails here.
    assert isinstance(call["config"], types.GenerateContentConfig)
    assert call["config"].response_mime_type == "application/json"
    assert call["config"].response_schema is ListingAnalysis
    assert call["config"].max_output_tokens == 8000
    # Criteria belong in the system instruction: it is the prefix Gemini caches.
    assert "investor_criteria" in call["config"].system_instruction
    assert "250,000" in call["config"].system_instruction
    # The listing itself is the turn content, never the cached prefix.
    assert "69003" in call["contents"]
    assert "investor_criteria" not in call["contents"]


def test_analysis_maps_a_parsed_response_onto_the_verdict(criteria):
    parsed = ListingAnalysis(
        price_per_sqm=3515.0,
        price_vs_dvf_pct=-14.3,
        estimated_monthly_rent=1020,
        gross_yield_pct=5.1,
        tension_locative="high",
        dpe="C",
        red_flags=["bail en cours"],
        score=74,
        analysis="Priced below the DVF median for the 69003.",
    )
    result = analysis_mod.analyse(
        FakeClient(parsed), listing(), market(), "page text", criteria
    )

    assert result["score"] == 74
    assert result["gross_yield_pct"] == 5.1
    assert result["red_flags"] == ["bail en cours"]
    assert result["dvf_median_per_sqm"] == 4100.0     # from DVF, not the model
    assert result["enrichment_source"] == "full_page"


def test_analysis_returns_none_when_the_response_has_no_parsed_output(criteria):
    """Gemini truncating on the token budget must lose one listing, not the run."""
    assert analysis_mod.analyse(
        FakeClient(None), listing(), market(), None, criteria
    ) is None


def test_analysis_survives_an_api_error(criteria):
    class Exploding:
        class models:
            @staticmethod
            def generate_content(**kwargs):
                raise RuntimeError("429 RESOURCE_EXHAUSTED")

    assert analysis_mod.analyse(
        Exploding(), listing(), market(), None, criteria
    ) is None


# ----------------------------------------------------------- the parsing path


def test_fallback_request_is_a_valid_gemini_request():
    parsed = ExtractedListings(listings=[])
    client = FakeClient(parsed)

    make_fallback(client, "gemini-3.5-flash-lite")(
        sender="alerte@leboncoin.fr",
        subject="1 nouvelle annonce",
        html="<html><body><a href='https://www.leboncoin.fr/ad/ventes_immobilieres/9'>T2 Lyon</a></body></html>",
        text="",
    )

    call = client.models.calls[0]
    assert call["model"] == "gemini-3.5-flash-lite"
    assert isinstance(call["config"], types.GenerateContentConfig)
    assert call["config"].response_schema is ExtractedListings
    assert call["config"].max_output_tokens == 16000
    assert "alerte@leboncoin.fr" in call["contents"]


def test_fallback_turns_parsed_output_into_listings():
    parsed = ExtractedListings.model_validate(
        {
            "listings": [
                {
                    "url": "https://www.leboncoin.fr/ad/ventes_immobilieres/9?utm_source=alert",
                    "title": "T2 Lyon 3e",
                    "price_eur": 189_000,
                    "surface_m2": 44.0,
                    "rooms": 2,
                    "city": "Lyon",
                    "postal_code": "69003",
                },
                {"url": "", "title": "no link, dropped"},
            ]
        }
    )
    listings = make_fallback(FakeClient(parsed), "gemini-3.5-flash-lite")(
        sender="x", subject="y", html="<p>body</p>", text=""
    )

    assert len(listings) == 1
    assert listings[0].price_eur == 189_000
    assert listings[0].provenance == "llm_fallback"
    assert "utm_source" not in listings[0].url


def test_fallback_survives_an_api_error():
    class Exploding:
        class models:
            @staticmethod
            def generate_content(**kwargs):
                raise RuntimeError("400 INVALID_ARGUMENT")

    assert make_fallback(Exploding(), "gemini-3.5-flash-lite")(
        sender="x", subject="y", html="<p>body</p>", text=""
    ) == []


# ------------------------------------------------------------------- schemas


@pytest.mark.parametrize("model", [ListingAnalysis, ExtractedListings])
def test_response_schemas_convert_for_gemini(model):
    """Gemini rejects some JSON Schema; our optional fields must survive it."""
    schema = genai_transformers.t_schema(None, model)
    assert schema is not None
    assert schema.model_dump(exclude_none=True)["properties"]


# ------------------------------------------- what the prompt says about DVF


def thin_market(fallback=None):
    """Three recorded sales against a threshold of five."""
    return MarketStats("93100", 4100.0, 4200.0, 3, "dvf", 5, fallback)


def test_a_confident_sample_is_stated_without_hedging(criteria):
    client = FakeClient(ListingAnalysis(score=70, analysis="."))
    analysis_mod.analyse(client, listing(), market(), None, criteria)

    contents = client.models.calls[0]["contents"]
    assert "median 4,100 €/m² across 42 comparable transactions" in contents
    assert "WARNING" not in contents


def test_a_thin_sample_is_flagged_as_weak_evidence(criteria):
    client = FakeClient(ListingAnalysis(score=70, analysis="."))
    analysis_mod.analyse(client, listing(), thin_market(), None, criteria)

    contents = client.models.calls[0]["contents"]
    # The median is still supplied — it is evidence, just not strong evidence.
    assert "median 4,100 €/m² across 3 comparable transactions" in contents
    assert "3 sale(s) is below the 5" in contents
    assert "weak evidence" in contents
    assert "do not let it dominate the score" in contents


def test_a_thin_sample_offers_the_investors_own_estimate_for_cross_checking(criteria):
    client = FakeClient(ListingAnalysis(score=70, analysis="."))
    analysis_mod.analyse(client, listing(), thin_market(fallback=5500.0), None, criteria)

    contents = client.models.calls[0]["contents"]
    assert "investor's own estimate for this area is 5,500 €/m²" in contents


def test_no_own_estimate_means_no_cross_check_line(criteria):
    client = FakeClient(ListingAnalysis(score=70, analysis="."))
    analysis_mod.analyse(client, listing(), thin_market(), None, criteria)

    assert "cross-check" not in client.models.calls[0]["contents"].lower()
