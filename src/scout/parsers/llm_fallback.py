"""LLM extraction for emails no deterministic parser could handle.

This is the safety net for site template changes: when a parser stops matching
(or a new site starts sending alerts), Gemini reads the email and produces the
same Listing records the parsers would have. It costs one Flash call per email,
so it only runs when the cheap path came back empty.
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup
from google.genai import types
from pydantic import BaseModel, Field

from .base import Listing, SITE_UNKNOWN, clean_url

log = logging.getLogger(__name__)

# Long alert digests can be enormous; this bounds the cost of one fallback call.
MAX_INPUT_CHARS = 40_000

SYSTEM_PROMPT = """\
You extract French property listings from real-estate alert emails \
(leboncoin, SeLoger, PAP, Bien'ici, LogicImmo and similar).

An email may contain zero, one, or many listings — return one entry per \
distinct property. Read values exactly as written; never estimate or invent a \
missing field, leave it null instead.

Field notes:
- price_eur: the asking price in euros as an integer, no separators. Ignore \
monthly charges, agency fees, and loan estimates.
- surface_m2: living area in square metres (surface habitable / Carrez).
- rooms: number of principal rooms (pièces), not bedrooms (chambres).
- postal_code: the 5-digit French code when present.
- url: the listing's own link, copied verbatim from the email.
- description: a short excerpt of the listing's own wording, if any.

Skip anything that is not a property listing: adverts, mortgage offers, \
newsletter articles, unsubscribe footers, and links back to the search page.\
"""


class ExtractedListing(BaseModel):
    url: str = Field(description="Link to the listing page, copied verbatim.")
    title: str | None = Field(default=None, description="Listing headline.")
    price_eur: int | None = Field(default=None, description="Asking price in euros.")
    surface_m2: float | None = Field(default=None, description="Living area in m².")
    rooms: int | None = Field(default=None, description="Number of pièces.")
    city: str | None = Field(default=None, description="City or district name.")
    postal_code: str | None = Field(default=None, description="5-digit postal code.")
    description: str | None = Field(default=None, description="Short excerpt.")
    photo_url: str | None = Field(default=None, description="Main photo URL.")


class ExtractedListings(BaseModel):
    listings: list[ExtractedListing] = Field(
        description="Every distinct property listing found in the email."
    )


def html_to_compact_text(html: str, text: str = "") -> str:
    """Flatten an email to text, keeping links inline as [label](url).

    The link targets are the part the model most needs and the part a plain
    `get_text()` would throw away.
    """
    if not html:
        return (text or "")[:MAX_INPUT_CHARS]

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()

    for anchor in soup.find_all("a", href=True):
        label = anchor.get_text(" ", strip=True)
        anchor.replace_with(f"[{label}]({anchor['href']}) ")

    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        img.replace_with(f"[image]({src}) " if src.startswith("http") else " ")

    flattened = re.sub(r"[ \t]+", " ", soup.get_text("\n", strip=True))
    flattened = re.sub(r"\n{3,}", "\n\n", flattened)
    return flattened[:MAX_INPUT_CHARS]


def make_fallback(client, model: str):
    """Build the callable that `parsers.parse_email` uses as its fallback."""

    def fallback(*, sender: str, subject: str, html: str, text: str) -> list[Listing]:
        body = html_to_compact_text(html, text)
        if not body.strip():
            return []

        try:
            response = client.models.generate_content(
                model=model,
                contents=(
                    f"From: {sender}\nSubject: {subject}\n\n"
                    f"--- email body ---\n{body}"
                ),
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    response_schema=ExtractedListings,
                    # A digest can hold dozens of listings, and thinking tokens
                    # come out of the same budget.
                    max_output_tokens=16000,
                    # No tools here either — silences the SDK's AFC warning.
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                ),
            )
            parsed = response.parsed
        except Exception:
            log.warning("LLM fallback parsing failed for %s", sender, exc_info=True)
            return []

        if parsed is None:
            log.warning("LLM fallback returned no parseable output for %s", sender)
            return []

        listings: list[Listing] = []
        for item in parsed.listings:
            url = clean_url(item.url)
            if not url:
                continue
            listings.append(
                Listing(
                    site=SITE_UNKNOWN,
                    external_id=None,
                    url=url,
                    title=item.title,
                    price_eur=item.price_eur,
                    surface_m2=item.surface_m2,
                    rooms=item.rooms,
                    city=item.city,
                    postal_code=item.postal_code,
                    description=item.description,
                    photo_url=item.photo_url,
                    provenance="llm_fallback",
                )
            )
        return listings

    return fallback
