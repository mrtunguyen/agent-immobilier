"""SQLite store: which listings we've seen, what we thought of them, DVF cache.

GitHub Actions is stateless, so this file is committed back to the repo at the
end of each run. It is the source of truth; Notion and the dashboard are
projections of it.

Two tables carry the listing state, split along the line between fact and
judgement. `listings` holds one row per property — what the ad says, and the
dedup keys that decide whether we have seen it before. `verdicts` holds one row
per (property, profile): the score, the analysis, and the triage state, because
the same flat can be a strong buy for one search and a clear pass for another.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import DATA_DIR, DEFAULT_PROFILE_NAME
from .parsers.base import Listing, fuzzy_key, listing_key

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = DATA_DIR / "listings.sqlite3"

STATUS_NEW = "New"
VALID_STATUSES = ("New", "Interested", "Rejected", "Contacted")

# Columns the analysis writes, in the order save_analysis binds them.
VERDICT_FIELDS = (
    "dvf_median_per_sqm",
    "dvf_sample_size",
    "price_vs_dvf_pct",
    "estimated_monthly_rent",
    "gross_yield_pct",
    "tension_locative",
    "dpe",
    "red_flags",
    "score",
    "analysis",
    "enrichment_source",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    key                     TEXT PRIMARY KEY,
    fuzzy_key               TEXT,
    site                    TEXT NOT NULL,
    external_id             TEXT,
    url                     TEXT NOT NULL,
    title                   TEXT,
    price_eur               INTEGER,
    surface_m2              REAL,
    rooms                   INTEGER,
    city                    TEXT,
    postal_code             TEXT,
    description             TEXT,
    photo_url               TEXT,
    provenance              TEXT,
    price_per_sqm           REAL,
    first_seen_at           TEXT NOT NULL,
    last_seen_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_listings_fuzzy ON listings(fuzzy_key);

-- One verdict per listing per profile: the same flat is judged once for each
-- search whose hard filters it passed.
CREATE TABLE IF NOT EXISTS verdicts (
    listing_key             TEXT NOT NULL REFERENCES listings(key) ON DELETE CASCADE,
    profile                 TEXT NOT NULL,
    dvf_median_per_sqm      REAL,
    dvf_sample_size         INTEGER,
    price_vs_dvf_pct        REAL,
    estimated_monthly_rent  INTEGER,
    gross_yield_pct         REAL,
    tension_locative        TEXT,
    dpe                     TEXT,
    red_flags               TEXT,   -- JSON array
    score                   INTEGER,
    analysis                TEXT,
    enrichment_source       TEXT,
    -- triage
    status                  TEXT NOT NULL DEFAULT 'New',
    notified                INTEGER NOT NULL DEFAULT 0,
    notion_page_id          TEXT,
    created_at              TEXT NOT NULL,
    PRIMARY KEY (listing_key, profile)
);

CREATE INDEX IF NOT EXISTS idx_verdicts_score ON verdicts(score DESC);
CREATE INDEX IF NOT EXISTS idx_verdicts_profile ON verdicts(profile);

CREATE TABLE IF NOT EXISTS dvf_cache (
    cache_key       TEXT PRIMARY KEY,   -- postal code or INSEE commune code
    median_per_sqm  REAL,
    mean_per_sqm    REAL,
    sample_size     INTEGER NOT NULL DEFAULT 0,
    fetched_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    emails_seen     INTEGER NOT NULL DEFAULT 0,
    listings_found  INTEGER NOT NULL DEFAULT 0,
    listings_new    INTEGER NOT NULL DEFAULT 0,
    notified        INTEGER NOT NULL DEFAULT 0,
    error           TEXT
);
"""

# Listing columns joined ahead of the verdict columns. Spelled out rather than
# `l.*, v.*` so a future column rename can't silently shadow one of the others.
_JOINED_COLUMNS = """
    l.key, l.fuzzy_key, l.site, l.external_id, l.url, l.title, l.price_eur,
    l.surface_m2, l.rooms, l.city, l.postal_code, l.description, l.photo_url,
    l.provenance, l.price_per_sqm, l.first_seen_at, l.last_seen_at,
    v.profile, v.dvf_median_per_sqm, v.dvf_sample_size, v.price_vs_dvf_pct,
    v.estimated_monthly_rent, v.gross_yield_pct, v.tension_locative, v.dpe,
    v.red_flags, v.score, v.analysis, v.enrichment_source, v.status,
    v.notified, v.notion_page_id
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _needs_split_migration(conn: sqlite3.Connection) -> bool:
    """True for a pre-profile database, where analysis lives on `listings`."""
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "listings" not in tables:
        return False
    columns = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
    return "score" in columns


def _split_listings_into_verdicts(conn: sqlite3.Connection) -> None:
    """Move analysis and triage columns off `listings` onto `verdicts`.

    Existing rows land on the default profile: before profiles existed there was
    exactly one search, and that is what those scores were judged against.
    """
    moved = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    conn.executescript(SCHEMA)
    conn.execute(
        f"""
        INSERT OR IGNORE INTO verdicts (
            listing_key, profile, {", ".join(VERDICT_FIELDS)},
            status, notified, notion_page_id, created_at
        )
        SELECT key, ?, {", ".join(VERDICT_FIELDS)},
               status, notified, notion_page_id, first_seen_at
        FROM listings
        """,
        (DEFAULT_PROFILE_NAME,),
    )
    conn.executescript(
        """
        CREATE TABLE listings_migrated (
            key                     TEXT PRIMARY KEY,
            fuzzy_key               TEXT,
            site                    TEXT NOT NULL,
            external_id             TEXT,
            url                     TEXT NOT NULL,
            title                   TEXT,
            price_eur               INTEGER,
            surface_m2              REAL,
            rooms                   INTEGER,
            city                    TEXT,
            postal_code             TEXT,
            description             TEXT,
            photo_url               TEXT,
            provenance              TEXT,
            price_per_sqm           REAL,
            first_seen_at           TEXT NOT NULL,
            last_seen_at            TEXT NOT NULL
        );
        INSERT INTO listings_migrated
            SELECT key, fuzzy_key, site, external_id, url, title, price_eur,
                   surface_m2, rooms, city, postal_code, description, photo_url,
                   provenance, price_per_sqm, first_seen_at, last_seen_at
            FROM listings;
        DROP TABLE listings;
        ALTER TABLE listings_migrated RENAME TO listings;
        """
    )
    conn.commit()
    log.info(
        "migrated %d listing(s) to the profile schema, on profile %r",
        moved,
        DEFAULT_PROFILE_NAME,
    )


class Store:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or DEFAULT_DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        if _needs_split_migration(self.conn):
            _split_listings_into_verdicts(self.conn)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---------------------------------------------------------------- listings

    # How far two asking prices may differ and still count as the same flat.
    FUZZY_PRICE_TOLERANCE = 0.05

    def is_known(self, listing: Listing) -> bool:
        """True if we've seen this listing before, exactly or by fuzzy match.

        Deliberately profile-blind: this asks whether the *property* is new, so
        adding a profile never re-opens listings already in the database.

        The fuzzy pass catches the same property listed on a second site or
        reposted after a price drop: same postal code, same size, same room
        count, and a price within a few percent.
        """
        key = listing_key(listing)
        row = self.conn.execute(
            "SELECT 1 FROM listings WHERE key = ?", (key,)
        ).fetchone()
        if row:
            return True

        fkey = fuzzy_key(listing)
        if not fkey or listing.price_eur is None:
            return False

        candidates = self.conn.execute(
            "SELECT price_eur FROM listings WHERE fuzzy_key = ?", (fkey,)
        ).fetchall()
        tolerance = listing.price_eur * self.FUZZY_PRICE_TOLERANCE
        return any(
            c["price_eur"] is not None
            and abs(c["price_eur"] - listing.price_eur) <= tolerance
            for c in candidates
        )

    def touch(self, listing: Listing) -> None:
        """Record that a known listing showed up again."""
        self.conn.execute(
            "UPDATE listings SET last_seen_at = ? WHERE key = ?",
            (_now(), listing_key(listing)),
        )
        self.conn.commit()

    def insert(self, listing: Listing, profiles: Iterable[str] | None = None) -> str:
        """Store a listing and open a pending verdict per matched profile."""
        key = listing_key(listing)
        now = _now()
        self.conn.execute(
            """
            INSERT OR IGNORE INTO listings (
                key, fuzzy_key, site, external_id, url, title, price_eur,
                surface_m2, rooms, city, postal_code, description, photo_url,
                provenance, price_per_sqm, first_seen_at, last_seen_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                key,
                fuzzy_key(listing),
                listing.site,
                listing.external_id,
                listing.url,
                listing.title,
                listing.price_eur,
                listing.surface_m2,
                listing.rooms,
                listing.city,
                listing.postal_code,
                listing.description,
                listing.photo_url,
                listing.provenance,
                listing.price_per_sqm,
                now,
                now,
            ),
        )
        self.open_verdicts(key, profiles or (DEFAULT_PROFILE_NAME,))
        return key

    def open_verdicts(self, key: str, profiles: Iterable[str]) -> None:
        """Create the pending (unscored) verdict rows for a listing."""
        now = _now()
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO verdicts (listing_key, profile, status, created_at)
            VALUES (?,?,?,?)
            """,
            [(key, profile, STATUS_NEW, now) for profile in profiles],
        )
        self.conn.commit()

    def save_analysis(
        self,
        key: str,
        analysis: dict[str, Any],
        profile: str = DEFAULT_PROFILE_NAME,
    ) -> None:
        """Write one profile's verdict, creating the row if it isn't open yet."""
        self.conn.execute(
            """
            INSERT INTO verdicts (listing_key, profile, status, created_at)
            VALUES (?,?,?,?)
            ON CONFLICT(listing_key, profile) DO NOTHING
            """,
            (key, profile, STATUS_NEW, _now()),
        )
        self.conn.execute(
            """
            UPDATE verdicts SET
                dvf_median_per_sqm     = ?,
                dvf_sample_size        = ?,
                price_vs_dvf_pct       = ?,
                estimated_monthly_rent = ?,
                gross_yield_pct        = ?,
                tension_locative       = ?,
                dpe                    = ?,
                red_flags              = ?,
                score                  = ?,
                analysis               = ?,
                enrichment_source      = ?
            WHERE listing_key = ? AND profile = ?
            """,
            (
                analysis.get("dvf_median_per_sqm"),
                analysis.get("dvf_sample_size"),
                analysis.get("price_vs_dvf_pct"),
                analysis.get("estimated_monthly_rent"),
                analysis.get("gross_yield_pct"),
                analysis.get("tension_locative"),
                analysis.get("dpe"),
                json.dumps(analysis.get("red_flags") or [], ensure_ascii=False),
                analysis.get("score"),
                analysis.get("analysis"),
                analysis.get("enrichment_source"),
                key,
                profile,
            ),
        )
        self.conn.commit()

    def mark_notified(self, key: str, profile: str = DEFAULT_PROFILE_NAME) -> None:
        self.conn.execute(
            "UPDATE verdicts SET notified = 1 WHERE listing_key = ? AND profile = ?",
            (key, profile),
        )
        self.conn.commit()

    def set_notion_page_id(
        self, key: str, page_id: str, profile: str = DEFAULT_PROFILE_NAME
    ) -> None:
        self.conn.execute(
            """
            UPDATE verdicts SET notion_page_id = ?
            WHERE listing_key = ? AND profile = ?
            """,
            (page_id, key, profile),
        )
        self.conn.commit()

    def get(
        self, key: str, profile: str = DEFAULT_PROFILE_NAME
    ) -> sqlite3.Row | None:
        """One listing joined to one profile's verdict."""
        return self.conn.execute(
            f"""
            SELECT {_JOINED_COLUMNS}
            FROM listings l
            JOIN verdicts v ON v.listing_key = l.key
            WHERE l.key = ? AND v.profile = ?
            """,
            (key, profile),
        ).fetchone()

    def all_listings(
        self, limit: int = 500, profile: str | None = None
    ) -> list[sqlite3.Row]:
        """Every verdict, best first — one row per listing per profile."""
        where = "WHERE v.profile = ?" if profile else ""
        params: tuple = (profile, limit) if profile else (limit,)
        return self.conn.execute(
            f"""
            SELECT {_JOINED_COLUMNS}
            FROM listings l
            JOIN verdicts v ON v.listing_key = l.key
            {where}
            ORDER BY COALESCE(v.score, -1) DESC, l.first_seen_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    def counts_by_status(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM verdicts GROUP BY status"
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def counts_by_profile(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT profile, COUNT(*) AS n FROM verdicts GROUP BY profile ORDER BY profile"
        ).fetchall()
        return {row["profile"]: row["n"] for row in rows}

    def profiles_seen(self) -> list[str]:
        """Profile names present in the database, active or retired."""
        return [
            row["profile"]
            for row in self.conn.execute(
                "SELECT DISTINCT profile FROM verdicts ORDER BY profile"
            )
        ]

    # --------------------------------------------------------------- DVF cache

    def get_dvf(self, cache_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM dvf_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()

    def put_dvf(
        self,
        cache_key: str,
        median_per_sqm: float | None,
        mean_per_sqm: float | None,
        sample_size: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO dvf_cache (cache_key, median_per_sqm, mean_per_sqm,
                                   sample_size, fetched_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(cache_key) DO UPDATE SET
                median_per_sqm = excluded.median_per_sqm,
                mean_per_sqm   = excluded.mean_per_sqm,
                sample_size    = excluded.sample_size,
                fetched_at     = excluded.fetched_at
            """,
            (cache_key, median_per_sqm, mean_per_sqm, sample_size, _now()),
        )
        self.conn.commit()

    # -------------------------------------------------------------------- runs

    def record_run(
        self,
        emails_seen: int,
        listings_found: int,
        listings_new: int,
        notified: int,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO runs (started_at, emails_seen, listings_found,
                              listings_new, notified, error)
            VALUES (?,?,?,?,?,?)
            """,
            (_now(), emails_seen, listings_found, listings_new, notified, error),
        )
        self.conn.commit()

    def last_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


@contextmanager
def open_store(path: Path | None = None) -> Iterator[Store]:
    store = Store(path)
    try:
        yield store
    finally:
        store.close()
