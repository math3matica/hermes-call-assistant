"""Dashboard API for the Call Assistant access-control page.

Mounted by Hermes at /api/plugins/hermes-call-assistant/. The dashboard's
session-token middleware protects these routes; this module adds the second
boundary: only existing directories are accepted and the settings module owns
all persistence and validation.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from rex_voice_v1.settings import grant_root, inspect_settings, revoke_root

router = APIRouter()


class AccessGrant(BaseModel):
    path: str = Field(min_length=1)
    kind: str = Field(default="notes", pattern="^(notes|prepared)$")
    root_id: str | None = None


def _error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/settings")
def settings() -> dict[str, Any]:
    try:
        return {"status": "ok", **inspect_settings()}
    except (OSError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/access")
def add_access(payload: AccessGrant) -> dict[str, Any]:
    try:
        entry = grant_root(payload.path, kind=payload.kind, root_id=payload.root_id)
        return {"status": "ok", "grant": entry, "settings": inspect_settings()}
    except (OSError, ValueError) as exc:
        raise _error(exc) from exc


@router.delete("/access/{kind}/{root_id}")
def remove_access(kind: str, root_id: str) -> dict[str, Any]:
    try:
        result = revoke_root(root_id, kind=kind)
        return {"status": "ok", "result": result, "settings": inspect_settings()}
    except (OSError, ValueError) as exc:
        raise _error(exc) from exc
