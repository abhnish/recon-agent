"""
audit.py
────────
GET /api/audit-log — paginated, filterable audit trail.

Returns AuditLogEntry records persisted in the SQLite audit_log table by
audit_db.persist_entry(). The in-memory AppState list is kept for
backwards-compatibility with other internal readers, but this endpoint
now reads from the DB so entries survive server restarts.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.api.schemas import AuditLogEntrySchema, AuditLogResponse
from app.services.audit_db import load_entries

router = APIRouter(prefix="/api", tags=["audit"])

_VALID_EVENT_TYPES = {
    "llm_explanation",
    "llm_qa_query",
    "match_decision",
    "reconcile_run",
    "ingestion",
}


@router.get(
    "/audit-log",
    response_model=AuditLogResponse,
    summary="Paginated audit trail",
    description=(
        "Returns an append-only audit trail of every pipeline event: "
        "ingestion attempts, match decisions, LLM explain calls, and Q&A queries. "
        "Each entry records what was given, what was returned, latency, and "
        "whether a potential hallucination was detected. "
        "Filterable by event_type. Most recent entries are returned first. "
        "Persisted to SQLite — survives server restarts."
    ),
)
def get_audit_log(
    event_type: str | None = Query(
        default=None,
        description=(
            "Filter by event type: "
            "ingestion | reconcile_run | match_decision | "
            "llm_explanation | llm_qa_query"
        ),
    ),
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(default=20, ge=1, le=200, description="Items per page"),
) -> AuditLogResponse:
    """Return the audit log, most-recent-first, from SQLite.

    Args:
        event_type: Optional filter on event type string.
        page:       Page number (1-indexed).
        page_size:  Items per page (max 200).

    Returns:
        A paginated AuditLogResponse.

    Raises:
        HTTPException 422: If the event_type filter value is invalid.
    """
    if event_type is not None and event_type not in _VALID_EVENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid event_type '{event_type}'. "
                f"Valid values: {sorted(_VALID_EVENT_TYPES)}"
            ),
        )

    offset = (page - 1) * page_size
    rows, total = load_entries(
        event_type=event_type,
        limit=page_size,
        offset=offset,
    )

    items: list[AuditLogEntrySchema] = [
        AuditLogEntrySchema(
            event_type=row["event_type"],
            order_id=row.get("order_id"),
            model_name=row["model_name"],
            prompt_summary=row["prompt_summary"],
            response_text=row["response_text"],
            llm_status=row["llm_status"],
            latency_ms=row["latency_ms"],
            potential_hallucination=bool(row["potential_hallucination"]),
            timestamp_utc=row["timestamp_utc"],
        )
        for row in rows
    ]

    return AuditLogResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=items,
    )
