"""pap.fr (De Particulier à Particulier) saved-search alert emails."""

from __future__ import annotations

from .base import Listing
from ._generic import extract_listings

SITE = "pap"

_SENDER_HINTS = ("pap.fr", "particulier")
_URL_PATTERN = r"pap\.fr/annonces/"
_ID_PATTERN = r"r(\d{6,})"


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
