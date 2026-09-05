"""
test_llm_layer.py
─────────────────
Tests for the LLM explain-and-answer layer (llm_layer.py).

All tests mock the Gemini API — no live network calls are made in CI.
The mocking strategy:
  • ``genai.GenerativeModel`` is patched at the import boundary so the SDK
    object is never instantiated.
  • A mock model's ``generate_content`` method is configured to return
    fake responses, raise ResourceExhausted, or behave as needed per test.

Coverage targets:
  1. explain_exception — happy path: Gemini called with correct prompt shape.
  2. explain_exception — cache hit: second identical call returns cached response
     without calling Gemini again.
  3. explain_exception — 429 retry: ResourceExhausted triggers exponential backoff
     and a second attempt.
  4. explain_exception — all retries exhausted: returns fallback response with
     llm_status="fallback", no exception raised.
  5. explain_exception — hallucination flag: response containing a number not
     present in the diff is flagged in the audit entry.
  6. explain_exception — audit entry created on every call.
  7. answer_question — no relevant context: returns explicit "cannot answer" message.
  8. answer_question — context filtered by order ID: correct records injected.
  9. answer_question — context filtered by subtype keyword.
  10. _call_with_retry — backoff timing: sleep is called with correct intervals.
"""

from __future__ import annotations

# stdlib
from decimal import Decimal
from unittest.mock import MagicMock, patch

# third-party
import pytest
from google.api_core.exceptions import ResourceExhausted

from app.services import llm_layer

# internal
from app.services.classification import (
    classify,
)
from app.services.exception_diff import ExceptionDiff, build_diff
from app.services.llm_layer import (
    AuditEventType,
    AuditLogEntry,
    _check_hallucination,
    _diff_cache_key,
    _extract_numbers,
    _retrieve_context,
    answer_question,
    clear_explain_cache,
    explain_exception,
)
from app.services.matching import MatchResult, ScoreBreakdown

# ── Shared fixtures ────────────────────────────────────────────────────────────


def _make_match_result(
    *,
    order_id: str = "ORD2024043",
    composite_score: float = 0.62,
    amount_score: float = 0.0,
    reference_score: float = 1.0,
    date_score: float = 0.8,
    amount_diff_inr: str = "340.00",
    date_diff_days: int = 6,
    best_utr_ratio: float = 95.0,
    order_amount: str = "5000.00",
    settled_amount: str = "4660.00",
    fee: str = "0.00",
    tax_on_fee: str = "0.00",
) -> MatchResult:
    """Build a minimal MatchResult for testing."""
    from datetime import date

    return MatchResult(
        order_id=order_id,
        composite_score=composite_score,
        score_breakdown=ScoreBreakdown(
            amount_score=amount_score,
            reference_score=reference_score,
            date_score=date_score,
            amount_diff_inr=Decimal(amount_diff_inr),
            date_diff_days=date_diff_days,
            best_utr_ratio=best_utr_ratio,
        ),
        matched_settlement_id="SET2024043",
        order_amount=Decimal(order_amount),
        settled_amount=Decimal(settled_amount),
        fee=Decimal(fee),
        tax_on_fee=Decimal(tax_on_fee),
        order_date=date(2024, 3, 1),
        settled_date=date(2024, 3, 7),
    )


@pytest.fixture(autouse=True)
def _clear_cache_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the in-memory cache and set a dummy API key before each test."""
    clear_explain_cache()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")


@pytest.fixture()
def partial_refund_diff() -> ExceptionDiff:
    """A PARTIAL_REFUND diff for ORD2024043 (₹340 shortfall, 6-day delay)."""
    result = _make_match_result()
    classified = classify(result)
    return build_diff(classified)


@pytest.fixture()
def rounding_diff_obj() -> ExceptionDiff:
    """A ROUNDING_DIFF diff (₹1.29 shortfall) for ORD2024044."""
    result = _make_match_result(
        order_id="ORD2024044",
        composite_score=0.91,
        amount_score=0.7,
        amount_diff_inr="1.29",
        date_diff_days=2,
        order_amount="5000.00",
        settled_amount="4998.71",
    )
    classified = classify(result)
    return build_diff(classified)


# ── Helper: build a mock Gemini model ─────────────────────────────────────────


def _mock_client(response_text: str = "Mocked Gemini explanation.") -> MagicMock:
    """Return a mock genai.Client whose models.generate_content returns response_text."""
    mock_response = MagicMock()
    mock_response.text = response_text
    mock_models = MagicMock()
    mock_models.generate_content.return_value = mock_response
    mock = MagicMock()
    mock.models = mock_models
    return mock


# Backwards-compat alias used in some tests
_mock_model = _mock_client


# ── Task 1: Exception explainer ────────────────────────────────────────────────


class TestExplainException:
    """Tests for explain_exception()."""

    def test_happy_path_returns_explanation_text(
        self, partial_refund_diff: ExceptionDiff
    ) -> None:
        """explain_exception returns llm_status=ok and non-empty explanation text."""
        fake_text = (
            "The settlement for order ORD2024043 is ₹340.00 short of the order "
            "total of ₹5000.00. This is most likely a partial refund that was "
            "not recorded. Check for an unrecorded refund entry in the gateway."
        )
        with (
            patch.object(
                llm_layer, "_build_client", return_value=_mock_model(fake_text)
            ),
        ):
            resp = explain_exception(partial_refund_diff)

        assert resp.llm_status == "ok"
        assert resp.explanation == fake_text
        assert resp.order_id == "ORD2024043"
        assert isinstance(resp.raw_diff, dict)
        assert resp.raw_diff["order_id"] == "ORD2024043"

    def test_gemini_called_with_structured_input(
        self, partial_refund_diff: ExceptionDiff
    ) -> None:
        """The prompt sent to Gemini contains the diff's order_id and subtype."""
        mock_model = _mock_model()
        with patch.object(llm_layer, "_build_client", return_value=mock_model):
            explain_exception(partial_refund_diff)

        assert mock_model.models.generate_content.call_count == 1
        # New SDK uses keyword args: generate_content(model=..., contents=...)
        call_kwargs = mock_model.models.generate_content.call_args.kwargs
        prompt_arg: str = call_kwargs.get("contents", "")
        assert "ORD2024043" in prompt_arg
        assert "PARTIAL_REFUND" in prompt_arg or "partial_refund" in prompt_arg.lower()
        # Prompt must contain the diff JSON (at minimum the order_id field)
        assert '"order_id"' in prompt_arg

    def test_audit_entry_created_on_successful_call(
        self, partial_refund_diff: ExceptionDiff
    ) -> None:
        """An AuditLogEntry with event_type=llm_explanation is created per call."""
        with patch.object(llm_layer, "_build_client", return_value=_mock_model()):
            resp = explain_exception(partial_refund_diff)

        audit = resp.audit_entry
        assert isinstance(audit, AuditLogEntry)
        assert audit.event_type == AuditEventType.EXPLANATION
        assert audit.order_id == "ORD2024043"
        assert audit.llm_status == "ok"
        assert audit.latency_ms >= 0

    def test_audit_entry_contains_response_text(
        self, partial_refund_diff: ExceptionDiff
    ) -> None:
        """The audit entry records the full response text."""
        fake_text = "Settlement ₹340.00 short. Likely partial refund."
        with patch.object(
            llm_layer, "_build_client", return_value=_mock_model(fake_text)
        ):
            resp = explain_exception(partial_refund_diff)

        assert resp.audit_entry.response_text == fake_text

    def test_raw_diff_always_present(self, partial_refund_diff: ExceptionDiff) -> None:
        """raw_diff is populated regardless of llm_status."""
        with patch.object(llm_layer, "_build_client", return_value=_mock_model()):
            resp = explain_exception(partial_refund_diff)

        assert "entries" in resp.raw_diff
        assert "subtype" in resp.raw_diff
        assert resp.raw_diff["subtype"] != ""


# ── Task 3a: Cache behaviour ───────────────────────────────────────────────────


class TestCacheBehaviour:
    """Verify the in-memory cache prevents duplicate API calls."""

    def test_second_call_for_same_diff_uses_cache(
        self, partial_refund_diff: ExceptionDiff
    ) -> None:
        """The second call for an identical diff does NOT call generate_content."""
        mock_model = _mock_model("First explanation.")
        with patch.object(llm_layer, "_build_client", return_value=mock_model):
            resp1 = explain_exception(partial_refund_diff)
            resp2 = explain_exception(partial_refund_diff)

        # generate_content should only have been called once
        assert mock_model.models.generate_content.call_count == 1
        assert resp2.llm_status == "cached"
        assert resp2.explanation == resp1.explanation

    def test_second_call_audit_entry_is_cached(
        self, partial_refund_diff: ExceptionDiff
    ) -> None:
        """The audit entry for a cache hit reports model_name='cache'."""
        with patch.object(llm_layer, "_build_client", return_value=_mock_model()):
            explain_exception(partial_refund_diff)
            resp2 = explain_exception(partial_refund_diff)

        assert resp2.audit_entry.model_name == "cache"
        assert resp2.audit_entry.latency_ms == 0

    def test_different_diffs_are_not_confused(
        self, partial_refund_diff: ExceptionDiff, rounding_diff_obj: ExceptionDiff
    ) -> None:
        """Two structurally different diffs produce different cache keys."""
        key1 = _diff_cache_key(partial_refund_diff)
        key2 = _diff_cache_key(rounding_diff_obj)
        assert key1 != key2

    def test_clear_cache_allows_fresh_call(
        self, partial_refund_diff: ExceptionDiff
    ) -> None:
        """After clearing the cache, the next call contacts Gemini again."""
        mock_model = _mock_model("Fresh explanation.")
        with patch.object(llm_layer, "_build_client", return_value=mock_model):
            explain_exception(partial_refund_diff)
            cleared = clear_explain_cache()
            explain_exception(partial_refund_diff)

        assert cleared >= 1
        assert mock_model.models.generate_content.call_count == 2


# ── Task 3b: Retry / backoff ───────────────────────────────────────────────────


class TestRetryBehaviour:
    """Verify exponential backoff triggers correctly on 429 responses."""

    def test_single_429_then_success_retries_once(
        self, partial_refund_diff: ExceptionDiff
    ) -> None:
        """A single 429 causes one retry; the second call succeeds."""
        mock_response = MagicMock()
        mock_response.text = "Succeeded on retry."

        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = [
            ResourceExhausted("quota exceeded"),
            mock_response,
        ]

        with (
            patch.object(llm_layer, "_build_client", return_value=mock_client),
            patch("app.services.llm_layer.time.sleep") as mock_sleep,
        ):
            resp = explain_exception(partial_refund_diff)

        assert resp.llm_status == "ok"
        assert resp.explanation == "Succeeded on retry."
        # sleep must have been called once (between attempt 0 and attempt 1)
        assert mock_sleep.call_count == 1

    def test_backoff_sleep_duration_correct(
        self, partial_refund_diff: ExceptionDiff
    ) -> None:
        """Backoff sleep is called with base=1.0s on first retry."""
        mock_response = MagicMock()
        mock_response.text = "OK after retry."
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = [
            ResourceExhausted("rate limited"),
            mock_response,
        ]

        with (
            patch.object(llm_layer, "_build_client", return_value=mock_client),
            patch("app.services.llm_layer.time.sleep") as mock_sleep,
        ):
            explain_exception(partial_refund_diff)

        first_sleep_arg = mock_sleep.call_args_list[0][0][0]
        # First retry: base * factor^0 = 1.0 * 1 = 1.0
        assert first_sleep_arg == pytest.approx(1.0)

    def test_all_retries_exhausted_returns_fallback(
        self, partial_refund_diff: ExceptionDiff
    ) -> None:
        """When all retries fail, llm_status='fallback' is returned — no exception raised."""
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = ResourceExhausted(
            "always rate limited"
        )

        with (
            patch.object(llm_layer, "_build_client", return_value=mock_client),
            patch("app.services.llm_layer.time.sleep"),
        ):
            resp = explain_exception(partial_refund_diff)

        assert resp.llm_status == "fallback"
        # explanation is empty — caller uses raw_diff instead
        assert resp.explanation == ""
        # raw_diff must still be present
        assert isinstance(resp.raw_diff, dict)
        assert "order_id" in resp.raw_diff

    def test_fallback_audit_entry_records_fallback_status(
        self, partial_refund_diff: ExceptionDiff
    ) -> None:
        """The audit entry for a fallback response has llm_status='fallback'."""
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = ResourceExhausted("quota")

        with (
            patch.object(llm_layer, "_build_client", return_value=mock_client),
            patch("app.services.llm_layer.time.sleep"),
        ):
            resp = explain_exception(partial_refund_diff)

        assert resp.audit_entry.llm_status == "fallback"
        assert resp.audit_entry.model_name == "fallback"

    def test_retry_count_equals_max_retries(
        self, partial_refund_diff: ExceptionDiff
    ) -> None:
        """generate_content is called exactly _MAX_RETRIES times before giving up."""
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = ResourceExhausted("quota")

        with (
            patch.object(llm_layer, "_build_client", return_value=mock_client),
            patch("app.services.llm_layer.time.sleep"),
        ):
            explain_exception(partial_refund_diff)

        assert mock_client.models.generate_content.call_count == llm_layer._MAX_RETRIES


# ── Task 4: Hallucination guard ────────────────────────────────────────────────


class TestHallucinationGuard:
    """Verify the number-extraction hallucination detection."""

    def test_extract_numbers_finds_plain_integers(self) -> None:
        """_extract_numbers correctly extracts plain integer strings."""
        nums = _extract_numbers("There are 42 orders and 3 exceptions.")
        assert "42.0" in nums
        assert "3.0" in nums

    def test_extract_numbers_finds_rupee_amounts(self) -> None:
        """_extract_numbers handles ₹ prefixed amounts."""
        nums = _extract_numbers("Settlement is ₹4660.00 short by ₹340.00.")
        assert "4660.0" in nums
        assert "340.0" in nums

    def test_no_hallucination_when_numbers_match(
        self, partial_refund_diff: ExceptionDiff
    ) -> None:
        """No hallucination flag when response numbers are all present in the diff."""
        partial_refund_diff.to_dict()
        # Use the amount that IS in the diff
        response = "The settlement for ORD2024043 shows ₹4660.00 settled."
        assert not _check_hallucination(response, partial_refund_diff)

    def test_hallucination_flagged_when_new_number_appears(
        self, partial_refund_diff: ExceptionDiff
    ) -> None:
        """Hallucination flag is True when response contains a number not in the diff."""
        response = "The order ORD2024043 has a shortfall of ₹99999.99 which is not in any record."
        assert _check_hallucination(response, partial_refund_diff)

    def test_audit_entry_flags_hallucination(
        self, partial_refund_diff: ExceptionDiff
    ) -> None:
        """When a hallucinated number is detected, audit_entry.potential_hallucination=True."""
        hallucinated_text = (
            "ORD2024043 has a shortfall of ₹99999.00 which is not in the diff."
        )
        mock_model = _mock_model(hallucinated_text)
        with patch.object(llm_layer, "_build_client", return_value=mock_model):
            resp = explain_exception(partial_refund_diff)

        assert resp.audit_entry.potential_hallucination is True

    def test_audit_entry_no_hallucination_on_clean_response(
        self, partial_refund_diff: ExceptionDiff
    ) -> None:
        """potential_hallucination=False when all response numbers are in the diff."""
        clean_text = (
            "The settlement amount of ₹4660.00 does not match the order amount."
        )
        mock_model = _mock_model(clean_text)
        with patch.object(llm_layer, "_build_client", return_value=mock_model):
            resp = explain_exception(partial_refund_diff)

        assert resp.audit_entry.potential_hallucination is False


# ── Task 2: Q&A layer ─────────────────────────────────────────────────────────


class TestQALayer:
    """Tests for answer_question()."""

    def _sample_results(self) -> list[dict]:
        """Build a small list of mock reconciliation result dicts."""
        return [
            {
                "order_id": "ORD2024043",
                "status": "NEEDS_REVIEW",
                "subtype": "PARTIAL_REFUND",
                "composite_score": 0.62,
                "anomaly_flags": ["amount_diff_₹340.00"],
            },
            {
                "order_id": "ORD2024046",
                "status": "NEEDS_REVIEW",
                "subtype": "ROUNDING_DIFF",
                "composite_score": 0.91,
                "anomaly_flags": ["amount_diff_₹0.66"],
            },
            {
                "order_id": "ORD2024052",
                "status": "UNRESOLVED",
                "subtype": "FAILED_PAYMENT",
                "composite_score": 0.0,
                "anomaly_flags": [],
            },
            {
                "order_id": "ORD2024001",
                "status": "AUTO_MATCHED",
                "subtype": "CLEAN",
                "composite_score": 1.0,
                "anomaly_flags": [],
            },
        ]

    def test_no_matching_context_returns_cannot_answer(self) -> None:
        """If no records match the question, return explicit 'cannot answer' response."""
        resp = answer_question(
            "What is the XYZABC123 category?",
            self._sample_results(),
        )
        assert resp.llm_status == "fallback"
        assert "cannot answer" in resp.answer.lower()
        assert resp.audit_entry.event_type == AuditEventType.QA_QUERY

    def test_order_id_filter_injects_correct_record(self) -> None:
        """A question mentioning ORD2024043 retrieves only that order's record."""
        mock_model = _mock_model("ORD2024043 has a partial refund shortfall.")
        with patch.object(llm_layer, "_build_client", return_value=mock_model):
            resp = answer_question(
                "Why does ORD2024043 have a shortfall?",
                self._sample_results(),
            )

        assert resp.llm_status == "ok"
        prompt_arg: str = mock_model.models.generate_content.call_args[1]["contents"]
        assert "ORD2024043" in prompt_arg
        # Should NOT inject unrelated orders
        assert "ORD2024052" not in prompt_arg

    def test_subtype_keyword_filter_returns_matching_subtype(self) -> None:
        """A question about 'failed' payments retrieves FAILED_PAYMENT records."""
        mock_model = _mock_model("There is 1 failed payment.")
        with patch.object(llm_layer, "_build_client", return_value=mock_model):
            resp = answer_question(
                "Show me all failed payments",
                self._sample_results(),
            )

        assert resp.llm_status == "ok"
        prompt_arg: str = mock_model.models.generate_content.call_args[1]["contents"]
        assert "FAILED_PAYMENT" in prompt_arg
        assert "ORD2024052" in prompt_arg

    def test_qa_audit_entry_created(self) -> None:
        """Every Q&A call creates an AuditLogEntry with event_type=llm_qa_query."""
        mock_model = _mock_model("Rounding differences are small.")
        with patch.object(llm_layer, "_build_client", return_value=mock_model):
            resp = answer_question(
                "What rounding issues do we have?",
                self._sample_results(),
            )

        assert resp.audit_entry.event_type == AuditEventType.QA_QUERY
        assert resp.audit_entry.llm_status == "ok"

    def test_no_context_returns_without_calling_gemini(self) -> None:
        """When no context is retrieved, Gemini is not called."""
        mock_model = _mock_model()
        with patch.object(llm_layer, "_build_client", return_value=mock_model):
            answer_question("What is XYZINVALID?", self._sample_results())

        mock_model.models.generate_content.assert_not_called()

    def test_qa_fallback_on_gemini_error(self) -> None:
        """Q&A falls back gracefully when Gemini raises an exception."""
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = ResourceExhausted("quota")

        with (
            patch.object(llm_layer, "_build_client", return_value=mock_client),
            patch("app.services.llm_layer.time.sleep"),
        ):
            resp = answer_question(
                "Why does ORD2024043 have a shortfall?",
                self._sample_results(),
            )

        assert resp.llm_status == "fallback"
        assert resp.audit_entry.model_name == "fallback"


# ── Context retrieval unit tests ───────────────────────────────────────────────


class TestRetrieveContext:
    """Unit tests for the _retrieve_context helper."""

    def _records(self) -> list[dict]:
        return [
            {
                "order_id": "ORD2024043",
                "status": "NEEDS_REVIEW",
                "subtype": "PARTIAL_REFUND",
            },
            {
                "order_id": "ORD2024046",
                "status": "NEEDS_REVIEW",
                "subtype": "ROUNDING_DIFF",
            },
            {
                "order_id": "ORD2024052",
                "status": "UNRESOLVED",
                "subtype": "FAILED_PAYMENT",
            },
        ]

    def test_order_id_match_takes_priority(self) -> None:
        """When question contains an order ID, only that record is returned."""
        records, _summary = _retrieve_context(
            "What happened with ORD2024052?", self._records()
        )
        assert len(records) == 1
        assert records[0]["order_id"] == "ORD2024052"

    def test_subtype_keyword_rounding_matches(self) -> None:
        """'rounding' keyword returns ROUNDING_DIFF records."""
        records, _ = _retrieve_context("Show me all rounding issues", self._records())
        assert all(r["subtype"] == "ROUNDING_DIFF" for r in records)

    def test_subtype_keyword_refund_matches(self) -> None:
        """'refund' keyword returns PARTIAL_REFUND records."""
        records, _ = _retrieve_context("Any refund issues today?", self._records())
        assert all(r["subtype"] == "PARTIAL_REFUND" for r in records)

    def test_no_match_returns_empty(self) -> None:
        """Unknown query returns empty list."""
        records, _ = _retrieve_context("xyzzy frobble??", self._records())
        assert records == []

    def test_summary_question_returns_all_records(self) -> None:
        """'how many' question returns all records for a full summary."""
        records, _ = _retrieve_context(
            "how many exceptions do we have?", self._records()
        )
        assert len(records) == len(self._records())
