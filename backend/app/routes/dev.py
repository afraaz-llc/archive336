"""Developer-only endpoints. Admin-gated. Mounted at /api/dev.

Everything in here exists to support the website's /dev page - the
admin-only testing surface (Payment status simulator, future internal
utilities). Never user-facing.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.tiers import ALL_TIERS


router = APIRouter()


@router.get("/tiers")
def list_tiers() -> dict:
    """Return the canonical list of all 8 tiers so the Dev page
    dropdown is wired to the same source of truth as the backend."""
    return {
        "all": list(ALL_TIERS),
        # Convenience split for grouping the dropdown.
        "commercial": ["core", "basic", "creator", "studio"],
        "internal": ["partner", "dev", "vip", "admin"],
    }
