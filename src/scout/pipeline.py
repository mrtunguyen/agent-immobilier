"""The daily run: ingest → parse → dedupe → DVF → analyse → deliver.

Every stage degrades rather than aborting. A site whose template changed falls
back to the LLM parser; a listing page behind DataDome is analysed from the
email alone; a missing Telegram or Notion secret just skips that channel. Only
an unhandled exception ends the run, and that gets pushed to Telegram so a
broken pipeline doesn't fail silently for days.

A listing is matched against every profile in criteria.yaml and analysed once
per profile it fits. The expensive shared work — the DVF lookup and the listing
page fetch — happens once regardless, since neither depends on the profile.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field

import httpx

from . import analysis as analysis_mod
from . import configure_stdio, dashboard, dvf, enrich, parsers
from .config import Criteria, CriteriaSet, Settings, load_criteria_set, load_settings
from .dedupe import Store
from .gmail_client import AlertEmail, GmailClient
from .notify_telegram import TelegramNotifier
from .parsers.base import Listing
from .parsers.llm_fallback import make_fallback

log = logging.getLogger("scout")


@dataclass
class RunStats:
    emails: int = 0
    listings_found: int = 0
    listings_new: int = 0
    analysed: int = 0
    notified: int = 0
    synced: int = 0
    # Verdicts produced per profile — a listing matching two profiles counts twice.
    per_profile: Counter = field(default_factory=Counter)


def _gemini_client(settings: Settings):
    if not settings.gemini_api_key:
        log.warning("GEMINI_API_KEY not set — parsing fallback and analysis skipped")
        return None
    from google import genai

    return genai.Client(api_key=settings.gemini_api_key)


def collect_emails(settings: Settings, limit: int | None) -> tuple[list[AlertEmail], list[str]]:
    if not settings.gmail_enabled:
        log.warning("Gmail credentials missing — no emails ingested")
        return [], []
    with GmailClient(settings.gmail_address, settings.gmail_app_password) as gmail:
        emails = gmail.fetch_unseen(limit=limit)
    return emails, [e.uid for e in emails]


def extract_listings(
    emails: list[AlertEmail], criteria: Criteria, client
) -> list[Listing]:
    fallback = make_fallback(client, criteria.parsing_model) if client else None
    listings: list[Listing] = []

    for message in emails:
        parsed = parsers.parse_email(
            sender=message.sender,
            subject=message.subject,
            html=message.html,
            text=message.text,
            llm_fallback=fallback,
        )
        for listing in parsed.listings:
            listing.provenance = parsed.provenance
        log.info(
            "%s -> %d listing(s) via %s",
            message.subject[:60] or message.sender,
            len(parsed.listings),
            parsed.provenance,
        )
        listings.extend(parsed.listings)

    return listings


def passes_hard_filters(listing: Listing, criteria: Criteria) -> bool:
    """Cheap local rejects, so we never pay for a model call on an obvious no."""
    if criteria.max_price_eur and listing.price_eur:
        if listing.price_eur > criteria.max_price_eur:
            return False
    if criteria.min_price_eur and listing.price_eur:
        if listing.price_eur < criteria.min_price_eur:
            return False
    if criteria.min_surface_m2 and listing.surface_m2:
        if listing.surface_m2 < criteria.min_surface_m2:
            return False
    if criteria.min_rooms and listing.rooms:
        if listing.rooms < criteria.min_rooms:
            return False
    # Only reject on location when we actually know where it is.
    if listing.postal_code or listing.city:
        if not criteria.matches_location(listing.postal_code, listing.city):
            return False
    return True


def matching_profiles(listing: Listing, criteria_set: CriteriaSet) -> list[Criteria]:
    """Every profile whose hard filters this listing clears.

    Empty means no search wants it, and the listing is dropped before it costs
    a model call.
    """
    return [p for p in criteria_set if passes_hard_filters(listing, p)]


def _verdict_from_row(row) -> dict:
    """Rebuild the analysis dict from a stored row.

    The delivery helpers take the verdict as a dict because that's what the
    model call returns; replaying a stored verdict means reconstructing it.
    """
    try:
        red_flags = json.loads(row["red_flags"] or "[]")
    except (json.JSONDecodeError, TypeError):
        red_flags = []
    return {
        "price_per_sqm": row["price_per_sqm"],
        "dvf_median_per_sqm": row["dvf_median_per_sqm"],
        "dvf_sample_size": row["dvf_sample_size"],
        "price_vs_dvf_pct": row["price_vs_dvf_pct"],
        "estimated_monthly_rent": row["estimated_monthly_rent"],
        "gross_yield_pct": row["gross_yield_pct"],
        "tension_locative": row["tension_locative"],
        "dpe": row["dpe"],
        "red_flags": red_flags,
        "score": row["score"],
        "analysis": row["analysis"],
        "enrichment_source": row["enrichment_source"],
    }


def deliver_pending(
    settings: Settings,
    criteria_set: CriteriaSet,
    store: Store,
    *,
    dry_run: bool = False,
) -> RunStats:
    """Send stored verdicts that were never delivered, without re-analysing.

    A verdict produced under --dry-run is stored but deliberately not sent, and
    the listing is then known — so the normal run will never look at it again.
    This replays those verdicts from the database: Telegram for anything at or
    above its profile's threshold and not yet notified, Notion for any row
    without a page id. Both are idempotent, so running it twice sends nothing
    twice.
    """
    stats = RunStats()
    named = len(criteria_set.all_profiles) > 1
    thresholds = {
        p.name: p.score_threshold_notify for p in criteria_set.all_profiles
    }
    labels = {p.name: p.display_name for p in criteria_set.all_profiles}
    default_threshold = criteria_set.base.score_threshold_notify

    notifier = (
        TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        if settings.telegram_enabled
        else None
    )
    notion = None
    if settings.notion_enabled:
        from .sync_notion import NotionSync

        notion = NotionSync(settings.notion_token, settings.notion_database_id)

    if notifier is None and notion is None:
        log.warning("neither Telegram nor Notion is configured — nothing to deliver")
        return stats

    rows = [r for r in store.all_listings(limit=10_000) if r["score"] is not None]
    log.info("%d analysed verdict(s) in the database", len(rows))

    for row in rows:
        key, profile = row["key"], row["profile"]
        verdict = _verdict_from_row(row)
        threshold = thresholds.get(profile, default_threshold)

        if (
            notifier
            and not row["notified"]
            and row["score"] >= threshold
            and not dry_run
        ):
            label = labels.get(profile, profile) if named else None
            if notifier.send_listing(row, verdict, profile=label):
                store.mark_notified(key, profile)
                stats.notified += 1
                log.info("notified %s [%s] score=%s", key, profile, row["score"])

        if notion and not row["notion_page_id"] and not dry_run:
            page_id = notion.upsert(row, verdict, profile=profile)
            if page_id:
                store.set_notion_page_id(key, page_id, profile)
                stats.synced += 1
                log.info("synced %s [%s] to Notion", key, profile)

    dashboard.render(store, criteria_set=criteria_set)
    return stats


def run(
    settings: Settings,
    criteria_set: CriteriaSet,
    store: Store,
    *,
    dry_run: bool = False,
    email_limit: int | None = None,
) -> RunStats:
    stats = RunStats()
    client = _gemini_client(settings)
    # Models, DVF settings and pipeline caps are shared by every profile.
    base = criteria_set.base
    # Name the profile in alerts whenever criteria.yaml defines more than one,
    # even on a --profile run: the reader still needs to know which bar it met.
    named = len(criteria_set.all_profiles) > 1

    log.info(
        "%d profile(s) active: %s", len(criteria_set), ", ".join(criteria_set.names)
    )
    known = {p.name for p in criteria_set.all_profiles}
    retired = set(store.profiles_seen()) - known
    if retired:
        log.info(
            "database also holds verdicts for %s — no longer in criteria.yaml, "
            "kept for history",
            ", ".join(sorted(retired)),
        )

    emails, uids = collect_emails(settings, email_limit)
    stats.emails = len(emails)
    if not emails:
        log.info("no new alert emails")

    listings = extract_listings(emails, base, client)
    stats.listings_found = len(listings)

    fresh: list[tuple[str, Listing, list[Criteria]]] = []
    for listing in listings:
        if store.is_known(listing):
            store.touch(listing)
            continue
        matched = matching_profiles(listing, criteria_set)
        if not matched:
            log.debug("filtered out by every profile: %s", listing.url)
            continue
        key = store.insert(listing, [p.name for p in matched])
        fresh.append((key, listing, matched))

    stats.listings_new = len(fresh)
    log.info(
        "%d new listing(s) after dedupe and filters, %d verdict(s) to produce",
        len(fresh),
        sum(len(m) for _, _, m in fresh),
    )

    if len(fresh) > base.max_listings_per_run:
        log.warning(
            "capping this run at %d of %d new listings (max_listings_per_run); "
            "the rest stay in the database unanalysed and will not be retried",
            base.max_listings_per_run,
            len(fresh),
        )
        fresh = fresh[: base.max_listings_per_run]

    notifier = (
        TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
        if settings.telegram_enabled
        else None
    )
    notion = None
    if settings.notion_enabled:
        from .sync_notion import NotionSync

        notion = NotionSync(settings.notion_token, settings.notion_database_id)

    with httpx.Client(follow_redirects=True) as http:
        for key, listing, matched in fresh:
            # Both of these are properties of the flat, not of the search, so
            # they are paid for once even when several profiles want it.
            market = dvf.market_stats(listing.postal_code, store, base, http)
            page_text = enrich.fetch_listing_page(
                listing.url,
                base.enrichment_fetch_timeout_s,
                base.enrichment_max_retries,
            )

            if client is None:
                log.info("no Gemini client — stored without analysis: %s", listing.url)
                continue

            for profile in matched:
                verdict = analysis_mod.analyse(
                    client, listing, market, page_text, profile
                )
                if verdict is None:
                    continue

                store.save_analysis(key, verdict, profile.name)
                stats.analysed += 1
                stats.per_profile[profile.name] += 1
                row = store.get(key, profile.name)

                score = verdict.get("score") or 0
                if (
                    notifier
                    and score >= profile.score_threshold_notify
                    and not row["notified"]
                    and not dry_run
                ):
                    label = profile.display_name if named else None
                    if notifier.send_listing(row, verdict, profile=label):
                        store.mark_notified(key, profile.name)
                        stats.notified += 1

                if notion and not dry_run:
                    page_id = notion.upsert(row, verdict, profile=profile.name)
                    if page_id:
                        store.set_notion_page_id(key, page_id, profile.name)
                        stats.synced += 1

    store.record_run(
        emails_seen=stats.emails,
        listings_found=stats.listings_found,
        listings_new=stats.listings_new,
        notified=stats.notified,
    )
    dashboard.render(store, criteria_set=criteria_set)

    # Only now, once everything is persisted, do we let the mailbox forget.
    if uids and settings.gmail_enabled and not dry_run:
        with GmailClient(settings.gmail_address, settings.gmail_app_password) as gmail:
            gmail.mark_processed(uids)

    return stats


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(prog="scout", description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyse and store, but send nothing and leave emails unread.",
    )
    parser.add_argument(
        "--email-limit", type=int, default=None, help="Process at most N emails."
    )
    parser.add_argument(
        "--profile",
        action="append",
        metavar="NAME",
        help=(
            "Run only this profile from criteria.yaml. Repeatable; "
            "default is every enabled profile."
        ),
    )
    parser.add_argument(
        "--deliver-pending",
        action="store_true",
        help=(
            "Send stored verdicts that were never delivered (anything analysed "
            "under --dry-run), then exit. Re-analyses nothing; idempotent."
        ),
    )
    parser.add_argument(
        "--dashboard-only",
        action="store_true",
        help="Re-render the dashboard from the existing database and exit.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Third-party debug logs would drown the run's own output.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    settings = load_settings()
    try:
        criteria_set = load_criteria_set()
        if args.profile:
            criteria_set = criteria_set.select(args.profile)
    except (ValueError, OSError) as exc:
        log.error("criteria.yaml: %s", exc)
        return 2

    with Store() as store:
        if args.dashboard_only:
            dashboard.render(store, criteria_set=criteria_set)
            return 0

        if args.deliver_pending:
            stats = deliver_pending(
                settings, criteria_set, store, dry_run=args.dry_run
            )
            log.info(
                "delivered — %d notified, %d synced to Notion",
                stats.notified,
                stats.synced,
            )
            return 0

        try:
            stats = run(
                settings,
                criteria_set,
                store,
                dry_run=args.dry_run,
                email_limit=args.email_limit,
            )
        except Exception as exc:
            log.exception("pipeline failed")
            store.record_run(0, 0, 0, 0, error=str(exc))
            if settings.telegram_enabled:
                TelegramNotifier(
                    settings.telegram_bot_token, settings.telegram_chat_id
                ).send_text(f"🚨 Rental scout run failed: {exc}")
            return 1

    log.info(
        "done — %d emails, %d listings (%d new), %d analysed, "
        "%d notified, %d synced to Notion",
        stats.emails,
        stats.listings_found,
        stats.listings_new,
        stats.analysed,
        stats.notified,
        stats.synced,
    )
    if len(criteria_set) > 1 and stats.per_profile:
        log.info(
            "verdicts by profile — %s",
            ", ".join(f"{name}: {n}" for name, n in sorted(stats.per_profile.items())),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
