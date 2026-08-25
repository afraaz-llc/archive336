"""Mercury Bank API integration — admin Stack tab.

Reads the live ARCHIVE336 checking-account state via Mercury's
read-only API. The user has multiple Mercury accounts across personal
and project contexts; we pin to the one ARCHIVE336-owned account by UUID
so unrelated balances never leak into the admin UI.

Env vars:
    MERCURY_API_KEY — bearer token from Mercury → Settings → Banking →
                      API tokens. Read-only scope is enough.

API key is optional. When unset, ``admin_mercury_snapshot`` returns
a "not configured" sentinel that the frontend renders as "—" with a
helpful hint card.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import requests


log = logging.getLogger("archive336.mercury")


_BASE = "https://backend.mercury.com/api/v1"
_TIMEOUT = 8  # seconds — Mercury's API isn't latency-critical, but keep
              # it short so the admin page degrades fast rather than
              # blocking. service-health pattern from cloudflare.py.

# The single ARCHIVE336-owned Mercury account. The user has other Mercury
# accounts for unrelated projects; pinning to this UUID keeps those
# out of the admin response payload entirely. ID copied from the
# dashboard URL: app.mercury.com/accounts/depository/<uuid>.
# Read from env, not hardcoded. This is a bank account identifier and
# it was reaching the public JS bundle through the admin page, which is
# a lazy-loaded chunk rather than a protected one.
ARCHIVE336_ACCOUNT_ID = os.environ.get("MERCURY_ACCOUNT_ID", "")


def _token() -> Optional[str]:
    return os.environ.get("MERCURY_API_KEY") or None


def _headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {_token()}"}


def admin_mercury_snapshot() -> Dict[str, Any]:
    """Pull the live ARCHIVE336 Mercury account state for the AccountBox.

    Response shape:
        configured: bool — false if MERCURY_API_KEY isn't set
        account: dict | None — balance, kind, last4, routing, etc.
                               Full account number is dropped before
                               returning so it never reaches the JSON
                               that ships to the browser.
        recentTransactions: list — up to 5 most recent transactions
        errors: list[str] — non-fatal per-endpoint fetch errors
    """
    if not _token():
        return {
            "configured": False,
            "account": None,
            "recentTransactions": [],
            "errors": [],
        }

    errors: List[str] = []
    account: Optional[Dict[str, Any]] = None
    transactions: List[Dict[str, Any]] = []

    # 1) Account info
    try:
        r = requests.get(
            f"{_BASE}/account/{ARCHIVE336_ACCOUNT_ID}",
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        if r.status_code in (401, 403):
            errors.append(
                f"auth failed ({r.status_code}) — check MERCURY_API_KEY scope"
            )
        elif not r.ok:
            errors.append(f"account: HTTP {r.status_code}")
        else:
            raw = r.json()
            full_account_number = raw.get("accountNumber") or ""
            account = {
                "name": raw.get("name"),
                "nickname": raw.get("nickname"),
                "kind": raw.get("kind"),
                "status": raw.get("status"),
                "currentBalance": raw.get("currentBalance"),
                "availableBalance": raw.get("availableBalance"),
                "routingNumber": raw.get("routingNumber"),
                "last4": (
                    full_account_number[-4:] if full_account_number else None
                ),
            }
    except requests.RequestException as e:
        errors.append(f"account: {e}")
        log.warning("Mercury account fetch failed: %s", e)

    # 2) Recent transactions (last 5). On 401/403 we already reported
    # the auth issue above; on other failures just degrade silently.
    try:
        r = requests.get(
            f"{_BASE}/account/{ARCHIVE336_ACCOUNT_ID}/transactions",
            headers=_headers(),
            params={"limit": 5},
            timeout=_TIMEOUT,
        )
        if r.ok:
            raw_txs = r.json().get("transactions", [])
            for t in raw_txs[:5]:
                transactions.append({
                    "id": t.get("id"),
                    "amount": t.get("amount"),
                    "status": t.get("status"),
                    "createdAt": t.get("createdAt"),
                    "kind": t.get("kind"),
                    "counterparty": t.get("counterpartyName"),
                    "note": t.get("note"),
                })
        elif r.status_code in (401, 403):
            pass  # already noted in the account call
        else:
            errors.append(f"transactions: HTTP {r.status_code}")
    except requests.RequestException as e:
        errors.append(f"transactions: {e}")
        log.warning("Mercury transactions fetch failed: %s", e)

    return {
        "configured": True,
        "account": account,
        "recentTransactions": transactions,
        "errors": errors,
    }
