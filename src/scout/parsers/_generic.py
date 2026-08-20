"""Link-anchored block extraction shared by every deterministic parser.

Alert-email templates differ per site and change without notice, but they all
share one shape: each listing is a block of markup containing exactly one link
to that listing's page. So rather than hard-coding each site's CSS classes, we
find the listing links and walk up to the smallest ancestor that holds the
price, then read the fields out of that block's text. That survives most
template redesigns; when it doesn't, the LLM fallback picks up the slack.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .base import (
    _PRICE_RE,
    Listing,
    clean_url,
    id_from_url,
    parse_postal_code,
    parse_price,
    parse_rooms,
    parse_surface,
)

# How far up the tree to look for the block that holds a listing's details.
_MAX_ANCESTOR_HOPS = 6
# How much further to look for the thumbnail, which often sits in a sibling cell.
_MAX_PHOTO_HOPS = 3


def _block_text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True))


def _listing_block(anchor, need_surface: bool = False):
    """Smallest ancestor of `anchor` whose text contains a price.

    `need_surface` keeps climbing until a surface appears too. Some templates
    wrap the price in its own link, so stopping at the first price would return
    a block holding nothing else; requiring a surface walks past that without
    reaching for a container that holds the whole email.
    """
    node = anchor
    for _ in range(_MAX_ANCESTOR_HOPS):
        parent = node.parent
        if parent is None:
            break
        node = parent
        text = _block_text(node)
        if parse_price(text) is None:
            continue
        if need_surface and parse_surface(text) is None:
            continue
        return node
    return node


# Tracking pixels, layout spacers and UI furniture.
_IMAGE_NOISE_RE = re.compile(r"(pixel|spacer|logo|icon|1x1|track)", re.IGNORECASE)
# Salesforce Marketing Cloud serves an email's own template assets from an
# account asset library. A property photo comes from the site's media CDN, so
# anything under /lib/<account>/m/ is furniture, however it happens to be named.
_TEMPLATE_ASSET_RE = re.compile(r"/lib/[0-9a-f]{8,}/m/", re.IGNORECASE)


def _first_image(node) -> str | None:
    for img in node.find_all("img"):
        src = (img.get("src") or "").strip()
        if not src.startswith("http"):
            continue
        if _IMAGE_NOISE_RE.search(src) or _TEMPLATE_ASSET_RE.search(src):
            continue
        return src
    return None


def _photo_url(block, url_re, anchor_re=None) -> str | None:
    """Find the listing's thumbnail, which often sits in a sibling cell.

    The price block is usually the text column of a two-column row, so we walk
    up until an image appears — but stop before any ancestor that contains a
    second listing link, or we'd hand this listing its neighbour's photo.
    """
    def is_listing_link(anchor) -> bool:
        if not url_re.search(anchor["href"]):
            return False
        # When selection narrowed on link text, the neighbour test has to narrow
        # the same way — or a tracking host makes every link look like a listing.
        return bool(anchor_re.search(_block_text(anchor))) if anchor_re else True

    node = block
    for _ in range(_MAX_PHOTO_HOPS + 1):
        if len([a for a in node.find_all("a", href=True) if is_listing_link(a)]) > 1:
            return None
        found = _first_image(node)
        if found:
            return found
        if node.parent is None:
            return None
        node = node.parent
    return None


# "Appartement à vendre", "Duplex à vendre - Première occupation": the property
# type plus its qualifiers, stopping at the first digit because what follows is
# always the price, room count or surface.
_TYPE_TITLE_RE = re.compile(
    r"((?:Appartement|Maison|Duplex|Studio|Loft|Villa|Terrain|Immeuble|Ch[âa]teau"
    r"|Propri[ée]t[ée]|Local|Parking|Garage)\b[^\d|•·]{0,70})",
    re.IGNORECASE,
)


def _title(anchor, block_text: str, prefer_block: bool = False) -> str | None:
    """The listing headline.

    Normally the link text is the headline. When anchors were selected by their
    call to action, that text reads "Voir l'annonce" for every listing, so the
    headline has to come out of the block: the property type and its qualifiers
    if they can be found, and the flattened block only as a last resort.
    """
    if not prefer_block:
        anchor_text = _block_text(anchor)
        if len(anchor_text) >= 8:
            return anchor_text[:200]

    match = _TYPE_TITLE_RE.search(block_text)
    if match:
        return match.group(1).strip(" -–—|·•,")[:200]

    # Whatever precedes the price, when the template leads with a headline.
    match = _PRICE_RE.search(block_text)
    if match and match.start() > 3:
        head = block_text[: match.start()].strip(" -–—|·•,")
        if len(head) >= 5:
            return head[:200]
    return block_text[:200] or None


def extract_listings(
    html: str,
    *,
    site: str,
    url_pattern: str,
    id_pattern: str | None = None,
    city_pattern: str | None = None,
    anchor_text_pattern: str | None = None,
    require_surface: bool = False,
) -> list[Listing]:
    """Pull every listing out of one alert email's HTML.

    `url_pattern` selects the anchors that point at a listing page;
    `id_pattern` extracts the site's own listing id from that URL.

    `anchor_text_pattern` narrows those anchors further by their own link text.
    Some sites route every link — listings, footer, unsubscribe — through one
    tracking host, and then the URL says nothing about what it points at; the
    per-listing call to action ("Voir l'annonce") is the only honest signal.
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    url_re = re.compile(url_pattern, re.IGNORECASE)
    anchor_re = (
        re.compile(anchor_text_pattern, re.IGNORECASE) if anchor_text_pattern else None
    )

    listings: list[Listing] = []
    seen_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        raw_href = anchor["href"]
        if not url_re.search(raw_href):
            continue
        if anchor_re and not anchor_re.search(_block_text(anchor)):
            continue
        url = clean_url(raw_href)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)

        block = _listing_block(anchor, need_surface=require_surface)
        text = _block_text(block)

        city = None
        if city_pattern:
            match = re.search(city_pattern, text)
            if match:
                city = match.group(1).strip()

        listings.append(
            Listing(
                site=site,
                external_id=id_from_url(url, id_pattern) if id_pattern else None,
                url=url,
                title=_title(anchor, text, prefer_block=anchor_re is not None),
                price_eur=parse_price(text),
                surface_m2=parse_surface(text),
                rooms=parse_rooms(text),
                city=city,
                postal_code=parse_postal_code(text),
                description=text[:1000] or None,
                photo_url=_photo_url(block, url_re, anchor_re),
            )
        )

    return _drop_shared_photos(listings)


def _drop_shared_photos(listings: list[Listing]) -> list[Listing]:
    """Null out any image that several listings claim.

    A per-listing thumbnail is unique by definition, so a repeated one is a
    placeholder the template reuses in every slot. Sending that to Telegram as
    "the listing photo" is worse than sending no photo at all.
    """
    counts: dict[str, int] = {}
    for listing in listings:
        if listing.photo_url:
            counts[listing.photo_url] = counts.get(listing.photo_url, 0) + 1
    for listing in listings:
        if listing.photo_url and counts[listing.photo_url] > 1:
            listing.photo_url = None
    return listings
