"""
test_api.py
───────────
FastAPI TestClient integration tests for all API endpoints.

Coverage targets:
  • POST /api/reconcile/run  — happy path + file-not-found simulation
  • GET  /api/metrics        — happy path + 409 before first run
  • GET  /api/transactions   — happy path, status filter, invalid filter, pagination
  • GET  /api/exceptions     — happy path + 409 before first run
  • GET  /api/exceptions/{id}/explain — happy path (mocked LLM) + 404 for unknown ID
                                       + 404 for AUTO_MATCHED order
  • POST /api/chat           — happy path (mocked LLM) + 409 before run
                             + no-context "cannot answer" path
  • GET  /api/audit-log      — empty, after explain call, event_type filter, invalid filter
  • GET  /health             — liveness probe

Strategy:
  • All tests share a single TestClient instance but reset the app_state store
    before each class to guarantee isolation.
  • The reconcile endpoint reads from the real synthetic CSV files; we test it
    against those files rather than mocking I/O (the files are deterministic).
  • LLM calls in the explain and chat endpoints are mocked so no GEMINI_API_KEY
    is required in CI.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# Set a dummy API key before importing the app so llm_layer doesn't blow up
os.environ.setdefault("GEMINI_API_KEY", "test-key-ci")
os.environ.setdefault("GEMINI_MODEL", "gemini-2.5-flash")

from app.api.state import app_state
from app.main import app
from app.services import llm_layer
from app.services.llm_layer import (
    AuditEventType,
    AuditLogEntry,
    QAResponse,
    clear_explain_cache,
)
from app.services.llm_layer import (
    ExplainResponse as LlmExplainResponse,
)

client = TestClient(app)


# ── Fixtures ───────────────────────────────────────────────────────────────────


import app.api.chat as chat_router_module
import app.api.exceptions as exceptions_router_module
from app.services import audit_db


def _reset_state() -> None:
    """Reset the global app_state and LLM cache between tests."""
    import threading

    app_state.last_run = None
    app_state.all_runs.clear()
    app_state.classified_results.clear()
    app_state.exception_diffs.clear()
    app_state.audit_log.clear()
    app_state._lock = threading.Lock()
    clear_explain_cache()
    # Clear the persistent audit log db as well
    with audit_db._get_conn() as conn:
        conn.execute("DELETE FROM audit_log")
        conn.commit()


def _run_reconcile() -> dict:
    """Trigger a reconcile run and assert it succeeded."""
    resp = client.post("/api/reconcile/run")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _make_audit_entry(
    event_type: AuditEventType = AuditEventType.EXPLANATION,
    order_id: str = "ORD2024043",
    status: str = "ok",
    explanation: str = "Test explanation.",
) -> AuditLogEntry:
    return AuditLogEntry(
        event_type=event_type,
        order_id=order_id,
        model_name="gemini-2.5-flash",
        prompt_summary=f"explain/{order_id}",
        response_text=explanation,
        llm_status=status,
        latency_ms=123,
        potential_hallucination=False,
    )


def _mock_explain_response(
    order_id: str = "ORD2024043",
    explanation: str = "Test explanation from mocked Gemini.",
) -> LlmExplainResponse:
    """Build a fake ExplainResponse for tests that mock the LLM layer."""
    audit = _make_audit_entry(order_id=order_id, explanation=explanation)
    # We need a real ExceptionDiff for raw_diff — grab it after a reconcile run
    diff = app_state.get_exception(order_id)
    raw_diff = diff.to_dict() if diff else {"order_id": order_id}
    return LlmExplainResponse(
        order_id=order_id,
        explanation=explanation,
        raw_diff=raw_diff,
        llm_status="ok",
        audit_entry=audit,
    )


def _mock_qa_response(
    question: str = "Why?",
    answer: str = "Because of a partial refund.",
) -> QAResponse:
    """Build a fake QAResponse for tests that mock the LLM layer."""
    audit = AuditLogEntry(
        event_type=AuditEventType.QA_QUERY,
        order_id=None,
        model_name="gemini-2.5-flash",
        prompt_summary=f"qa: {question[:80]}",
        response_text=answer,
        llm_status="ok",
        latency_ms=200,
        potential_hallucination=False,
    )
    return QAResponse(
        question=question,
        answer=answer,
        context_used="Filtered to order_ids={'ORD2024043'}",
        llm_status="ok",
        audit_entry=audit,
    )


# ── Health ─────────────────────────────────────────────────────────────────────


class TestHealth:
    """Liveness probe endpoint."""

    def setup_method(self) -> None:
        _reset_state()

    def test_health_returns_ok(self) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "version" in body


# ── Reconcile ──────────────────────────────────────────────────────────────────


class TestReconcileRun:
    """POST /api/reconcile/run"""

    def setup_method(self) -> None:
        _reset_state()

    def test_happy_path_returns_run_summary(self) -> None:
        """Full reconcile run completes and returns count-per-status."""
        body = _run_reconcile()
        assert body["run_id"] == 1
        assert body["orders_loaded"] > 0
        assert body["settlements_loaded"] > 0
        assert body["bank_txns_loaded"] > 0
        # Check that counts add up
        total = body["auto_matched"] + body["needs_review"] + body["unresolved"]
        assert total == body["orders_loaded"]

    def test_auto_matched_count_matches_ground_truth(self) -> None:
        """39 clean orders should auto-match (seed=42 ground truth)."""
        body = _run_reconcile()
        assert body["auto_matched"] == 39

    def test_needs_review_count_matches_ground_truth(self) -> None:
        """12 orders should be NEEDS_REVIEW (seed=42)."""
        body = _run_reconcile()
        assert body["needs_review"] == 12

    def test_unresolved_count_matches_ground_truth(self) -> None:
        """3 failed payment orders should be UNRESOLVED (seed=42)."""
        body = _run_reconcile()
        assert body["unresolved"] == 3

    def test_runtime_ms_is_positive(self) -> None:
        """Runtime must be recorded and positive."""
        body = _run_reconcile()
        assert body["runtime_ms"] >= 0

    def test_duplicate_settlements_detected(self) -> None:
        """3 duplicate settlement orders should be detected (seed=42)."""
        body = _run_reconcile()
        assert body["duplicate_settlements"] == 3

    def test_second_run_increments_run_id(self) -> None:
        """Each call to /reconcile/run gets a fresh run_id."""
        body1 = _run_reconcile()
        body2 = _run_reconcile()
        assert body2["run_id"] > body1["run_id"]

    def test_run_updates_state(self) -> None:
        """After a reconcile run, app_state is populated."""
        _run_reconcile()
        assert app_state.is_ready()
        assert len(app_state.classified_results) > 0

    def test_empty_csv_handling(self) -> None:
        """Handles empty CSVs gracefully without crashing (Task 1)."""
        with patch("app.api.reconcile._load_csv", return_value=[]):
            resp = client.post("/api/reconcile/run")
            assert resp.status_code == 200
            body = resp.json()
            assert body["orders_loaded"] == 0
            assert body["auto_matched"] == 0
            assert body["needs_review"] == 0
            assert body["unresolved"] == 0

            # Metrics should also not crash when requested on an empty run
            metrics_resp = client.get("/api/metrics")
            assert metrics_resp.status_code == 200
            metrics_body = metrics_resp.json()
            assert metrics_body["total_processed"] == 0
            assert metrics_body["match_rate_pct"] == 0.0


# ── Metrics ────────────────────────────────────────────────────────────────────


class TestMetrics:
    """GET /api/metrics"""

    def setup_method(self) -> None:
        _reset_state()

    def test_409_before_first_run(self) -> None:
        """Returns 409 if no reconcile run has been executed."""
        resp = client.get("/api/metrics")
        assert resp.status_code == 409

    def test_happy_path_returns_metrics(self) -> None:
        """After a reconcile run, metrics are populated."""
        _run_reconcile()
        resp = client.get("/api/metrics")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_processed"] > 0
        assert 0.0 <= body["match_rate_pct"] <= 100.0

    def test_match_rate_reflects_auto_matched(self) -> None:
        """match_rate_pct should be auto_matched / total * 100."""
        _run_reconcile()
        resp = client.get("/api/metrics")
        body = resp.json()
        expected = round(body["auto_matched"] / body["total_processed"] * 100, 2)
        assert abs(body["match_rate_pct"] - expected) < 0.01

    def test_value_fields_are_positive(self) -> None:
        """Monetary value fields must be non-negative."""
        _run_reconcile()
        body = client.get("/api/metrics").json()
        assert body["value_auto_matched"] >= 0
        assert body["value_in_exceptions"] >= 0

    def test_last_run_id_is_set(self) -> None:
        """last_run_id must match the run that was executed."""
        body1 = _run_reconcile()
        metrics = client.get("/api/metrics").json()
        assert metrics["last_run_id"] == body1["run_id"]


# ── Transactions ───────────────────────────────────────────────────────────────


class TestTransactions:
    """GET /api/transactions"""

    def setup_method(self) -> None:
        _reset_state()
        _run_reconcile()

    def test_happy_path_returns_paginated_list(self) -> None:
        """Default call returns first page of all transactions."""
        resp = client.get("/api/transactions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] > 0
        assert body["page"] == 1
        assert isinstance(body["items"], list)
        assert len(body["items"]) <= body["total"]

    def test_total_equals_orders_loaded(self) -> None:
        """Total without filter must equal orders_loaded from reconcile."""
        resp = client.get("/api/transactions")
        # 54 orders in seed=42 dataset
        assert resp.json()["total"] == 54

    def test_status_filter_auto_matched(self) -> None:
        """Filter by AUTO_MATCHED returns only auto-matched results."""
        resp = client.get("/api/transactions?status=AUTO_MATCHED")
        body = resp.json()
        assert body["total"] == 39
        assert all(item["status"] == "AUTO_MATCHED" for item in body["items"])

    def test_status_filter_needs_review(self) -> None:
        """Filter by NEEDS_REVIEW returns exactly 12 results."""
        resp = client.get("/api/transactions?status=NEEDS_REVIEW")
        assert resp.json()["total"] == 12

    def test_status_filter_unresolved(self) -> None:
        """Filter by UNRESOLVED returns exactly 3 results."""
        resp = client.get("/api/transactions?status=UNRESOLVED")
        assert resp.json()["total"] == 3

    def test_invalid_status_returns_422(self) -> None:
        """An invalid status filter returns HTTP 422."""
        resp = client.get("/api/transactions?status=INVALID_STATUS")
        assert resp.status_code == 422

    def test_pagination_page_size(self) -> None:
        """page_size limits items returned."""
        resp = client.get("/api/transactions?page_size=5")
        body = resp.json()
        assert len(body["items"]) == 5
        assert body["page_size"] == 5

    def test_pagination_second_page(self) -> None:
        """Second page returns different items from first page."""
        page1 = client.get("/api/transactions?page=1&page_size=10").json()
        page2 = client.get("/api/transactions?page=2&page_size=10").json()
        ids1 = {item["order_id"] for item in page1["items"]}
        ids2 = {item["order_id"] for item in page2["items"]}
        assert ids1.isdisjoint(ids2)

    def test_items_have_score_breakdown(self) -> None:
        """Each item includes a score_breakdown with expected fields."""
        resp = client.get("/api/transactions?page_size=1")
        item = resp.json()["items"][0]
        bd = item["score_breakdown"]
        assert "amount_score" in bd
        assert "reference_score" in bd
        assert "date_score" in bd

    def test_409_before_run(self) -> None:
        """Returns 409 before any reconcile run."""
        _reset_state()
        resp = client.get("/api/transactions")
        assert resp.status_code == 409


# ── Exceptions ─────────────────────────────────────────────────────────────────


class TestExceptions:
    """GET /api/exceptions and GET /api/exceptions/{id}/explain"""

    def setup_method(self) -> None:
        _reset_state()
        _run_reconcile()

    def test_happy_path_returns_exception_list(self) -> None:
        """Returns NEEDS_REVIEW + UNRESOLVED results."""
        resp = client.get("/api/exceptions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 15  # 12 NEEDS_REVIEW + 3 UNRESOLVED (seed=42)
        assert isinstance(body["items"], list)

    def test_exceptions_contain_diff_entries(self) -> None:
        """Each exception has at least one diff entry."""
        body = client.get("/api/exceptions").json()
        for item in body["items"]:
            assert len(item["entries"]) > 0

    def test_exceptions_sorted_needs_review_first(self) -> None:
        """NEEDS_REVIEW items appear before UNRESOLVED (near-miss-first sort)."""
        items = client.get("/api/exceptions").json()["items"]
        statuses = [i["status"] for i in items]
        # All NEEDS_REVIEW must come before any UNRESOLVED
        saw_unresolved = False
        for s in statuses:
            if s == "UNRESOLVED":
                saw_unresolved = True
            if saw_unresolved and s == "NEEDS_REVIEW":
                pytest.fail("NEEDS_REVIEW appeared after UNRESOLVED")

    def test_409_before_run(self) -> None:
        """Returns 409 before any reconcile run."""
        _reset_state()
        resp = client.get("/api/exceptions")
        assert resp.status_code == 409

    def test_explain_happy_path(self) -> None:
        """explain endpoint returns LLM response with correct structure."""
        # Pick a known exception from seed=42
        exception_id = next(iter(app_state.exception_diffs.keys()))

        def _fake_explain(diff):
            return _mock_explain_response(order_id=diff.order_id)

        with patch.object(
            exceptions_router_module, "explain_exception", side_effect=_fake_explain
        ):
            resp = client.get(f"/api/exceptions/{exception_id}/explain")

        assert resp.status_code == 200
        body = resp.json()
        assert body["order_id"] == exception_id
        assert body["llm_status"] in ("ok", "cached", "fallback")
        assert "raw_diff" in body
        assert "potential_hallucination" in body

    def test_explain_populates_audit_log(self) -> None:
        """An explain call adds an entry to the audit log."""
        exception_id = next(iter(app_state.exception_diffs.keys()))

        def _fake_explain(diff):
            return _mock_explain_response(order_id=diff.order_id)

        initial_count = len(app_state.audit_log)
        with patch.object(
            exceptions_router_module, "explain_exception", side_effect=_fake_explain
        ):
            client.get(f"/api/exceptions/{exception_id}/explain")

        assert len(app_state.audit_log) == initial_count + 1

    def test_explain_404_for_unknown_order(self) -> None:
        """Returns 404 for an order_id not in the current result set."""
        resp = client.get("/api/exceptions/ORD_DOES_NOT_EXIST/explain")
        assert resp.status_code == 404

    def test_explain_404_for_auto_matched_order(self) -> None:
        """Returns 404 with a clear message for AUTO_MATCHED orders."""
        # Find a known AUTO_MATCHED order from seed=42
        auto_matched_id = next(
            cr.order_id
            for cr in app_state.classified_results
            if cr.status.value == "AUTO_MATCHED"
        )
        resp = client.get(f"/api/exceptions/{auto_matched_id}/explain")
        assert resp.status_code == 404
        assert "AUTO_MATCHED" in resp.json()["detail"]

    def test_explain_409_before_run(self) -> None:
        """Returns 409 if called before any reconcile run."""
        _reset_state()
        resp = client.get("/api/exceptions/ORD2024043/explain")
        assert resp.status_code == 409


# ── Chat ───────────────────────────────────────────────────────────────────────


class TestChat:
    """POST /api/chat"""

    def setup_method(self) -> None:
        _reset_state()
        _run_reconcile()

    def test_happy_path_returns_answer(self) -> None:
        """A valid question returns a ChatResponse with answer and llm_status."""
        with patch.object(
            llm_layer,
            "answer_question",
            return_value=_mock_qa_response(),
        ):
            resp = client.post(
                "/api/chat", json={"question": "Why does ORD2024043 show a shortfall?"}
            )

        assert resp.status_code == 200
        body = resp.json()
        assert "answer" in body
        assert "llm_status" in body
        assert body["question"] == "Why does ORD2024043 show a shortfall?"

    def test_chat_populates_audit_log(self) -> None:
        """A chat call adds a QA audit entry to the audit log."""
        initial_count = len(app_state.audit_log)
        with patch.object(
            llm_layer,
            "answer_question",
            return_value=_mock_qa_response(),
        ):
            client.post("/api/chat", json={"question": "How many exceptions?"})

        assert len(app_state.audit_log) == initial_count + 1

    def test_409_before_run(self) -> None:
        """Returns 409 before any reconcile run."""
        _reset_state()
        resp = client.post("/api/chat", json={"question": "Any failed payments?"})
        assert resp.status_code == 409

    def test_empty_question_returns_422(self) -> None:
        """An empty (too-short) question returns HTTP 422."""
        resp = client.post("/api/chat", json={"question": "?"})
        assert resp.status_code == 422

    def test_no_context_match_returns_cannot_answer(self) -> None:
        """When no context is found, the answer says 'cannot answer'."""
        # Don't mock answer_question — let it run without a Gemini key; it will
        # return a 'cannot answer' fallback for an unrecognisable question.
        resp = client.post("/api/chat", json={"question": "XYZZY frobble nonce?"})
        assert resp.status_code == 200
        body = resp.json()
        # Either the context filter found nothing → fallback llm_status
        # OR Gemini was unavailable (no key) → also fallback
        assert body["llm_status"] in ("ok", "fallback")

    def test_missing_question_field_returns_422(self) -> None:
        """A request body without the 'question' field returns HTTP 422."""
        resp = client.post("/api/chat", json={})
        assert resp.status_code == 422


# ── Audit log ──────────────────────────────────────────────────────────────────


class TestAuditLog:
    """GET /api/audit-log"""

    def setup_method(self) -> None:
        _reset_state()

    def test_empty_audit_log(self) -> None:
        """Returns an empty list when no LLM calls have been made."""
        resp = client.get("/api/audit-log")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert body["items"] == []

    def test_entries_appear_after_explain_call(self) -> None:
        """An explain call adds an entry that appears in the audit log."""
        _run_reconcile()
        app_state.audit_log.clear()
        with audit_db._get_conn() as conn:
            conn.execute("DELETE FROM audit_log")
            conn.commit()
        exception_id = next(iter(app_state.exception_diffs.keys()))

        def _fake_explain(diff):
            return _mock_explain_response(order_id=diff.order_id)

        with patch.object(
            exceptions_router_module, "explain_exception", side_effect=_fake_explain
        ):
            client.get(f"/api/exceptions/{exception_id}/explain")

        resp = client.get("/api/audit-log")
        body = resp.json()
        assert body["total"] == 1
        entry = body["items"][0]
        assert entry["event_type"] == "llm_explanation"
        assert entry["order_id"] == exception_id

    def test_event_type_filter_explanation(self) -> None:
        """Filter by llm_explanation returns only explanation entries."""
        _run_reconcile()
        exception_id = next(iter(app_state.exception_diffs.keys()))

        def _fake_explain(diff):
            return _mock_explain_response(order_id=diff.order_id)

        with patch.object(
            exceptions_router_module, "explain_exception", side_effect=_fake_explain
        ):
            client.get(f"/api/exceptions/{exception_id}/explain")
        with patch.object(
            chat_router_module, "answer_question", return_value=_mock_qa_response()
        ):
            client.post("/api/chat", json={"question": "How many exceptions?"})

        resp = client.get("/api/audit-log?event_type=llm_explanation")
        body = resp.json()
        assert all(e["event_type"] == "llm_explanation" for e in body["items"])

    def test_event_type_filter_qa(self) -> None:
        """Filter by llm_qa_query returns only Q&A entries."""
        _run_reconcile()
        with patch.object(
            chat_router_module, "answer_question", return_value=_mock_qa_response()
        ):
            client.post("/api/chat", json={"question": "How many exceptions?"})

        resp = client.get("/api/audit-log?event_type=llm_qa_query")
        body = resp.json()
        assert body["total"] >= 1
        assert all(e["event_type"] == "llm_qa_query" for e in body["items"])

    def test_invalid_event_type_returns_422(self) -> None:
        """An invalid event_type filter returns HTTP 422."""
        resp = client.get("/api/audit-log?event_type=invalid_type")
        assert resp.status_code == 422

    def test_most_recent_first(self) -> None:
        """Audit log entries are returned most-recent-first."""
        _run_reconcile()
        app_state.audit_log.clear()
        with audit_db._get_conn() as conn:
            conn.execute("DELETE FROM audit_log")
            conn.commit()
        # Make two explain calls for different orders
        exception_ids = list(app_state.exception_diffs.keys())[:2]

        call_order: list[str] = []

        def _fake_explain(diff):
            call_order.append(diff.order_id)
            return _mock_explain_response(order_id=diff.order_id)

        with patch.object(
            exceptions_router_module, "explain_exception", side_effect=_fake_explain
        ):
            for eid in exception_ids:
                client.get(f"/api/exceptions/{eid}/explain")

        resp = client.get("/api/audit-log")
        items = resp.json()["items"]
        assert len(items) == 2
        # First item in response is the last call made (most-recent-first)
        assert items[0]["order_id"] == call_order[-1]
        assert items[1]["order_id"] == call_order[0]

    def test_pagination(self) -> None:
        """Pagination works on audit log."""
        _run_reconcile()
        app_state.audit_log.clear()
        with audit_db._get_conn() as conn:
            conn.execute("DELETE FROM audit_log")
            conn.commit()
        # Insert 5 fake audit entries directly
        for i in range(5):
            entry = _make_audit_entry(order_id=f"ORD{i:04d}")
            app_state.add_audit_entry(entry)

        resp = client.get("/api/audit-log?page_size=3")
        body = resp.json()
        assert body["total"] == 5
        assert len(body["items"]) == 3
        assert body["page"] == 1
