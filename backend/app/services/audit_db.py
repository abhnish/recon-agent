"""
audit_db.py
───────────
SQLite persistence layer for the audit log.

Provides three public functions used by the rest of the application:

  init_db()       — create the audit_log table (idempotent, run at startup)
  persist_entry() — write one AuditLogEntry to SQLite
  load_entries()  — read entries for GET /api/audit-log (most-recent-first)

Design choices:
  • stdlib sqlite3 only — no new dependencies.
  • DB file path defaults to backend/data/audit_log.db, overridable via
    AUDIT_DB_PATH env var so tests can redirect to a temp file.
  • check_same_thread=False + a module-level Lock makes it safe under
    uvicorn's default single-worker sync handler.
  • Rows are append-only (INSERT only, no UPDATE/DELETE) — enforced here
    by never exposing an update interface.
  • Schema mirrors AuditLogEntry exactly so no serialisation shim is needed
    at the call site.

⚠️  LLM MATCHING PROHIBITION: This module stores pipeline results.
    It does not interact with the LLM layer for matching decisions.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.llm_layer import AuditLogEntry

logger = logging.getLogger(__name__)

# ── DB path ────────────────────────────────────────────────────────────────────

_DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "data" / "audit_log.db"
_DB_PATH: Path = Path(os.environ.get("AUDIT_DB_PATH", str(_DEFAULT_DB_PATH)))

# ── Thread safety ──────────────────────────────────────────────────────────────

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    """Return (and lazily create) the module-level SQLite connection."""
    global _conn
    if _conn is None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


# ── DDL ────────────────────────────────────────────────────────────────────────

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type              TEXT    NOT NULL,
    order_id                TEXT,
    model_name              TEXT    NOT NULL,
    prompt_summary          TEXT    NOT NULL,
    response_text           TEXT    NOT NULL,
    llm_status              TEXT    NOT NULL,
    latency_ms              INTEGER NOT NULL,
    potential_hallucination INTEGER NOT NULL DEFAULT 0,
    timestamp_utc           TEXT    NOT NULL
);
"""


def init_db() -> None:
    """Create the audit_log table if it does not already exist.

    Idempotent — safe to call on every application startup.
    """
    with _lock:
        conn = _get_conn()
        conn.execute(_CREATE_TABLE_SQL)
        conn.commit()
    logger.info("audit_db: audit_log table ready at %s", _DB_PATH)


# ── Write ──────────────────────────────────────────────────────────────────────

_INSERT_SQL = """
INSERT INTO audit_log (
    event_type, order_id, model_name, prompt_summary, response_text,
    llm_status, latency_ms, potential_hallucination, timestamp_utc
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def persist_entry(entry: AuditLogEntry) -> None:
    """Insert one AuditLogEntry into the SQLite audit_log table.

    Called from AppState.add_audit_entry() so every code path that
    appends to the in-memory list also persists to SQLite.

    Args:
        entry: The AuditLogEntry to persist.
    """
    try:
        with _lock:
            conn = _get_conn()
            conn.execute(
                _INSERT_SQL,
                (
                    entry.event_type.value,
                    entry.order_id,
                    entry.model_name,
                    entry.prompt_summary,
                    entry.response_text,
                    entry.llm_status,
                    entry.latency_ms,
                    int(entry.potential_hallucination),
                    entry.timestamp_utc,
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.error("audit_db: failed to persist entry: %s", exc)


# ── Read ───────────────────────────────────────────────────────────────────────


def load_entries(
    event_type: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Load audit log entries from SQLite, most-recent-first.

    Args:
        event_type: Optional filter on the event_type column.
        limit:      Maximum rows to return.
        offset:     Row offset for pagination.

    Returns:
        A (rows, total_count) tuple where rows is a list of plain dicts
        matching the AuditLogEntry field names (plus the SQLite row id).
    """
    try:
        with _lock:
            conn = _get_conn()

            where = "WHERE event_type = ?" if event_type else ""
            params_count: tuple = (event_type,) if event_type else ()
            params_page: tuple = (
                (event_type, limit, offset) if event_type else (limit, offset)
            )

            total: int = conn.execute(
                f"SELECT COUNT(*) FROM audit_log {where}", params_count
            ).fetchone()[0]

            rows = conn.execute(
                f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                params_page,
            ).fetchall()

        return [dict(r) for r in rows], total

    except Exception as exc:
        logger.error("audit_db: failed to load entries: %s", exc)
        return [], 0
