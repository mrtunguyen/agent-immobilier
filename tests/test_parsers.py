"""Parser tests run against saved alert-email fixtures — no network, no API."""

from __future__ import annotations

from pathlib import Path

import pytest

from scout import parsers
from scout.parsers import leboncoin, seloger
from scout.parsers.base import (
    Listing,
    clean_url,
    fuzzy_key,
    listing_key,
    parse_postal_code,
    parse_price,
    parse_rooms,
    parse_surface,
)

FIXTURES = Path(__file__).parent / "fixtures"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ------------------------------------------------------------------ extractors


@pytest.mark.parametrize(
    "text,expected",
    [
        ("239 000 €", 239_000),
        ("239.000€", 239_000),
        ("Prix : 1 250 000 EUR", 1_250_000),
        ("charges 45 €", None),  # below the plausible-price floor
        ("aucun prix ici", None),
    ],
)
def test_parse_price(text, expected):
    assert parse_price(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [("68 m²", 68.0), ("44,5 m2", 44.5), ("2 m²", None), ("pas de surface", None)],
)
def test_parse_surface(text, expected):
    assert parse_surface(text) == expected


def test_parse_rooms():
    assert parse_rooms("3 pièces") == 3
    assert parse_rooms("1 pièce") == 1
    assert parse_rooms("sans info") is None


def test_parse_postal_code_ignores_implausible_codes():
    assert parse_postal_code("Lyon 69003") == "69003"
    assert parse_postal_code("réf 00123") is None


def test_clean_url_strips_tracking_and_trailing_slash():
    url = clean_url(
        "https://www.leboncoin.fr/ad/ventes_immobilieres/24/?utm_source=a&id=7#top"
    )
    assert url == "https://www.leboncoin.fr/ad/ventes_immobilieres/24?id=7"


def test_clean_url_rejects_non_http():
    assert clean_url("mailto:someone@example.com") is None
    assert clean_url(None) is None


# --------------------------------------------------------------------- routing


def test_sender_routing():
    assert parsers.site_for_sender("alerte@leboncoin.fr") == "leboncoin"
    assert parsers.site_for_sender("no-reply@seloger.com") == "seloger"
    assert parsers.site_for_sender("news@example.org") == "unknown"


# -------------------------------------------------------------- site fixtures


def test_leboncoin_extracts_both_listings():
    listings = leboncoin.parse(read_fixture("leboncoin_alert.html"))
    assert len(listings) == 2, "search and unsubscribe links must not become listings"

    first = listings[0]
    assert first.site == "leboncoin"
    assert first.external_id == "2412345678"
    assert first.price_eur == 239_000
    assert first.surface_m2 == 68.0
    assert first.rooms == 3
    assert first.postal_code == "69003"
    assert first.photo_url and first.photo_url.endswith(".jpg")
    assert "utm_source" not in first.url

    second = listings[1]
    assert second.external_id == "2498765432"
    assert second.price_eur == 132_500
    assert second.postal_code == "69100"


def test_seloger_extracts_both_listings():
    listings = seloger.parse(read_fixture("seloger_alert.html"))
    assert len(listings) == 2
    assert listings[0].price_eur == 185_000
    assert listings[0].surface_m2 == 44.0
    assert listings[0].postal_code == "69007"
    assert listings[1].price_eur == 149_900


def test_parse_email_uses_deterministic_parser_and_skips_fallback():
    calls = []

    def fallback(**kwargs):
        calls.append(kwargs)
        return []

    result = parsers.parse_email(
        sender="alerte@leboncoin.fr",
        subject="2 nouvelles annonces",
        html=read_fixture("leboncoin_alert.html"),
        text="",
        llm_fallback=fallback,
    )

    assert result.site == "leboncoin"
    assert result.provenance == "deterministic"
    assert len(result.listings) == 2
    assert calls == [], "fallback must not run when the cheap path succeeded"


def test_parse_email_falls_back_when_no_parser_matches():
    fallback_listing = Listing(
        url="https://example.com/annonce/1", price_eur=200_000, site="unknown"
    )

    def fallback(**kwargs):
        return [fallback_listing]

    result = parsers.parse_email(
        sender="alerts@some-new-site.fr",
        subject="Nouvelle annonce",
        html="<html><body>whatever</body></html>",
        text="",
        llm_fallback=fallback,
    )

    assert result.provenance == "llm_fallback"
    assert len(result.listings) == 1


def test_parse_email_falls_back_when_template_changed():
    """A known sender whose markup no longer matches still yields listings."""

    def fallback(**kwargs):
        return [Listing(url="https://www.leboncoin.fr/ad/x/9", price_eur=100_000)]

    result = parsers.parse_email(
        sender="alerte@leboncoin.fr",
        subject="Redesigned template",
        html="<html><body><p>no listing links at all</p></body></html>",
        text="",
        llm_fallback=fallback,
    )

    assert result.provenance == "llm_fallback"
    assert result.listings[0].site == "leboncoin", "site is inferred from the sender"


def test_parse_email_without_fallback_returns_empty():
    result = parsers.parse_email(
        sender="alerts@unknown.fr", subject="", html="<html></html>", text=""
    )
    assert result.listings == []


# ------------------------------------------------------------------------ keys


def test_listing_key_prefers_external_id():
    listing = Listing(site="leboncoin", external_id="123", url="https://x/1")
    assert listing_key(listing) == "leboncoin:123"


def test_listing_key_falls_back_to_url_hash():
    listing = Listing(site="pap", url="https://www.pap.fr/annonces/r123")
    key = listing_key(listing)
    assert key.startswith("pap:url:")
    assert key == listing_key(listing), "key must be stable across calls"


def test_fuzzy_key_ignores_price():
    """Price is matched as a range by the store, not baked into the key."""
    a = Listing(price_eur=239_000, surface_m2=68, rooms=3, postal_code="69003")
    b = Listing(price_eur=210_000, surface_m2=68, rooms=3, postal_code="69003")
    assert fuzzy_key(a) == fuzzy_key(b) == "69003:68:3"


def test_fuzzy_key_needs_surface_and_postal_code():
    assert fuzzy_key(Listing(price_eur=200_000, surface_m2=50)) is None
    assert fuzzy_key(Listing(price_eur=200_000, postal_code="69003")) is None


# --------------------------------------------- seloger's tracking-link template


def test_seloger_tracker_digest_extracts_every_listing():
    """The current template routes all 46 links through one tracking host."""
    listings = seloger.parse(read_fixture("seloger_tracker_digest.html"))

    assert len(listings) == 5
    assert [l.price_eur for l in listings] == [155_000, 160_000, 169_000, 180_000, 195_000]
    assert [l.surface_m2 for l in listings] == [25.0, 48.9, 29.0, 40.7, 39.8]
    assert {l.rooms for l in listings} == {2}
    assert {l.city for l in listings} == {"Montreuil"}
    assert {l.postal_code for l in listings} == {"93100"}
    assert all(l.is_usable() for l in listings)


def test_seloger_tracker_titles_are_the_property_type_not_the_call_to_action():
    listings = seloger.parse(read_fixture("seloger_tracker_digest.html"))
    titles = [l.title for l in listings]

    assert "Duplex à vendre" in titles
    assert "Appartement à vendre - Première occupation" in titles
    assert not any("Voir l" in (t or "") for t in titles)


def test_seloger_tracker_carries_no_photo_rather_than_a_placeholder():
    """This template embeds no thumbnails at all.

    Every listing slot reuses one 270x200 placeholder from the email's own asset
    library. Sending that to Telegram as the listing photo would be worse than
    sending none, so both the shared-image and template-asset rules reject it.
    """
    listings = seloger.parse(read_fixture("seloger_tracker_digest.html"))
    assert [l.photo_url for l in listings] == [None] * 5

    single = seloger.parse(read_fixture("seloger_tracker_single.html"))
    assert single[0].photo_url is None


def test_seloger_tracker_ignores_the_search_criteria_summary():
    """The header repeats the search as "jusqu'à 200 000 €" and "2-3 pièces"."""
    listings = seloger.parse(read_fixture("seloger_tracker_digest.html"))

    assert 200_000 not in [l.price_eur for l in listings]


def test_seloger_tracker_single_listing_email():
    listings = seloger.parse(read_fixture("seloger_tracker_single.html"))

    assert len(listings) == 1
    listing = listings[0]
    assert listing.price_eur == 160_000
    assert listing.surface_m2 == 56.0
    assert listing.rooms == 3
    # No comma before the postal code in this template.
    assert listing.city == "Rosny-sous-Bois"
    assert listing.postal_code == "93110"


def test_seloger_tracker_urls_are_distinct_per_listing():
    """Dedup keys off the URL, so two listings must never share one."""
    listings = seloger.parse(read_fixture("seloger_tracker_digest.html"))
    urls = [l.url for l in listings]

    assert len(set(urls)) == len(urls)
    assert all("click.by.seloger.com" in u for u in urls)


def test_seloger_still_parses_the_older_direct_link_template():
    """The fallback path: no call to action, links straight to the listing."""
    listings = seloger.parse(read_fixture("seloger_alert.html"))

    assert len(listings) == 2
    assert all("seloger.com" in l.url for l in listings)
