"""Mirrors analysed listings into a Notion database for manual triage.

Notion is a projection of the SQLite store, not a second source of truth. The
one field flowing the other way is Status: the pipeline sets it on creation and
never touches it again, so a listing you mark Interested or Rejected in Notion
keeps that value even if the same listing reappears in a later alert.

With several profiles active a listing gets one row per profile it matched, so
you triage "worth a visit for the cashflow search" independently of the same
flat under a different set of thresholds.
"""

from __future__ import annotations

import logging

from .config import DEFAULT_PROFILE_NAME

log = logging.getLogger(__name__)

# Property names expected on the Notion database. Create them with these exact
# names and types (see README) — Notion matches by name, not position.
PROP_NAME = "Name"
PROP_KEY = "Listing Key"
PROP_PROFILE = "Profile"
PROP_SCORE = "Score"
PROP_PRICE = "Price"
PROP_SURFACE = "Surface"
PROP_YIELD = "Yield %"
PROP_CITY = "City"
PROP_URL = "URL"
PROP_STATUS = "Status"
PROP_ANALYSIS = "Analysis"


def _truncate(text: str | None, limit: int = 1900) -> str:
    """Notion caps a rich-text field at 2000 characters."""
    return (text or "")[:limit]


class NotionSync:
    def __init__(self, token: str, database_id: str):
        from notion_client import Client  # imported lazily so tests stay offline

        self.client = Client(auth=token)
        self.database_id = database_id

    def _find_page(self, key: str, profile: str) -> str | None:
        """Locate an existing row. A listing has one row per profile, so the
        profile is part of the identity, not just a displayed field."""
        try:
            result = self.client.databases.query(
                database_id=self.database_id,
                filter={
                    "and": [
                        {"property": PROP_KEY, "rich_text": {"equals": key}},
                        {"property": PROP_PROFILE, "rich_text": {"equals": profile}},
                    ]
                },
                page_size=1,
            )
        except Exception:
            log.warning("Notion lookup failed for %s [%s]", key, profile, exc_info=True)
            return None
        results = result.get("results") or []
        return results[0]["id"] if results else None

    def _properties(
        self, listing_row, analysis: dict, include_status: bool, profile: str
    ) -> dict:
        title = (listing_row["title"] or listing_row["url"] or "Listing")[:200]
        location = " ".join(
            p for p in (listing_row["city"], listing_row["postal_code"]) if p
        )

        props: dict = {
            PROP_NAME: {"title": [{"text": {"content": title}}]},
            PROP_KEY: {"rich_text": [{"text": {"content": listing_row["key"]}}]},
            PROP_PROFILE: {"rich_text": [{"text": {"content": profile}}]},
            PROP_URL: {"url": listing_row["url"]},
            PROP_CITY: {"rich_text": [{"text": {"content": location or "—"}}]},
            PROP_ANALYSIS: {
                "rich_text": [
                    {"text": {"content": _truncate(analysis.get("analysis"))}}
                ]
            },
        }
        if analysis.get("score") is not None:
            props[PROP_SCORE] = {"number": analysis["score"]}
        if listing_row["price_eur"] is not None:
            props[PROP_PRICE] = {"number": listing_row["price_eur"]}
        if listing_row["surface_m2"] is not None:
            props[PROP_SURFACE] = {"number": listing_row["surface_m2"]}
        if analysis.get("gross_yield_pct") is not None:
            props[PROP_YIELD] = {"number": analysis["gross_yield_pct"]}
        if include_status:
            props[PROP_STATUS] = {"select": {"name": "New"}}
        return props

    def upsert(
        self, listing_row, analysis: dict, profile: str = DEFAULT_PROFILE_NAME
    ) -> str | None:
        """Create or update the row for one listing under one profile.

        Returns the Notion page id.
        """
        key = listing_row["key"]
        page_id = listing_row["notion_page_id"] or self._find_page(key, profile)

        try:
            if page_id:
                # Status is deliberately excluded — it belongs to the user now.
                self.client.pages.update(
                    page_id=page_id,
                    properties=self._properties(
                        listing_row, analysis, include_status=False, profile=profile
                    ),
                )
                return page_id

            page = self.client.pages.create(
                parent={"database_id": self.database_id},
                properties=self._properties(
                    listing_row, analysis, include_status=True, profile=profile
                ),
            )
            return page["id"]
        except Exception:
            log.warning("Notion sync failed for %s [%s]", key, profile, exc_info=True)
            return None
