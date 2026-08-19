"""Scores one listing as a rental investment, grounded in DVF sale data.

The model gets the listing's own figures, the DVF median €/m² for its postal
code, and the user's criteria, then returns a structured verdict. The criteria
go in the system instruction, identical for every listing a profile judges in a
run — Gemini caches repeated prefixes implicitly, so there is no breakpoint
to place, only a stable prefix worth keeping stable.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from google.genai import types
from pydantic import BaseModel, Field

from .config import Criteria
from .dvf import MarketStats
from .parsers.base import Listing

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an experienced French rental-investment analyst. You evaluate one \
property at a time against an investor's stated criteria and score it out of \
100.

Ground your price judgement in the DVF figure supplied with each listing: DVF \
is the French public register of actual recorded sale prices, so it says what \
comparable properties in that postal code really sold for, not what sellers \
asked. When the DVF sample size is small (or the median is missing), say so in \
your analysis and treat the price comparison as weak evidence rather than \
guessing.

Estimating rent: use the investor's rent-per-m² figure for the area when one is \
given. Otherwise estimate from your knowledge of that local market and say in \
the analysis that the rent is an estimate. Gross yield is \
(monthly rent x 12) / asking price x 100 — compute it from your own rent \
estimate and the asking price, and leave it null if you cannot estimate rent.

Red flags worth raising: major works needed, high copropriété charges, ongoing \
legal proceedings, a poor DPE (E, F, G — "passoire thermique"), a ground floor \
or a flat over a noisy commercial unit, an existing tenant on a below-market \
lease, and anything that would block a rental (no bathroom, non-conforming \
surface, indivision). Only list flags you have actual evidence for in the \
supplied text — absence of information is not a red flag, though you may note \
in the analysis when critical information is missing.

Scoring guidance, weighted by the investor's stated weights:
- 85-100: buy-grade — meets or beats every threshold, priced under the DVF \
median, no serious flags.
- 70-84: worth a visit — solid on yield and price, minor concerns only.
- 50-69: marginal — one clear weakness (thin yield, above-market price, or a \
real flag).
- 25-49: poor fit for the stated criteria.
- 0-24: fails a hard constraint (out of budget, out of area, far too small).

Be sceptical and concrete. The investor would rather read "priced 12% above the \
DVF median for the 69003, and the 5.1% yield is below your 5.5% floor" than a \
paragraph of hedging. Write the analysis in English, one short paragraph.\
"""


class ListingAnalysis(BaseModel):
    price_per_sqm: float | None = Field(
        default=None, description="Asking price divided by living area, €/m²."
    )
    price_vs_dvf_pct: float | None = Field(
        default=None,
        description=(
            "How far the asking €/m² sits above (+) or below (-) the DVF "
            "median, as a percentage. Null when no DVF median was supplied."
        ),
    )
    estimated_monthly_rent: int | None = Field(
        default=None, description="Realistic achievable monthly rent in euros."
    )
    gross_yield_pct: float | None = Field(
        default=None, description="Annual rent as a percentage of asking price."
    )
    tension_locative: str | None = Field(
        default=None,
        description="Rental demand in this area: high, medium, or low, with a few words of why.",
    )
    dpe: str | None = Field(
        default=None, description="DPE energy class A-G if stated in the source text."
    )
    red_flags: list[str] = Field(
        default_factory=list,
        description="Concrete concerns evidenced in the supplied text, one short phrase each.",
    )
    score: int = Field(description="Overall fit against the criteria, 0-100.")
    analysis: str = Field(
        description="One short paragraph explaining the score to the investor."
    )


def _criteria_context(criteria: Criteria) -> str:
    """One profile's criteria, rendered once per run so it caches cleanly.

    With several profiles active each gets its own prefix, reused across every
    listing that profile judges.
    """
    cities = "\n".join(
        f"  - {c.name} ({', '.join(c.postal_codes)})"
        + (
            f" — reference rent {c.avg_rent_per_sqm_eur} €/m²/month"
            if c.avg_rent_per_sqm_eur
            else " — no reference rent supplied"
        )
        for c in criteria.target_cities
    )
    weights = "\n".join(
        f"  - {k.removeprefix('weight_')}: {v}"
        for k, v in sorted(criteria.scoring_weights.items())
    )
    budget_floor = (
        f"{criteria.min_price_eur:,} €" if criteria.min_price_eur else "none"
    )
    budget_cap = (
        f"{criteria.max_price_eur:,} €" if criteria.max_price_eur else "no stated cap"
    )
    return f"""\
<investor_criteria>
Search: {criteria.display_name} — score this property against these criteria \
only, and name the search in your analysis if it is relevant to the verdict.
Budget: up to {budget_cap} (floor: {budget_floor})
Target areas:
{cities}
Minimum surface: {criteria.min_surface_m2} m²
Minimum rooms: {criteria.min_rooms}
Ground floor acceptable: {"yes" if criteria.max_floor_ground_ok else "no — flag it"}
Gross yield floor: {criteria.min_gross_yield_pct}% (target {criteria.target_gross_yield_pct}%)
Acceptable DPE classes: {", ".join(criteria.dpe_acceptable_classes) or "any"}
Hard DPE rejection at or below: {criteria.dpe_reject_below or "none"}
Keywords the investor treats as warning signs: {", ".join(criteria.red_flag_keywords)}
Scoring weights:
{weights}
</investor_criteria>"""


def _listing_context(
    listing: Listing, market: MarketStats, page_text: str | None
) -> str:
    parts = [
        "<listing>",
        f"Site: {listing.site}",
        f"URL: {listing.url}",
        f"Title: {listing.title or 'not given'}",
        f"Asking price: {listing.price_eur:,} €" if listing.price_eur else "Asking price: not given",
        f"Surface: {listing.surface_m2} m²" if listing.surface_m2 else "Surface: not given",
        f"Rooms: {listing.rooms}" if listing.rooms else "Rooms: not given",
        f"Location: {listing.city or 'unknown'} {listing.postal_code or ''}".strip(),
    ]
    if listing.price_per_sqm:
        parts.append(f"Asking price per m²: {listing.price_per_sqm:,.0f} €/m²")
    if listing.description:
        parts.append(f"Description from the alert email: {listing.description}")
    parts.append("</listing>")

    parts.append("<dvf_market_data>")
    if market.median_per_sqm and market.source in ("dvf", "dvf_cache"):
        parts.append(
            f"Recorded sales in postal code {market.postal_code}: median "
            f"{market.median_per_sqm:,.0f} €/m² across {market.sample_size} "
            f"comparable transactions."
        )
        if market.mean_per_sqm:
            parts.append(f"Mean: {market.mean_per_sqm:,.0f} €/m².")
    elif market.median_per_sqm:
        parts.append(
            f"No DVF data available; the investor's own estimate for this area "
            f"is {market.median_per_sqm:,.0f} €/m². Treat it as weak evidence."
        )
    else:
        parts.append(
            "No sale-price data available for this area. Do not invent a "
            "market comparison; say the comparison could not be made."
        )
    parts.append("</dvf_market_data>")

    parts.append("<listing_page>")
    if page_text:
        parts.append(page_text)
    else:
        parts.append(
            "The full listing page could not be retrieved (the site blocks "
            "automated access). Work from the email content above alone, and "
            "note in your analysis which details are missing."
        )
    parts.append("</listing_page>")

    return "\n".join(parts)


def analyse(
    client,
    listing: Listing,
    market: MarketStats,
    page_text: str | None,
    criteria: Criteria,
) -> dict[str, Any] | None:
    """Score one listing. Returns None if the model call failed."""
    try:
        response = client.models.generate_content(
            model=criteria.analysis_model,
            contents=_listing_context(listing, market, page_text),
            config=types.GenerateContentConfig(
                # Identical for every listing this profile judges, which is what
                # makes Gemini's implicit prefix caching worth anything here.
                system_instruction=(
                    SYSTEM_PROMPT + "\n\n" + _criteria_context(criteria)
                ),
                response_mime_type="application/json",
                response_schema=ListingAnalysis,
                # Generous because thinking tokens draw on the same budget, and a
                # truncated response parses to None — losing the whole listing.
                max_output_tokens=8000,
                # We pass no tools; without this the SDK warns about automatic
                # function calling on every single call.
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            ),
        )
        parsed = response.parsed
    except Exception:
        log.warning("analysis failed for %s", listing.url, exc_info=True)
        return None

    if parsed is None:
        log.warning("analysis returned no parseable output for %s", listing.url)
        return None

    return {
        "price_per_sqm": parsed.price_per_sqm or listing.price_per_sqm,
        "dvf_median_per_sqm": market.median_per_sqm,
        "dvf_sample_size": market.sample_size,
        "price_vs_dvf_pct": parsed.price_vs_dvf_pct,
        "estimated_monthly_rent": parsed.estimated_monthly_rent,
        "gross_yield_pct": parsed.gross_yield_pct,
        "tension_locative": parsed.tension_locative,
        "dpe": parsed.dpe,
        "red_flags": parsed.red_flags,
        "score": parsed.score,
        "analysis": parsed.analysis,
        "enrichment_source": "full_page" if page_text else "email_only",
    }
