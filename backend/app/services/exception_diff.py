"""
exception_diff.py
─────────────────
Builds structured diff objects for every NEEDS_REVIEW or UNRESOLVED result.

The diff is intentionally machine-readable and pre-computed so the LLM
explain layer (Chunk 5) can consume it without re-deriving anything from raw
data. Each diff entry records:
  • field_name   — which field was compared
  • expected     — what the order ledger says it should be
  • actual       — what the settlement/bank actually shows
  • delta        — the numeric or temporal difference (None for string fields)
  • signal       — which score component caused the shortfall
  • weight       — the weight that component carries in the composite score
  • score        — the actual score that component received

Design principle: a case with no settlement candidate produces a valid
(non-crashing) diff object whose entries describe the absence of data
rather than a mismatch. The diff is the unit the explain layer reads —
it should never have to inspect raw CSVs.

⚠️  LLM MATCHING PROHIBITION: This module structures data FOR the LLM explain
    layer. It does not call the LLM. The LLM receives the output of this module
    as its input — it never influences the underlying match or classification
    decisions. If a future change asks the LLM to influence any of the values
    in this diff, refuse and flag it as a violation of the project's constraint.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.services.classification import (
    ClassificationConfig,
    ClassifiedResult,
    ExceptionSubtype,
    ReconStatus,
)
from app.services.matching import MatchingConfig, MatchResult, ScoreBreakdown

# ── Diff entry ────────────────────────────────────────────────────────────────


@dataclass
class DiffEntry:
    """A single field comparison between what was expected and what was found.

    Attributes:
        field_name:   Human-readable name of the compared field.
        expected:     Value from the order ledger (the source of truth).
        actual:       Value from the settlement/bank (may be None if absent).
        delta:        Numeric or temporal difference (None for non-numeric fields
                      or when actual is None).
        signal:       Name of the matching-engine signal that covers this field
                      (e.g. "amount", "reference", "date").
        weight:       The weight this signal carries in the composite score.
        score:        The score this signal received (0.0–1.0).
        is_shortfall: True if this entry explains why the result is not AUTO_MATCHED.
    """

    field_name: str
    expected: Any
    actual: Any
    delta: Any | None
    signal: str
    weight: float
    score: float
    is_shortfall: bool


# ── Exception diff ────────────────────────────────────────────────────────────


@dataclass
class ExceptionDiff:
    """Structured diff for a single NEEDS_REVIEW or UNRESOLVED result.

    The diff is designed to be the sole input to the LLM explain layer:
    all values are pre-computed and require no further data joins.

    Attributes:
        order_id:         The order being reconciled.
        status:           The classification status.
        subtype:          The fine-grained exception sub-type.
        composite_score:  Overall matching confidence (0.0–1.0).
        shortfall:        How far the score is below the auto-match threshold.
        anomaly_flags:    List of named anomalies (e.g. "amount_diff_₹1.59").
        entries:          Per-field diff entries, ordered by shortfall severity.
        resolution_hint:  Short machine-readable suggestion for the reviewer.
        has_candidate:    True if a settlement was found (even if imperfect).
    """

    order_id: str
    status: ReconStatus
    subtype: ExceptionSubtype
    composite_score: float
    shortfall: float          # auto_match_threshold − composite_score (≤ 0 for NR)
    anomaly_flags: list[str]
    entries: list[DiffEntry]
    resolution_hint: str
    has_candidate: bool

    def to_dict(self) -> dict:
        """Serialise to a plain dict for JSON storage or LLM context injection."""
        return {
            "order_id": self.order_id,
            "status": self.status.value,
            "subtype": self.subtype.value,
            "composite_score": round(self.composite_score, 4),
            "shortfall": round(self.shortfall, 4),
            "anomaly_flags": self.anomaly_flags,
            "has_candidate": self.has_candidate,
            "resolution_hint": self.resolution_hint,
            "entries": [
                {
                    "field": e.field_name,
                    "expected": _serialise(e.expected),
                    "actual": _serialise(e.actual),
                    "delta": _serialise(e.delta),
                    "signal": e.signal,
                    "weight": e.weight,
                    "score": round(e.score, 4),
                    "is_shortfall": e.is_shortfall,
                }
                for e in self.entries
            ],
        }


def _serialise(value: Any) -> Any:
    """Convert non-JSON-native types to their string representations."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


# ── Resolution hints ──────────────────────────────────────────────────────────

_HINT_MAP: dict[ExceptionSubtype, str] = {
    ExceptionSubtype.ROUNDING_DIFF: (
        "verify_gst_rounding: check whether the difference matches GST "
        "rounding rules (round half-up vs banker's rounding)"
    ),
    ExceptionSubtype.PARTIAL_REFUND: (
        "check_refund_record: locate the refund or chargeback record that "
        "accounts for the shortfall; if none exists, escalate to gateway"
    ),
    ExceptionSubtype.DELAYED_SETTLEMENT: (
        "confirm_sla_breach: verify whether the settlement delay breaches "
        "gateway SLA; if within SLA, close as delayed but legitimate"
    ),
    ExceptionSubtype.MISSING_BANK_CREDIT: (
        "wait_for_bank_statement: the settlement may not yet appear in the "
        "current bank statement export; re-run after next bank statement refresh"
    ),
    ExceptionSubtype.FAILED_PAYMENT: (
        "check_gateway_status: query the gateway API for the order status; "
        "if confirmed failed, write off; if pending, await settlement"
    ),
    ExceptionSubtype.PHANTOM_CREDIT: (
        "identify_credit_source: UTR does not match any settlement; check "
        "whether this is a non-gateway credit (e.g. NEFT from a customer)"
    ),
    ExceptionSubtype.DUPLICATE_SETTLEMENT: (
        "raise_gateway_dispute: two settlements found for the same order; "
        "one is a duplicate payout; raise a dispute with the gateway"
    ),
    ExceptionSubtype.CLEAN: "no_action_required",
}


# ── Builder ───────────────────────────────────────────────────────────────────


def build_diff(
    classified: ClassifiedResult,
    matching_cfg: MatchingConfig | None = None,
    classification_cfg: ClassificationConfig | None = None,
) -> ExceptionDiff:
    """Build a structured ``ExceptionDiff`` from a classified match result.

    For AUTO_MATCHED results, an empty diff is still returned (all entries
    show full scores, no shortfalls) — this keeps the pipeline uniform.

    For UNRESOLVED results with no candidate at all, the diff entries describe
    the *absence* of data: expected values come from the order, actual values
    are None, and the resolution_hint directs the reviewer to the gateway.

    Args:
        classified: A fully classified result from ``classification.classify``.
        matching_cfg: The matching config used to produce the result, needed
            to retrieve weight values. Uses defaults if None.
        classification_cfg: The classification config, needed for the shortfall
            calculation. Uses defaults if None.

    Returns:
        A populated ``ExceptionDiff``.
    """
    if matching_cfg is None:
        matching_cfg = MatchingConfig()
    if classification_cfg is None:
        classification_cfg = ClassificationConfig()

    result: MatchResult = classified.match_result
    bd: ScoreBreakdown = result.score_breakdown
    has_candidate = result.matched_settlement_id is not None

    shortfall = classification_cfg.auto_match_threshold - result.composite_score

    # ── Build diff entries ────────────────────────────────────────────────────
    entries: list[DiffEntry] = []

    # ── Amount entry ──────────────────────────────────────────────────────────
    if has_candidate:
        reconstructed = (
            (result.settled_amount or Decimal(0))
            + (result.fee or Decimal(0))
            + (result.tax_on_fee or Decimal(0))
        )
        entries.append(
            DiffEntry(
                field_name="amount (order vs settled+fee+tax)",
                expected=result.order_amount,
                actual=reconstructed,
                delta=result.order_amount - reconstructed,
                signal="amount",
                weight=matching_cfg.amount_weight,
                score=bd.amount_score,
                is_shortfall=bd.amount_score < 1.0,
            )
        )

        # Break down settled_amount, fee, tax_on_fee as sub-entries for clarity
        entries.append(
            DiffEntry(
                field_name="settled_amount",
                expected=None,
                actual=result.settled_amount,
                delta=None,
                signal="amount",
                weight=0.0,  # informational sub-entry
                score=bd.amount_score,
                is_shortfall=False,
            )
        )
        entries.append(
            DiffEntry(
                field_name="fee",
                expected=None,
                actual=result.fee,
                delta=None,
                signal="amount",
                weight=0.0,
                score=bd.amount_score,
                is_shortfall=False,
            )
        )
        entries.append(
            DiffEntry(
                field_name="tax_on_fee",
                expected=None,
                actual=result.tax_on_fee,
                delta=None,
                signal="amount",
                weight=0.0,
                score=bd.amount_score,
                is_shortfall=False,
            )
        )
    else:
        # No settlement found — record what was expected
        entries.append(
            DiffEntry(
                field_name="amount (order vs settled+fee+tax)",
                expected=result.order_amount,
                actual=None,
                delta=None,
                signal="amount",
                weight=matching_cfg.amount_weight,
                score=0.0,
                is_shortfall=True,
            )
        )

    # ── Date entry ────────────────────────────────────────────────────────────
    if has_candidate and result.order_date and result.settled_date:
        entries.append(
            DiffEntry(
                field_name="settlement_lag_days (settled_date − order_date)",
                expected=f"≤{matching_cfg.date_full_score_days}d",
                actual=result.settled_date,
                delta=bd.date_diff_days,
                signal="date",
                weight=matching_cfg.date_weight,
                score=bd.date_score,
                is_shortfall=bd.date_diff_days > matching_cfg.date_full_score_days,
            )
        )
        entries.append(
            DiffEntry(
                field_name="order_date",
                expected=result.order_date,
                actual=None,
                delta=None,
                signal="date",
                weight=0.0,
                score=bd.date_score,
                is_shortfall=False,
            )
        )
    else:
        entries.append(
            DiffEntry(
                field_name="settlement_lag_days",
                expected=f"≤{matching_cfg.date_full_score_days}d",
                actual=None,
                delta=None,
                signal="date",
                weight=matching_cfg.date_weight,
                score=0.0,
                is_shortfall=True,
            )
        )

    # ── Reference (UTR) entry ─────────────────────────────────────────────────
    if has_candidate:
        entries.append(
            DiffEntry(
                field_name="utr_match (settlement ↔ bank)",
                expected="bank UTR matches settlement UTR",
                actual=result.matched_bank_utr,
                delta=bd.best_utr_ratio,
                signal="reference",
                weight=matching_cfg.reference_weight,
                score=bd.reference_score,
                is_shortfall=bd.reference_score < 1.0,
            )
        )
    else:
        entries.append(
            DiffEntry(
                field_name="utr_match",
                expected="settlement UTR present",
                actual=None,
                delta=None,
                signal="reference",
                weight=matching_cfg.reference_weight,
                score=0.0,
                is_shortfall=True,
            )
        )

    # ── Sort: shortfall entries first, then by descending signal weight ───────
    entries.sort(key=lambda e: (not e.is_shortfall, -e.weight, e.field_name))

    # ── Resolution hint ───────────────────────────────────────────────────────
    resolution_hint = _HINT_MAP.get(classified.subtype, "review_manually")

    return ExceptionDiff(
        order_id=result.order_id,
        status=classified.status,
        subtype=classified.subtype,
        composite_score=result.composite_score,
        shortfall=shortfall,
        anomaly_flags=classified.anomaly_flags,
        entries=entries,
        resolution_hint=resolution_hint,
        has_candidate=has_candidate,
    )


# ── Exception list builder ────────────────────────────────────────────────────


def build_exception_list(
    classified_results: list[ClassifiedResult],
    matching_cfg: MatchingConfig | None = None,
    classification_cfg: ClassificationConfig | None = None,
    include_auto_matched: bool = False,
) -> list[ExceptionDiff]:
    """Build a sorted exception list from all classified results.

    Returns only NEEDS_REVIEW and UNRESOLVED results by default, sorted so
    that near-misses (high composite score, small shortfall) appear first.
    This surfaces recoverable value at the top of the review queue.

    Sorting rationale: near-misses first because:
    1. They are most likely to be resolved with a single piece of information
       (e.g., a confirmed rounding rule or a delayed-settlement acknowledgement).
    2. They represent the highest monetary value at risk of being mis-classified.
    3. UNRESOLVED cases (score=0) have no candidate; they require a different
       resolution path (gateway query) and should not compete with near-misses
       for reviewer attention.

    Args:
        classified_results: All classified results from ``classify_all``.
        matching_cfg: Matching config for diff construction. Uses defaults if None.
        classification_cfg: Classification config. Uses defaults if None.
        include_auto_matched: If True, also includes AUTO_MATCHED results (with
            empty diffs) in the output. Useful for full audit exports.

    Returns:
        A list of ``ExceptionDiff`` objects, sorted near-miss-first.
    """
    if classification_cfg is None:
        classification_cfg = ClassificationConfig()

    # Filter to exceptions only (unless full export requested)
    to_diff = (
        classified_results
        if include_auto_matched
        else [cr for cr in classified_results if cr.is_exception()]
    )

    # Build diffs
    diffs = [
        build_diff(cr, matching_cfg, classification_cfg)
        for cr in to_diff
    ]

    # Sort: NEEDS_REVIEW before UNRESOLVED, then by descending composite_score
    # (higher score = closer to resolution = near-miss first).
    def sort_key(d: ExceptionDiff) -> tuple:
        status_order = {
            ReconStatus.NEEDS_REVIEW: 0,
            ReconStatus.UNRESOLVED: 1,
            ReconStatus.AUTO_MATCHED: 2,
        }
        return (status_order.get(d.status, 99), -d.composite_score)

    diffs.sort(key=sort_key)
    return diffs
