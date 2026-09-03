# ReconAgent — Architecture

> **Living document.** Update this file when each chunk is completed.
> Add new design decisions to the [Design Decision Log](#design-decision-log) as they are made.
> Current status tags: `[DONE]` `[IN PROGRESS]` `[PLANNED]`

---

## Pipeline Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        DATA SOURCES (3)                             │
│                                                                     │
│  order_ledger.csv       settlement_report.csv      bank_statement.csv │
│  (internal orders)      (gateway settlements)      (bank credits)   │
└────────────┬───────────────────┬──────────────────────┬────────────┘
             │                   │                      │
             ▼                   ▼                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     NORMALISATION LAYER                             │
│  • Canonicalise UTR formats (strip hyphens, normalise case)         │
│  • Parse dates to ISO 8601                                          │
│  • Coerce amounts to Decimal (avoid float rounding)                 │
│  • Assign internal source-tagged IDs                                │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                  WEIGHTED MATCHING ENGINE                           │
│  • Candidate generation: UTR exact/fuzzy, order_id, amount window   │
│  • Score each candidate pair across N weighted signals              │
│  • Emit best-match + score; never calls LLM                         │
│                                                                     │
│  ⚠️  LLM NEVER TOUCHES THIS LAYER — matching is 100% deterministic  │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    CLASSIFICATION LAYER                             │
│  • CLEAN_MATCH       → score ≥ threshold, no anomalies              │
│  • HARD_MISMATCH     → matched but with flagged discrepancy         │
│  • EXCEPTION         → no viable match found                       │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                 LLM EXPLAIN & Q&A LAYER (Gemini)                    │
│  • Input: already-classified result + source row data               │
│  • Output: plain-language explanation of why it's an exception      │
│  • Also handles: natural-language questions from the dashboard       │
│  • Rate-limit handling: exponential backoff + cache + graceful degrad│
│                                                                     │
│  ⚠️  Gemini ONLY reads results — it never re-classifies or re-matches│
└──────────────────┬───────────────────────────────┬─────────────────┘
                   │                               │
                   ▼                               ▼
┌──────────────────────────┐         ┌─────────────────────────────┐
│   FASTAPI REST LAYER     │         │       AUDIT LOG             │
│   /api/v1/reconcile      │         │  Append-only record of every │
│   /api/v1/explain        │         │  match decision + score +    │
│   /api/v1/qa             │         │  rule that fired             │
│   /api/v1/audit          │         └─────────────────────────────┘
└──────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    FRONTEND DASHBOARD                               │
│  Summary cards → transaction table → exception detail + explanation │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Component Reference

### 1. Data Layer `[DONE]`

**What it does:** Provides three synthetic CSV files that simulate real-world reconciliation
inputs, and will later include a SQLAlchemy-backed database for persisting normalised
transactions, match results, and the audit log.

**Inputs:** Raw CSV exports from three sources (order system, gateway, bank).

**Outputs (current):** Three CSV files in `backend/data/`.
**Outputs (after Chunk 2):** SQLAlchemy models mapped to SQLite; Pydantic schemas for
validation at ingestion boundaries.

**Status:** Synthetic data generator complete. DB models planned (Chunk 2).

**Key files:**
- `backend/data/generate_synthetic_data.py` — reproducible generator, seed=42
- `backend/data/order_ledger.csv` — 54 rows
- `backend/data/settlement_report.csv` — 54 rows (incl. 3 duplicate settlements)
- `backend/data/bank_statement.csv` — 57 rows (incl. 3 phantom credits)

---

### 2. Normalisation Layer `[DONE]`

**What it does:** Reads raw CSVs (or DB rows), applies source-specific cleaning rules,
and emits a unified internal representation that the matching engine can compare
without worrying about formatting differences.

**Key transformations:**
- UTR canonicalisation: strip hyphens, lowercase, remove prefix variations
  → `"UTR-2024-SBIN-Q4FM..."` and `"utr2024sbinq4fm..."` → same canonical key
- Amount: `float` → `Decimal` with 2dp (avoids IEEE 754 drift in comparisons)
- Date: any format → `datetime.date` ISO 8601
- Description: extract candidate UTR substrings from free-text bank descriptions

**Inputs:** Raw CSV rows / DB rows
**Outputs:** `NormalisedOrder`, `NormalisedSettlement`, `NormalisedBankTxn` Pydantic models

**Key file:** `backend/app/services/normalisation.py`

**Status:** `[DONE]` — implemented in Chunk 3 alongside matching engine.

---

### 3. Weighted Matching Engine `[DONE]`

> ⚠️ **LLM MATCHING PROHIBITION: The LLM NEVER decides whether two transactions match.
> All decisions made here are deterministic and score-based.**

**What it does:** Takes the normalised transaction pool and produces a scored match for
every order against (settlement, bank_txn) candidates.

**Matching signals and weights (implemented):**

| Signal | Weight | Notes |
|---|---|---|
| Amount reconstruction (settled+fee+tax vs order) | 0.50 | Highest-confidence signal; Decimal arithmetic |
| Reference match (UTR fuzzy, settlement↔bank) | 0.30 | rapidfuzz partial_ratio; handles all 4 noise variants |
| Date proximity (settled_date vs order_date) | 0.20 | Full score ≤T+2, linear decay to T+10 |

**Candidate generation strategy (implemented):**
1. order_id lookup (O(1) via hash index) — primary path; settlement always carries order_id
2. Amount + date window fallback (same order_id required) — safety net only

**Exception detection:**
- `detect_duplicate_settlements()` — finds order_ids with >1 settlement row
- `detect_unmatched_bank_credits()` — phantom credit detection (threshold=90 to avoid
  prefix-match false positives from shared UTR+YEAR+BANKCODE structure)

**Outputs:** `MatchResult` — best candidate pair + composite score + per-signal `ScoreBreakdown` (JSON)

**Validation results (seed=42 dataset):**

*Score-band metrics (intermediate — composite score only, no classify()):*
- CLEAN_MATCH: 42/42 in HIGH band; FAILED_PAYMENT: 3/3 in LOW band
- HARD_MISMATCH score-band recall: 6/9 (0.67) — 3 delayed-settlement and 3 rounding-diff cases
  score HIGH because amount and UTR are perfect; the date signal alone cannot pull the
  composite below the 0.70 HIGH floor. This is expected behaviour — the classification
  layer’s anomaly-flag override catches all 9. See end-to-end numbers below.

*End-to-end pipeline metrics (matching + classify() — these are the numbers that matter):*
- AUTO_MATCHED precision: 1.0 (42/42 correct, 0 false positives)
- HARD_MISMATCH recall: 1.0 (9/9 routed to NEEDS_REVIEW, 0 missed)
- FAILED_PAYMENT recall: 1.0 (3/3 UNRESOLVED, 0 missed)
- The composite score alone routes 8/9 hard-mismatch cases (score < 0.97); one edge case
  (ORD2024046, ₹0.66 diff, score=0.9822) is caught solely by the anomaly-flag override.
- Phantom credits: 3/3 detected
- Duplicate settlements: 3/3 detected
- Throughput: ~79,000 orders/sec

**Known limitation — threshold margins on synthetic data:**
The anomaly-flag thresholds (`amount_diff > ₹0.50`, `date_diff > 5 days`) have narrow margins
against this dataset: the closest clean match has ₹0.00 amount diff (₹0.16 margin above
threshold) and 3-day settlement (2-day margin). This is a property of the synthetic data,
which is arithmetically perfect for clean matches. Real-world data will include clean matches
with minor GST rounding artefacts (e.g. a ₹0.40 fee diff) or T+4 settlements that are still
legitimate — those cases sit closer to the thresholds. Threshold recalibration against a real
merchant dataset is the expected next step before production use.

**Key files:**
- `backend/app/services/matching.py` — engine + config + exception detection
- `backend/app/services/normalisation.py` — normalisation layer
- `backend/app/services/validate_matching.py` — validation + precision/recall report
- `backend/app/services/test_matching.py` — 44 tests (36 unit + 8 integration)

**Status:** `[DONE]` — Chunk 3.

---

### 4. Classification Layer `[DONE]`

**What it does:** Applies threshold rules (with secondary anomaly override) to
`MatchResult` scores to assign a `ReconStatus` and `ExceptionSubtype` to each order.
Builds a structured `ExceptionDiff` for every non-auto-matched result.

**Threshold rationale (full argument in DDL-007):**

| Class | Primary condition | Secondary override |
|---|---|---|
| `AUTO_MATCHED` | Composite score ≥ **0.97** | Downgraded if any anomaly flag present |
| `NEEDS_REVIEW` | 0.10 ≤ score < 0.97, OR score ≥ 0.97 with anomaly flag | — |
| `UNRESOLVED` | Score < **0.10** (no viable match) | — |

The `0.97` threshold was chosen empirically: all 42 clean-match orders score 0.975–1.000;
no amount-exact clean match scores below 0.97. The secondary anomaly override ensures
that a high-scoring but anomalous result (e.g., ₹0.66 rounding diff) is never silently
auto-matched — a human must confirm any measurable discrepancy.

**Exception sub-types:**
- `CLEAN` — all signals match within tolerance (AUTO_MATCHED)
- `ROUNDING_DIFF` — small amount difference (₹0.50–₹50), likely GST rounding
- `PARTIAL_REFUND` — large amount shortfall (> ₹50), refund record not found
- `DELAYED_SETTLEMENT` — settlement date > T+5, SLA may be breached
- `MISSING_BANK_CREDIT` — settlement present but no matching bank credit found
- `FAILED_PAYMENT` — no settlement row for the order at all
- `PHANTOM_CREDIT` — bank credit with no matching settlement UTR
- `DUPLICATE_SETTLEMENT` — same order_id appears >1 time in settlement report

**Classification breakdown (seed=42 dataset):**

| Status | Count | Sub-type breakdown |
|---|---|---|
| AUTO_MATCHED | 39 | CLEAN=39 |
| NEEDS_REVIEW | 12 | DUPLICATE_SETTLEMENT=3, ROUNDING_DIFF=3, DELAYED_SETTLEMENT=3, PARTIAL_REFUND=3 |
| UNRESOLVED | 3 | FAILED_PAYMENT=3 |

**ExceptionDiff:** For every non-AUTO_MATCHED result, a structured diff object is built
containing per-field comparisons (expected vs actual, delta, signal name, weight, score).
Sorted near-miss-first so the highest-value recoverable cases appear at the top of the
review queue. This is the sole input to the LLM explain layer — no raw CSV joins needed.

**Key files:**
- `backend/app/services/classification.py` — classifier + `ClassificationConfig`
- `backend/app/services/exception_diff.py` — `ExceptionDiff` builder + list sorter
- `backend/app/services/test_classification.py` — 38 tests (29 unit + 9 integration)

**Status:** `[DONE]` — Chunk 4.

---

### 5. LLM Explain & Q&A Layer `[DONE]`

> ⚠️ **Gemini receives already-classified results. It does not re-classify or re-match.**

**What it does:**
- **Explain mode:** Given a `ClassifiedResult` marked as HARD_MISMATCH or EXCEPTION,
  generates a plain-language paragraph explaining what the anomaly is and what a
  human reviewer should check.
- **Q&A mode:** Answers natural-language questions from the dashboard about the
  reconciliation results (e.g. "Why does ORD2024049 show a shortfall?").

**Rate-limit handling (mandatory — free tier, implemented):**
1. Exponential backoff on `ResourceExhausted` (HTTP 429) — base 1s, factor 2×, max 32s, 3 retries
2. In-process SHA-256 content-hash cache — identical exceptions get cached responses;
   no redundant API calls during a demo
3. Graceful degradation — if all retries exhausted, return `llm_status: "fallback"`
   with a structured plain-text fallback containing the raw diff; never blocks the
   reconciliation result

**Hallucination guard:**
Every explanation is compared against the diff using numeric extraction (regex).
Numbers appearing in the response that are not present in the input diff are flagged
`potential_hallucination=True` in the `AuditLogEntry` for manual review.  This is a
best-effort check, not a filter — false positives (e.g., cited percentages) are acceptable.

**Context retrieval for Q&A:**
Simple 4-priority heuristic filtering (no vector RAG — dataset is small and structured):
1. Order ID regex match (highest precision)
2. Exception subtype keyword match
3. Summary-trigger keywords → return full dataset
4. Status keyword match
If no records match, returns explicit "cannot answer" without calling Gemini.

**Audit logging:**
Every call (success, cache hit, fallback) writes an `AuditLogEntry` with:
`event_type`, `order_id`, `model_name`, `prompt_summary`, `response_text`,
`llm_status`, `latency_ms`, `potential_hallucination`, `timestamp_utc`.
Chunk 8 will persist these to the `audit_log` DB table.

**Inputs:** `ExceptionDiff` (from `exception_diff.py`) + list of reconciliation result dicts
**Outputs:** `ExplainResponse` / `QAResponse` — plain-language text + `llm_status` + `AuditLogEntry`

**SDK note:** Uses `google-genai` (v1.47+), the official successor to the deprecated
`google-generativeai` package which reached end-of-life in 2025.  See DDL-008.

**Key files:**
- `backend/app/services/llm_layer.py` — full explain + Q&A implementation
- `backend/app/services/test_llm_layer.py` — 31 tests (all Gemini calls mocked)

**Status:** `[DONE]` — Chunk 5.

---

### 6. FastAPI REST Layer `[DONE]`

**What it does:** Exposes all pipeline outputs as a typed REST API.

**Endpoints (all implemented):**

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/reconcile/run` | Trigger full pipeline; returns count-per-status + runtime |
| `GET`  | `/api/metrics` | Aggregate stats: match rate, ₹ values, avg runtime |
| `GET`  | `/api/transactions` | Paginated, filterable by status; stable sort by order_id |
| `GET`  | `/api/exceptions` | Exception list, near-miss sorted (NEEDS_REVIEW → UNRESOLVED) |
| `GET`  | `/api/exceptions/{id}/explain` | LLM explanation (cached), with audit log entry |
| `POST` | `/api/chat` | Natural-language Q&A over current results |
| `GET`  | `/api/audit-log` | Audit trail, most-recent-first, filterable by event_type |
| `GET`  | `/health` | Liveness probe |

**Design choices:**
- Pydantic v2 response models on every endpoint — no raw dicts.
- 409 (not 400) for endpoints called before first reconcile run.
- CORS: localhost:5173 (Vite) + localhost:3000; extendable via `CORS_ORIGINS` env var.
- Request logging middleware: `METHOD /path → STATUS  latency_ms`.
- In-process state store (`state.py`): thin interface designed so Chunk 8 DB swap
  only touches that module.  See DDL-010.

**Key files:** `main.py` + `app/api/` (schemas, state, reconcile, metrics, transactions,
exceptions, chat, audit, middleware, test_api.py).

**Status:** `[DONE]` — Chunk 6.

---


### 7. Frontend Dashboard `[DONE]`

**What it does:** Visualises reconciliation results — summary KPIs, transaction table
with status badges, exception detail panel with Gemini explanation, Q&A chat interface, and audit trail.

**Stack:** React (Vite) + TypeScript + Tailwind CSS (v3).

**Components:**
- `Dashboard.tsx`: Displays metrics (`/api/metrics`) and a visual breakdown of statuses.
- `ExceptionsQueue.tsx`: Table of exceptions (`/api/exceptions`) sorted by severity. Rows expand to show a structured diff and an embedded "Generate Explanation" button that hits the Gemini LLM and caches the output locally.
- `ChatPanel.tsx`: Interactive chat hitting `/api/chat` with message history and context tracking.
- `AuditTrail.tsx`: Paginated and filterable table viewing `/api/audit-log`.

**Design Principles:**
- Clean, minimal, internal finance tool aesthetic.
- CSS variables for semantic status mapping (`status-match`, `status-review`, `status-unresolved`).

**Status:** `[DONE]` — Chunk 7.

---

### 7.5. Performance & Benchmarks `[DONE]`

**Throughput (Local Macbook)**
- **Dataset N=60**: ~16ms pipeline execution latency (client-side API latency ~30ms).
- **Dataset N=600 (10x)**: ~818ms pipeline execution latency (client-side API latency ~821ms).
- **Complexity**: The deterministic rules engine operates entirely in memory after CSV ingestion, scaling roughly O(N) due to indexed lookups.
- **LLM Latency**: External API calls to Gemini average ~1.5s - 3s but are triggered strictly on-demand (per exception) and cached client/server-side to minimize overhead.

---

### 8. Audit Trail `[DONE]`

**What it does:** Records every pipeline event in an append-only SQLite table
(`backend/data/audit_log.db`). Survives server restarts. Queryable via
`GET /api/audit-log` with event_type filtering and pagination.

**Event types logged:**

| Event type | When | Coverage |
|---|---|---|
| `ingestion` | Start of every `/api/reconcile/run` call, before CSVs are read | Captures intent + failure if CSV read throws |
| `ingestion` (failure) | Immediately on `OSError` during CSV load, before re-raising | Leaves a trace even when the run never completes |
| `reconcile_run` | After pipeline completes successfully | Summary: order/settlement/bank counts, status breakdown |
| `match_decision` | Once per order after classification | status, subtype, composite score, anomaly flags |
| `llm_explanation` | Every `GET /api/exceptions/{id}/explain` call | prompt summary, response, latency, hallucination flag |
| `llm_qa_query` | Every `POST /api/chat` call | question, answer, context_used, latency |

**Storage:** `backend/data/audit_log.db` — SQLite, `audit_log` table. Rows are
INSERT-only; no UPDATE or DELETE is exposed anywhere in the codebase. The
in-memory `list[AuditLogEntry]` in `AppState` is kept for internal callers but
`GET /api/audit-log` reads from SQLite, not the in-memory list.

**Not implemented:** Hash-chaining or any cryptographic tamper-evidence mechanism.
An in-process modification of the SQLite file would not be automatically detected.

**Key files:**
- `backend/app/services/audit_db.py` — `init_db()`, `persist_entry()`, `load_entries()`
- `backend/app/api/audit.py` — `GET /api/audit-log` (reads from SQLite)
- `backend/app/api/state.py` — `add_audit_entry()` appends to memory + calls `persist_entry()`
- `backend/app/main.py` — `_lifespan()` calls `init_db()` at startup

**Status:** `[DONE]` — Chunk 8.


---

## Design Decision Log

> Every deliberate architectural choice is logged here with its rationale.
> This is what makes choices defensible under questioning — not just the conclusion
> but the tradeoffs considered and rejected.

---

### DDL-001 — SQLite over PostgreSQL

**Decision:** Use SQLite for the database.

**Reasoning:**
- No concurrent writers in a single-process hackathon context — SQLite's write-lock
  limitation is irrelevant
- Zero setup cost — no Docker container, no service to start, works out of the box
- File-based — trivially portable and inspectable with any SQLite browser
- Trivially swappable — SQLAlchemy abstracts the dialect; a one-line DSN change
  migrates to PostgreSQL

**Rejected alternatives:**
- PostgreSQL: adds operational overhead (service management) with no benefit at this scale
- In-memory dict: no persistence, no audit trail, not inspectable

---

### DDL-002 — Gemini over other LLM providers

**Decision:** Use Google Gemini API (`google-generativeai` SDK).

**Reasoning:**
- Free tier available without a credit card — zero cost during hackathon development
- `gemini-2.5-flash` provides good reasoning quality at low latency
- Model is configurable via `GEMINI_MODEL` env var — swapping to a different model
  requires no code changes
- Razorpay Buildathon context makes Google AI an appropriate choice

**Rejected alternatives:**
- OpenAI GPT: requires a paid API key or strict credit limits on free tier
- Anthropic Claude: no free tier without credits
- Local models (Ollama): too slow for real-time explanation in a demo context

---

### DDL-003 — LLM never touches matching decisions

**Decision:** The Gemini layer operates exclusively post-hoc on already-classified results.

**Reasoning:**
- LLM outputs are probabilistic and non-deterministic — reconciliation decisions must
  be reproducible and auditable
- A match decision that can't be traced to a specific rule and score can't be defended
  to a merchant disputing a classification
- Keeps the system safe to run at scale: the deterministic engine is O(N log N);
  LLM calls are only triggered for anomalies (estimated 15% of transactions)
- Any latency or availability issue with Gemini degrades UX (explanation missing) but
  never blocks correctness (match result always present)

---

### DDL-004 — Decimal arithmetic for amounts

**Decision:** All monetary comparisons use `decimal.Decimal` with 2dp, not `float`.

**Reasoning:**
- `float` arithmetic on prices produces systematic drift: `0.1 + 0.2 != 0.3` in IEEE 754
- A ₹0.01 rounding difference is a meaningful reconciliation signal; float noise would
  create false positives in the mismatch detector
- `Decimal("17252.99") == Decimal("17252.99")` is reliable; the equivalent float
  comparison is not guaranteed

---

### DDL-005 — Weighted score, not rule cascade

**Decision:** Use a weighted sum of signals rather than a priority-ordered rule cascade.

**Reasoning:**
- Real-world data is rarely clean enough for a hard cascade (e.g., UTR present but
  reformatted, amount correct but date delayed) — a cascade fails on any one missing
  signal
- A weighted score degrades gracefully: a UTR-mismatch + correct amount + correct date
  still produces a high composite score that a human reviewer can understand
- Weights are tunable against ground truth without changing the matching logic
- The score is human-readable in the audit trail: "UTR match: 0.40, amount match: 0.20,
  date OK: 0.10 → total: 0.70 → HARD_MISMATCH"

### DDL-006 — Two fuzzy thresholds: 60 for pair scoring, 90 for phantom detection

**Decision:** The matching engine uses `fuzzy_utr_threshold=60` for within-pair UTR
scoring, but `PHANTOM_DETECTION_THRESHOLD=90` for `detect_unmatched_bank_credits()`.

**Reasoning:**
- All UTRs share the prefix `UTR+YEAR+BANKCODE` (e.g. `UTR2024HDFC...`). At threshold=60,
  `partial_ratio` matches any two UTRs from the same bank because the 12-character shared
  prefix alone scores 60–85 regardless of the unique suffix.
- `partial_ratio=100` on truncated UTRs is the correct within-pair behaviour — a truncated
  UTR is a full prefix match and should score high to account for bank statement noise.
- For phantom detection, what we want to know is: "does this UTR exist in any settlement?"
  This requires a meaningful suffix match, not just a prefix match. Threshold=90 correctly
  distinguishes phantom UTRs (suffix entirely different) from legitimate truncations
  (suffix identical up to the truncation point, so partial_ratio=100).
- Separating the two concerns (pair scoring vs existence detection) prevents false negatives
  in phantom detection while preserving sensitivity to truncation noise in pair scoring.

---

### DDL-007 — Classification thresholds chosen empirically from score distribution

**Decision:** `auto_match_threshold=0.97`, `unresolved_threshold=0.10`, plus a secondary
anomaly override that downgrades AUTO_MATCHED → NEEDS_REVIEW when any anomaly flag is present.

**Methodology:** Threshold sweep over [0.75, 0.80, 0.85, 0.875, 0.90, 0.95, 0.97] × [0.10, 0.35, 0.50]
run against the seed=42 synthetic dataset. The score distribution has a natural gap:
- All 42 clean-match orders score **0.975–1.000** (tight cluster; min=0.975)
- Highest-scoring hard-mismatch: **0.982** (ORD2024046, ₹0.66 rounding diff, T+2)
- Lowest-scoring hard-mismatch: **0.475** (ORD2024051, ₹2999.66 partial refund)
- All 3 failed-payment orders: **0.000**

**Why 0.97 (not 0.975)?**
The natural clean-match minimum is 0.975. Using 0.975 as the threshold would auto-match
ORD2024046 (score=0.982) despite its ₹0.66 amount difference — because the secondary
anomaly override hadn't fired yet during the threshold analysis. Using 0.97 leaves a
buffer below the clean minimum so that any future order scoring between 0.97–0.975
(not present in seed=42) gets the review it deserves. The secondary override (`amount_diff >
₹0.50`) catches ORD2024046 regardless of where the primary threshold sits.

**Why a secondary override instead of only a lower threshold?**
Using only a lower primary threshold (e.g., 0.95) would send 1 clean order into NEEDS_REVIEW
(recall = 41/42 = 97.6%). The secondary override achieves precision=1.0 and recall=1.0 on
clean orders while still flagging ORD2024046. It decouples "high match confidence" from
"zero anomaly" — a distinction that matters for the audit trail.

**Why 0.10 for UNRESOLVED?**
All 3 failed-payment orders score exactly 0.0. Using 0.10 gives generous headroom: a score
of 0.01–0.09 would indicate an extremely weak partial match (e.g., same bank code but
nothing else) — these should be reviewed, not discarded. The gap between 0.10 and 0.475
(lowest NEEDS_REVIEW) means there is no risk of misclassifying a mid-range result.

### DDL-008 — google-genai over google-generativeai

**Decision:** Use the `google-genai` package (v1.47+), not `google-generativeai`.

**Reasoning:**
- `google-generativeai` was officially deprecated in mid-2025 and will no longer
  receive bug fixes or updates. Pip install emits a deprecation warning pointing to
  the migration guide.
- `google-genai` is the officially maintained successor with the same free-tier access.
- The API surface change is minimal: `genai.Client(api_key=...)` and
  `client.models.generate_content(model=..., contents=...)` replace the old
  `genai.configure()` + `GenerativeModel().generate_content()` pattern.
- Switching now avoids a forced migration mid-project when the old package stops working.

**Rejected alternatives:**
- Continue with `google-generativeai`: works today but accumulates technical debt;
  known to emit deprecation warnings that would appear in demo output.

---

### DDL-009 — Free-tier resilience: backoff + cache + fallback

**Decision:** Three-layer resilience strategy for the Gemini free tier.

**Reasoning and implementation:**

1. **Exponential backoff (base 1s, factor 2×, max 32s, 3 retries):**
   The Gemini free tier for `gemini-2.5-flash` allows as few as 10–15 RPM depending
   on current quota allocation. Back-to-back explain calls during a demo will reliably
   hit rate limits. Three retries with backoff handle transient bursts without locking
   up the request thread for more than ~10s (1s + 2s + 4s + processing).

2. **Content-hash in-memory cache (`_explain_cache`):**
   The cache is keyed on the SHA-256 of the serialised diff JSON.  The same exception
   diff re-submitted (e.g., page refresh during a demo) returns the cached response
   with zero latency and zero API calls.  Cache lifetime is process-scoped; a restart
   resets it.  This is sufficient for a demo context — a production deployment would
   use Redis with a TTL.

3. **Graceful degradation (llm_status="fallback"):**
   If all retries are exhausted, `explain_exception()` returns a structured plain-text
   fallback containing the raw diff fields, never an exception.  This is a deliberate
   resilience feature — the reconciliation result (which is deterministic and already
   computed) is never blocked by LLM availability.  The `llm_status` field in the
   response makes it explicit to the caller and frontend whether the explanation is
   live or fallback.

**Why three layers instead of one?**
- Backoff alone: still fails if quota is exhausted for the demo window (minutes, not
  seconds).
- Cache alone: only helps for repeat requests; first call per exception still hits the
  API.
- Fallback alone: abandons quota optimisation, produces a worse user experience when
  the API is actually available.
- Together: backoff handles short bursts, cache eliminates repeats, fallback ensures
  correctness is never sacrificed for explanation quality.

---

*Last updated: Chunk 5 (LLM explain & Q&A layer)*

