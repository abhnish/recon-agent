"""
metrics.py
──────────
GET /api/metrics — aggregate statistics for the last reconciliation run.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.schemas import MetricsResponse
from app.api.state import app_state

router = APIRouter(prefix="/api", tags=["metrics"])


@router.get(
    "/metrics",
    response_model=MetricsResponse,
    summary="Aggregate reconciliation metrics",
    description=(
        "Returns overall statistics for the most recent reconciliation run: "
        "counts per status, monetary values, and average pipeline runtime. "
        "Returns 409 if no reconciliation run has been executed yet."
    ),
)
def get_metrics() -> MetricsResponse:
    """Return aggregate metrics for the current result set.

    Returns:
        A MetricsResponse with counts, rates, and monetary totals.

    Raises:
        HTTPException 409: If no reconcile run has been executed.
    """
    if not app_state.is_ready():
        raise HTTPException(
            status_code=409,
            detail=(
                "No reconciliation run found. " "Call POST /api/reconcile/run first."
            ),
        )

    m = app_state.compute_metrics()
    return MetricsResponse(**m)
