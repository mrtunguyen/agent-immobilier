"""Best-effort fetch of the full listing page.

leboncoin and SeLoger sit behind DataDome and will refuse most of these; that
is expected and fine. When the fetch fails we analyse the email content alone
and record `enrichment_source = "email_only"` so the provenance is visible.
Deliberately no proxy rotation or browser automation: the extra detail isn't
worth the fragility or the terms-of-service problem for a personal tool.
"""

from __future__ import annotations

import logging
import re
import time

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 6000

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Pages that come back as a bot-check interstitial rather than the listing.
_BLOCK_MARKERS = re.compile(
    r"(datadome|captcha|are you a human|acc[eè]s refus[eé]|blocked)", re.IGNORECASE
)


def fetch_listing_page(url: str, timeout_s: float, max_retries: int) -> str | None:
    """Return the page's visible text, or None if it couldn't be retrieved."""
    if not url:
        return None

    for attempt in range(max_retries + 1):
        try:
            with httpx.Client(
                follow_redirects=True, headers=_HEADERS, timeout=timeout_s
            ) as client:
                response = client.get(url)
            if response.status_code != 200:
                log.debug("enrichment %s -> HTTP %s", url, response.status_code)
                return None

            soup = BeautifulSoup(response.text, "lxml")
            for tag in soup(["script", "style", "nav", "footer", "head"]):
                tag.decompose()
            text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

            if not text or _BLOCK_MARKERS.search(text[:2000]):
                log.debug("enrichment %s -> anti-bot interstitial", url)
                return None
            return text[:MAX_TEXT_CHARS]

        except Exception as exc:
            log.debug("enrichment %s failed (attempt %d): %s", url, attempt + 1, exc)
            if attempt < max_retries:
                time.sleep(1.0)

    return None
