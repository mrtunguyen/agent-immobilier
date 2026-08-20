"""seloger.com saved-search alert emails.

Two templates in the wild, and the parser handles both:

* the older one links straight to `seloger.com/annonces/...`;
* the current one routes *every* link — listings, header, footer, unsubscribe —
  through `click.by.seloger.com?qs=<opaque token>`, so the URL no longer says
  what it points at. There the per-listing call to action is the signal: each
  listing block ends in exactly one "Voir l'annonce" link.

The tracking token is minted per send, so the same property arriving in two
alerts has two different URLs and therefore two different listing keys. Dedup
falls to the fuzzy key (postal code + surface + rooms + price), which is what it
is for.
"""

from __future__ import annotations

from ._generic import extract_listings
from .base import Listing

SITE = "seloger"

_SENDER_HINTS = ("seloger", "selogerneuf")
# Both the direct listing paths and the tracking host that replaced them.
_URL_PATTERN = r"seloger\.com/(?:annonces|detail|expose)|click\.by\.seloger\.com"
_ID_PATTERN = r"(\d{7,})"
# "Voir l'annonce", tolerating the curly apostrophe and any accent handling.
_ANCHOR_TEXT_PATTERN = r"voir\s+l\s*['’]?\s*annonce"
# "La Noue-Clos Français, Montreuil (93100)" -> Montreuil, and
# "... 56 m² Rosny-sous-Bois (93110)" -> Rosny-sous-Bois. Restricted to name
# characters: a looser class swallows the preceding price and surface, because
# in the second form there is no comma to stop at.
_CITY_PATTERN = r"([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'\- ]{1,40}?)\s*\(\d{5}\)"


def matches(sender: str) -> bool:
    sender = (sender or "").casefold()
    return any(hint in sender for hint in _SENDER_HINTS)


def parse(html: str) -> list[Listing]:
    listings = extract_listings(
        html,
        site=SITE,
        url_pattern=_URL_PATTERN,
        id_pattern=_ID_PATTERN,
        city_pattern=_CITY_PATTERN,
        anchor_text_pattern=_ANCHOR_TEXT_PATTERN,
        require_surface=True,
    )
    if listings:
        return listings

    # Older template: the listing title itself is the link, so there is no call
    # to action to anchor on.
    return extract_listings(
        html,
        site=SITE,
        url_pattern=r"seloger\.com/(?:annonces|detail|expose)",
        id_pattern=_ID_PATTERN,
        city_pattern=_CITY_PATTERN,
    )
