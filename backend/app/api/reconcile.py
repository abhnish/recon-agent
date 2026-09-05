"""
reconcile.py
────────────
POST /api/reconcile/run — full reconciliation pipeline trigger.

Ingests the three CSV files, runs normalisation + matching + classification,
builds exception diffs, updates app state, and returns a run summary.

⚠️  LLM MATCHING PROHIBITION: This router runs the deterministic pipeline
    only.  No LLM calls are made here.  The LLM layer is invoked only from
    the explain and chat endpoints.
"""

from __future__ import annotations

import csv
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.api.schemas import ReconcileRunResponse
from app.api.state import ReconcileRunMeta, app_state
from app.services.classification import classify_all, summarise
from app.services.exception_diff import build_exception_list
from app.services.llm_layer import AuditEventType, AuditLogEntry
from app.services.matching import (
    detect_ambiguous_bank_matches,
    detect_duplicate_orders,
    detect_duplicate_settlements,
    detect_unmatched_bank_credits,
    run_matching,
)
from app.services.normalisation import (
    normalise_bank_txn,
    normalise_order,
    normalise_settlement,
)

router = APIRouter(prefix="/api", tags=["reconcile"])

# Default data directory — relative to the backend/ root so it works from
# any working directory as long as the conftest sys.path is active.
_DATA_DIR = Path(__file__).parent.parent.parent / "data"

# ── Run counter ───────────────────────────────────────────────────────────────

_run_counter: int = 0


def _next_run_id() -> int:
    """Increment and return the next run ID (module-level counter)."""
    global _run_counter
    _run_counter += 1
    return _run_counter


# ── Helpers ───────────────────────────────────────────────────────────────────


def _load_csv(path: Path) -> list[dict]:
    """Load a CSV file and return rows as plain dicts.

    Args:
        path: Absolute path to the CSV file.

    Returns:
        List of row dicts.

    Raises:
        HTTPException 500: If the file cannot be read.
    """
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Cannot read data file {path.name}: {exc}",
        ) from exc


# ── Route ─────────────────────────────────────────────────────────────────────


@router.post(
    "/reconcile/run",
    response_model=ReconcileRunResponse,
    summary="Run full reconciliation pipeline",
    description=(
        "Ingests order ledger, settlement report, and bank statement CSVs. "
        "Runs normalisation → matching → classification → exception diff build. "
        "Results are held in memory and queryable via /api/transactions, "
        "/api/exceptions, and /api/metrics.  "
        "Calling this endpoint again overwrites the previous run's results."
    ),
)
def run_reconciliation() -> ReconcileRunResponse:
    """Trigger the full deterministic reconciliation pipeline.

    ⚠️  No LLM calls are made here.  Matching and classification are 100%
    deterministic.

    Returns:
        A run summary with counts per status and pipeline runtime.

    Raises:
        HTTPException 500: If a data file cannot be read.
    """
    t_start = time.perf_counter()
    run_id = _next_run_id()

    # ── Ingestion audit: record attempt BEFORE we know if it will succeed ──────
    ingestion_start_entry = AuditLogEntry(
        event_type=AuditEventType.INGESTION,
        order_id=None,
        model_name="pipeline",
        prompt_summary=(
            f"run_id={run_id}: ingestion started — "
            f"orders={_DATA_DIR / 'order_ledger.csv'}, "
            f"settlements={_DATA_DIR / 'settlement_report.csv'}, "
            f"bank={_DATA_DIR / 'bank_statement.csv'}"
        ),
        response_text="pending",
        llm_status="n/a",
        latency_ms=0,
        potential_hallucination=False,
    )
    app_state.add_audit_entry(ingestion_start_entry)

    # ── Ingest ────────────────────────────────────────────────────────────────
    try:
        raw_orders = _load_csv(_DATA_DIR / "order_ledger.csv")
        raw_settlements = _load_csv(_DATA_DIR / "settlement_report.csv")
        raw_bank = _load_csv(_DATA_DIR / "bank_statement.csv")
    except Exception as exc:
        failure_entry = AuditLogEntry(
            event_type=AuditEventType.INGESTION,
            order_id=None,
            model_name="pipeline",
            prompt_summary=f"run_id={run_id}: ingestion FAILED",
            response_text=str(exc),
            llm_status="n/a",
            latency_ms=int((time.perf_counter() - t_start) * 1000),
            potential_hallucination=False,
        )
        app_state.add_audit_entry(failure_entry)
        raise

    if not raw_orders and not raw_settlements and not raw_bank:
        pass  # Gracefully handle empty datasets

    orders = [normalise_order(r) for r in raw_orders]
    settlements = [normalise_settlement(r) for r in raw_settlements]
    bank_txns = [normalise_bank_txn(r) for r in raw_bank]

    # ── Match ─────────────────────────────────────────────────────────────────
    match_results, _match_elapsed = run_matching(orders, settlements, bank_txns)

    duplicate_settlement_ids = detect_duplicate_settlements(settlements)
    duplicate_ledger_ids = detect_duplicate_orders(orders)
    phantom_bank_utrs = detect_unmatched_bank_credits(bank_txns, settlements)
    ambiguous_order_ids = detect_ambiguous_bank_matches(match_results)

    # ── Classify ──────────────────────────────────────────────────────────────
    classified = classify_all(
        match_results,
        duplicate_settlement_order_ids=duplicate_settlement_ids,
        duplicate_ledger_order_ids=duplicate_ledger_ids,
        ambiguous_order_ids=ambiguous_order_ids,
    )

    # ── Build exception diffs ─────────────────────────────────────────────────
    exception_list = build_exception_list(classified)
    exception_index = {d.order_id: d for d in exception_list}

    # ── Update state ──────────────────────────────────────────────────────────
    counts = summarise(classified)
    runtime_ms = int((time.perf_counter() - t_start) * 1000)

    meta = ReconcileRunMeta(
        run_id=run_id,
        orders_loaded=len(orders),
        settlements_loaded=len(settlements),
        bank_txns_loaded=len(bank_txns),
        auto_matched=counts.get("AUTO_MATCHED", 0),
        needs_review=counts.get("NEEDS_REVIEW", 0),
        unresolved=counts.get("UNRESOLVED", 0),
        duplicate_settlements=len(duplicate_settlement_ids),
        phantom_credits=len(phantom_bank_utrs),
        runtime_ms=runtime_ms,
    )

    with app_state._lock:
        app_state.classified_results = classified
        app_state.exception_diffs = exception_index
        app_state.last_run = meta
        app_state.all_runs.append(meta)

    # Emit Audit Log Entry
    summary_text = (
        f"Orders: {len(orders)}, Settlements: {len(settlements)}, "
        f"Bank: {len(bank_txns)}. Auto-matched: {counts.get('AUTO_MATCHED', 0)}, "
        f"Needs Review: {counts.get('NEEDS_REVIEW', 0)}, Unresolved: {counts.get('UNRESOLVED', 0)}"
    )
    audit = AuditLogEntry(
        event_type=AuditEventType.RECONCILE_RUN,
        order_id=None,
        model_name="pipeline",
        prompt_summary="Triggered deterministic pipeline",
        response_text=summary_text,
        llm_status="n/a",
        latency_ms=runtime_ms,
        potential_hallucination=False,
    )
    app_state.add_audit_entry(audit)

    import json

    # Emit match decision audit logs
    for cr in classified:
        decision_summary = json.dumps(
            {
                "status": cr.status.value,
                "subtype": cr.subtype.value,
                "score": round(cr.composite_score, 3),
                "flags": cr.anomaly_flags,
            }
        )

        decision_audit = AuditLogEntry(
            event_type=AuditEventType.MATCH_DECISION,
            order_id=cr.order_id,
            model_name="deterministic_matcher",
            prompt_summary=f"Classify order {cr.order_id}",
            response_text=decision_summary,
            llm_status="n/a",
            latency_ms=0,
            potential_hallucination=False,
        )
        app_state.add_audit_entry(decision_audit)

    return ReconcileRunResponse(
        run_id=run_id,
        orders_loaded=len(orders),
        settlements_loaded=len(settlements),
        bank_txns_loaded=len(bank_txns),
        auto_matched=counts.get("AUTO_MATCHED", 0),
        needs_review=counts.get("NEEDS_REVIEW", 0),
        unresolved=counts.get("UNRESOLVED", 0),
        duplicate_settlements=len(duplicate_settlement_ids),
        phantom_credits=len(phantom_bank_utrs),
        runtime_ms=runtime_ms,
    )
