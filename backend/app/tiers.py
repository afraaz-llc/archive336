"""Membership tier registry + helpers.

Single source of truth for the 8 tiers the User model can be in. Any
backend gate that branches on tier should go through ``effective_tier()``
rather than reading ``user.tier`` directly, so any future tier
indirection stays centralized in one place.

See the `project_tier_architecture` memory file for the full design
(commercial vs internal, what each tier gets, worker-pool model, etc.).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import User


# Commercial (paying) tiers. Order is ascending commitment - matches
# the Revenue tab's bucket order.
COMMERCIAL_TIERS: tuple[str, ...] = ("core", "basic", "creator", "studio")

# Internal (non-paying) tiers. These get their own sidebar entries in
# the admin UI and don't appear in Revenue.
INTERNAL_TIERS: tuple[str, ...] = ("partner", "dev", "vip", "admin")

# Every valid tier, used by Pydantic validators to reject typos.
ALL_TIERS: tuple[str, ...] = COMMERCIAL_TIERS + INTERNAL_TIERS


def is_valid_tier(value: str) -> bool:
    return value in ALL_TIERS


# Tiers allowed to OAuth-connect external accounts (Google/YouTube,
# etc.) from the website's Settings → Connected Accounts panel.
# Basic users sync through their own worker app's embedded webview and
# need no OAuth token, so they are not listed here. Internal tiers
# qualify so devs/admins/VIPs can connect for testing.
EXTERNAL_ACCOUNT_TIERS: tuple[str, ...] = (
    "creator",
    "studio",
    "partner",
    "dev",
    "vip",
    "admin",
)


def can_connect_external_accounts(user: "User") -> bool:
    """Whether this user is allowed to OAuth-connect Google/YouTube.

    Reads through ``effective_tier`` so any tier indirection is applied
    consistently with every other gate.
    """
    return effective_tier(user) in EXTERNAL_ACCOUNT_TIERS


def effective_tier(user: "User") -> str:
    """Return the tier the rest of the app should treat this user as.

    Reads ``tier_override`` first if set, falling back to the real
    ``tier`` column. The override column is currently dormant - the
    admin impersonation UI was retired in favor of real test accounts -
    but it's kept as the single hook for any future server-side tier
    override.

    Every product-code gate / branch that depends on tier must call
    this rather than reading ``user.tier`` directly.
    """
    if user.tier_override and is_valid_tier(user.tier_override):
        return user.tier_override
    return user.tier
