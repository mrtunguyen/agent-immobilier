"""Pushes high-scoring listings to a Telegram chat.

One-way push only, so the raw Bot API is enough — no bot framework needed.
"""

from __future__ import annotations

import html
import logging

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
TIMEOUT = 20.0
# Telegram rejects photo captions over 1024 characters.
MAX_CAPTION = 1024
MAX_MESSAGE = 4096


def _fmt_money(value: float | int | None, suffix: str = " €") -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}".replace(",", " ") + suffix


def format_listing(listing_row, analysis: dict, profile: str | None = None) -> str:
    """Build the HTML-formatted message body for one listing.

    `profile` names the search that fired. Worth showing whenever more than one
    is active: otherwise a listing well outside the budget of the search you had
    in mind reads as a bug.
    """
    title = html.escape(listing_row["title"] or "Listing")[:120]
    score = analysis.get("score")
    yield_pct = analysis.get("gross_yield_pct")
    rent = analysis.get("estimated_monthly_rent")
    vs_dvf = analysis.get("price_vs_dvf_pct")
    dvf_median = analysis.get("dvf_median_per_sqm")

    lines = [f"<b>{score}/100 — {title}</b>"]
    if profile:
        lines.append(f"🎯 {html.escape(profile)}")
    lines += [
        "",
        f"💶 {_fmt_money(listing_row['price_eur'])}"
        + (f"  ·  {listing_row['surface_m2']:g} m²" if listing_row["surface_m2"] else "")
        + (f"  ·  {listing_row['rooms']}p" if listing_row["rooms"] else ""),
    ]

    location = " ".join(
        p for p in (listing_row["city"], listing_row["postal_code"]) if p
    )
    if location:
        lines.append(f"📍 {html.escape(location)}")

    price_line = f"📊 {_fmt_money(analysis.get('price_per_sqm'), ' €/m²')}"
    if dvf_median:
        price_line += f"  vs DVF {_fmt_money(dvf_median, ' €/m²')}"
        if vs_dvf is not None:
            arrow = "🔺" if vs_dvf > 0 else "🔻"
            price_line += f" ({arrow}{abs(vs_dvf):.0f}%)"
    lines.append(price_line)

    if yield_pct is not None:
        rent_part = f" (~{_fmt_money(rent)}/mo)" if rent else ""
        lines.append(f"📈 Gross yield {yield_pct:.1f}%{rent_part}")

    if analysis.get("dpe"):
        lines.append(f"🔋 DPE {html.escape(str(analysis['dpe']))}")

    red_flags = analysis.get("red_flags") or []
    if red_flags:
        flags = "; ".join(html.escape(f) for f in red_flags[:4])
        lines.append(f"⚠️ {flags}")

    if analysis.get("analysis"):
        lines.append("")
        lines.append(html.escape(analysis["analysis"]))

    if analysis.get("enrichment_source") == "email_only":
        lines.append("")
        lines.append("<i>Analysed from the alert email only — page not reachable.</i>")

    lines.append("")
    lines.append(f'<a href="{html.escape(listing_row["url"])}">View listing →</a>')

    return "\n".join(lines)


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def _post(self, method: str, payload: dict) -> bool:
        url = f"{API_BASE}/bot{self.bot_token}/{method}"
        try:
            response = httpx.post(url, json=payload, timeout=TIMEOUT)
            if response.status_code != 200:
                log.warning("Telegram %s failed: %s", method, response.text[:300])
                return False
            return True
        except Exception:
            log.warning("Telegram %s errored", method, exc_info=True)
            return False

    def send_listing(self, listing_row, analysis: dict, profile: str | None = None) -> bool:
        body = format_listing(listing_row, analysis, profile=profile)
        photo = listing_row["photo_url"]

        if photo and len(body) <= MAX_CAPTION:
            sent = self._post(
                "sendPhoto",
                {
                    "chat_id": self.chat_id,
                    "photo": photo,
                    "caption": body,
                    "parse_mode": "HTML",
                },
            )
            if sent:
                return True
            # A dead or hotlink-protected image shouldn't lose us the alert.
            log.info("photo send failed, retrying as text")

        return self._post(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": body[:MAX_MESSAGE],
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            },
        )

    def send_text(self, text: str) -> bool:
        return self._post(
            "sendMessage",
            {"chat_id": self.chat_id, "text": text[:MAX_MESSAGE]},
        )
