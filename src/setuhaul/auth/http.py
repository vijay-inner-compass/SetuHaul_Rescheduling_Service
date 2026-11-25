"""Shared error response for the auth routes and guard."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from fastapi.responses import JSONResponse


def reject(rid: str, status: int, code: str) -> JSONResponse:
    # Same shape as the rest of the API.
    return JSONResponse({"status": "REJECTED", "reason_code": code, "request_id": rid}, status_code=status)
