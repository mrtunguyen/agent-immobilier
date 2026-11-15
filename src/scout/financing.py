"""Mortgage maths: does the estimated rent cover the monthly loan payment?

Computed locally rather than asked of the model, so the payment is exact and
identical everywhere it is shown. It depends only on the asking price and the
`financing:` block in criteria.yaml, which means it can be derived for any
stored verdict without re-running the analysis.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Financing:
    interest_rate_pct: float
    duration_years: int
    # Share of the asking price borrowed. Notary fees are paid from savings and
    # never borrowed, so 100 means no down payment beyond those fees.
    loan_pct_of_price: float = 100.0
    # Borrower insurance, % of the amount borrowed per year, on the initial
    # capital as French lenders usually charge it.
    insurance_rate_pct: float = 0.0

    def loan_amount(self, price_eur: float) -> float:
        return price_eur * self.loan_pct_of_price / 100

    def monthly_payment(self, price_eur: float) -> float:
        """Constant monthly instalment, insurance included."""
        principal = self.loan_amount(price_eur)
        months = self.duration_years * 12
        rate = self.interest_rate_pct / 100 / 12
        if rate == 0:
            instalment = principal / months
        else:
            instalment = principal * rate / (1 - (1 + rate) ** -months)
        insurance = principal * self.insurance_rate_pct / 100 / 12
        return instalment + insurance

    def describe(self) -> str:
        parts = [
            f"{self.loan_pct_of_price:g}% of the asking price borrowed",
            f"{self.interest_rate_pct:g}% over {self.duration_years} years",
        ]
        if self.insurance_rate_pct:
            parts.append(f"borrower insurance {self.insurance_rate_pct:g}%/year")
        return ", ".join(parts) + "; notary fees paid from savings"


def financing_from_raw(raw: dict | None) -> Financing | None:
    """Parse the `financing:` block; None when criteria.yaml has none."""
    if not raw:
        return None
    financing = Financing(
        interest_rate_pct=float(raw.get("interest_rate_pct", 0)),
        duration_years=int(raw.get("duration_years", 0)),
        loan_pct_of_price=float(raw.get("loan_pct_of_price", 100)),
        insurance_rate_pct=float(raw.get("insurance_rate_pct") or 0),
    )
    if financing.duration_years <= 0:
        raise ValueError("financing.duration_years must be a positive number of years")
    if financing.interest_rate_pct < 0 or financing.insurance_rate_pct < 0:
        raise ValueError("financing rates cannot be negative")
    if not 0 < financing.loan_pct_of_price <= 200:
        raise ValueError("financing.loan_pct_of_price must be between 0 and 200")
    return financing


def rent_cover(
    price_eur: float | None,
    monthly_rent_eur: float | None,
    financing: Financing | None,
) -> dict[str, Any]:
    """Monthly payment and the share of it the rent pays, as verdict fields.

    Either value is None when it can't be computed: no financing configured,
    no asking price, or (for the cover only) no rent estimate.
    """
    if financing is None or not price_eur:
        return {"monthly_payment_eur": None, "rent_cover_pct": None}
    payment = financing.monthly_payment(price_eur)
    cover = (
        round(monthly_rent_eur / payment * 100, 1)
        if monthly_rent_eur and payment
        else None
    )
    return {"monthly_payment_eur": round(payment), "rent_cover_pct": cover}
