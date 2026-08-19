"""The Listing record plus the text-extraction helpers every parser shares."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse

SITE_UNKNOWN = "unknown"

# Alert emails route links through trackers; these are the query keys to drop
# so the same listing seen twice produces the same dedup key.
_TRACKING_PARAMS = re.compile(
    r"^(utm_|xtor|mtm_|pk_|_ga|gclid|fbclid|cmp|campaign|source|medium|email)",
    re.IGNORECASE,
)

_PRICE_RE = re.compile(r"(\d[\d\s .,]{2,})\s*(?:€|eur)", re.IGNORECASE)
_SURFACE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m\s*(?:²|2|²)", re.IGNORECASE)
_ROOMS_RE = re.compile(r"(\d+)\s*(?:pi[eè]ces?|p\.\b|\bP\b)", re.IGNORECASE)
_POSTAL_RE = re.compile(r"\b(\d{5})\b")


@dataclass
class Listing:
    """One property as extracted from an alert email."""

    site: str = SITE_UNKNOWN
    external_id: str | None = None
    url: str | None = None
    title: str | None = None
    price_eur: int | None = None
    surface_m2: float | None = None
    rooms: int | None = None
    city: str | None = None
    postal_code: str | None = None
    description: str | None = None
    photo_url: str | None = None
    provenance: str = "deterministic"

    def is_usable(self) -> bool:
        """A listing needs a link plus enough numbers to be worth analysing."""
        return bool(self.url) and self.price_eur is not None

    @property
    def price_per_sqm(self) -> float | None:
        if self.price_eur and self.surface_m2:
            return round(self.price_eur / self.surface_m2, 1)
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "site": self.site,
            "external_id": self.external_id,
            "url": self.url,
            "title": self.title,
            "price_eur": self.price_eur,
            "surface_m2": self.surface_m2,
            "rooms": self.rooms,
            "city": self.city,
            "postal_code": self.postal_code,
            "description": self.description,
            "photo_url": self.photo_url,
            "provenance": self.provenance,
        }


@dataclass
class ParsedEmail:
    site: str
    listings: list[Listing] = field(default_factory=list)
    provenance: str = "deterministic"


def clean_url(url: str | None) -> str | None:
    """Strip tracking parameters and fragments so URLs compare equal."""
    if not url:
        return None
    url = url.strip()
    if not url.startswith("http"):
        return None
    parsed = urlparse(url)
    kept = [
        pair
        for pair in parsed.query.split("&")
        if pair and not _TRACKING_PARAMS.match(pair.split("=", 1)[0])
    ]
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "&".join(kept), "")
    )


def parse_price(text: str | None) -> int | None:
    """First euro amount in `text`, as an int. Handles 250 000 € / 250.000€."""
    if not text:
        return None
    match = _PRICE_RE.search(text)
    if not match:
        return None
    digits = re.sub(r"[^\d]", "", match.group(1))
    if not digits:
        return None
    value = int(digits)
    # Guard against matching a charge or a phone number rather than a price.
    return value if 1_000 <= value <= 50_000_000 else None


def parse_surface(text: str | None) -> float | None:
    if not text:
        return None
    match = _SURFACE_RE.search(text)
    if not match:
        return None
    value = float(match.group(1).replace(",", "."))
    return value if 5 <= value <= 2000 else None


def parse_rooms(text: str | None) -> int | None:
    if not text:
        return None
    match = _ROOMS_RE.search(text)
    if not match:
        return None
    value = int(match.group(1))
    return value if 1 <= value <= 20 else None


def parse_postal_code(text: str | None) -> str | None:
    if not text:
        return None
    for candidate in _POSTAL_RE.findall(text):
        # French postal codes start at 01000; 00000 and years like 2024 aren't.
        if candidate[:2] != "00":
            return candidate
    return None


def id_from_url(url: str | None, pattern: str) -> str | None:
    if not url:
        return None
    match = re.search(pattern, url)
    return match.group(1) if match else None


def listing_key(listing: Listing) -> str:
    """Primary dedup key: site + the site's own listing id, else the URL."""
    if listing.external_id:
        return f"{listing.site}:{listing.external_id}"
    if listing.url:
        digest = hashlib.sha1(listing.url.encode("utf-8")).hexdigest()[:16]
        return f"{listing.site}:url:{digest}"
    return f"{listing.site}:unknown"


def fuzzy_key(listing: Listing) -> str | None:
    """Secondary key catching the same flat reposted or listed on another site.

    Deliberately excludes the price: bucketing a price puts two listings a few
    hundred euros apart on opposite sides of a boundary, which is exactly the
    case this key exists to catch. Callers match on this key and then compare
    prices as a range — see `Store.is_known`.
    """
    if listing.surface_m2 is None or not listing.postal_code:
        return None
    return f"{listing.postal_code}:{round(listing.surface_m2)}:{listing.rooms or 0}"
