"""
test_edge_cases.py
──────────────────
Tests for edge cases added in Chunk 9: empty files, duplicate orders, refunds, ambiguous matches.
"""

import datetime
from decimal import Decimal

from app.services.classification import ExceptionSubtype, ReconStatus, classify_all
from app.services.matching import (
    MatchResult,
    ScoreBreakdown,
    detect_ambiguous_bank_matches,
    detect_duplicate_orders,
)
from app.services.normalisation import NormalisedOrder


def test_detect_duplicate_orders() -> None:
    orders = [
        NormalisedOrder("ORD1", datetime.date(2024, 1, 1), "A", Decimal(100), "INR"),
        NormalisedOrder("ORD2", datetime.date(2024, 1, 1), "B", Decimal(200), "INR"),
        NormalisedOrder(
            "ORD1", datetime.date(2024, 1, 2), "A", Decimal(100), "INR"
        ),  # Duplicate
    ]
    dupes = detect_duplicate_orders(orders)
    assert dupes == {"ORD1"}


def test_detect_ambiguous_bank_matches() -> None:
    # Two results matching the same bank UTR with the same reference score
    r1 = MatchResult(
        order_id="ORD1",
        matched_settlement_id="SET1",
        matched_bank_utr="UTR123",
        composite_score=0.9,
        score_breakdown=ScoreBreakdown(
            reference_score=1.0,
            amount_score=0.9,
            date_score=0.9,
            bank_amount_score=1.0,
            bank_utr_score=1.0,
            amount_diff_inr=Decimal("0.0"),
            date_diff_days=0,
            best_utr_ratio=100.0,
        ),
    )
    r2 = MatchResult(
        order_id="ORD2",
        matched_settlement_id="SET2",
        matched_bank_utr="UTR123",
        composite_score=0.9,
        score_breakdown=ScoreBreakdown(
            reference_score=1.0,
            amount_score=0.9,
            date_score=0.9,
            bank_amount_score=1.0,
            bank_utr_score=1.0,
            amount_diff_inr=Decimal("0.0"),
            date_diff_days=0,
            best_utr_ratio=100.0,
        ),
    )
    r3 = MatchResult(
        order_id="ORD3",
        matched_settlement_id="SET3",
        matched_bank_utr="UTR999",
        composite_score=0.9,
        score_breakdown=ScoreBreakdown(
            reference_score=1.0,
            amount_score=0.9,
            date_score=0.9,
            bank_amount_score=1.0,
            bank_utr_score=1.0,
            amount_diff_inr=Decimal("0.0"),
            date_diff_days=0,
            best_utr_ratio=100.0,
        ),
    )
    ambiguous = detect_ambiguous_bank_matches([r1, r2, r3])
    assert "ORD1" in ambiguous
    assert "ORD2" in ambiguous
    assert "ORD3" not in ambiguous


def test_classification_overrides() -> None:
    # Test that batch overrides work correctly
    r1 = MatchResult(
        order_id="ORD1",
        matched_settlement_id="SET1",
        matched_bank_utr="UTR123",
        composite_score=0.99,  # normally AUTO_MATCHED
        score_breakdown=ScoreBreakdown(
            reference_score=1.0,
            amount_score=1.0,
            date_score=1.0,
            bank_amount_score=1.0,
            bank_utr_score=1.0,
            amount_diff_inr=Decimal("0.0"),
            date_diff_days=0,
            best_utr_ratio=100.0,
        ),
    )
    classified = classify_all([r1], duplicate_ledger_order_ids={"ORD1"})
    assert classified[0].status == ReconStatus.NEEDS_REVIEW
    assert classified[0].subtype == ExceptionSubtype.DUPLICATE_ORDER

    classified2 = classify_all([r1], ambiguous_order_ids={"ORD1"})
    assert classified2[0].status == ReconStatus.NEEDS_REVIEW
    assert classified2[0].subtype == ExceptionSubtype.AMBIGUOUS_MATCH


def test_refund_mismatch_classification() -> None:
    # Simulate a result with a negative settlement amount
    r1 = MatchResult(
        order_id="ORD1",
        matched_settlement_id="SET1",
        matched_bank_utr="UTR123",
        composite_score=0.8,
        score_breakdown=ScoreBreakdown(
            reference_score=1.0,
            amount_score=0.5,
            date_score=1.0,
            bank_amount_score=1.0,
            bank_utr_score=1.0,
            amount_diff_inr=Decimal("200.0"),
            date_diff_days=0,
            best_utr_ratio=100.0,
        ),
    )
    r1.settled_amount = Decimal("-100.0")  # Refund!

    classified = classify_all([r1])
    assert classified[0].status == ReconStatus.NEEDS_REVIEW
    assert classified[0].subtype == ExceptionSubtype.REFUND_MISMATCH
