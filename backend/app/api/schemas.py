"""
schemas.py
──────────
Pydantic v2 response and request models for the ReconAgent REST API.

Every endpoint returns a typed model — not a raw dict.  This is a deliberate
code-quality signal: typed responses are self-documenting, appear correctly in
the OpenAPI schema, and prevent silent field-omission bugs.

Design notes:
  • All monetary amounts are represented as float in the API layer (adequate for
    display; Decimal is used internally in the pipeline for computation).
  • IDs are always str (forward-compatible with UUIDs if the schema migrates).
  • Timestamps are ISO 8601 strings (timezone-aware where known).

⚠️  LLM MATCHING PROHIBITION: These schemas carry the outputs of the deterministic
    pipeline to the API layer.  They do not carry any LLM-derived match decisions.
    The LLM fields (explanation, llm_status) are post-hoc annotations only.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ── Envelope ──────────────────────────────────────────────────────────────────


class ApiResponse(BaseModel):
    """Standard API response envelope wrapping every endpoint's payload.

    Attributes:
        ok:      True if the request succeeded.
        data:    The endpoint-specific payload.
        error:   Human-readable error message (None on success).
    """

    ok: bool = True
    data: Any = None
    error: str | None = None


# ── Reconcile ─────────────────────────────────────────────────────────────────


class ReconcileRunResponse(BaseModel):
    """Response body for POST /api/reconcile/run.

    Attributes:
        run_id:          Monotonic integer run identifier (in-process counter).
        orders_loaded:   Number of order rows ingested.
        settlements_loaded: Number of settlement rows ingested.
        bank_txns_loaded: Number of bank transaction rows ingested.
        auto_matched:    Count of AUTO_MATCHED results.
        needs_review:    Count of NEEDS_REVIEW results.
        unresolved:      Count of UNRESOLVED results.
        duplicate_settlements: Number of duplicate settlement order IDs detected.
        phantom_credits: Number of unmatched bank credits detected.
        runtime_ms:      Wall-clock time for the full pipeline in milliseconds.
    """

    run_id: int
    orders_loaded: int
    settlements_loaded: int
    bank_txns_loaded: int
    auto_matched: int
    needs_review: int
    unresolved: int
    duplicate_settlements: int
    phantom_credits: int
    runtime_ms: int


# ── Metrics ───────────────────────────────────────────────────────────────────


class MetricsResponse(BaseModel):
    """Response body for GET /api/metrics.

    Attributes:
        total_processed:     Total orders reconciled in the last run.
        auto_matched:        Count of AUTO_MATCHED orders.
        needs_review:        Count of NEEDS_REVIEW orders.
        unresolved:          Count of UNRESOLVED orders.
        match_rate_pct:      Percentage of orders auto-matched (0–100).
        value_auto_matched:  Total ₹ order value in AUTO_MATCHED results.
        value_in_exceptions: Total ₹ order value in NEEDS_REVIEW + UNRESOLVED.
        avg_runtime_ms:      Average pipeline runtime across all runs (ms).
        last_run_id:         Run ID of the most recent reconcile call (None if none).
    """

    total_processed: int
    auto_matched: int
    needs_review: int
    unresolved: int
    match_rate_pct: float
    value_auto_matched: float
    value_in_exceptions: float
    avg_runtime_ms: float
    last_run_id: int | None


# ── Transactions ──────────────────────────────────────────────────────────────


class ScoreBreakdownSchema(BaseModel):
    """Per-signal score breakdown for a single MatchResult.

    Attributes:
        amount_score:    Score for the amount reconstruction signal (0–1).
        reference_score: Score for the UTR reference match signal (0–1).
        date_score:      Score for the settlement-date proximity signal (0–1).
        amount_diff_inr: Absolute monetary difference between order and settlement.
        date_diff_days:  Days between order date and settlement date.
        best_utr_ratio:  Raw rapidfuzz score for the best UTR match.
    """

    amount_score: float
    reference_score: float
    date_score: float
    amount_diff_inr: float
    date_diff_days: int
    best_utr_ratio: float


class TransactionSchema(BaseModel):
    """A single reconciliation result as returned by GET /api/transactions.

    Attributes:
        order_id:              Internal order identifier.
        status:                AUTO_MATCHED | NEEDS_REVIEW | UNRESOLVED.
        subtype:               Fine-grained sub-classification.
        composite_score:       Overall match confidence (0–1).
        anomaly_flags:         List of named anomaly strings.
        order_amount:          Gross order amount (₹).
        settled_amount:        Net amount settled by the gateway (₹, None if absent).
        fee:                   Gateway fee deducted (₹, None if absent).
        matched_settlement_id: ID of the best-matching settlement row (None if absent).
        order_date:            Date the order was placed (ISO 8601 string).
        settled_date:          Date the settlement was made (ISO 8601 string, None if absent).
        score_breakdown:       Per-signal score detail.
    """

    order_id: str
    status: str
    subtype: str
    composite_score: float
    anomaly_flags: list[str]
    order_amount: float
    settled_amount: float | None
    fee: float | None
    matched_settlement_id: str | None
    order_date: str | None
    settled_date: str | None
    score_breakdown: ScoreBreakdownSchema


class TransactionListResponse(BaseModel):
    """Paginated response for GET /api/transactions.

    Attributes:
        total:        Total number of transactions matching the filter.
        page:         Current page number (1-indexed).
        page_size:    Number of items per page.
        items:        The transactions on this page.
    """

    total: int
    page: int
    page_size: int
    items: list[TransactionSchema]


# ── Exceptions ────────────────────────────────────────────────────────────────


class DiffEntrySchema(BaseModel):
    """A single field comparison within an ExceptionDiff.

    Attributes:
        field:        Name of the compared field.
        expected:     Value from the order ledger.
        actual:       Value from the settlement/bank (None if absent).
        delta:        Numeric or temporal difference (None for non-numeric fields).
        signal:       Matching signal that covers this field.
        weight:       Signal weight in the composite score.
        score:        Score this signal received.
        is_shortfall: True if this entry explains why the result is not AUTO_MATCHED.
    """

    field: str
    expected: Any
    actual: Any
    delta: Any
    signal: str
    weight: float
    score: float
    is_shortfall: bool


class ExceptionSchema(BaseModel):
    """An exception record as returned by GET /api/exceptions.

    Attributes:
        order_id:        The order being reconciled.
        status:          NEEDS_REVIEW | UNRESOLVED.
        subtype:         Fine-grained exception sub-type.
        composite_score: Overall match confidence (0–1).
        shortfall:       How far the score is below the auto-match threshold.
        anomaly_flags:   Named anomaly strings.
        has_candidate:   True if a settlement candidate was found.
        resolution_hint: Machine-readable suggested next action.
        entries:         Per-field diff entries, shortfall-first.
    """

    order_id: str
    status: str
    subtype: str
    composite_score: float
    shortfall: float
    anomaly_flags: list[str]
    has_candidate: bool
    resolution_hint: str
    entries: list[DiffEntrySchema]


class ExceptionListResponse(BaseModel):
    """Response for GET /api/exceptions.

    Attributes:
        total:  Total number of exceptions.
        items:  The exception records.
    """

    total: int
    items: list[ExceptionSchema]


class ExplainResponse(BaseModel):
    """Response for GET /api/exceptions/{id}/explain.

    Attributes:
        order_id:      The order that was explained.
        explanation:   Plain-language paragraph (empty string on fallback).
        llm_status:    ok | cached | fallback.
        raw_diff:      The structured diff dict — always present.
        potential_hallucination: True if the model may have fabricated a number.
        latency_ms:    Gemini round-trip latency (0 for cache hits).
    """

    order_id: str
    explanation: str
    llm_status: str
    raw_diff: dict[str, Any]
    potential_hallucination: bool
    latency_ms: int


# ── Chat / Q&A ────────────────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    """Request body for POST /api/chat.

    Attributes:
        question: Natural-language question about the reconciliation results.
    """

    question: str = Field(..., min_length=3, max_length=1000)


class ChatResponse(BaseModel):
    """Response body for POST /api/chat.

    Attributes:
        question:     The original question (echoed back).
        answer:       The model's answer, or an explicit "cannot answer" message.
        context_used: Description of which records were retrieved as context.
        llm_status:   ok | fallback.
    """

    question: str
    answer: str
    context_used: str
    llm_status: str


# ── Audit log ─────────────────────────────────────────────────────────────────


class AuditLogEntrySchema(BaseModel):
    """A single audit log entry as returned by GET /api/audit-log.

    Attributes:
        event_type:              llm_explanation | llm_qa_query | match_decision | reconcile_run.
        order_id:                Order ID (None for Q&A and run events).
        model_name:              Gemini model used, "cache", "fallback", "pipeline", or "deterministic_matcher".
        prompt_summary:          Truncated description of the input.
        response_text:           Full response text (may be long).
        llm_status:              ok | cached | fallback | n/a.
        latency_ms:              Round-trip latency in milliseconds.
        potential_hallucination: True if the response contained an unexpected number.
        timestamp_utc:           ISO 8601 timestamp.
    """

    event_type: str
    order_id: str | None
    model_name: str
    prompt_summary: str
    response_text: str
    llm_status: str
    latency_ms: int
    potential_hallucination: bool
    timestamp_utc: str


class AuditLogResponse(BaseModel):
    """Paginated response for GET /api/audit-log.

    Attributes:
        total:     Total entries matching the filter.
        page:      Current page number (1-indexed).
        page_size: Items per page.
        items:     The audit log entries on this page.
    """

    total: int
    page: int
    page_size: int
    items: list[AuditLogEntrySchema]
