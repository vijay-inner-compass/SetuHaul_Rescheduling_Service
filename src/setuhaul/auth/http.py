"""Shared error response for the auth routes and guard."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


def reject(rid: str, status: int, code: str, **extra: Any) -> JSONResponse:
    # Same shape as the rest of the API.
    return JSONResponse({"status": "REJECTED", "reason_code": code, "request_id": rid, **extra}, status_code=status)
