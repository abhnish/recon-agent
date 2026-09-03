"""
middleware.py
─────────────
Request logging middleware for ReconAgent.

Logs method, path, status code, and latency for every request.
Uses Python's standard logging module so the output integrates with uvicorn's
log stream — no third-party logging libraries required.
"""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("recon_agent.access")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware that logs method, path, status code, and latency for every request.

    Log format:
        METHOD /path → STATUS  latency_ms ms

    Uses INFO level so it is visible in default uvicorn output.
    """

    async def dispatch(self, request: Request, call_next: object) -> Response:
        """Process the request, log the result, and return the response.

        Args:
            request:   The incoming Starlette request.
            call_next: The next middleware or route handler.

        Returns:
            The response from the next handler.
        """
        t0 = time.perf_counter()
        response: Response = await call_next(request)  # type: ignore[operator]
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        logger.info(
            "%s %s → %s  %dms",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response
