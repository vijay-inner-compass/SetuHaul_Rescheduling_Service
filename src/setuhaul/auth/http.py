"""Shared error response for the auth routes and guard."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


def reject(rid: str, status: int, code: str, **extra: Any) -> JSONResponse:
    # Same shape as the rest of the API.
    headers = {"Retry-After": str(extra["retry_after_seconds"])} if "retry_after_seconds" in extra else None
    return JSONResponse({"status": "REJECTED", "reason_code": code, "request_id": rid, **extra}, status_code=status, headers=headers)
