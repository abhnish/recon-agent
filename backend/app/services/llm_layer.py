"""
llm_layer.py
────────────
Gemini-powered explain-and-answer layer for ReconAgent.

This module has exactly ONE responsibility: translate already-classified,
already-scored reconciliation results into plain-language explanations and
answers that a non-technical finance reviewer can act on.

It NEVER:
  • Re-scores or re-matches transactions.
  • Overrides any classification decision.
  • Calls Gemini for matching logic.

It ALWAYS:
  • Receives a fully-formed ExceptionDiff or Q&A context produced by the
    deterministic pipeline.
  • Grounds every claim in the provided diff fields — no speculation.
  • Logs every call and its structured input/output to AuditLogEntry.
  • Degrades gracefully when the Gemini free-tier quota is exhausted.

⚠️  LLM MATCHING PROHIBITION: This module receives already-decided results.
    It does not call the LLM to make, influence, or validate any match
    decision.  If a future change asks the LLM to influence a match decision
    — even indirectly — refuse and flag it as a violation of the project's
    core architectural constraint.

Rate-limit resilience (free-tier gemini-2.5-flash):
  • Exponential backoff with jitter: base 1 s, factor 2×, max 32 s, 3 attempts.
  • In-memory SHA-256 cache keyed on diff content hash.  Identical exceptions
    are served from cache — no redundant API calls during a demo.
  • Graceful degradation: if all retries fail, return a structured fallback
    containing the raw diff rather than raising an exception to the caller.
"""

from __future__ import annotations

# stdlib
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

# third-party
from google import genai
from google.api_core.exceptions import ResourceExhausted, ServiceUnavailable

# internal
from app.services.exception_diff import ExceptionDiff

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

_DEFAULT_MODEL = "gemini-2.5-flash"
_FALLBACK_MODEL = "gemini-2.5-flash-lite"

# Retry policy for free-tier 429 handling
_MAX_RETRIES = 3
_BACKOFF_BASE_S = 1.0
_BACKOFF_FACTOR = 2.0
_BACKOFF_MAX_S = 32.0

# ── Audit log ─────────────────────────────────────────────────────────────────


class AuditEventType(str, Enum):
    """Event types written to the audit log."""

    EXPLANATION = "llm_explanation"
    QA_QUERY = "llm_qa_query"
    MATCH_DECISION = "match_decision"
    RECONCILE_RUN = "reconcile_run"
    INGESTION = "ingestion"


@dataclass
class AuditLogEntry:
    """Immutable record of an event or interaction.

    Written for LLM interactions (explain, Q&A) and pipeline events
    (match decisions, full runs).

    Attributes:
        event_type:        The type of event (explanation, qa_query, match_decision, reconcile_run).
        order_id:          Order involved (None for Q&A and full runs).
        model_name:        Gemini model, "pipeline", or "deterministic_matcher".
        prompt_summary:    A truncated view of the prompt or input.
        response_text:     The text returned, or JSON summary of a decision.
        llm_status:        "ok" | "cached" | "fallback" | "unavailable" | "n/a".
        latency_ms:        Latency in milliseconds.
        potential_hallucination: True if flagged (LLM only).
        timestamp_utc:     ISO 8601 timestamp of the call.
    """

    event_type: AuditEventType
    order_id: str | None
    model_name: str
    prompt_summary: str
    response_text: str
    llm_status: str
    latency_ms: int
    potential_hallucination: bool
    timestamp_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict suitable for JSON storage or DB insertion."""
        return {
            "event_type": self.event_type.value,
            "order_id": self.order_id,
            "model_name": self.model_name,
            "prompt_summary": self.prompt_summary,
            "response_text": self.response_text,
            "llm_status": self.llm_status,
            "latency_ms": self.latency_ms,
            "potential_hallucination": self.potential_hallucination,
            "timestamp_utc": self.timestamp_utc,
        }


# ── Response dataclasses ──────────────────────────────────────────────────────


@dataclass
class ExplainResponse:
    """Return value of ``explain_exception``.

    Attributes:
        order_id:     The order that was explained.
        explanation:  Plain-language paragraph for a finance reviewer.  Empty
                      string when ``llm_status`` is "fallback" or "unavailable".
        raw_diff:     The structured diff dict — always present regardless of
                      llm_status so the caller can display something useful.
        llm_status:   "ok" | "cached" | "fallback".
        audit_entry:  The complete audit log entry for this call.
    """

    order_id: str
    explanation: str
    raw_diff: dict[str, Any]
    llm_status: str
    audit_entry: AuditLogEntry


@dataclass
class QAResponse:
    """Return value of ``answer_question``.

    Attributes:
        question:     The original natural-language question.
        answer:       The model's answer, or an explicit "cannot answer" message.
        context_used: Summary of which records were retrieved and injected.
        llm_status:   "ok" | "fallback".
        audit_entry:  The complete audit log entry for this call.
    """

    question: str
    answer: str
    context_used: str
    llm_status: str
    audit_entry: AuditLogEntry


# ── In-memory cache ───────────────────────────────────────────────────────────

# Maps content-hash → ExplainResponse.  Bounded only by process lifetime;
# for a demo/hackathon context this is sufficient.  A production deployment
# would use Redis or a DB-backed cache with TTL.
_explain_cache: dict[str, ExplainResponse] = {}


def _diff_cache_key(diff: ExceptionDiff) -> str:
    """Compute a stable SHA-256 hash over the serialised diff content.

    The key is content-addressed: two diffs for the same order with the same
    fields produce the same hash and the same cached response.

    Args:
        diff: The ExceptionDiff to hash.

    Returns:
        A 64-character hex string suitable for use as a dict key.
    """
    canonical = json.dumps(diff.to_dict(), sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


# ── Gemini client factory ─────────────────────────────────────────────────────


def _get_model_name() -> str:
    """Read the configured model name from the environment.

    Falls back to _DEFAULT_MODEL if GEMINI_MODEL is not set.  The env var
    is the single source of truth — the model name is never hardcoded in
    call sites.

    Returns:
        The Gemini model name string to pass to GenerativeModel().
    """
    return os.environ.get("GEMINI_MODEL", _DEFAULT_MODEL)


def _build_client() -> genai.Client:
    """Instantiate and configure the Gemini Client (google.genai SDK).

    Reads the API key from GEMINI_API_KEY (required).  If the key is absent
    the function raises a RuntimeError immediately so the failure is visible
    at startup rather than silently returning bad responses later.

    Uses the ``google.genai`` package (the successor to the deprecated
    ``google.generativeai`` package, which reached end-of-life in 2025).

    Returns:
        A configured ``genai.Client`` instance.

    Raises:
        RuntimeError: If GEMINI_API_KEY is not set in the environment.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set.  "
            "Add it to your .env file or environment before using the LLM layer."
        )
    model_name = _get_model_name()
    logger.debug("Building Gemini client for model: %s", model_name)
    return genai.Client(api_key=api_key)


# ── Retry helper ──────────────────────────────────────────────────────────────


def _call_with_retry(
    client: genai.Client,
    prompt: str,
) -> tuple[str, str]:
    """Call ``client.models.generate_content`` with exponential backoff on 429.

    Retries up to _MAX_RETRIES times on ResourceExhausted (HTTP 429) or
    ServiceUnavailable (HTTP 503).  Each retry waits ``base * factor^attempt``
    seconds, capped at _BACKOFF_MAX_S.

    Uses the ``google.genai`` SDK interface:
    ``client.models.generate_content(model=<name>, contents=<prompt>)``.

    Args:
        client: A configured ``genai.Client``.
        prompt: The full prompt string to send.

    Returns:
        A (response_text, model_used) tuple where model_used is the model
        name string (may differ from requested if a fallback was used).

    Raises:
        ResourceExhausted: If all retries are exhausted.
        Exception: On any non-retryable error.
    """
    model_name = _get_model_name()
    last_exc: Exception = RuntimeError("No attempts made")

    for attempt in range(_MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            return response.text, model_name
        except (ResourceExhausted, ServiceUnavailable) as exc:
            last_exc = exc
            wait_s = min(_BACKOFF_BASE_S * (_BACKOFF_FACTOR**attempt), _BACKOFF_MAX_S)
            logger.warning(
                "Gemini rate-limit hit (attempt %d/%d). Backing off %.1fs. Error: %s",
                attempt + 1,
                _MAX_RETRIES,
                wait_s,
                exc,
            )
            time.sleep(wait_s)
        except Exception as exc:
            # Non-retryable — surface immediately
            logger.error("Gemini non-retryable error: %s", exc)
            raise

    logger.error("Gemini: all %d retries exhausted.", _MAX_RETRIES)
    raise last_exc


# ── Number extraction for hallucination guard ─────────────────────────────────


def _extract_numbers(text: str) -> set[str]:
    """Extract all numeric literals from a text string.

    Matches integers, decimals, and amounts with or without the ₹ sign.
    Used for the hallucination guard: numbers in the response that do not
    appear in the source diff may indicate fabricated data.

    Args:
        text: Any plain-text string.

    Returns:
        A set of normalised numeric strings (stripped of leading zeros and
        trailing decimal zeros for stable comparison).
    """
    # Match optional ₹, then digits with optional decimal part
    raw_numbers = re.findall(r"₹?\d+(?:\.\d+)?", text)
    result: set[str] = set()
    for raw in raw_numbers:
        # Strip currency symbol, normalise to a canonical decimal string
        stripped = raw.lstrip("₹").strip()
        try:
            # Normalise: "1000.00" and "1000" → "1000.0"
            result.add(str(float(stripped)))
        except ValueError:
            pass
    return result


def _check_hallucination(response_text: str, diff: ExceptionDiff) -> bool:
    """Detect numbers in the response that are not present in the input diff.

    This is a best-effort guard, not a perfect filter.  Its purpose is to
    surface cases where the model has introduced specific figures not grounded
    in the diff for manual review.  False positives are possible (e.g., the
    model citing a percentage) and are acceptable — the check errs on the
    side of caution.

    Args:
        response_text: The model's response string.
        diff:          The ExceptionDiff the model was given.

    Returns:
        True if the response contains at least one number not present in the
        serialised diff.  False otherwise.
    """
    diff_text = json.dumps(diff.to_dict(), default=str)
    diff_numbers = _extract_numbers(diff_text)
    response_numbers = _extract_numbers(response_text)
    unseen = response_numbers - diff_numbers
    if unseen:
        logger.warning(
            "Potential hallucination detected for %s — numbers in response "
            "not present in diff: %s",
            diff.order_id,
            unseen,
        )
    return bool(unseen)


# ── Prompt templates ──────────────────────────────────────────────────────────


_EXPLAIN_SYSTEM_INSTRUCTION = """\
You are a reconciliation assistant helping finance operations staff understand
payment exceptions. You will be given a structured diff describing a single
payment reconciliation anomaly. Your job is to write a clear, concise explanation
of what went wrong and what the reviewer should do next.

Rules you must follow:
1. Write for a non-technical finance person — avoid technical jargon like
   "composite score" or "signal weight". Translate numbers into business language.
2. Ground EVERY claim in the diff fields provided. Do not invent, speculate, or
   reference any amounts, dates, or identifiers not present in the diff.
3. Be specific: say "the settlement is ₹340 short" not "there is an amount issue".
4. Include one concrete suggested next action appropriate for the exception type.
5. Write 2–4 sentences maximum. Do not pad with caveats or disclaimers.
"""


def _build_explain_prompt(diff: ExceptionDiff) -> str:
    """Build the full explain prompt for a given ExceptionDiff.

    The diff is serialised to JSON and injected verbatim so the model sees
    exactly what the deterministic pipeline produced — no paraphrasing that
    could introduce drift between what was computed and what was shown.

    Args:
        diff: The ExceptionDiff to explain.

    Returns:
        A complete prompt string ready to send to the Gemini API.
    """
    diff_json = json.dumps(diff.to_dict(), indent=2, default=str)
    return (
        f"{_EXPLAIN_SYSTEM_INSTRUCTION}\n\n"
        f"--- RECONCILIATION DIFF (JSON) ---\n{diff_json}\n"
        f"--- END DIFF ---\n\n"
        f"Write the explanation now. Start directly with the issue — "
        f"no preamble like 'Based on the diff...'."
    )


_QA_SYSTEM_INSTRUCTION = """\
You are a reconciliation assistant helping finance operations staff query
their reconciliation results using plain English. You will be given:
  1. A natural-language question from the user.
  2. A JSON context block containing the relevant reconciliation records
     retrieved from the database.

Rules you must follow:
1. Answer ONLY using information present in the provided context.
2. If the context does not contain enough information to answer the question,
   say exactly: "I cannot answer this from the available data." Then briefly
   explain what data would be needed.
3. Be specific and cite order IDs, amounts, and dates when they support the answer.
4. Do not speculate about causes not evidenced in the context.
"""


def _build_qa_prompt(question: str, context_json: str) -> str:
    """Build the full Q&A prompt.

    Args:
        question:     The raw natural-language question from the user.
        context_json: JSON string of the retrieved reconciliation records.

    Returns:
        A complete prompt string.
    """
    return (
        f"{_QA_SYSTEM_INSTRUCTION}\n\n"
        f"--- USER QUESTION ---\n{question}\n"
        f"--- END QUESTION ---\n\n"
        f"--- CONTEXT (JSON) ---\n{context_json}\n"
        f"--- END CONTEXT ---\n\n"
        f"Answer now."
    )


# ── Public API ─────────────────────────────────────────────────────────────────


def explain_exception(diff: ExceptionDiff) -> ExplainResponse:
    """Generate a plain-language explanation of a reconciliation exception.

    Given a structured ``ExceptionDiff`` (produced by the deterministic
    classification pipeline), calls Gemini to produce a 2–4 sentence
    explanation suitable for a non-technical finance reviewer.

    The function is idempotent for identical diffs: the second call is served
    from the in-memory cache without contacting the API.

    If the Gemini API is unavailable after retries, a fallback response is
    returned that contains the raw diff as a structured dictionary — the
    caller always gets a usable response even when the LLM is down.

    Args:
        diff: An ``ExceptionDiff`` for a NEEDS_REVIEW or UNRESOLVED result.
             AUTO_MATCHED results are accepted but are uncommon (their diff
             has no shortfall entries).

    Returns:
        An ``ExplainResponse`` with explanation text, raw diff, llm_status,
        and audit log entry.

    Note:
        Does not raise.  All errors are captured in the audit entry and
        surfaced as llm_status="fallback".
    """
    cache_key = _diff_cache_key(diff)

    # ── Cache hit ──────────────────────────────────────────────────────────────
    if cache_key in _explain_cache:
        cached = _explain_cache[cache_key]
        logger.debug("Cache hit for %s (key=%s…)", diff.order_id, cache_key[:8])
        audit = AuditLogEntry(
            event_type=AuditEventType.EXPLANATION,
            order_id=diff.order_id,
            model_name="cache",
            prompt_summary=f"[cache hit for order {diff.order_id}]",
            response_text=cached.explanation,
            llm_status="cached",
            latency_ms=0,
            potential_hallucination=False,
        )
        _log_audit(audit)
        # Return a fresh ExplainResponse with updated audit entry
        return ExplainResponse(
            order_id=diff.order_id,
            explanation=cached.explanation,
            raw_diff=diff.to_dict(),
            llm_status="cached",
            audit_entry=audit,
        )

    # ── Build prompt ───────────────────────────────────────────────────────────
    prompt = _build_explain_prompt(diff)
    raw_diff = diff.to_dict()
    prompt_summary = (
        f"explain/{diff.order_id} subtype={diff.subtype.value} "
        f"score={diff.composite_score:.3f} flags={diff.anomaly_flags}"
    )

    # ── Call Gemini with retry ────────────────────────────────────────────────
    t0 = time.monotonic()
    try:
        model = _build_client()
        response_text, model_used = _call_with_retry(model, prompt)
        latency_ms = int((time.monotonic() - t0) * 1000)

        has_hallucination = _check_hallucination(response_text, diff)

        audit = AuditLogEntry(
            event_type=AuditEventType.EXPLANATION,
            order_id=diff.order_id,
            model_name=model_used,
            prompt_summary=prompt_summary,
            response_text=response_text,
            llm_status="ok",
            latency_ms=latency_ms,
            potential_hallucination=has_hallucination,
        )
        _log_audit(audit)

        result = ExplainResponse(
            order_id=diff.order_id,
            explanation=response_text,
            raw_diff=raw_diff,
            llm_status="ok",
            audit_entry=audit,
        )
        # Populate cache on success
        _explain_cache[cache_key] = result
        return result

    except Exception as exc:  # noqa: BLE001
        # ── Graceful degradation ───────────────────────────────────────────────
        latency_ms = int((time.monotonic() - t0) * 1000)
        fallback_text = _build_fallback_explanation(diff)
        logger.warning(
            "Gemini explain call failed for %s after retries (%s). "
            "Returning raw-diff fallback.",
            diff.order_id,
            exc,
        )

        audit = AuditLogEntry(
            event_type=AuditEventType.EXPLANATION,
            order_id=diff.order_id,
            model_name="fallback",
            prompt_summary=prompt_summary,
            response_text=fallback_text,
            llm_status="fallback",
            latency_ms=latency_ms,
            potential_hallucination=False,
        )
        _log_audit(audit)

        return ExplainResponse(
            order_id=diff.order_id,
            explanation="",
            raw_diff=raw_diff,
            llm_status="fallback",
            audit_entry=audit,
        )


def answer_question(
    question: str,
    classified_results: list[dict[str, Any]],
) -> QAResponse:
    """Answer a natural-language question about the reconciliation results.

    Retrieves relevant records from the provided list using simple keyword
    and order-ID filtering (not vector RAG — the dataset is small and
    structured), injects them as JSON context, and calls Gemini to answer.

    If no relevant records are found, or if the model cannot answer from
    the context, the response will explicitly say so rather than guessing.

    Args:
        question:            Natural-language question from the dashboard user.
        classified_results:  List of dicts, each representing one reconciliation
                             result (order_id, status, subtype, composite_score,
                             anomaly_flags, etc.).  Typically the output of
                             ``build_exception_list`` with ``include_auto_matched=True``.

    Returns:
        A ``QAResponse`` with the answer text, context summary, llm_status,
        and audit log entry.

    Note:
        Does not raise.  Falls back to an explicit "cannot answer" message if
        Gemini is unavailable.
    """
    # ── Retrieve relevant context ──────────────────────────────────────────────
    relevant_records, context_summary = _retrieve_context(question, classified_results)

    if not relevant_records:
        no_data_answer = (
            "I cannot answer this from the available data. "
            "No reconciliation records matched the query terms. "
            "Try asking about a specific order ID or exception type."
        )
        audit = AuditLogEntry(
            event_type=AuditEventType.QA_QUERY,
            order_id=None,
            model_name="no_context",
            prompt_summary=f"qa: {question[:120]}",
            response_text=no_data_answer,
            llm_status="fallback",
            latency_ms=0,
            potential_hallucination=False,
        )
        _log_audit(audit)
        return QAResponse(
            question=question,
            answer=no_data_answer,
            context_used=context_summary,
            llm_status="fallback",
            audit_entry=audit,
        )

    # ── Build prompt ───────────────────────────────────────────────────────────
    context_json = json.dumps(relevant_records, indent=2, default=str)
    prompt = _build_qa_prompt(question, context_json)
    prompt_summary = (
        f"qa: {question[:120]} | " f"context_records={len(relevant_records)}"
    )

    # ── Call Gemini with retry ────────────────────────────────────────────────
    t0 = time.monotonic()
    try:
        model = _build_client()
        response_text, model_used = _call_with_retry(model, prompt)
        latency_ms = int((time.monotonic() - t0) * 1000)

        audit = AuditLogEntry(
            event_type=AuditEventType.QA_QUERY,
            order_id=None,
            model_name=model_used,
            prompt_summary=prompt_summary,
            response_text=response_text,
            llm_status="ok",
            latency_ms=latency_ms,
            potential_hallucination=False,  # Q&A answers not number-checked
        )
        _log_audit(audit)

        return QAResponse(
            question=question,
            answer=response_text,
            context_used=context_summary,
            llm_status="ok",
            audit_entry=audit,
        )

    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.monotonic() - t0) * 1000)
        fallback_answer = (
            "I cannot answer this right now — the explanation service is temporarily "
            f"unavailable ({type(exc).__name__}). "
            "Here are the relevant records for manual review."
        )
        logger.warning("Gemini Q&A call failed: %s. Returning fallback.", exc)

        audit = AuditLogEntry(
            event_type=AuditEventType.QA_QUERY,
            order_id=None,
            model_name="fallback",
            prompt_summary=prompt_summary,
            response_text=fallback_answer,
            llm_status="fallback",
            latency_ms=latency_ms,
            potential_hallucination=False,
        )
        _log_audit(audit)

        return QAResponse(
            question=question,
            answer=fallback_answer,
            context_used=context_summary,
            llm_status="fallback",
            audit_entry=audit,
        )


def clear_explain_cache() -> int:
    """Flush the in-memory explain cache.

    Primarily useful for tests and for forcing fresh explanations after data
    changes.  Returns the number of entries that were cleared.

    Returns:
        Count of cache entries removed.
    """
    count = len(_explain_cache)
    _explain_cache.clear()
    logger.debug("Explain cache cleared (%d entries removed).", count)
    return count


# ── Internal helpers ──────────────────────────────────────────────────────────


def _retrieve_context(
    question: str,
    results: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """Filter reconciliation results to those relevant to the question.

    Uses simple heuristic matching appropriate for a small, structured dataset:
      1. If the question mentions a specific order ID, return only that order.
      2. If the question mentions an exception type keyword, filter to matching
         subtypes.
      3. If the question asks about a status (NEEDS_REVIEW, UNRESOLVED,
         AUTO_MATCHED), filter to that status.
      4. If none of the above applies, return all records (let Gemini reason).

    This is intentionally simple — the dataset has at most a few hundred rows.
    A full vector-RAG implementation would be over-engineering for this scope.

    Args:
        question: The raw user question.
        results:  All reconciliation result dicts.

    Returns:
        A (filtered_records, summary_string) tuple.
    """
    q_lower = question.lower()

    # ── Priority 1: specific order ID mentioned ────────────────────────────────
    order_id_matches = re.findall(r"ORD\d+", question, re.IGNORECASE)
    if order_id_matches:
        ids = {oid.upper() for oid in order_id_matches}
        matched = [r for r in results if str(r.get("order_id", "")).upper() in ids]
        if matched:
            return matched, f"Filtered to order_ids={ids}"

    # ── Priority 2: exception subtype keyword ─────────────────────────────────
    subtype_keywords: dict[str, list[str]] = {
        "ROUNDING_DIFF": ["rounding", "gst", "round"],
        "PARTIAL_REFUND": ["refund", "partial", "shortfall", "short"],
        "DELAYED_SETTLEMENT": ["delay", "delayed", "late", "sla"],
        "MISSING_BANK_CREDIT": ["missing", "bank credit", "not credited"],
        "FAILED_PAYMENT": ["failed", "failure", "no settlement", "declined"],
        "PHANTOM_CREDIT": ["phantom", "unknown credit", "unmatched credit"],
        "DUPLICATE_SETTLEMENT": ["duplicate", "double", "twice"],
    }
    for subtype_val, keywords in subtype_keywords.items():
        if any(kw in q_lower for kw in keywords):
            matched = [
                r for r in results if str(r.get("subtype", "")).upper() == subtype_val
            ]
            if matched:
                return (
                    matched,
                    f"Filtered to subtype={subtype_val} ({len(matched)} records)",
                )

    # ── Priority 3: summary-level questions — return all records ─────────────
    # Checked before status keywords to prevent summary questions like
    # "how many exceptions" from being hijacked by the "exception" → UNRESOLVED
    # keyword match, which would return an incomplete subset.
    summary_triggers = [
        "how many",
        "total",
        "count",
        "summary",
        "breakdown",
        "overview",
        "all",
        "list",
        "show me",
    ]
    if any(t in q_lower for t in summary_triggers):
        return results, f"Full dataset ({len(results)} records)"

    # ── Priority 4: status keyword ────────────────────────────────────────────
    status_keywords = {
        "NEEDS_REVIEW": ["needs review", "needs_review", "review", "mismatch"],
        "UNRESOLVED": ["unresolved", "exception", "failed"],
        "AUTO_MATCHED": ["auto matched", "auto_matched", "clean", "matched"],
    }
    for status_val, keywords in status_keywords.items():
        if any(kw in q_lower for kw in keywords):
            matched = [
                r for r in results if str(r.get("status", "")).upper() == status_val
            ]
            if matched:
                return (
                    matched,
                    f"Filtered to status={status_val} ({len(matched)} records)",
                )

    # ── No match — return empty; caller handles the "no context" case ─────────
    return [], "No matching records found"


def _build_fallback_explanation(diff: ExceptionDiff) -> str:
    """Build a structured plain-text fallback when Gemini is unavailable.

    This is shown to the user when the LLM call fails after retries.  It
    presents the same information the model would have had as a formatted
    summary, so the reviewer still gets a usable view.

    Args:
        diff: The ExceptionDiff that could not be explained by the LLM.

    Returns:
        A formatted string describing the exception fields directly.
    """
    lines = [
        "[Explanation unavailable — Gemini API unreachable. Raw diff below.]",
        "",
        f"Order:     {diff.order_id}",
        f"Status:    {diff.status.value}  |  Sub-type: {diff.subtype.value}",
        (
            f"Score:     {diff.composite_score:.3f}  "
            f"(shortfall from auto-match: {diff.shortfall:.3f})"
        ),
    ]
    if diff.anomaly_flags:
        lines.append(f"Anomalies: {', '.join(diff.anomaly_flags)}")
    lines.append(f"Hint:      {diff.resolution_hint}")
    lines.append("")
    shortfalls = [e for e in diff.entries if e.is_shortfall]
    if shortfalls:
        lines.append("Shortfall details:")
        for e in shortfalls:
            lines.append(
                f"  {e.field_name}: expected={e.expected}  "
                f"actual={e.actual}  delta={e.delta}"
            )
    return "\n".join(lines)


def _log_audit(entry: AuditLogEntry) -> None:
    """Write an audit log entry to the Python logger.

    In this implementation the entry is emitted as a structured JSON log line
    at INFO level.  Chunk 8 will persist these to the audit_log DB table;
    this function is the single integration point that will be updated then.

    Args:
        entry: The AuditLogEntry to record.
    """
    logger.info(
        "AUDIT | %s",
        json.dumps(entry.to_dict(), default=str),
    )
