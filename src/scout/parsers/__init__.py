"""Per-site alert-email parsers, with an LLM fallback."""

from __future__ import annotations

import logging
import re

from .base import Listing, ParsedEmail, SITE_UNKNOWN, listing_key
from . import bienici, leboncoin, logicimmo, pap, seloger

log = logging.getLogger(__name__)

# Ordered: the first parser whose `matches()` accepts the sender wins.
DETERMINISTIC_PARSERS = [
    leboncoin,
    seloger,
    pap,
    bienici,
    logicimmo,
]


def site_for_sender(sender: str) -> str:
    for parser in DETERMINISTIC_PARSERS:
        if parser.matches(sender):
            return parser.SITE
    return SITE_UNKNOWN


def parse_email(
    sender: str,
    subject: str,
    html: str,
    text: str,
    llm_fallback=None,
) -> ParsedEmail:
    """Parse one alert email into listings.

    Tries the deterministic parser for the sender's domain; falls back to the
    LLM when no parser matches or the parser returns nothing usable. The
    fallback is a callable so callers can pass None in tests and stay offline.
    """
    site = SITE_UNKNOWN
    listings: list[Listing] = []

    for parser in DETERMINISTIC_PARSERS:
        if not parser.matches(sender):
            continue
        site = parser.SITE
        try:
            listings = parser.parse(html or text)
        except Exception:  # a site redesign should degrade, not crash the run
            log.warning("deterministic parser failed for %s", site, exc_info=True)
            listings = []
        break

    usable = [l for l in listings if l.is_usable()]
    if usable:
        return ParsedEmail(site=site, listings=usable, provenance="deterministic")

    if llm_fallback is None:
        return ParsedEmail(site=site, listings=[], provenance="deterministic")

    log.info("falling back to LLM parsing for sender=%s site=%s", sender, site)
    llm_listings = llm_fallback(sender=sender, subject=subject, html=html, text=text)
    for listing in llm_listings:
        if listing.site == SITE_UNKNOWN and site != SITE_UNKNOWN:
            listing.site = site
    return ParsedEmail(
        site=site,
        listings=[l for l in llm_listings if l.is_usable()],
        provenance="llm_fallback",
    )


__all__ = [
    "Listing",
    "ParsedEmail",
    "listing_key",
    "parse_email",
    "site_for_sender",
    "SITE_UNKNOWN",
]
