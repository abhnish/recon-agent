"""
main.py
───────
ReconAgent — FastAPI application entrypoint.

Wires together all route routers, CORS middleware, request logging,
and the OpenAPI metadata.

CORS is configured for local frontend development at the Vite default
origin (http://localhost:5173).  Extend ``_ALLOWED_ORIGINS`` or override
via the CORS_ORIGINS env var in production.

⚠️  LLM MATCHING PROHIBITION: This module wires routes only.  No matching,
    classification, or LLM calls are made here.  Matching is 100%
    deterministic and occurs only in response to POST /api/reconcile/run.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.audit import router as audit_router
from app.api.chat import router as chat_router
from app.api.exceptions import router as exceptions_router
from app.api.metrics import router as metrics_router
from app.api.middleware import RequestLoggingMiddleware
from app.api.reconcile import router as reconcile_router, _load_csv, _DATA_DIR
from app.api.transactions import router as transactions_router
from app.services.audit_db import init_db
from app.services.normalisation import normalise_order, normalise_settlement, normalise_bank_txn
from app.services.matching import run_matching, detect_duplicate_settlements, detect_duplicate_orders, detect_ambiguous_bank_matches
from app.services.classification import classify_all
from app.services.exception_diff import build_exception_list
from app.services.llm_layer import explain_exception

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)

# ── Startup ───────────────────────────────────────────────────────────────────


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Initialise resources at startup, clean up at shutdown."""
    init_db()   # create SQLite audit_log table if not exists

    logger = logging.getLogger(__name__)
    logger.info("Pre-warming LLM explain cache with demo dataset...")
    try:
        raw_orders = _load_csv(_DATA_DIR / "order_ledger.csv")
        raw_settlements = _load_csv(_DATA_DIR / "settlement_report.csv")
        raw_bank = _load_csv(_DATA_DIR / "bank_statement.csv")
        
        orders = [normalise_order(r) for r in raw_orders]
        settlements = [normalise_settlement(r) for r in raw_settlements]
        bank_txns = [normalise_bank_txn(r) for r in raw_bank]
        
        match_results, _ = run_matching(orders, settlements, bank_txns)
        
        duplicate_settlement_ids = detect_duplicate_settlements(settlements)
        duplicate_ledger_ids = detect_duplicate_orders(orders)
        ambiguous_order_ids = detect_ambiguous_bank_matches(match_results)

        classified = classify_all(
            match_results,
            duplicate_settlement_order_ids=duplicate_settlement_ids,
            duplicate_ledger_order_ids=duplicate_ledger_ids,
            ambiguous_order_ids=ambiguous_order_ids,
        )
        
        exceptions = build_exception_list(classified)
        
        cached_count = 0
        fallback_count = 0
        for exc in exceptions:
            try:
                resp = explain_exception(exc)
                if resp.llm_status in ("ok", "cached"):
                    cached_count += 1
                else:
                    fallback_count += 1
            except Exception as e:
                logger.error(f"Error explaining exception {exc.order_id} during warmup: {e}")
                fallback_count += 1
        
        logger.info(f"LLM cache warmup complete: {cached_count} successfully cached, {fallback_count} fell back.")
    except Exception as e:
        logger.error(f"Failed to pre-warm LLM cache: {e}. Server will continue starting.")

    yield


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    lifespan=_lifespan,
    title="ReconAgent",
    description=(
        "Deterministic payment reconciliation across bank statements, "
        "payment gateway settlements, and internal order ledgers.\n\n"
        "**Architecture guarantee:** The LLM layer (Gemini) is used exclusively "
        "for exception explanation and natural-language Q&A — never for matching "
        "decisions.  All reconciliation outcomes are 100% deterministic and "
        "score-based, making every decision auditable and reproducible.\n\n"
        "**Free-tier resilience:** The LLM layer implements exponential backoff, "
        "content-hash caching, and graceful degradation so rate-limit exhaustion "
        "never blocks a reconciliation result."
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── CORS ──────────────────────────────────────────────────────────────────────

# Vite default dev server port; extend for other frontends as needed.
_ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:3000",
]

# Allow override via env var (comma-separated origins) for staging/prod
_extra = os.environ.get("CORS_ORIGINS", "")
if _extra:
    _ALLOWED_ORIGINS.extend(o.strip() for o in _extra.split(",") if o.strip())

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Request logging ───────────────────────────────────────────────────────────

app.add_middleware(RequestLoggingMiddleware)

# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(reconcile_router)
app.include_router(metrics_router)
app.include_router(transactions_router)
app.include_router(exceptions_router)
app.include_router(chat_router)
app.include_router(audit_router)


# ── Health ────────────────────────────────────────────────────────────────────


@app.get("/health", tags=["system"], summary="Liveness probe")
async def health_check() -> dict:
    """Return service liveness status and version.

    Returns:
        A dict with status and version fields.
    """
    return {"status": "ok", "version": app.version}
