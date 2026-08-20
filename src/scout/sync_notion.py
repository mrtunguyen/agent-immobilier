"""Mirrors analysed listings into a Notion database for manual triage.

Notion is a projection of the SQLite store, not a second source of truth. The
one field flowing the other way is Status: the pipeline sets it on creation and
never touches it again, so a listing you mark Interested or Rejected in Notion
keeps that value even if the same listing reappears in a later alert.

With several profiles active a listing gets one row per profile it matched, so
you triage "worth a visit for the cashflow search" independently of the same
flat under a different set of thresholds.

Written against Notion API 2025-09-03 (notion-client 3.x), where a database
holds one or more *data sources* and the properties live on the data source, not
the database. The id in your .env is the database id, so the data source is
resolved from it once per run and cached.
"""

from __future__ import annotations

import logging

from .config import DEFAULT_PROFILE_NAME

log = logging.getLogger(__name__)

# Property names expected on the Notion database. Create them with these exact
# names and types (see README) — Notion matches by name, not position. Matching
# ignores case and surrounding whitespace, because a column named "Profile "
# looks identical to "Profile" in the UI and is easy to create by accident.
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

# Without these two a row cannot be created or found again.
REQUIRED_PROPS = (PROP_NAME, PROP_KEY)


def _truncate(text: str | None, limit: int = 1900) -> str:
    """Notion caps a rich-text field at 2000 characters."""
    return (text or "")[:limit]


def _normalise(name: str) -> str:
    return name.strip().casefold()


class NotionSync:
    def __init__(self, token: str, database_id: str):
        from notion_client import Client  # imported lazily so tests stay offline

        self.client = Client(auth=token)
        self.database_id = database_id
        self._data_source_id: str | None = None
        # normalised property name -> the exact name Notion knows it by
        self._names: dict[str, str] | None = None

    # ------------------------------------------------------------ the schema

    def _resolve(self) -> bool:
        """Find the data source and map its property names. True if usable."""
        if self._names is not None:
            return bool(self._data_source_id)

        self._names = {}
        try:
            database = self.client.databases.retrieve(database_id=self.database_id)
            sources = database.get("data_sources") or []
            if not sources:
                log.warning("Notion database %s has no data source", self.database_id)
                return False
            # A database created through the UI has exactly one.
            self._data_source_id = sources[0]["id"]
            schema = self.client.data_sources.retrieve(
                data_source_id=self._data_source_id
            )
            self._names = {
                _normalise(name): name for name in (schema.get("properties") or {})
            }
        except Exception:
            log.warning("Notion schema lookup failed", exc_info=True)
            self._data_source_id = None
            return False

        missing = [p for p in REQUIRED_PROPS if not self._prop(p)]
        if missing:
            log.warning(
                "Notion database is missing required propert(ies) %s — add them and "
                "re-run; found: %s",
                ", ".join(repr(m) for m in missing),
                ", ".join(sorted(self._names.values())) or "none",
            )
            self._data_source_id = None
            return False

        optional = [
            p
            for p in (
                PROP_PROFILE, PROP_SCORE, PROP_PRICE, PROP_SURFACE, PROP_YIELD,
                PROP_CITY, PROP_URL, PROP_STATUS, PROP_ANALYSIS,
            )
            if not self._prop(p)
        ]
        if optional:
            log.info(
                "Notion database has no %s column(s); those fields won't be written",
                ", ".join(optional),
            )
        return True

    def _prop(self, wanted: str) -> str | None:
        """The name Notion actually uses for a property, or None if absent."""
        return (self._names or {}).get(_normalise(wanted))

    # -------------------------------------------------------------- the rows

    def _find_page(self, key: str, profile: str) -> str | None:
        """Locate an existing row.

        A listing has one row per profile, so the profile is part of the
        identity, not just a displayed field. When the database has no Profile
        column we can only match on the key.
        """
        key_prop = self._prop(PROP_KEY)
        profile_prop = self._prop(PROP_PROFILE)
        conditions = [{"property": key_prop, "rich_text": {"equals": key}}]
        if profile_prop:
            conditions.append(
                {"property": profile_prop, "rich_text": {"equals": profile}}
            )

        try:
            result = self.client.data_sources.query(
                data_source_id=self._data_source_id,
                filter={"and": conditions} if len(conditions) > 1 else conditions[0],
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

        # (expected name, value) — entries whose column is absent are dropped, so
        # a partially built database still gets everything it can hold.
        candidates: list[tuple[str, dict]] = [
            (PROP_NAME, {"title": [{"text": {"content": title}}]}),
            (PROP_KEY, {"rich_text": [{"text": {"content": listing_row["key"]}}]}),
            (PROP_PROFILE, {"rich_text": [{"text": {"content": profile}}]}),
            (PROP_URL, {"url": listing_row["url"]}),
            (PROP_CITY, {"rich_text": [{"text": {"content": location or "—"}}]}),
            (
                PROP_ANALYSIS,
                {"rich_text": [{"text": {"content": _truncate(analysis.get("analysis"))}}]},
            ),
        ]
        if analysis.get("score") is not None:
            candidates.append((PROP_SCORE, {"number": analysis["score"]}))
        if listing_row["price_eur"] is not None:
            candidates.append((PROP_PRICE, {"number": listing_row["price_eur"]}))
        if listing_row["surface_m2"] is not None:
            candidates.append((PROP_SURFACE, {"number": listing_row["surface_m2"]}))
        if analysis.get("gross_yield_pct") is not None:
            candidates.append((PROP_YIELD, {"number": analysis["gross_yield_pct"]}))
        if include_status:
            candidates.append((PROP_STATUS, {"select": {"name": "New"}}))

        props: dict = {}
        for wanted, value in candidates:
            actual = self._prop(wanted)
            if actual:
                props[actual] = value
        return props

    def upsert(
        self, listing_row, analysis: dict, profile: str = DEFAULT_PROFILE_NAME
    ) -> str | None:
        """Create or update the row for one listing under one profile.

        Returns the Notion page id.
        """
        if not self._resolve():
            return None

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
                parent={
                    "type": "data_source_id",
                    "data_source_id": self._data_source_id,
                },
                properties=self._properties(
                    listing_row, analysis, include_status=True, profile=profile
                ),
            )
            return page["id"]
        except Exception:
            log.warning("Notion sync failed for %s [%s]", key, profile, exc_info=True)
            return None
