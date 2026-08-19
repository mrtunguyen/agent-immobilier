"""seloger.com saved-search alert emails."""

from __future__ import annotations

from .base import Listing
from ._generic import extract_listings

SITE = "seloger"

_SENDER_HINTS = ("seloger", "selogerneuf")
_URL_PATTERN = r"seloger\.com/(?:annonces|detail|expose)"
_ID_PATTERN = r"(\d{7,})"


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
