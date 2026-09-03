"""
state.py
────────
In-process application state for the ReconAgent API.

This module owns the single mutable store that holds the results of the
most recent reconciliation run.  It is intentionally simple: a module-level
dict that is mutated by the reconcile endpoint and read by all query endpoints.

Design decision (DDL-010 in ARCHITECTURE.md):
  Chunk 2 (database schema + SQLAlchemy models) is not yet complete, so
  this module provides an in-memory stand-in.  The interface is deliberately
  thin — replacing it with DB reads requires changing only the query helpers
  below, not the route handlers.

Thread safety: FastAPI/uvicorn runs a single-threaded async event loop for
most workloads; the reconcile operation is synchronous and blocking, so
concurrent writes are not possible in practice.  A lock is added as
defensive programming for if the app is ever moved to a multi-worker setup.

⚠️  LLM MATCHING PROHIBITION: This module stores the results of the
    deterministic matching engine.  It does not interact with the LLM layer.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from app.services.classification import ClassifiedResult, ReconStatus
from app.services.exception_diff import ExceptionDiff
from app.services.llm_layer import AuditLogEntry
from app.services.audit_db import persist_entry as _persist_to_db

# ── App state ──────────────────────────────────────────────────────────────────


@dataclass
class ReconcileRunMeta:
    """Metadata for a single reconciliation run.

    Attributes:
        run_id:          Monotonically increasing run counter.
        orders_loaded:   Number of order rows processed.
        settlements_loaded: Number of settlement rows processed.
        bank_txns_loaded: Number of bank transaction rows processed.
        auto_matched:    Count of AUTO_MATCHED results.
        needs_review:    Count of NEEDS_REVIEW results.
        unresolved:      Count of UNRESOLVED results.
        duplicate_settlements: Duplicate settlement order IDs found.
        phantom_credits: Number of phantom bank credits found.
        runtime_ms:      Wall-clock pipeline time in milliseconds.
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


@dataclass
class AppState:
    """Mutable in-process store for the most recent reconciliation run.

    Attributes:
        last_run:           Metadata for the most recent run (None before first run).
        all_runs:           List of metadata for all runs (for avg runtime).
        classified_results: The full list of ClassifiedResult from the last run.
        exception_diffs:    Pre-built ExceptionDiff objects, indexed by order_id.
        audit_log:          Append-only list of AuditLogEntry objects.
        _lock:              Threading lock for concurrent-write protection.
    """

    last_run: ReconcileRunMeta | None = None
    all_runs: list[ReconcileRunMeta] = field(default_factory=list)
    classified_results: list[ClassifiedResult] = field(default_factory=list)
    exception_diffs: dict[str, ExceptionDiff] = field(default_factory=dict)
    audit_log: list[AuditLogEntry] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def is_ready(self) -> bool:
        """Return True if at least one reconcile run has completed."""
        return self.last_run is not None

    def get_exception(self, order_id: str) -> ExceptionDiff | None:
        """Look up an exception diff by order_id.

        Args:
            order_id: The order identifier to look up.

        Returns:
            The ExceptionDiff, or None if not found or order is AUTO_MATCHED.
        """
        return self.exception_diffs.get(order_id)

    def get_classified(self, order_id: str) -> ClassifiedResult | None:
        """Look up a classified result by order_id.

        Args:
            order_id: The order identifier to look up.

        Returns:
            The ClassifiedResult, or None if not found.
        """
        for cr in self.classified_results:
            if cr.order_id == order_id:
                return cr
        return None

    def add_audit_entry(self, entry: AuditLogEntry) -> None:
        """Append an audit log entry (thread-safe) and persist to SQLite.

        Args:
            entry: The AuditLogEntry to append.
        """
        with self._lock:
            self.audit_log.append(entry)
        # Persist outside the state lock — audit_db has its own lock.
        _persist_to_db(entry)

    def compute_metrics(self) -> dict:
        """Compute aggregate metrics across the current result set.

        Returns:
            A dict with keys matching MetricsResponse fields.
        """
        if not self.classified_results:
            return {
                "total_processed": 0,
                "auto_matched": 0,
                "needs_review": 0,
                "unresolved": 0,
                "match_rate_pct": 0.0,
                "value_auto_matched": 0.0,
                "value_in_exceptions": 0.0,
                "avg_runtime_ms": 0.0,
                "last_run_id": None,
            }

        total = len(self.classified_results)
        auto = sum(1 for cr in self.classified_results if cr.status == ReconStatus.AUTO_MATCHED)
        review = sum(1 for cr in self.classified_results if cr.status == ReconStatus.NEEDS_REVIEW)
        unresolved = sum(1 for cr in self.classified_results if cr.status == ReconStatus.UNRESOLVED)

        value_auto = sum(
            float(cr.match_result.order_amount)
            for cr in self.classified_results
            if cr.status == ReconStatus.AUTO_MATCHED
        )
        value_exc = sum(
            float(cr.match_result.order_amount)
            for cr in self.classified_results
            if cr.status != ReconStatus.AUTO_MATCHED
        )

        avg_rt = (
            sum(r.runtime_ms for r in self.all_runs) / len(self.all_runs)
            if self.all_runs else 0.0
        )

        return {
            "total_processed": total,
            "auto_matched": auto,
            "needs_review": review,
            "unresolved": unresolved,
            "match_rate_pct": round(auto / total * 100, 2) if total else 0.0,
            "value_auto_matched": round(value_auto, 2),
            "value_in_exceptions": round(value_exc, 2),
            "avg_runtime_ms": round(avg_rt, 2),
            "last_run_id": self.last_run.run_id if self.last_run else None,
        }


# ── Singleton instance ────────────────────────────────────────────────────────

# One global state object shared across all request handlers.
# Replaced by DB reads in Chunk 8 when the audit_log table is wired up.
app_state = AppState()
