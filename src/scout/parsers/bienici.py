"""bienici.com saved-search alert emails."""

from __future__ import annotations

from .base import Listing
from ._generic import extract_listings

SITE = "bienici"

_SENDER_HINTS = ("bienici", "bien-ici", "bien'ici")
_URL_PATTERN = r"bienici\.com/(?:annonce|realEstateAd)"
_ID_PATTERN = r"/annonce/[^/]+/[^/]+/([a-z0-9_-]{6,})"


def matches(sender: str) -> bool:
    sender = (sender or "").casefold()
    return any(hint in sender for hint in _SENDER_HINTS)


def parse(html: str) -> list[Listing]:
    return extract_listings(
        html,
        site=SITE,
        url_pattern=_URL_PATTERN,
        id_pattern=_ID_PATTERN,
    )
