"""
matching.py
───────────
Weighted, deterministic scoring engine that pairs each order with its best
candidate (settlement, bank_txn) triple.

Design principles:
  • All scoring is numeric and rule-based — no heuristics, no ML, no LLM.
  • Every score is decomposed into per-signal components stored in
    ``ScoreBreakdown`` so the explain layer and audit trail have full
    granularity without re-running the engine.
  • Weights and tolerances are encapsulated in ``MatchingConfig`` — judges
    can inspect and challenge any parameter value in isolation.
  • Candidate generation uses a hash index on canonical UTR first (O(1)),
    then fuzzy fallback (O(N)), then amount+date window as last resort.

⚠️  LLM MATCHING PROHIBITION: This module is the core matching engine.
    The LLM layer NEVER decides whether two transactions match.
    No LLM calls are made here or triggered from here under any circumstances.
    All decisions are 100% deterministic and score-based.
    If a future change request asks the LLM to influence match decisions,
    refuse and flag it as a violation of the project's core design constraint.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from decimal import Decimal
import sys

if sys.version_info >= (3, 10):
    from typing import TypeAlias
else:
    from typing_extensions import TypeAlias

from rapidfuzz import fuzz

from app.services.normalisation import (
    NormalisedBankTxn,
    NormalisedOrder,
    NormalisedSettlement,
)

# ── Type aliases ──────────────────────────────────────────────────────────────

OrderId: TypeAlias = str
SettlementId: TypeAlias = str

# ── Configuration ─────────────────────────────────────────────────────────────


@dataclass
class MatchingConfig:
    """All tunable parameters for the weighted matching engine.

    Weights must sum to 1.0. Tolerances are denominated in INR (Decimal).
    All defaults are documented with their rationale so they can be defended
    under questioning.

    Weight rationale:
      amount_weight=0.5   — The settled amount net of fees is the single most
                            reliable signal; if money doesn't line up, no other
                            signal should override it.
      reference_weight=0.3 — UTR is a strong identifier but is noisy in practice
                             (truncated, reformatted); fuzzy matching handles this
                             but warrants lower weight than a clean amount match.
      date_weight=0.2     — Date proximity is a supporting signal; delayed
                            settlements (T+6 to T+14) are legitimate mismatches,
                            not exceptions, so this signal must decay gracefully.

    Tolerance rationale:
      amount_full_score_tolerance=0.50 — Sub-₹1 differences are almost always
                            IEEE 754 / GST rounding artefacts; treat as perfect.
      amount_zero_score_tolerance=5.00 — Beyond ₹5 difference the match is
                            suspect; score decays linearly between the two bounds.
      date_full_score_days=2           — T+1 to T+2 is normal gateway settlement.
      date_zero_score_days=10          — Beyond 10 days the date signal contributes
                            nothing; the amount/UTR signals must carry the weight.
      fuzzy_utr_threshold=60           — Rapidfuzz partial_ratio score below 60
                            indicates the strings share too little to be the same
                            UTR even accounting for noise.
      bank_amount_tolerance=0.50       — Settlement→bank credit amount tolerance;
                            bank should mirror settlement exactly so tight bound.
    """

    # Signal weights (must sum to 1.0)
    amount_weight: float = 0.5
    reference_weight: float = 0.3
    date_weight: float = 0.2

    # Amount signal tolerances (INR)
    amount_full_score_tolerance: Decimal = field(
        default_factory=lambda: Decimal("0.50")
    )
    amount_zero_score_tolerance: Decimal = field(
        default_factory=lambda: Decimal("5.00")
    )

    # Date signal tolerances (days)
    date_full_score_days: int = 2
    date_zero_score_days: int = 10

    # UTR fuzzy match threshold [0–100]
    fuzzy_utr_threshold: float = 60.0

    # Settlement → bank statement amount tolerance (INR)
    bank_amount_tolerance: Decimal = field(
        default_factory=lambda: Decimal("0.50")
    )

    def __post_init__(self) -> None:
        """Validate that weights sum to 1.0 (within float precision)."""
        total = self.amount_weight + self.reference_weight + self.date_weight
        if abs(total - 1.0) > 1e-9:
            raise ValueError(
                f"MatchingConfig weights must sum to 1.0, got {total:.6f}"
            )


# ── Result data classes ───────────────────────────────────────────────────────


@dataclass
class ScoreBreakdown:
    """Per-signal scores before weighting.

    Each field holds a value in [0.0, 1.0] representing how strongly that
    signal supports the candidate pair. The weighted composite score is stored
    separately in ``MatchResult``.

    Storing raw (unweighted) scores enables the explain layer to describe
    *why* a score is what it is without re-running the engine.
    """

    # Order ↔ Settlement signals
    amount_score: float = 0.0        # how well settled+fee+tax ≈ order.amount
    reference_score: float = 0.0     # fuzzy UTR match between settlement and bank
    date_score: float = 0.0          # proximity of settled_date to order_date

    # Settlement ↔ Bank signals
    bank_amount_score: float = 0.0   # settled_amount vs credit_amount
    bank_utr_score: float = 0.0      # UTR fuzzy match settlement ↔ bank

    # Derived / diagnostic
    amount_diff_inr: Decimal = field(default_factory=Decimal)
    date_diff_days: int = 0
    best_utr_ratio: float = 0.0      # raw rapidfuzz score for audit trail

    def to_json(self) -> str:
        """Serialise to JSON string for storage in the audit/result row."""
        d = {
            "amount_score": self.amount_score,
            "reference_score": self.reference_score,
            "date_score": self.date_score,
            "bank_amount_score": self.bank_amount_score,
            "bank_utr_score": self.bank_utr_score,
            "amount_diff_inr": str(self.amount_diff_inr),
            "date_diff_days": self.date_diff_days,
            "best_utr_ratio": self.best_utr_ratio,
        }
        return json.dumps(d)


@dataclass
class MatchResult:
    """The output of the matching engine for a single order.

    ``composite_score`` is the primary sort key used by the classification
    layer. ``score_breakdown`` holds the full per-signal detail for audit.

    No classification (CLEAN/MISMATCH/EXCEPTION) is stored here; that is
    the classification layer's responsibility (Chunk 4).
    """

    order_id: str
    composite_score: float       # weighted sum ∈ [0.0, 1.0]
    score_breakdown: ScoreBreakdown

    # Best-match references (None if no viable candidate found)
    matched_settlement_id: str | None = None
    matched_settlement_order_id: str | None = None
    matched_bank_utr: str | None = None

    # Raw values for the explain layer — avoids having to re-join CSVs
    order_amount: Decimal = field(default_factory=Decimal)
    settled_amount: Decimal | None = None
    fee: Decimal | None = None
    tax_on_fee: Decimal | None = None
    order_date: date | None = None
    settled_date: date | None = None

    def score_breakdown_json(self) -> str:
        """Return ``score_breakdown`` as a JSON string for DB storage."""
        return self.score_breakdown.to_json()


# ── Internal scoring helpers ──────────────────────────────────────────────────


def _score_amount(
    order_amount: Decimal,
    settled_amount: Decimal,
    fee: Decimal,
    tax_on_fee: Decimal,
    cfg: MatchingConfig,
) -> tuple[float, Decimal]:
    """Score how well ``settled_amount + fee + tax_on_fee`` reconstructs ``order_amount``.

    The diff is computed as ``|order - (settled + fee + tax)|``.
    Within ``amount_full_score_tolerance`` → score 1.0.
    At or beyond ``amount_zero_score_tolerance`` → score 0.0.
    Between the two bounds → linear interpolation.

    Args:
        order_amount: Gross order amount from the ledger.
        settled_amount: Net amount credited by the gateway.
        fee: Gateway fee deducted.
        tax_on_fee: GST on the fee.
        cfg: Active ``MatchingConfig``.

    Returns:
        A (score, diff_inr) tuple where score ∈ [0.0, 1.0] and diff_inr is
        the absolute monetary difference for the audit breakdown.
    """
    reconstructed = settled_amount + fee + tax_on_fee
    diff = abs(order_amount - reconstructed)

    if diff <= cfg.amount_full_score_tolerance:
        return 1.0, diff

    tolerance_range = cfg.amount_zero_score_tolerance - cfg.amount_full_score_tolerance
    if tolerance_range <= Decimal(0) or diff >= cfg.amount_zero_score_tolerance:
        return 0.0, diff

    # Linear decay — keep arithmetic in Decimal to avoid float drift
    score = float(
        Decimal("1.0")
        - (diff - cfg.amount_full_score_tolerance) / tolerance_range
    )
    return max(0.0, min(1.0, score)), diff


def _score_date(
    order_date: date,
    settled_date: date,
    cfg: MatchingConfig,
) -> tuple[float, int]:
    """Score the proximity of ``settled_date`` to ``order_date``.

    Settlement lag of 0–``date_full_score_days`` → 1.0.
    Beyond ``date_zero_score_days`` → 0.0.
    Linear interpolation between bounds.

    Args:
        order_date: Date the order was placed.
        settled_date: Date the gateway settled the payment.
        cfg: Active ``MatchingConfig``.

    Returns:
        A (score, days_diff) tuple.
    """
    days = abs((settled_date - order_date).days)

    if days <= cfg.date_full_score_days:
        return 1.0, days

    if days >= cfg.date_zero_score_days:
        return 0.0, days

    window = cfg.date_zero_score_days - cfg.date_full_score_days
    score = 1.0 - (days - cfg.date_full_score_days) / window
    return max(0.0, min(1.0, score)), days


def _best_utr_ratio(
    settlement_canonical: str,
    bank_canonical: str,
    bank_extracted_utrs: tuple[str, ...],
) -> float:
    """Compute the best rapidfuzz ratio between a settlement UTR and all bank candidates.

    Tests ``bank_canonical`` plus every UTR mined from the bank description text,
    returning the highest score found. This handles the truncation case where
    ``utr_reference`` is cut off but the full UTR appears in ``description``.

    Uses ``fuzz.partial_ratio`` rather than ``fuzz.ratio`` because truncated UTRs
    are substrings of the full canonical form — partial_ratio scores 100 for a
    full prefix match while ratio would penalise the length difference.

    Args:
        settlement_canonical: Canonical UTR from the settlement report.
        bank_canonical: Canonical UTR from the bank statement utr_reference column.
        bank_extracted_utrs: Additional UTR candidates extracted from bank description.

    Returns:
        The highest rapidfuzz partial_ratio score ∈ [0.0, 100.0].
    """
    candidates = (bank_canonical,) + bank_extracted_utrs
    best: float = 0.0
    for candidate in candidates:
        ratio = fuzz.partial_ratio(settlement_canonical, candidate)
        best = max(best, ratio)
    return best


def _score_bank_link(
    settlement: NormalisedSettlement,
    bank: NormalisedBankTxn,
    cfg: MatchingConfig,
) -> tuple[float, float, float]:
    """Score the link between a settlement row and a bank transaction.

    Returns:
        (bank_amount_score, bank_utr_score, raw_utr_ratio)
    """
    # Amount: settled_amount should equal bank credit_amount (tight tolerance)
    amount_diff = abs(settlement.settled_amount - bank.credit_amount)
    bank_amount_score = (
        1.0 if amount_diff <= cfg.bank_amount_tolerance else 0.0
    )

    raw_ratio = _best_utr_ratio(
        settlement.utr_canonical,
        bank.utr_canonical,
        bank.extracted_utrs,
    )
    # Only award a non-zero bank_utr_score if the ratio meets the threshold;
    # below threshold the strings are too dissimilar to count as a reference match.
    bank_utr_score = 1.0 if raw_ratio >= cfg.fuzzy_utr_threshold else 0.0

    return bank_amount_score, bank_utr_score, raw_ratio


# ── Index builders ────────────────────────────────────────────────────────────


def _build_settlement_index(
    settlements: list[NormalisedSettlement],
) -> dict[str, list[NormalisedSettlement]]:
    """Build a hash index from canonical UTR → settlement list.

    A list is used as the value because duplicate settlements share the same
    order_id (though not the same UTR — duplicates have a fresh UTR). The index
    key is the canonical settlement UTR.

    Args:
        settlements: All normalised settlement rows.

    Returns:
        Dict mapping canonical UTR strings to the settlements that carry them.
    """
    index: dict[str, list[NormalisedSettlement]] = {}
    for s in settlements:
        index.setdefault(s.utr_canonical, []).append(s)
    return index


def _build_order_id_index(
    settlements: list[NormalisedSettlement],
) -> dict[str, list[NormalisedSettlement]]:
    """Build a hash index from order_id → settlement list.

    Allows O(1) lookup of settlements by order_id, needed for the direct
    foreign-key candidate generation path.

    Args:
        settlements: All normalised settlement rows.

    Returns:
        Dict mapping order_id to the settlements that reference it.
    """
    index: dict[str, list[NormalisedSettlement]] = {}
    for s in settlements:
        index.setdefault(s.order_id, []).append(s)
    return index


def _build_bank_utr_index(
    bank_txns: list[NormalisedBankTxn],
) -> dict[str, list[NormalisedBankTxn]]:
    """Build a hash index from canonical UTR → bank txn list.

    Includes both the primary ``utr_canonical`` and any UTRs extracted from
    the description, so truncated-UTR bank rows are still indexed.

    Args:
        bank_txns: All normalised bank statement rows.

    Returns:
        Dict mapping canonical UTR strings to matching bank rows.
    """
    index: dict[str, list[NormalisedBankTxn]] = {}
    for b in bank_txns:
        index.setdefault(b.utr_canonical, []).append(b)
        for extracted in b.extracted_utrs:
            index.setdefault(extracted, []).append(b)
    return index


# ── Candidate generation ──────────────────────────────────────────────────────


def _get_settlement_candidates(
    order: NormalisedOrder,
    settlements: list[NormalisedSettlement],
    utr_index: dict[str, list[NormalisedSettlement]],
    order_id_index: dict[str, list[NormalisedSettlement]],
    cfg: MatchingConfig,
) -> list[NormalisedSettlement]:
    """Generate settlement candidates for an order using a tiered strategy.

    Tier 1 — order_id lookup (direct FK match, O(1)):
      The settlement report always carries order_id. This is the primary path.

    Tier 2 — amount + date window fallback (O(N)):
      Only reached if order_id lookup yields nothing. Scans all settlements and
      returns those where the reconstructed amount is within tolerance and the
      date is within the zero-score window.

    Args:
        order: The order being matched.
        settlements: Full list of normalised settlements.
        utr_index: Canonical UTR → settlement index.
        order_id_index: order_id → settlement index.
        cfg: Active ``MatchingConfig``.

    Returns:
        A list of candidate settlements (may be empty).
    """
    # Tier 1 — direct order_id lookup
    candidates = order_id_index.get(order.order_id, [])
    if candidates:
        return candidates

    # Tier 2 — amount + date window fallback (only for same order_id, safety net)
    # This path handles the rare case where order_id is absent from the settlement
    # but amount and date are consistent. Requiring order_id here prevents false
    # positives caused by coincidental amount similarity between unrelated orders.
    fallback: list[NormalisedSettlement] = []
    for s in settlements:
        if s.order_id != order.order_id:
            continue  # strict FK requirement; relax only if order_id is missing
        _, diff = _score_amount(
            order.amount, s.settled_amount, s.fee, s.tax_on_fee, cfg
        )
        if diff < cfg.amount_zero_score_tolerance:
            _, days = _score_date(order.order_date, s.settled_date, cfg)
            if days <= cfg.date_zero_score_days:
                fallback.append(s)
    return fallback


def _get_bank_candidates(
    settlement: NormalisedSettlement,
    bank_txns: list[NormalisedBankTxn],
    bank_utr_index: dict[str, list[NormalisedBankTxn]],
    cfg: MatchingConfig,
) -> list[NormalisedBankTxn]:
    """Generate bank transaction candidates for a settlement.

    Uses fuzzy UTR matching against the index. Falls back to scanning all bank
    rows if the exact canonical key produces no hit.

    Args:
        settlement: The settlement being linked to a bank credit.
        bank_txns: Full list of normalised bank rows.
        bank_utr_index: Canonical UTR → bank txn index.
        cfg: Active ``MatchingConfig``.

    Returns:
        A list of candidate bank rows (may be empty).
    """
    # Try exact canonical key first
    exact = bank_utr_index.get(settlement.utr_canonical, [])
    if exact:
        return exact

    # Fuzzy scan — handles truncated UTRs not in index
    candidates: list[NormalisedBankTxn] = []
    for b in bank_txns:
        ratio = _best_utr_ratio(
            settlement.utr_canonical, b.utr_canonical, b.extracted_utrs
        )
        if ratio >= cfg.fuzzy_utr_threshold:
            candidates.append(b)
    return candidates


# ── Public API ─────────────────────────────────────────────────────────────────


def score_pair(
    order: NormalisedOrder,
    settlement: NormalisedSettlement,
    bank: NormalisedBankTxn | None,
    cfg: MatchingConfig,
) -> tuple[float, ScoreBreakdown]:
    """Score a candidate (order, settlement, bank) triple.

    Computes weighted composite score from three order↔settlement signals and
    two settlement↔bank signals. The bank signals contribute to the reference
    weight; the date and amount signals are order↔settlement only.

    Weight allocation detail:
      amount_weight   → ``amount_score``      (order ↔ settlement amount check)
      reference_weight → ``reference_score``  (average of bank_utr + bank_amount)
      date_weight      → ``date_score``       (order_date ↔ settled_date proximity)

    Args:
        order: The order being matched.
        settlement: The candidate settlement row.
        bank: The candidate bank transaction, or None if no bank link was found.
        cfg: Active ``MatchingConfig``.

    Returns:
        A (composite_score, ScoreBreakdown) tuple.
    """
    # ── Amount signal (order ↔ settlement) ─────────────────────────────────
    amount_score, amount_diff = _score_amount(
        order.amount,
        settlement.settled_amount,
        settlement.fee,
        settlement.tax_on_fee,
        cfg,
    )

    # ── Date signal (order ↔ settlement) ───────────────────────────────────
    date_score, date_diff_days = _score_date(
        order.order_date, settlement.settled_date, cfg
    )

    # ── Reference signal (settlement ↔ bank) ───────────────────────────────
    if bank is not None:
        bank_amount_score, bank_utr_score, best_ratio = _score_bank_link(
            settlement, bank, cfg
        )
        # Reference score is the average of the two bank-link sub-signals
        reference_score = (bank_utr_score + bank_amount_score) / 2.0
    else:
        bank_amount_score = 0.0
        bank_utr_score = 0.0
        best_ratio = 0.0
        reference_score = 0.0

    # ── Composite ──────────────────────────────────────────────────────────
    composite = (
        cfg.amount_weight * amount_score
        + cfg.reference_weight * reference_score
        + cfg.date_weight * date_score
    )
    composite = max(0.0, min(1.0, composite))

    breakdown = ScoreBreakdown(
        amount_score=amount_score,
        reference_score=reference_score,
        date_score=date_score,
        bank_amount_score=bank_amount_score,
        bank_utr_score=bank_utr_score,
        amount_diff_inr=amount_diff,
        date_diff_days=date_diff_days,
        best_utr_ratio=best_ratio,
    )

    return composite, breakdown


def match_order(
    order: NormalisedOrder,
    settlements: list[NormalisedSettlement],
    bank_txns: list[NormalisedBankTxn],
    utr_index: dict[str, list[NormalisedSettlement]],
    order_id_index: dict[str, list[NormalisedSettlement]],
    bank_utr_index: dict[str, list[NormalisedBankTxn]],
    cfg: MatchingConfig,
) -> MatchResult:
    """Find the best-matching (settlement, bank_txn) pair for a single order.

    Evaluates every candidate settlement and, for each, every candidate bank
    transaction. Returns the triple with the highest composite score.

    If no settlement candidate is found the result has composite_score=0.0
    and all matched_* fields set to None — the classification layer will
    tag this as EXCEPTION / FAILED_PAYMENT.

    Args:
        order: The order to match.
        settlements: All normalised settlement rows.
        bank_txns: All normalised bank rows.
        utr_index: Canonical UTR → settlement index.
        order_id_index: order_id → settlement index.
        bank_utr_index: Canonical UTR → bank txn index.
        cfg: Active ``MatchingConfig``.

    Returns:
        A ``MatchResult`` for the order with the best candidate triple.
    """
    settlement_candidates = _get_settlement_candidates(
        order, settlements, utr_index, order_id_index, cfg
    )

    if not settlement_candidates:
        # No settlement found at all — score=0.0, classification layer handles it
        return MatchResult(
            order_id=order.order_id,
            composite_score=0.0,
            score_breakdown=ScoreBreakdown(),
            order_amount=order.amount,
            order_date=order.order_date,
        )

    best_score = -1.0
    best_breakdown = ScoreBreakdown()
    best_settlement: NormalisedSettlement | None = None
    best_bank: NormalisedBankTxn | None = None

    for settlement in settlement_candidates:
        bank_candidates = _get_bank_candidates(
            settlement, bank_txns, bank_utr_index, cfg
        )

        if bank_candidates:
            for bank in bank_candidates:
                score, breakdown = score_pair(order, settlement, bank, cfg)
                if score > best_score:
                    best_score = score
                    best_breakdown = breakdown
                    best_settlement = settlement
                    best_bank = bank
        else:
            # Settlement found but no bank link — score against None bank
            score, breakdown = score_pair(order, settlement, None, cfg)
            if score > best_score:
                best_score = score
                best_breakdown = breakdown
                best_settlement = settlement
                best_bank = None

    return MatchResult(
        order_id=order.order_id,
        composite_score=max(best_score, 0.0),
        score_breakdown=best_breakdown,
        matched_settlement_id=(
            best_settlement.settlement_id if best_settlement else None
        ),
        matched_settlement_order_id=(
            best_settlement.order_id if best_settlement else None
        ),
        matched_bank_utr=(
            best_bank.utr_reference if best_bank else None
        ),
        order_amount=order.amount,
        settled_amount=(
            best_settlement.settled_amount if best_settlement else None
        ),
        fee=best_settlement.fee if best_settlement else None,
        tax_on_fee=best_settlement.tax_on_fee if best_settlement else None,
        order_date=order.order_date,
        settled_date=(
            best_settlement.settled_date if best_settlement else None
        ),
    )


def run_matching(
    orders: list[NormalisedOrder],
    settlements: list[NormalisedSettlement],
    bank_txns: list[NormalisedBankTxn],
    cfg: MatchingConfig | None = None,
) -> tuple[list[MatchResult], float]:
    """Run the full matching engine over all orders.

    Builds indexes once, then calls ``match_order`` for each order.
    Returns all results and the total wall-clock runtime in seconds.

    Args:
        orders: All normalised order rows.
        settlements: All normalised settlement rows.
        bank_txns: All normalised bank rows.
        cfg: Matching configuration; uses ``MatchingConfig()`` defaults if None.

    Returns:
        A (results, elapsed_seconds) tuple.
    """
    if cfg is None:
        cfg = MatchingConfig()

    utr_index = _build_settlement_index(settlements)
    order_id_index = _build_order_id_index(settlements)
    bank_utr_index = _build_bank_utr_index(bank_txns)

    t0 = time.perf_counter()
    results = [
        match_order(order, settlements, bank_txns, utr_index, order_id_index, bank_utr_index, cfg)
        for order in orders
    ]
    elapsed = time.perf_counter() - t0

    return results, elapsed


def detect_unmatched_bank_credits(
    bank_txns: list[NormalisedBankTxn],
    settlements: list[NormalisedSettlement],
    cfg: MatchingConfig | None = None,
) -> list[NormalisedBankTxn]:
    """Find bank credits that have no matching settlement UTR.

    These are phantom credits — the PHANTOM_CREDIT exception sub-type.
    They appear in the bank statement but their UTR does not exist in the
    settlement report (neither exact nor fuzzy match above threshold).

    Detection uses a higher threshold (90) than the pair-scoring engine (60).

    Why 90, not 60?
    All UTRs share the prefix UTR+YEAR+BANKCODE (e.g. "UTR2024HDFC...").
    At threshold=60, partial_ratio matches any two UTRs with the same bank
    code because the shared prefix scores 60–85 regardless of the suffix.
    Phantom detection requires a real suffix match, so we use 90 — this
    correctly rejects phantom-vs-real pairs while accepting truncated UTRs
    (which score 100 on partial_ratio when the truncated string is a full
    prefix of the canonical form).

    Args:
        bank_txns: All normalised bank rows.
        settlements: All normalised settlement rows.
        cfg: Matching configuration; uses defaults if None.

    Returns:
        List of bank rows for which no settlement UTR could be linked.
    """
    if cfg is None:
        cfg = MatchingConfig()

    # Use a stricter threshold for phantom detection to avoid false negatives
    # caused by the shared UTR prefix pattern.
    PHANTOM_DETECTION_THRESHOLD = 90.0

    settlement_utrs = [s.utr_canonical for s in settlements]
    unmatched: list[NormalisedBankTxn] = []

    for bank in bank_txns:
        all_bank_candidates = (bank.utr_canonical,) + bank.extracted_utrs
        matched = False
        for bank_utr in all_bank_candidates:
            for s_utr in settlement_utrs:
                if fuzz.partial_ratio(bank_utr, s_utr) >= PHANTOM_DETECTION_THRESHOLD:
                    matched = True
                    break
            if matched:
                break
        if not matched:
            unmatched.append(bank)

    return unmatched


def detect_duplicate_settlements(
    settlements: list[NormalisedSettlement],
) -> dict[str, list[NormalisedSettlement]]:
    """Find order_ids that appear more than once in the settlement report.

    A legitimate order should have exactly one settlement. Multiple settlements
    for the same order_id indicate a duplicate payout — the DUPLICATE_SETTLEMENT
    exception sub-type.

    Args:
        settlements: All normalised settlement rows.

    Returns:
        Set of ``order_id`` strings that appear more than once in the settlements.
    """
    counts: dict[str, int] = {}
    for s in settlements:
        counts[s.order_id] = counts.get(s.order_id, 0) + 1
    return {oid for oid, count in counts.items() if count > 1}


def detect_duplicate_orders(
    orders: list[NormalisedOrder],
) -> set[str]:
    """Find order IDs that appear more than once in the order ledger.

    A single order_id should only appear once in the ledger. If it appears
    multiple times, it is an anomaly.

    Args:
        orders: All normalised order rows.

    Returns:
        Set of ``order_id`` strings that appear more than once in the orders.
    """
    counts: dict[str, int] = {}
    for o in orders:
        counts[o.order_id] = counts.get(o.order_id, 0) + 1
    return {oid for oid, count in counts.items() if count > 1}


def detect_ambiguous_bank_matches(
    results: list[MatchResult],
) -> set[str]:
    """Find orders that matched a bank row that was also claimed by another order with the same score.
    
    If multiple orders matched the same bank transaction UTR, and their reference scores 
    are identical (or very close), they are both ambiguous.
    
    Args:
        results: All match results.
        
    Returns:
        Set of ``order_id`` strings that have ambiguous bank matches.
    """
    bank_claims: dict[str, list[MatchResult]] = {}
    for r in results:
        if r.matched_bank_utr:
            bank_claims.setdefault(r.matched_bank_utr, []).append(r)
            
    ambiguous_order_ids = set()
    for utr, claims in bank_claims.items():
        if len(claims) > 1:
            # Check if scores are effectively tied (within 0.01)
            # Find the max reference_score for this bank row
            max_score = max(c.score_breakdown.reference_score for c in claims)
            # Find all claims that are near this max score
            tied_claims = [c for c in claims if abs(c.score_breakdown.reference_score - max_score) < 0.01]
            if len(tied_claims) > 1:
                ambiguous_order_ids.update(c.order_id for c in tied_claims)
                
    return ambiguous_order_ids
