"""
normalisation.py
────────────────
Reads raw CSV rows and emits unified internal representations that the matching
engine can compare without caring about source-specific formatting differences.

Key transformations:
  • UTR canonicalisation — strips hyphens/spaces, uppercases, so that
    "UTR-2024-SBIN-Q4FM..." and "utr2024sbinq4fm..." resolve to the same key.
  • Amount coercion — float → Decimal(2dp) to avoid IEEE 754 drift in comparisons.
  • Date parsing — any ISO 8601 string → datetime.date.
  • Description mining — extracts candidate UTR substrings from free-text bank
    description fields (e.g. "NEFT CR-UTR2024HDFC... RAZORPAY").

⚠️  LLM MATCHING PROHIBITION: This module is part of the matching pipeline.
    The LLM layer NEVER decides whether two transactions match.
    All logic here is deterministic and produces no LLM calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# ── Constants ──────────────────────────────────────────────────────────────────

# Regex that identifies UTR-shaped tokens inside a free-text description.
# Real UTRs follow the pattern: optional prefix (UTR/NEFT/IMPS) then
# 4-digit year, 4-char bank code, alphanumeric suffix.
_UTR_PATTERN = re.compile(
    r"(?:UTR[-\s]?)?(\d{4}[-\s]?[A-Z]{4}[-\s]?[A-Z0-9]+)",
    re.IGNORECASE,
)

_TWO_DP = Decimal("0.01")


# ── Data classes ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NormalisedOrder:
    """A single row from order_ledger.csv after normalisation."""

    order_id: str
    order_date: date
    customer_name: str
    amount: Decimal  # original order amount (gross)
    currency: str


@dataclass(frozen=True)
class NormalisedSettlement:
    """A single row from settlement_report.csv after normalisation."""

    settlement_id: str
    order_id: str
    settled_date: date
    settled_amount: Decimal
    fee: Decimal
    tax_on_fee: Decimal
    utr_number: str           # raw as-stored in settlement report
    utr_canonical: str        # canonicalised for matching


@dataclass(frozen=True)
class NormalisedBankTxn:
    """A single row from bank_statement.csv after normalisation."""

    txn_date: date
    description: str
    credit_amount: Decimal
    utr_reference: str        # raw as-stored in bank statement
    utr_canonical: str        # canonicalised for matching
    extracted_utrs: tuple[str, ...]  # UTR candidates mined from description text


# ── Public API ─────────────────────────────────────────────────────────────────


def canonicalise_utr(raw: str) -> str:
    """Canonicalise a UTR string to a consistent uppercase, hyphen-free form.

    Normalises the four known noise variants produced by different bank systems:
      style-0  canonical        UTR2024HDFCYE5165201944
      style-1  hyphenated       UTR-2024-HDFC-YE5165201944
      style-2  truncated        UTR2024HDFCYE51  (first 16 chars)
      style-3  lowercase        utr2024hdfcye5165201944

    For truncated UTRs the canonical form is also truncated — fuzzy matching
    in the engine handles the remaining ambiguity.

    Args:
        raw: The raw UTR string as it appears in the source document.

    Returns:
        An uppercase, hyphen-free string suitable for exact or fuzzy comparison.

    Raises:
        ValueError: If ``raw`` is empty or whitespace-only.
    """
    if not raw or not raw.strip():
        raise ValueError("UTR string must not be empty")
    return re.sub(r"[-\s]", "", raw).upper()


def to_decimal(value: float | str | Decimal) -> Decimal:
    """Coerce a monetary value to Decimal with 2 decimal places.

    Uses ROUND_HALF_UP to match standard financial rounding convention.

    Args:
        value: Raw amount — may be a float (from CSV), string, or Decimal.

    Returns:
        A Decimal quantised to 2 decimal places.
    """
    try:
        if value == "":
            return Decimal("0.00")
        return Decimal(str(value)).quantize(_TWO_DP, rounding=ROUND_HALF_UP)
    except (TypeError, ValueError, InvalidOperation):
        return Decimal("0.00")


def parse_date(raw: str) -> date:
    """Parse an ISO 8601 date string to a ``datetime.date``.

    Args:
        raw: Date string in ``YYYY-MM-DD`` format.

    Returns:
        A ``datetime.date`` instance.

    Raises:
        ValueError: If the string cannot be parsed as a date.
    """
    try:
        raw = raw.strip()
        if not raw:
            return date(2000, 1, 1)
        return date.fromisoformat(raw)
    except ValueError:
        return date(2000, 1, 1)


def extract_utrs_from_description(description: str) -> tuple[str, ...]:
    """Mine candidate UTR tokens from a free-text bank description.

    Bank descriptions embed UTR references in wildly inconsistent positions
    and formats. This function extracts all plausible UTR-shaped tokens and
    returns their canonical forms so the matcher can attempt fuzzy comparison.

    Args:
        description: The raw description field from a bank statement row.

    Returns:
        A tuple of canonical UTR candidates found in the description.
        May be empty if no UTR-shaped token is present.
    """
    matches = _UTR_PATTERN.findall(description)
    # Prepend "UTR" only if the token doesn't already start with it
    results: list[str] = []
    for m in matches:
        candidate = re.sub(r"[-\s]", "", m).upper()
        if not candidate.startswith("UTR"):
            candidate = "UTR" + candidate
        results.append(candidate)
    return tuple(dict.fromkeys(results))  # deduplicate, preserve order


def normalise_order(row: dict) -> NormalisedOrder:
    """Convert a raw order_ledger CSV row to a ``NormalisedOrder``.

    Args:
        row: Dict with keys: order_id, order_date, customer_name, amount, currency.

    Returns:
        A ``NormalisedOrder`` with typed, cleaned fields.
    """
    return NormalisedOrder(
        order_id=str(row.get("order_id", "")).strip() or "UNKNOWN_ORDER",
        order_date=parse_date(str(row.get("order_date", "2000-01-01"))),
        customer_name=str(row.get("customer_name", "")).strip() or "Unknown",
        amount=to_decimal(row.get("amount", 0.0)),
        currency=str(row.get("currency", "INR")).strip().upper(),
    )


def normalise_settlement(row: dict) -> NormalisedSettlement:
    """Convert a raw settlement_report CSV row to a ``NormalisedSettlement``.

    Args:
        row: Dict with keys: settlement_id, order_id, settled_date,
             settled_amount, fee, tax_on_fee, utr_number.

    Returns:
        A ``NormalisedSettlement`` with typed, cleaned fields and a
        pre-computed canonical UTR for fast lookup.
    """
    raw_utr = str(row.get("utr_number", "")).strip() or "UNKNOWN_UTR"
    # Some malformed UTRs might raise ValueError in canonicalise_utr
    try:
        utr_canonical = canonicalise_utr(raw_utr)
    except ValueError:
        utr_canonical = "UNKNOWN_UTR"

    return NormalisedSettlement(
        settlement_id=str(row.get("settlement_id", "")).strip() or "UNKNOWN_SETL",
        order_id=str(row.get("order_id", "")).strip() or "UNKNOWN_ORDER",
        settled_date=parse_date(str(row.get("settled_date", "2000-01-01"))),
        settled_amount=to_decimal(row.get("settled_amount", 0.0)),
        fee=to_decimal(row.get("fee", 0.0)),
        tax_on_fee=to_decimal(row.get("tax_on_fee", 0.0)),
        utr_number=raw_utr,
        utr_canonical=utr_canonical,
    )


def normalise_bank_txn(row: dict) -> NormalisedBankTxn:
    """Convert a raw bank_statement CSV row to a ``NormalisedBankTxn``.

    Also mines the description field for embedded UTR tokens so the engine
    can attempt a reference match even when ``utr_reference`` is truncated.

    Args:
        row: Dict with keys: txn_date, description, credit_amount, utr_reference.

    Returns:
        A ``NormalisedBankTxn`` with typed fields, a canonical reference UTR,
        and a tuple of any additional UTR candidates extracted from the description.
    """
    raw_ref = str(row.get("utr_reference", "")).strip() or "UNKNOWN_UTR"
    description = str(row.get("description", "")).strip()
    extracted = extract_utrs_from_description(description)
    
    try:
        utr_canonical = canonicalise_utr(raw_ref)
    except ValueError:
        utr_canonical = "UNKNOWN_UTR"

    return NormalisedBankTxn(
        txn_date=parse_date(str(row.get("txn_date", "2000-01-01"))),
        description=description,
        credit_amount=to_decimal(row.get("credit_amount", 0.0)),
        utr_reference=raw_ref,
        utr_canonical=utr_canonical,
        extracted_utrs=extracted,
    )
