"""Renders the SQLite store into a static page for GitHub Pages.

Static HTML rather than a served app: the pipeline already runs as a cron job
that commits its state, so generating a page in the same step costs nothing and
needs no process to stay up.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import DEFAULT_PROFILE_NAME, DOCS_DIR, CriteriaSet
from .dedupe import VALID_STATUSES, Store

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
DEFAULT_OUTPUT = DOCS_DIR / "index.html"


def _fmt_int(value, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}".replace(",", " ") + suffix


def _score_class(score: int | None) -> str:
    if score is None:
        return ""
    if score >= 70:
        return "good"
    if score >= 50:
        return "mid"
    return "bad"


def _row_view(
    row,
    thresholds: dict[str, int],
    labels: dict[str, str],
    default_threshold: int,
    min_comparable: int = 0,
) -> dict:
    location = " ".join(p for p in (row["city"], row["postal_code"]) if p) or "—"
    title = (row["title"] or row["url"] or "Listing").strip()

    try:
        red_flags = json.loads(row["red_flags"] or "[]")
    except (json.JSONDecodeError, TypeError):
        red_flags = []

    delta = row["price_vs_dvf_pct"]
    if delta is None:
        delta_fmt, delta_class = "—", ""
    else:
        delta_fmt = f"{delta:+.0f}%"
        delta_class = "delta-up" if delta > 0 else "delta-down"

    first_seen = row["first_seen_at"] or ""
    profile = row["profile"] or DEFAULT_PROFILE_NAME
    threshold = thresholds.get(profile, default_threshold)

    return {
        "title": title[:140],
        "profile": profile,
        "profile_label": labels.get(profile, profile),
        # A profile still in the database but no longer in criteria.yaml: its
        # verdicts are history, not something the next run will refresh.
        "profile_retired": profile not in thresholds,
        "above_threshold": row["score"] is not None and row["score"] >= threshold,
        "url": row["url"],
        "site": row["site"],
        "location": location,
        "rooms": row["rooms"],
        "status": row["status"],
        "score": row["score"],
        "score_class": _score_class(row["score"]),
        "price_eur": row["price_eur"],
        "price_fmt": _fmt_int(row["price_eur"], " €"),
        "surface_m2": row["surface_m2"],
        "surface_fmt": f"{row['surface_m2']:g}" if row["surface_m2"] else "—",
        "price_per_sqm": row["price_per_sqm"],
        "price_per_sqm_fmt": _fmt_int(row["price_per_sqm"]),
        "dvf_median": row["dvf_median_per_sqm"],
        "dvf_median_fmt": _fmt_int(row["dvf_median_per_sqm"]),
        "dvf_sample": row["dvf_sample_size"],
        # Too few recorded sales to argue from: the median is shown, flagged.
        "dvf_thin": bool(
            row["dvf_median_per_sqm"]
            and (row["dvf_sample_size"] or 0) < min_comparable
        ),
        "price_vs_dvf_pct": delta,
        "delta_fmt": delta_fmt,
        "delta_class": delta_class,
        "gross_yield_pct": row["gross_yield_pct"],
        "yield_fmt": (
            f"{row['gross_yield_pct']:.1f}%" if row["gross_yield_pct"] else "—"
        ),
        "analysis": (row["analysis"] or "")[:280],
        "red_flags": "; ".join(red_flags[:3]),
        "email_only": row["enrichment_source"] == "email_only",
        "first_seen_at": first_seen,
        "first_seen_short": first_seen[:10] or "—",
        "search_blob": " ".join(
            str(p).lower()
            for p in (title, location, row["site"], row["postal_code"], profile)
            if p
        ),
    }


def render(
    store: Store,
    output_path: Path | None = None,
    threshold: int = 70,
    criteria_set: CriteriaSet | None = None,
) -> Path:
    """Write the dashboard. One row per listing per profile.

    `criteria_set` supplies each profile's own notify threshold and label; pass
    a bare `threshold` instead when there is only one search to report on.
    """
    output_path = Path(output_path or DEFAULT_OUTPUT)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if criteria_set is not None:
        # Every defined profile, not just the ones this run happened to touch.
        known = criteria_set.all_profiles
        thresholds = {p.name: p.score_threshold_notify for p in known}
        labels = {p.name: p.display_name for p in known}
        default_threshold = criteria_set.base.score_threshold_notify
        min_comparable = criteria_set.base.dvf_min_comparable_transactions
    else:
        thresholds = {DEFAULT_PROFILE_NAME: threshold}
        labels = {}
        default_threshold = threshold
        min_comparable = 0

    rows = [
        _row_view(row, thresholds, labels, default_threshold, min_comparable)
        for row in store.all_listings()
    ]
    runs = store.last_runs(limit=1)

    profiles = sorted({r["profile"] for r in rows} | set(thresholds))
    distinct_thresholds = sorted(set(thresholds.values())) or [threshold]
    threshold_label = (
        str(distinct_thresholds[0]) if len(distinct_thresholds) == 1 else "profile bar"
    )

    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )
    html = env.get_template("dashboard.html").render(
        rows=rows,
        total=len(rows),
        above_threshold=sum(1 for r in rows if r["above_threshold"]),
        threshold=default_threshold,
        threshold_label=threshold_label,
        profiles=profiles,
        show_profiles=len(profiles) > 1,
        counts=store.counts_by_status(),
        statuses=list(VALID_STATUSES),
        last_run=(runs[0]["started_at"][:16].replace("T", " ") if runs else None),
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )

    output_path.write_text(html, encoding="utf-8")
    log.info(
        "dashboard written to %s (%d verdict(s) across %d profile(s))",
        output_path,
        len(rows),
        len(profiles),
    )
    return output_path
