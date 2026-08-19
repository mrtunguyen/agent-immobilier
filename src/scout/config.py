"""Loads .env secrets and criteria.yaml into typed objects.

criteria.yaml has two layers. The top level is the base: shared defaults plus
the run-level settings (models, DVF lookup, pipeline caps) that cannot sensibly
differ between searches. An optional `profiles:` list then names the searches
you actually want to run, each overriding only the keys where it differs.

With no `profiles:` block the base is the single profile, so a one-search
criteria.yaml needs no profile syntax at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CRITERIA_PATH = REPO_ROOT / "criteria.yaml"
DATA_DIR = REPO_ROOT / "data"
DOCS_DIR = REPO_ROOT / "docs"

# Name of the implicit profile when criteria.yaml declares none. Also the
# profile that pre-profile database rows are migrated onto.
DEFAULT_PROFILE_NAME = "default"

# Keys describing *how the run works* rather than *what you are looking for*.
# A profile setting one of these is worth rejecting loudly: models and DVF
# lookups are shared, so a per-profile value would be silently ignored.
RUN_LEVEL_KEYS = ("models", "dvf", "pipeline")


@dataclass(frozen=True)
class TargetCity:
    name: str
    postal_codes: list[str]
    avg_rent_per_sqm_eur: float | None = None
    avg_price_per_sqm_eur: float | None = None


@dataclass(frozen=True)
class Criteria:
    """One search: the complete set of rules a listing is judged against."""

    max_price_eur: int | None
    min_price_eur: int | None
    target_cities: list[TargetCity]
    min_surface_m2: float
    min_rooms: int
    max_floor_ground_ok: bool
    min_gross_yield_pct: float
    target_gross_yield_pct: float
    dpe_acceptable_classes: list[str]
    dpe_reject_below: str | None
    red_flag_keywords: list[str]
    dvf_lookback_years: int
    dvf_min_comparable_transactions: int
    dvf_property_types: list[str]
    score_threshold_notify: int
    scoring_weights: dict[str, float]
    parsing_model: str
    analysis_model: str
    max_listings_per_run: int
    enrichment_fetch_timeout_s: float
    enrichment_max_retries: int
    name: str = DEFAULT_PROFILE_NAME
    label: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def display_name(self) -> str:
        return self.label or self.name

    def city_for_postal_code(self, postal_code: str | None) -> TargetCity | None:
        if not postal_code:
            return None
        for city in self.target_cities:
            if postal_code in city.postal_codes:
                return city
        return None

    def matches_location(self, postal_code: str | None, city_name: str | None) -> bool:
        """True when a listing falls in one of the target cities.

        Postal code is authoritative; the city name is a fallback for listings
        whose alert email omitted the code.
        """
        if self.city_for_postal_code(postal_code):
            return True
        if city_name:
            needle = city_name.strip().casefold()
            return any(c.name.casefold() in needle for c in self.target_cities)
        return False


@dataclass(frozen=True)
class CriteriaSet:
    """The active profiles, plus the base they were derived from.

    Run-level settings (models, DVF, pipeline caps) are read from `base`, the
    only place they can be set. When criteria.yaml declares no profiles, `base`
    is also the single entry in `profiles`.
    """

    base: Criteria
    profiles: list[Criteria]
    # Every profile criteria.yaml defines, even those this run skipped. The
    # dashboard reports on all of them, so `--profile` must not make the others
    # look like profiles you deleted.
    catalogue: list[Criteria] | None = None

    @property
    def all_profiles(self) -> list[Criteria]:
        return self.catalogue if self.catalogue is not None else self.profiles

    def __iter__(self) -> Iterator[Criteria]:
        return iter(self.profiles)

    def __len__(self) -> int:
        return len(self.profiles)

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.profiles]

    def get(self, name: str) -> Criteria | None:
        return next((p for p in self.profiles if p.name == name), None)

    def select(self, names: list[str]) -> "CriteriaSet":
        """Narrow to the named profiles — backs `--profile`."""
        unknown = [n for n in names if n not in self.names]
        if unknown:
            raise ValueError(
                f"unknown profile(s): {', '.join(unknown)}. "
                f"criteria.yaml defines: {', '.join(self.names)}"
            )
        return CriteriaSet(
            base=self.base,
            profiles=[p for p in self.profiles if p.name in names],
            catalogue=self.all_profiles,
        )


@dataclass(frozen=True)
class Settings:
    gmail_address: str | None
    gmail_app_password: str | None
    gemini_api_key: str | None
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    notion_token: str | None
    notion_database_id: str | None

    @property
    def gmail_enabled(self) -> bool:
        return bool(self.gmail_address and self.gmail_app_password)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def notion_enabled(self) -> bool:
        return bool(self.notion_token and self.notion_database_id)


def load_settings() -> Settings:
    load_dotenv(REPO_ROOT / ".env")

    def env(name: str) -> str | None:
        value = os.environ.get(name, "").strip()
        return value or None

    return Settings(
        gmail_address=env("GMAIL_ADDRESS"),
        gmail_app_password=env("GMAIL_APP_PASSWORD"),
        # GOOGLE_API_KEY is the other name the Gemini SDK reads, so accept both.
        gemini_api_key=env("GEMINI_API_KEY") or env("GOOGLE_API_KEY"),
        telegram_bot_token=env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=env("TELEGRAM_CHAT_ID"),
        notion_token=env("NOTION_TOKEN"),
        notion_database_id=env("NOTION_DATABASE_ID"),
    )


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Override wins, recursing into nested mappings. Lists replace wholesale.

    Replacing lists is the useful behaviour here: a profile naming its own
    postal codes or red-flag keywords means *these*, not "these as well".
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _cities(raw: dict[str, Any]) -> list[TargetCity]:
    return [
        TargetCity(
            name=entry["name"],
            postal_codes=[str(pc) for pc in entry.get("postal_codes", [])],
            avg_rent_per_sqm_eur=entry.get("avg_rent_per_sqm_eur"),
            avg_price_per_sqm_eur=entry.get("avg_price_per_sqm_eur"),
        )
        for entry in raw.get("target_cities", [])
    ]


def _criteria_from_raw(
    raw: dict[str, Any], name: str, label: str | None = None
) -> Criteria:
    budget = raw.get("budget") or {}
    yields = raw.get("yield_thresholds") or {}
    dpe = raw.get("dpe") or {}
    dvf = raw.get("dvf") or {}
    scoring = raw.get("scoring") or {}
    models = raw.get("models") or {}
    pipeline = raw.get("pipeline") or {}

    return Criteria(
        max_price_eur=budget.get("max_price_eur"),
        min_price_eur=budget.get("min_price_eur"),
        target_cities=_cities(raw),
        min_surface_m2=float(raw.get("min_surface_m2", 0)),
        min_rooms=int(raw.get("min_rooms", 0)),
        max_floor_ground_ok=bool(raw.get("max_floor_ground_ok", True)),
        min_gross_yield_pct=float(yields.get("min_gross_yield_pct", 0)),
        target_gross_yield_pct=float(yields.get("target_gross_yield_pct", 0)),
        dpe_acceptable_classes=list(dpe.get("acceptable_classes") or []),
        dpe_reject_below=dpe.get("reject_below"),
        red_flag_keywords=list(raw.get("red_flag_keywords") or []),
        dvf_lookback_years=int(dvf.get("lookback_years", 3)),
        dvf_min_comparable_transactions=int(dvf.get("min_comparable_transactions", 5)),
        dvf_property_types=list(dvf.get("property_types") or ["Appartement", "Maison"]),
        score_threshold_notify=int(scoring.get("score_threshold_notify", 70)),
        scoring_weights={
            k: float(v) for k, v in scoring.items() if k.startswith("weight_")
        },
        parsing_model=models.get("parsing_model", "gemini-3.6-flash"),
        analysis_model=models.get("analysis_model", "gemini-3.6-flash"),
        max_listings_per_run=int(pipeline.get("max_listings_per_run", 200)),
        enrichment_fetch_timeout_s=float(pipeline.get("enrichment_fetch_timeout_s", 8)),
        enrichment_max_retries=int(pipeline.get("enrichment_max_retries", 1)),
        name=name,
        label=label,
        raw=raw,
    )


def _restrict_cities(criteria: Criteria, wanted: list[str]) -> Criteria:
    """Keep only the named cities — backs the `cities:` shorthand in a profile."""
    needles = [str(w).strip().casefold() for w in wanted]
    defined = {c.name.casefold() for c in criteria.target_cities}
    missing = [w for w, n in zip(wanted, needles) if n not in defined]
    if missing:
        raise ValueError(
            f"profile {criteria.name!r} lists cities absent from target_cities: "
            f"{', '.join(str(m) for m in missing)}"
        )
    kept = [c for c in criteria.target_cities if c.name.casefold() in needles]
    return replace(criteria, target_cities=kept)


def load_criteria_set(path: Path | None = None) -> CriteriaSet:
    """Parse criteria.yaml into a base plus one Criteria per active profile."""
    path = path or DEFAULT_CRITERIA_PATH
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    base_raw = {k: v for k, v in raw.items() if k != "profiles"}
    base = _criteria_from_raw(base_raw, DEFAULT_PROFILE_NAME)

    entries = raw.get("profiles")
    if not entries:
        return CriteriaSet(base=base, profiles=[base])

    profiles: list[Criteria] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(
                f"profiles[{index}] must be a mapping, got {type(entry).__name__}"
            )

        name = str(entry.get("name") or f"profile-{index + 1}").strip()
        if name in seen:
            raise ValueError(f"duplicate profile name: {name!r}")
        seen.add(name)

        if entry.get("enabled") is False:
            continue

        overrides = {
            k: v
            for k, v in entry.items()
            if k not in ("name", "label", "enabled", "cities")
        }
        run_level = [k for k in RUN_LEVEL_KEYS if k in overrides]
        if run_level:
            raise ValueError(
                f"profile {name!r} sets run-level key(s) {', '.join(run_level)}; "
                "those are shared by every profile and belong at the top level "
                "of criteria.yaml"
            )

        criteria = _criteria_from_raw(
            _deep_merge(base_raw, overrides), name, label=entry.get("label")
        )
        if entry.get("cities"):
            criteria = _restrict_cities(criteria, list(entry["cities"]))
        if not criteria.target_cities:
            raise ValueError(f"profile {name!r} ends up with no target cities")
        profiles.append(criteria)

    if not profiles:
        raise ValueError(
            f"{path.name} defines {len(entries)} profile(s) but every one is disabled"
        )
    return CriteriaSet(base=base, profiles=profiles)


def load_criteria(path: Path | None = None) -> Criteria:
    """The base criteria alone — run-level settings and shared defaults."""
    return load_criteria_set(path).base


if __name__ == "__main__":  # smoke check: python -m scout.config
    settings = load_settings()
    criteria_set = load_criteria_set()
    print(f"criteria.yaml OK — {len(criteria_set)} active profile(s)")
    for profile in criteria_set:
        budget = f"{profile.max_price_eur:,} €" if profile.max_price_eur else "no cap"
        print(
            f"  [{profile.name}] up to {budget}, "
            f">={profile.min_surface_m2:g} m2, >={profile.min_rooms} rooms, "
            f"yield >={profile.min_gross_yield_pct}%, "
            f"notify at {profile.score_threshold_notify}"
        )
        for city in profile.target_cities:
            print(f"      {city.name}: {', '.join(city.postal_codes)}")
    print(
        "channels — gmail:{} telegram:{} notion:{} gemini:{}".format(
            settings.gmail_enabled,
            settings.telegram_enabled,
            settings.notion_enabled,
            bool(settings.gemini_api_key),
        )
    )
