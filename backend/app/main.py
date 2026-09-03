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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.audit import router as audit_router
from app.api.chat import router as chat_router
from app.api.exceptions import router as exceptions_router
from app.api.metrics import router as metrics_router
from app.api.middleware import RequestLoggingMiddleware
from app.api.reconcile import router as reconcile_router
from app.api.transactions import router as transactions_router
from app.services.audit_db import init_db

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
