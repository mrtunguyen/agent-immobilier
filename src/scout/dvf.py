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
a year, so refetching per run would be pure waste. Only answers are cached: a
lookup that failed on the network is retried next run, and a postal code that
came back with no sales is retried after a day rather than written off for the
full cache period.
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
# An empty result is cheap to retry and expensive to keep: it leaves every
# listing in that postal code with no market comparison.
EMPTY_CACHE_MAX_AGE_DAYS = 1

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
    # The `dvf.min_comparable_transactions` this sample was judged against, and
    # the investor's own €/m² estimate for the area when they supplied one.
    min_comparable: int = 0
    fallback_per_sqm: float | None = None

    @property
    def is_confident(self) -> bool:
        """True when there are enough real sales to argue from.

        Three recorded sales are not nothing, but they are not a market either:
        one unusual flat moves the median. Below the threshold the figure is
        still reported — flagged as weak evidence rather than discarded.
        """
        if self.median_per_sqm is None:
            return False
        return self.sample_size >= max(1, self.min_comparable)


class LookupFailed(Exception):
    """A DVF source could not be reached — not the same as "no sales here"."""


def insee_code_for_postal_code(postal_code: str, client: httpx.Client) -> str | None:
    """Resolve a postal code to the INSEE code DVF files are keyed by.

    None means geo.api.gouv.fr answered and knows no such postal code;
    LookupFailed means it could not be asked.
    """
    for params in (
        {"codePostal": postal_code, "type": "arrondissement-municipal"},
        {"codePostal": postal_code},
    ):
        try:
            response = client.get(GEO_API, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            communes = response.json()
        except Exception as exc:
            log.warning("geo.api.gouv.fr lookup failed for %s", postal_code)
            raise LookupFailed(postal_code) from exc
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
) -> list[float] | None:
    """€/m² of each qualifying sale that year; [] for none, None if unreachable."""
    url = f"{DVF_BASE}/{year}/communes/{insee[:2]}/{insee}.csv"
    try:
        response = client.get(url, timeout=REQUEST_TIMEOUT)
        if response.status_code == 404:
            return []  # year not published yet, or no sales recorded
        response.raise_for_status()
    except Exception:
        log.warning("DVF fetch failed for %s %s", insee, year)
        return None

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


def _cache_is_fresh(
    fetched_at: str | None, max_age_days: int = CACHE_MAX_AGE_DAYS
) -> bool:
    if not fetched_at:
        return False
    try:
        stamp = datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - stamp
    return age.days < max_age_days


def market_stats(
    postal_code: str | None,
    store,
    criteria,
    client: httpx.Client | None = None,
) -> MarketStats:
    """Median €/m² for a postal code, from cache, DVF, or criteria.yaml."""
    if not postal_code:
        return MarketStats("", None, None, 0, "unavailable")

    min_comparable = criteria.dvf_min_comparable_transactions
    city = criteria.city_for_postal_code(postal_code)
    own_estimate = float(city.avg_price_per_sqm_eur) if city and city.avg_price_per_sqm_eur else None

    cached = store.get_dvf(postal_code)
    max_age = (
        CACHE_MAX_AGE_DAYS
        if cached and cached["median_per_sqm"] is not None
        else EMPTY_CACHE_MAX_AGE_DAYS
    )
    if cached and _cache_is_fresh(cached["fetched_at"], max_age):
        return MarketStats(
            postal_code,
            cached["median_per_sqm"],
            cached["mean_per_sqm"],
            cached["sample_size"],
            "dvf_cache",
            min_comparable,
            own_estimate,
        )

    owns_client = client is None
    client = client or httpx.Client(follow_redirects=True)
    prices: list[float] = []
    # Set when any source was unreachable: an empty result is then not a fact
    # about the market, and must not be cached as one.
    failed = False
    try:
        try:
            insee = insee_code_for_postal_code(postal_code, client)
        except LookupFailed:
            insee, failed = None, True
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
                if found is None:
                    failed = True
                elif found:
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
            min_comparable,
            own_estimate,
        )
        # A year that failed to download makes this a partial sample: use it,
        # but fetch again next run rather than keep it for the cache period.
        if not failed:
            store.put_dvf(
                postal_code, stats.median_per_sqm, stats.mean_per_sqm, stats.sample_size
            )
        log.info(
            "DVF %s: median %.0f €/m² from %d sales%s",
            postal_code,
            stats.median_per_sqm,
            stats.sample_size,
            ""
            if stats.is_confident
            else f" — below min_comparable_transactions={min_comparable}, "
            "treated as weak evidence",
        )
        return stats

    # Nothing usable from DVF — fall back to the user's own estimate if given.
    if own_estimate:
        return MarketStats(
            postal_code, own_estimate, None, 0, "criteria_fallback", min_comparable
        )

    if failed:
        log.warning("DVF %s: lookup failed, not cached — retried next run", postal_code)
    else:
        store.put_dvf(postal_code, None, None, 0)
    return MarketStats(postal_code, None, None, 0, "unavailable", min_comparable)
