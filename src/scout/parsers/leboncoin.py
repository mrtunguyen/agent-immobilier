"""leboncoin.fr saved-search alert emails."""

from __future__ import annotations

from .base import Listing
from ._generic import extract_listings

SITE = "leboncoin"

_SENDER_HINTS = ("leboncoin", "lbc.fr")
_URL_PATTERN = r"leboncoin\.fr/(?:ad|ventes_immobilieres|vi)/"
_ID_PATTERN = r"/(\d{6,})(?:\.htm|/|$|\?)"


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
