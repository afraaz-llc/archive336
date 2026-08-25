"""Daily Hetzner egress check — email when outbound usage crosses 50%.

Independent tripwire for the Hetzner billing-period bandwidth cap.
Runs once per day via the archive336-hetzner-bandwidth.timer systemd unit
so we hear about a runaway user / leak before the overage compounds.
Threshold + email plumbing live in app.alerts and app.email
respectively; this script just polls and delegates.

Behavior:
  - Polls Hetzner Cloud API for the current server's outgoing_traffic
    and included_traffic using HCLOUD_TOKEN.
  - If used >= alerts.HETZNER_BANDWIDTH_WARNING_PCT of the included
    cap and we haven't already alerted this calendar month (file-
    marker dedup), sends the admin an email via
    app.email.send_hetzner_bandwidth_warning.
  - Logs every run to stderr — systemd journal is the audit trail.

Exit codes:
  0  - ran cleanly (alert fired OR under threshold OR already alerted)
  1  - config missing (no HCLOUD_TOKEN) or Hetzner API call failed.
       Timer re-fires tomorrow.

Usage:
    /opt/aether/venv/bin/python -m scripts.check_hetzner_bandwidth
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Optional, Tuple


log = logging.getLogger("aether.check_hetzner_bandwidth")


HETZNER_SERVER_ID = 128288947


def _fetch_bandwidth(token: str) -> Tuple[int, int]:
    """Hit Hetzner Cloud API and return (outgoing_bytes,
    included_bytes) for the current billing period. Raises on any
    HTTP / parse failure so the caller exits 1 and the timer retries.
    """
    import requests

    resp = requests.get(
        f"https://api.hetzner.cloud/v1/servers/{HETZNER_SERVER_ID}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    resp.raise_for_status()
    server = resp.json().get("server") or {}
    return (
        int(server.get("outgoing_traffic") or 0),
        int(server.get("included_traffic") or 0),
    )


def main() -> int:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )

    from app import alerts

    token = os.environ.get("HCLOUD_TOKEN")
    if not token:
        log.error(
            "HCLOUD_TOKEN missing from env; cannot poll Hetzner. "
            "Set it in /opt/aether/.env and retry."
        )
        return 1

    try:
        used, included = _fetch_bandwidth(token)
    except Exception:
        log.exception("hetzner bandwidth fetch failed")
        return 1

    if included <= 0:
        log.warning("hetzner included_traffic is 0; cannot compute pct")
        return 0

    pct = used / included * 100
    log.info(
        "hetzner egress: %.1f%% (%d / %d bytes, threshold %.1f%%)",
        pct,
        used,
        included,
        alerts.HETZNER_BANDWIDTH_WARNING_PCT,
    )

    if pct < alerts.HETZNER_BANDWIDTH_WARNING_PCT:
        log.info("under threshold; nothing to alert")
        return 0

    sent = alerts.maybe_send_hetzner_bandwidth_alert(used, included)
    if sent:
        log.info("alert email sent")
    else:
        log.info("alert NOT sent (already alerted this month, or send failed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
