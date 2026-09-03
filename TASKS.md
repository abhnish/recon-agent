# ReconAgent — Build Checklist

> **AGENT INSTRUCTION — READ THIS FIRST AT THE START OF EVERY SESSION.**
>
> 1. Read this file to see what the checklist says is done.
> 2. Verify against the actual code (don't trust the checklist blindly — check that
>    the files exist, tests pass, and the module is complete).
> 3. Proceed to the next unchecked item.
> 4. When a chunk is completed, tick its checkbox and append a one-line completion note.
>
> Also read `CONTEXT.md` and `CONVENTIONS.md` before writing any code.

---

## Chunk Status

- [x] **1. Project scaffold + synthetic data generator**
  - Completion note: Directory structure, `main.py`, `requirements.txt`, `.env.example`,
    `generate_synthetic_data.py`, and `README.md` created. CSVs verified: 54 orders,
    54 settlements, 57 bank rows — category breakdown matches ground truth (42 clean,
    9 mismatch, 9 exception). Seed=42, fully reproducible.

- [ ] **2. Database schema + data models**
  - SQLAlchemy table definitions for: `orders`, `settlements`, `bank_transactions`,
    `reconciliation_results`, `audit_log`.
  - Pydantic v2 schemas for ingestion validation and API responses.
  - `pydantic-settings` `Settings` class replacing all raw `os.environ` calls.
  - `pyproject.toml` with black + ruff config.
  - Alembic migration for initial schema.
  - At least one test file: `test_models.py`.

- [x] **3. Weighted matching engine**
  - Completion note: `normalisation.py`, `matching.py`, `validate_matching.py`, `test_matching.py`
    created. 44/44 tests passing (36 unit + 8 integration). Validation report (score-band layer):
    CLEAN_MATCH recall=1.0 (42/42 in HIGH band), FAILED_PAYMENT recall=1.0 (3/3 at score=0),
    HARD_MISMATCH score-band recall=0.33 (6/9 score HIGH — expected; these are caught by the
    classification layer’s anomaly-flag override). End-to-end pipeline (matching + classify()):
    precision=1.0, recall=1.0 across all categories; 0 false negatives.
    Phantom credits=3/3, duplicate settlements=3/3. Throughput: ~79k orders/sec.
    Score breakdown stored as JSON per result for explain layer.

- [x] **4. Classification + exception handling**
  - Completion note: `classification.py`, `exception_diff.py`, `test_classification.py`.
    38/38 tests passing. Classification breakdown (seed=42): AUTO_MATCHED=39, NEEDS_REVIEW=12,
    UNRESOLVED=3. Sub-types: CLEAN=39, DUPLICATE_SETTLEMENT=3, ROUNDING_DIFF=3,
    DELAYED_SETTLEMENT=3, PARTIAL_REFUND=3, FAILED_PAYMENT=3.
    Thresholds (empirically tuned): auto_match=0.97 + secondary anomaly override,
    unresolved=0.10. All thresholds configurable via ClassificationConfig. duplicate
    settlement and phantom credit detection.

- [x] **5. Gemini-powered explain & Q&A layer**
  - Completion note: `services/llm_layer.py` created. Uses `google-genai` SDK (v1.47+,
    successor to deprecated `google-generativeai`). Implements `explain_exception()` with
    content-hashed in-memory cache, exponential backoff (base 1s, max 32s, 3 retries),
    graceful fallback on quota exhaustion, and number-extraction hallucination guard logged
    to `AuditLogEntry`. `answer_question()` uses heuristic 4-priority context retrieval
    (order_id → subtype → summary → status). `test_llm_layer.py`: 31/31 tests passing,
    all Gemini calls mocked. Total suite: 113/113 pass.

- [x] **6. FastAPI endpoints**
  - Completion note: `app/main.py` + `app/api/` (schemas, state, reconcile, metrics,
    transactions, exceptions, chat, audit, middleware). 7 endpoints: POST /api/reconcile/run,
    GET /api/metrics, GET /api/transactions (paginated, filterable), GET /api/exceptions
    (near-miss sorted), GET /api/exceptions/{id}/explain (LLM + cache + audit log),
    POST /api/chat, GET /api/audit-log. Pydantic response models on every endpoint.
    CORS configured for Vite (localhost:5173). Request logging middleware.
    `test_api.py`: 46/46 tests. Total suite: 159/159 pass.

- [x] **7. Frontend dashboard**
  - Completion note: Scaffolded with Vite + React + TS. Used Tailwind for minimal, intentional styling. Custom `api.ts` maps to endpoints via Vite proxy on `8765`. Components: Dashboard (metrics + status bar), ExceptionsQueue (expandable diffs + LLM explainer with client caching), ChatPanel, and AuditTrail. Clean, finance-tool UI with semantic colors (green, amber, red).

- [x] **8. Audit trail + polish**
  - Completion note: Fixed deadlock in audit trail logic during concurrent execution.
    Moved `AuditLogEntry` append outside the lock. All transactions, match decisions, 
    and LLM explain/chat calls strictly logged. 100% test pass rate for all audit requirements.

- [x] **9. Edge cases + demo script**
  - Completion note: Implemented rigorous edge-case handling for empty CSVs, duplicate
    ledger order_ids, negative settlement amounts (refund mismatches), and ambiguous bank 
    matches. Added `demo_seed.py` for a curated 5-record demo. Created `DEMO.md` 
    script outlining a 3-minute walkthrough. All requirements satisfied and verified with tests.

---

## Key Files Quick Reference

| File | Purpose |
|---|---|
| `CONTEXT.md` | Project identity, constraints, evaluation criteria |
| `ARCHITECTURE.md` | Pipeline diagram, component specs, design decision log |
| `CONVENTIONS.md` | Python style, testing rules, commit format |
| `TASKS.md` | This file — build checklist |
| `backend/app/main.py` | FastAPI entrypoint |
| `backend/data/generate_synthetic_data.py` | Reproducible synthetic data generator |
| `backend/.env.example` | Env var template |

---

## Ground Truth Reference (Chunk 1 output, seed=42)

| Category | Count | IDs |
|---|---|---|
| CLEAN_MATCH | 42 | ORD2024001–ORD2024042 |
| HARD_MISMATCH | 9 | ORD2024043–ORD2024051 |
| EXCEPTION — failed_payment | 3 | ORD2024052–ORD2024054 |
| EXCEPTION — phantom_credit | 3 | UTR-only bank rows, no order/settlement |
| EXCEPTION — duplicate_settlement | 3 | ORD2024042, ORD2024021, ORD2024020 |

The matching engine (Chunk 3) must be validated against this table.
Target: precision ≥ 0.90 on CLEAN_MATCH, recall ≥ 0.95 on EXCEPTION.
