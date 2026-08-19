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


def _listing_block(anchor):
    """Smallest ancestor of `anchor` whose text contains a price."""
    node = anchor
    for _ in range(_MAX_ANCESTOR_HOPS):
        parent = node.parent
        if parent is None:
            break
        node = parent
        if parse_price(_block_text(node)) is not None:
            return node
    return node


def _first_image(node) -> str | None:
    for img in node.find_all("img"):
        src = (img.get("src") or "").strip()
        if not src.startswith("http"):
            continue
        # Skip tracking pixels and layout spacers.
        if re.search(r"(pixel|spacer|logo|1x1|track)", src, re.IGNORECASE):
            continue
        return src
    return None


def _photo_url(block, url_re) -> str | None:
    """Find the listing's thumbnail, which often sits in a sibling cell.

    The price block is usually the text column of a two-column row, so we walk
    up until an image appears — but stop before any ancestor that contains a
    second listing link, or we'd hand this listing its neighbour's photo.
    """
    node = block
    for _ in range(_MAX_PHOTO_HOPS + 1):
        if len([a for a in node.find_all("a", href=True) if url_re.search(a["href"])]) > 1:
            return None
        found = _first_image(node)
        if found:
            return found
        if node.parent is None:
            return None
        node = node.parent
    return None


def _title(anchor, block_text: str) -> str | None:
    anchor_text = _block_text(anchor)
    if len(anchor_text) >= 8:
        return anchor_text[:200]
    return block_text[:200] or None


def extract_listings(
    html: str,
    *,
    site: str,
    url_pattern: str,
    id_pattern: str | None = None,
    city_pattern: str | None = None,
) -> list[Listing]:
    """Pull every listing out of one alert email's HTML.

    `url_pattern` selects the anchors that point at a listing page;
    `id_pattern` extracts the site's own listing id from that URL.
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    url_re = re.compile(url_pattern, re.IGNORECASE)

    listings: list[Listing] = []
    seen_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        raw_href = anchor["href"]
        if not url_re.search(raw_href):
            continue
        url = clean_url(raw_href)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)

        block = _listing_block(anchor)
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
                title=_title(anchor, text),
                price_eur=parse_price(text),
                surface_m2=parse_surface(text),
                rooms=parse_rooms(text),
                city=city,
                postal_code=parse_postal_code(text),
                description=text[:1000] or None,
                photo_url=_photo_url(block, url_re),
            )
        )

    return listings
