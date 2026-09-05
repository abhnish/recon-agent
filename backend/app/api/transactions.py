"""
transactions.py
───────────────
GET /api/transactions — paginated list of all MatchResults with status filter.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.api.schemas import (
    ScoreBreakdownSchema,
    TransactionListResponse,
    TransactionSchema,
)
from app.api.state import app_state
from app.services.classification import ReconStatus

router = APIRouter(prefix="/api", tags=["transactions"])


@router.get(
    "/transactions",
    response_model=TransactionListResponse,
    summary="Paginated list of reconciliation results",
    description=(
        "Returns all MatchResults from the last reconciliation run with optional "
        "filtering by status (AUTO_MATCHED | NEEDS_REVIEW | UNRESOLVED). "
        "Results are sorted by order_id. "
        "Returns 409 if no run has been executed."
    ),
)
def list_transactions(
    status: str | None = Query(
        default=None,
        description="Filter by status: AUTO_MATCHED | NEEDS_REVIEW | UNRESOLVED",
    ),
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(default=20, ge=1, le=200, description="Items per page"),
) -> TransactionListResponse:
    """Return a paginated, optionally-filtered list of reconciliation results.

    Args:
        status:    Optional status filter string.
        page:      Page number (1-indexed).
        page_size: Items per page (max 200).

    Returns:
        A TransactionListResponse with pagination metadata and items.

    Raises:
        HTTPException 409: If no reconcile run has been executed.
        HTTPException 422: If the status filter value is invalid.
    """
    if not app_state.is_ready():
        raise HTTPException(
            status_code=409,
            detail="No reconciliation run found. Call POST /api/reconcile/run first.",
        )

    # ── Validate status filter ────────────────────────────────────────────────
    valid_statuses = {s.value for s in ReconStatus}
    if status is not None and status not in valid_statuses:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid status '{status}'. " f"Valid values: {sorted(valid_statuses)}"
            ),
        )

    # ── Filter ────────────────────────────────────────────────────────────────
    results = app_state.classified_results
    if status:
        results = [cr for cr in results if cr.status.value == status]

    # Sort by order_id for stable pagination
    results = sorted(results, key=lambda cr: cr.order_id)
    total = len(results)

    # ── Paginate ──────────────────────────────────────────────────────────────
    start = (page - 1) * page_size
    end = start + page_size
    page_items = results[start:end]

    # ── Serialise ─────────────────────────────────────────────────────────────
    items: list[TransactionSchema] = []
    for cr in page_items:
        mr = cr.match_result
        bd = mr.score_breakdown
        items.append(
            TransactionSchema(
                order_id=mr.order_id,
                status=cr.status.value,
                subtype=cr.subtype.value,
                composite_score=round(mr.composite_score, 4),
                anomaly_flags=cr.anomaly_flags,
                order_amount=float(mr.order_amount),
                settled_amount=(
                    float(mr.settled_amount) if mr.settled_amount is not None else None
                ),
                fee=float(mr.fee) if mr.fee is not None else None,
                matched_settlement_id=mr.matched_settlement_id,
                order_date=mr.order_date.isoformat() if mr.order_date else None,
                settled_date=mr.settled_date.isoformat() if mr.settled_date else None,
                score_breakdown=ScoreBreakdownSchema(
                    amount_score=round(bd.amount_score, 4),
                    reference_score=round(bd.reference_score, 4),
                    date_score=round(bd.date_score, 4),
                    amount_diff_inr=float(bd.amount_diff_inr),
                    date_diff_days=bd.date_diff_days,
                    best_utr_ratio=round(bd.best_utr_ratio, 2),
                ),
            )
        )

    return TransactionListResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=items,
    )
