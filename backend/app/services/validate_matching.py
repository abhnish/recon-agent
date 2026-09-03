"""
validate_matching.py
────────────────────
Validation script: runs the matching engine against the synthetic dataset and
measures precision/recall against the known ground truth (seed=42).

Usage (from repo root):
    python backend/app/services/validate_matching.py

Outputs two sections:
  SECTION A — Intermediate score-band metrics (composite score only, no classify()).
              Use to debug the raw scoring signal. These are NOT the final pipeline numbers.
  SECTION B — End-to-end pipeline metrics (calls classify() on every result).
              These are the numbers that reflect actual system behaviour.
  Plus: runtime / throughput, exception detection, per-order mismatch detail.

Ground truth is derived from the order_id naming convention established by
generate_synthetic_data.py:
  ORD2024001–ORD2024042  → CLEAN_MATCH
  ORD2024043–ORD2024051  → HARD_MISMATCH
  ORD2024052–ORD2024054  → EXCEPTION (failed_payment)
  Phantom credits         → detected via detect_unmatched_bank_credits()
  Duplicate settlements   → detected via detect_duplicate_settlements()

⚠️  LLM MATCHING PROHIBITION: This script exercises the deterministic
    matching engine only. No LLM calls are made here.
"""

from __future__ import annotations

import csv
import statistics
import time
from pathlib import Path

from app.services.matching import (
    MatchingConfig,
    MatchResult,
    _build_bank_utr_index,
    _build_order_id_index,
    _build_settlement_index,
    detect_duplicate_settlements,
    detect_unmatched_bank_credits,
    match_order,
)
from app.services.normalisation import (
    normalise_bank_txn,
    normalise_order,
    normalise_settlement,
)
from app.services.classification import classify, ReconStatus

# ── Paths ──────────────────────────────────────────────────────────────────────

DATA_DIR = Path(__file__).parent.parent.parent / "data"

# ── Ground truth labels (derived from generate_synthetic_data.py, seed=42) ────

# Orders 1–42 are clean matches (direct UTR-traceable, amount exact)
CLEAN_ORDER_IDS = {f"ORD{2024_000 + i:06d}" for i in range(1, 43)}
# Orders 43–51 have a mismatch (rounding diff / partial refund / delayed)
MISMATCH_ORDER_IDS = {f"ORD{2024_000 + i:06d}" for i in range(43, 52)}
# Orders 52–54 have no settlement (failed payment)
FAILED_PAYMENT_IDS = {f"ORD{2024_000 + i:06d}" for i in range(52, 55)}


# ── Score bands (Chunk 4 will formalise these as the classification layer) ────
# Using these provisional thresholds only to produce a confusion matrix here;
# they are not baked into the matching engine itself.

HIGH_SCORE_THRESHOLD = 0.70    # likely CLEAN_MATCH
MID_SCORE_THRESHOLD = 0.35     # likely HARD_MISMATCH; below → likely EXCEPTION


def _load_csv(path: Path) -> list[dict]:
    """Load a CSV file and return rows as dicts."""
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _band(score: float) -> str:
    """Map a composite score to a human-readable score band."""
    if score >= HIGH_SCORE_THRESHOLD:
        return "HIGH (≥0.70)"
    if score >= MID_SCORE_THRESHOLD:
        return "MID (0.35–0.69)"
    return "LOW (<0.35)"


def _print_separator(char: str = "─", width: int = 72) -> None:
    print(char * width)


def run_validation() -> None:
    """Run the full validation pipeline and print a summary report."""

    # ── Load CSVs ────────────────────────────────────────────────────────────
    raw_orders = _load_csv(DATA_DIR / "order_ledger.csv")
    raw_settlements = _load_csv(DATA_DIR / "settlement_report.csv")
    raw_bank = _load_csv(DATA_DIR / "bank_statement.csv")

    orders = [normalise_order(r) for r in raw_orders]
    settlements = [normalise_settlement(r) for r in raw_settlements]
    bank_txns = [normalise_bank_txn(r) for r in raw_bank]

    cfg = MatchingConfig()

    # ── Run per-order matching (time each individually for p50/p99) ──────────
    utr_index = _build_settlement_index(settlements)
    order_id_index = _build_order_id_index(settlements)
    bank_utr_index = _build_bank_utr_index(bank_txns)

    per_order_times_ms: list[float] = []
    results: list[MatchResult] = []

    for order in orders:
        t0 = time.perf_counter()
        result = match_order(
            order, settlements, bank_txns, utr_index, order_id_index, bank_utr_index, cfg
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        per_order_times_ms.append(elapsed_ms)
        results.append(result)

    total_ms = sum(per_order_times_ms)

    # ── Detect phantom credits and duplicate settlements ─────────────────────
    phantom_credits = detect_unmatched_bank_credits(bank_txns, settlements, cfg)
    duplicate_settlements = detect_duplicate_settlements(settlements)

    # ── Build result lookup ──────────────────────────────────────────────────
    result_by_order: dict[str, MatchResult] = {r.order_id: r for r in results}

    # ── Precision / Recall calculation ────────────────────────────────────────
    # We define "detected" conservatively:
    #   CLEAN: score ≥ HIGH_SCORE_THRESHOLD
    #   MISMATCH: MID_SCORE_THRESHOLD ≤ score < HIGH_SCORE_THRESHOLD (matched but imperfect)
    #   FAILED_PAYMENT: score < MID_SCORE_THRESHOLD (no viable match)

    clean_results = [result_by_order[oid] for oid in CLEAN_ORDER_IDS if oid in result_by_order]
    mismatch_results = [result_by_order[oid] for oid in MISMATCH_ORDER_IDS if oid in result_by_order]
    failed_results = [result_by_order[oid] for oid in FAILED_PAYMENT_IDS if oid in result_by_order]

    # True positives for each category (how many were correctly banded)
    clean_tp = sum(1 for r in clean_results if r.composite_score >= HIGH_SCORE_THRESHOLD)
    mismatch_tp = sum(1 for r in mismatch_results if MID_SCORE_THRESHOLD <= r.composite_score < HIGH_SCORE_THRESHOLD)
    failed_tp = sum(1 for r in failed_results if r.composite_score < MID_SCORE_THRESHOLD)
    len(phantom_credits)  # all 3 expected; validate below
    len(duplicate_settlements)  # all 3 expected

    # False positives: items in the wrong band
    clean_fp = sum(
        1 for r in mismatch_results + failed_results
        if r.composite_score >= HIGH_SCORE_THRESHOLD
    )
    mismatch_fp = sum(
        1 for r in clean_results + failed_results
        if MID_SCORE_THRESHOLD <= r.composite_score < HIGH_SCORE_THRESHOLD
    )
    failed_fp = sum(
        1 for r in clean_results + mismatch_results
        if r.composite_score < MID_SCORE_THRESHOLD
    )

    def safe_div(n: int, d: int) -> float:
        return n / d if d > 0 else 0.0

    clean_precision = safe_div(clean_tp, clean_tp + clean_fp)
    clean_recall = safe_div(clean_tp, len(CLEAN_ORDER_IDS))

    mismatch_precision = safe_div(mismatch_tp, mismatch_tp + mismatch_fp)
    mismatch_recall = safe_div(mismatch_tp, len(MISMATCH_ORDER_IDS))

    failed_precision = safe_div(failed_tp, failed_tp + failed_fp)
    failed_recall = safe_div(failed_tp, len(FAILED_PAYMENT_IDS))

    # ── Print report ─────────────────────────────────────────────────────────
    print()
    _print_separator("═")
    print("ReconAgent — Matching Engine Validation Report")
    _print_separator("═")
    print("Dataset:  backend/data/  (seed=42, reproducible)")
    print(f"Config:   MatchingConfig defaults — weights {cfg.amount_weight}/{cfg.reference_weight}/{cfg.date_weight}")
    print(f"          amount tolerance ₹{cfg.amount_full_score_tolerance}–₹{cfg.amount_zero_score_tolerance}")
    print(f"          date window {cfg.date_full_score_days}–{cfg.date_zero_score_days} days")
    print(f"          UTR fuzzy threshold {cfg.fuzzy_utr_threshold}")
    print()

    # ── Score summary ─────────────────────────────────────────────────────────
    all_scores = [r.composite_score for r in results]
    _print_separator()
    print(f"{'SCORE DISTRIBUTION (all orders)':}")
    _print_separator()
    print(f"  min    : {min(all_scores):.4f}")
    print(f"  max    : {max(all_scores):.4f}")
    print(f"  mean   : {statistics.mean(all_scores):.4f}")
    print(f"  median : {statistics.median(all_scores):.4f}")
    print(f"  stdev  : {statistics.stdev(all_scores):.4f}")
    print()

    # ── Per-category score breakdown ──────────────────────────────────────────
    _print_separator()
    print("SCORE BREAKDOWN BY GROUND TRUTH CATEGORY")
    _print_separator()
    print(f"{'Category':<22} {'N':>4}  {'Min':>6}  {'Mean':>6}  {'Max':>6}  {'Stdev':>6}")
    _print_separator()

    def stats_row(label: str, result_list: list[MatchResult]) -> None:
        scores = [r.composite_score for r in result_list]
        if not scores:
            print(f"  {label:<20} {'0':>4}  {'—':>6}  {'—':>6}  {'—':>6}  {'—':>6}")
            return
        print(
            f"  {label:<20} {len(scores):>4}  "
            f"{min(scores):>6.4f}  {statistics.mean(scores):>6.4f}  "
            f"{max(scores):>6.4f}  {statistics.stdev(scores) if len(scores) > 1 else 0:>6.4f}"
        )

    stats_row("CLEAN_MATCH (42)", clean_results)
    stats_row("HARD_MISMATCH (9)", mismatch_results)
    stats_row("FAILED_PAYMENT (3)", failed_results)
    print()

    # ── Confusion matrix (score bands vs ground truth) ────────────────────────
    _print_separator()
    print("CONFUSION MATRIX  (rows=ground truth, cols=score band assigned)")
    _print_separator()
    header = f"{'Ground Truth':<22}  {'HIGH≥0.70':>12}  {'MID 0.35–0.69':>14}  {'LOW<0.35':>10}"
    print(header)
    _print_separator()

    def cm_row(label: str, result_list: list[MatchResult]) -> None:
        hi = sum(1 for r in result_list if r.composite_score >= HIGH_SCORE_THRESHOLD)
        mid = sum(1 for r in result_list if MID_SCORE_THRESHOLD <= r.composite_score < HIGH_SCORE_THRESHOLD)
        lo = sum(1 for r in result_list if r.composite_score < MID_SCORE_THRESHOLD)
        print(f"  {label:<20}  {hi:>12}  {mid:>14}  {lo:>10}")

    cm_row("CLEAN_MATCH (42)", clean_results)
    cm_row("HARD_MISMATCH (9)", mismatch_results)
    cm_row("FAILED_PAYMENT (3)", failed_results)
    print()

    # ── Section A: Score-band precision / recall ──────────────────────────────
    _print_separator()
    print("SECTION A — SCORE-BAND PRECISION / RECALL  (intermediate layer only)")
    print("  ⚠  These use raw composite score bands (HIGH/MID/LOW), NOT classify().")
    print("  ⚠  They do NOT reflect anomaly-flag overrides. See Section B for final numbers.")
    _print_separator()
    print(f"{'Category':<22}  {'TP':>4}  {'FP':>4}  {'Precision':>10}  {'Recall':>8}  {'F1':>8}")
    _print_separator()

    def f1(p: float, r: float) -> float:
        return 2 * p * r / (p + r) if (p + r) > 0 else 0.0

    print(
        f"  {'CLEAN_MATCH':<20}  {clean_tp:>4}  {clean_fp:>4}  "
        f"{clean_precision:>10.4f}  {clean_recall:>8.4f}  {f1(clean_precision, clean_recall):>8.4f}"
    )
    print(
        f"  {'HARD_MISMATCH':<20}  {mismatch_tp:>4}  {mismatch_fp:>4}  "
        f"{mismatch_precision:>10.4f}  {mismatch_recall:>8.4f}  {f1(mismatch_precision, mismatch_recall):>8.4f}"
    )
    print(
        f"  {'FAILED_PAYMENT':<20}  {failed_tp:>4}  {failed_fp:>4}  "
        f"{failed_precision:>10.4f}  {failed_recall:>8.4f}  {f1(failed_precision, failed_recall):>8.4f}"
    )
    print()

    # ── Exception detection ────────────────────────────────────────────────────
    _print_separator()
    print("EXCEPTION DETECTION")
    _print_separator()
    print(f"  Phantom credits detected   : {len(phantom_credits)} (expected 3)")
    for b in phantom_credits:
        print(f"    UTR={b.utr_reference[:30]}  amount=₹{b.credit_amount}")
    print()
    print(f"  Duplicate settlements detected : {len(duplicate_settlements)} order(s) (expected 3)")
    for oid in duplicate_settlements:
        print(f"    ORD={oid}")
    print()

    # ── Runtime ───────────────────────────────────────────────────────────────
    _print_separator()
    print("THROUGHPUT")
    _print_separator()
    sorted_times = sorted(per_order_times_ms)
    n = len(sorted_times)
    p50 = sorted_times[int(n * 0.50)]
    p99 = sorted_times[min(int(n * 0.99), n - 1)]
    print(f"  Orders processed   : {n}")
    print(f"  Total runtime      : {total_ms:.2f} ms")
    print(f"  Avg per order      : {total_ms / n:.3f} ms")
    print(f"  p50 latency        : {p50:.3f} ms")
    print(f"  p99 latency        : {p99:.3f} ms")
    print(f"  Throughput         : {n / (total_ms / 1000):.0f} orders/sec  "
          f"(extrapolated; index build not included)")
    print()

    # ── Per-order detail (mismatch and exception rows) ─────────────────────────
    _print_separator()
    print("PER-ORDER DETAIL — MISMATCHES AND EXCEPTIONS")
    _print_separator()
    print(
        f"  {'order_id':<14}  {'gt_category':<20}  {'score':>7}  {'band':<16}  "
        f"{'amt_diff':>10}  {'days':>5}  {'utr_ratio':>10}"
    )
    _print_separator()

    focus_ids = MISMATCH_ORDER_IDS | FAILED_PAYMENT_IDS
    for r in results:
        if r.order_id not in focus_ids:
            continue
        if r.order_id in MISMATCH_ORDER_IDS:
            gt = "HARD_MISMATCH"
        else:
            gt = "FAILED_PAYMENT"
        bd = r.score_breakdown
        print(
            f"  {r.order_id:<14}  {gt:<20}  {r.composite_score:>7.4f}  "
            f"{_band(r.composite_score):<16}  "
            f"₹{bd.amount_diff_inr!s:>9}  {bd.date_diff_days:>5}  "
            f"{bd.best_utr_ratio:>10.1f}"
        )
    print()
    _print_separator("=")
    print("Validation complete.")
    _print_separator("=")
    print()

    # ── SECTION B: End-to-end classify() metrics ───────────────────────────
    _print_separator("=")
    print("SECTION B — END-TO-END PIPELINE PRECISION / RECALL  (this is what matters)")
    print("  Calls classify() on every MatchResult, including anomaly-flag overrides.")
    print("  AUTO_MATCHED ≃ CLEAN_MATCH, NEEDS_REVIEW ≃ HARD_MISMATCH, UNRESOLVED ≃ FAILED")
    _print_separator("=")
    print()

    auto_set: set[str] = set()
    review_set: set[str] = set()
    unresolved_set: set[str] = set()

    for r in results:
        cr = classify(r)
        if cr.status == ReconStatus.AUTO_MATCHED:
            auto_set.add(r.order_id)
        elif cr.status == ReconStatus.NEEDS_REVIEW:
            review_set.add(r.order_id)
        else:
            unresolved_set.add(r.order_id)

    # Confusion matrix against ground truth
    _print_separator()
    print("CONFUSION MATRIX  (rows=ground truth, cols=classify() status)")
    _print_separator()
    print(f"{'Ground Truth':<22}  {'AUTO_MATCHED':>12}  {'NEEDS_REVIEW':>12}  {'UNRESOLVED':>10}")
    _print_separator()

    def b2_cm_row(label: str, gt_ids: set[str]) -> None:
        am  = sum(1 for oid in gt_ids if oid in auto_set)
        nr  = sum(1 for oid in gt_ids if oid in review_set)
        un  = sum(1 for oid in gt_ids if oid in unresolved_set)
        print(f"  {label:<20}  {am:>12}  {nr:>12}  {un:>10}")

    b2_cm_row("CLEAN_MATCH (42)",    CLEAN_ORDER_IDS)
    b2_cm_row("HARD_MISMATCH (9)",   MISMATCH_ORDER_IDS)
    b2_cm_row("FAILED_PAYMENT (3)",  FAILED_PAYMENT_IDS)
    print()

    # Precision / recall
    _print_separator()
    print("PRECISION / RECALL  (end-to-end classify() output)")
    _print_separator()
    print(f"{'Category':<22}  {'TP':>4}  {'FP':>4}  {'Precision':>10}  {'Recall':>8}  {'F1':>8}")
    _print_separator()

    # AUTO_MATCHED: TP = clean orders that are AUTO_MATCHED
    b2_clean_tp = len(CLEAN_ORDER_IDS & auto_set)
    b2_clean_fp = len(auto_set - CLEAN_ORDER_IDS)   # non-clean orders auto-matched
    b2_clean_pr = safe_div(b2_clean_tp, b2_clean_tp + b2_clean_fp)
    b2_clean_rc = safe_div(b2_clean_tp, len(CLEAN_ORDER_IDS))

    # NEEDS_REVIEW: TP = hard-mismatch orders in NEEDS_REVIEW
    b2_mm_tp = len(MISMATCH_ORDER_IDS & review_set)
    b2_mm_fp = len(review_set - MISMATCH_ORDER_IDS - FAILED_PAYMENT_IDS)  # clean orders flagged
    b2_mm_pr = safe_div(b2_mm_tp, b2_mm_tp + b2_mm_fp)
    b2_mm_rc = safe_div(b2_mm_tp, len(MISMATCH_ORDER_IDS))

    # UNRESOLVED: TP = failed-payment orders that are UNRESOLVED
    b2_fp_tp = len(FAILED_PAYMENT_IDS & unresolved_set)
    b2_fp_fp = len(unresolved_set - FAILED_PAYMENT_IDS)  # non-failed orders unresolved
    b2_fp_pr = safe_div(b2_fp_tp, b2_fp_tp + b2_fp_fp)
    b2_fp_rc = safe_div(b2_fp_tp, len(FAILED_PAYMENT_IDS))

    print(
        f"  {'AUTO_MATCHED (clean)':<20}  {b2_clean_tp:>4}  {b2_clean_fp:>4}  "
        f"{b2_clean_pr:>10.4f}  {b2_clean_rc:>8.4f}  {f1(b2_clean_pr, b2_clean_rc):>8.4f}"
    )
    print(
        f"  {'NEEDS_REVIEW (mismatch)':<20}  {b2_mm_tp:>4}  {b2_mm_fp:>4}  "
        f"{b2_mm_pr:>10.4f}  {b2_mm_rc:>8.4f}  {f1(b2_mm_pr, b2_mm_rc):>8.4f}"
    )
    print(
        f"  {'UNRESOLVED (failed)':<20}  {b2_fp_tp:>4}  {b2_fp_fp:>4}  "
        f"{b2_fp_pr:>10.4f}  {b2_fp_rc:>8.4f}  {f1(b2_fp_pr, b2_fp_rc):>8.4f}"
    )
    print()

    # Per-order anomaly detail for hard-mismatch cases
    _print_separator()
    print("PER-CASE ANOMALY FLAGS  (hard-mismatch cases only)")
    print("  'composite alone' = score < 0.97, routes to NEEDS_REVIEW without anomaly override")
    print("  'anomaly override' = score ≥ 0.97, anomaly flag triggers downgrade")
    _print_separator()
    print(f"  {'order_id':<14}  {'score':>7}  {'status':<14}  {'caught_by':<18}  anomaly_flags")
    _print_separator()

    from app.services.classification import ClassificationConfig
    cls_cfg = ClassificationConfig()

    for r in results:
        if r.order_id not in MISMATCH_ORDER_IDS:
            continue
        cr = classify(r)
        caught = "anomaly override" if r.composite_score >= cls_cfg.auto_match_threshold else "composite alone"
        flags = ", ".join(cr.anomaly_flags) if cr.anomaly_flags else "(none)"
        print(f"  {r.order_id:<14}  {r.composite_score:>7.4f}  {cr.status.value:<14}  {caught:<18}  {flags}")
    print()

    _print_separator("=")
    print("Validation complete.")
    _print_separator("=")
    print()


if __name__ == "__main__":
    run_validation()
