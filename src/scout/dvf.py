"""Local market prices from DVF — France's public register of real sales.

Rather than trusting a listing's asking price or a hand-maintained estimate,
we compare it against what comparable properties in the same postal code
actually sold for. Source is Etalab's geo-dvf export of the Demandes de
Valeurs Foncières dataset: official, free, no key.

Two lookups per postal code:
  1. geo.api.gouv.fr  — postal code -> INSEE commune code. Uses the
     `arrondissement-municipal` type first, because Paris, Lyon and Marseille
     have one commune code covering the whole city but DVF publishes per
     arrondissement (Lyon 3e is 69383, not Lyon's 69123). Arrondissement-level
     comparables are far more meaningful than city-wide ones.
  2. files.data.gouv.fr — one CSV per commune per year.

Results are cached per postal code in SQLite: DVF is republished roughly twice
a year, so refetching per run would be pure waste.
"""

from __future__ import annotations

import csv
import io
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from statistics import median

import httpx

log = logging.getLogger(__name__)

GEO_API = "https://geo.api.gouv.fr/communes"
DVF_BASE = "https://files.data.gouv.fr/geo-dvf/latest/csv"

# Sanity bounds on €/m². Below the floor is usually a garage or a cellar sold
# as a "flat"; above the ceiling is usually a mis-keyed valeur_fonciere.
MIN_PRICE_PER_SQM = 500.0
MAX_PRICE_PER_SQM = 30_000.0
MIN_SURFACE_M2 = 9.0

# Re-fetch a commune at most this often.
CACHE_MAX_AGE_DAYS = 90

# Extra years to try past the lookback window, covering the unpublished current
# year and any year a small commune recorded no sales.
MAX_EMPTY_YEARS = 2

REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


@dataclass(frozen=True)
class MarketStats:
    """What comparable sales say a square metre costs here."""

    postal_code: str
    median_per_sqm: float | None
    mean_per_sqm: float | None
    sample_size: int
    source: str  # "dvf", "dvf_cache", "criteria_fallback", or "unavailable"

    @property
    def is_confident(self) -> bool:
        return self.median_per_sqm is not None and self.sample_size > 0


def insee_code_for_postal_code(postal_code: str, client: httpx.Client) -> str | None:
    """Resolve a postal code to the INSEE code DVF files are keyed by."""
    for params in (
        {"codePostal": postal_code, "type": "arrondissement-municipal"},
        {"codePostal": postal_code},
    ):
        try:
            response = client.get(GEO_API, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            communes = response.json()
        except Exception:
            log.warning("geo.api.gouv.fr lookup failed for %s", postal_code)
            return None
        if communes:
            return communes[0].get("code")
    return None


def _mutation_price_per_sqm(
    rows: list[dict[str, str]], property_types: set[str]
) -> float | None:
    """Price per m² for one sale, or None if it isn't a clean comparable.

    A DVF mutation spans one row per lot, so a sale of a flat plus its cellar
    is several rows sharing an id and a single valeur_fonciere. We sum the
    surfaces of the rows we care about and reject mixed sales (a flat sold
    together with a shop) whose combined price can't be attributed.
    """
    if not rows:
        return None
    if rows[0].get("nature_mutation") != "Vente":
        return None

    built_types = {r.get("type_local") for r in rows if r.get("type_local")}
    built_types.discard("Dépendance")  # cellars and parking ride along; ignore
    if not built_types or not built_types.issubset(property_types):
        return None

    try:
        value = float(rows[0].get("valeur_fonciere") or 0)
    except ValueError:
        return None
    if value <= 0:
        return None

    surface = 0.0
    for row in rows:
        if row.get("type_local") not in property_types:
            continue
        try:
            surface += float(row.get("surface_reelle_bati") or 0)
        except ValueError:
            continue
    if surface < MIN_SURFACE_M2:
        return None

    price_per_sqm = value / surface
    if not MIN_PRICE_PER_SQM <= price_per_sqm <= MAX_PRICE_PER_SQM:
        return None
    return price_per_sqm


def _fetch_year(
    insee: str, year: int, postal_code: str, property_types: set[str],
    client: httpx.Client,
) -> list[float]:
    url = f"{DVF_BASE}/{year}/communes/{insee[:2]}/{insee}.csv"
    try:
        response = client.get(url, timeout=REQUEST_TIMEOUT)
        if response.status_code == 404:
            return []  # year not published yet, or no sales recorded
        response.raise_for_status()
    except Exception:
        log.warning("DVF fetch failed for %s %s", insee, year)
        return []

    mutations: dict[str, list[dict[str, str]]] = defaultdict(list)
    reader = csv.DictReader(io.StringIO(response.text))
    for row in reader:
        # Commune files for Paris/Lyon/Marseille cover one arrondissement, but
        # elsewhere a commune spans several postal codes — filter to ours.
        if row.get("code_postal") and row["code_postal"] != postal_code:
            continue
        mutations[row.get("id_mutation", "")].append(row)

    prices: list[float] = []
    for rows in mutations.values():
        price = _mutation_price_per_sqm(rows, property_types)
        if price is not None:
            prices.append(price)
    return prices


def _cache_is_fresh(fetched_at: str | None) -> bool:
    if not fetched_at:
        return False
    try:
        stamp = datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - stamp
    return age.days < CACHE_MAX_AGE_DAYS


def market_stats(
    postal_code: str | None,
    store,
    criteria,
    client: httpx.Client | None = None,
) -> MarketStats:
    """Median €/m² for a postal code, from cache, DVF, or criteria.yaml."""
    if not postal_code:
        return MarketStats("", None, None, 0, "unavailable")

    cached = store.get_dvf(postal_code)
    if cached and _cache_is_fresh(cached["fetched_at"]):
        return MarketStats(
            postal_code,
            cached["median_per_sqm"],
            cached["mean_per_sqm"],
            cached["sample_size"],
            "dvf_cache",
        )

    owns_client = client is None
    client = client or httpx.Client(follow_redirects=True)
    try:
        insee = insee_code_for_postal_code(postal_code, client)
        prices: list[float] = []
        if insee:
            property_types = set(criteria.dvf_property_types)
            years_with_data = 0
            year = date.today().year
            # The current year is usually not published yet, and a small commune
            # can have a year with no recorded sales. Walk back until we have
            # `lookback_years` years that actually returned data, with a hard
            # stop so a commune with no DVF history can't loop for long.
            oldest_year = year - criteria.dvf_lookback_years - MAX_EMPTY_YEARS
            while years_with_data < criteria.dvf_lookback_years and year > oldest_year:
                found = _fetch_year(insee, year, postal_code, property_types, client)
                if found:
                    prices.extend(found)
                    years_with_data += 1
                year -= 1
    finally:
        if owns_client:
            client.close()

    if prices:
        stats = MarketStats(
            postal_code,
            round(median(prices), 1),
            round(sum(prices) / len(prices), 1),
            len(prices),
            "dvf",
        )
        store.put_dvf(
            postal_code, stats.median_per_sqm, stats.mean_per_sqm, stats.sample_size
        )
        log.info(
            "DVF %s: median %.0f €/m² from %d sales",
            postal_code,
            stats.median_per_sqm,
            stats.sample_size,
        )
        return stats

    # Nothing usable from DVF — fall back to the user's own estimate if given.
    city = criteria.city_for_postal_code(postal_code)
    if city and city.avg_price_per_sqm_eur:
        return MarketStats(
            postal_code, float(city.avg_price_per_sqm_eur), None, 0, "criteria_fallback"
        )

    store.put_dvf(postal_code, None, None, 0)
    return MarketStats(postal_code, None, None, 0, "unavailable")
