"""Summarize captured DMARC aggregate reports from R2.

The aether-dmarc-ingest Cloudflare Email Worker stores each inbound DMARC
report's raw .eml to the R2 bucket (default: aether-dmarc-reports) under
the dmarc/ prefix. This script reads the most recent ones, unpacks the
zipped/gzipped XML, and prints a scannable pass/fail summary so we never
have to read raw DMARC XML by hand.

What matters in a report: each <record> is a group of messages from one
source IP claiming to be from our domain. DMARC "passes" for that group
when the *aligned* DKIM or SPF result is pass. Anything failing is either
a misconfigured legit sender (fix it before tightening the policy) or
someone spoofing the domain.

Usage (on the prod box, R2_* already in env):
    set -a; . /opt/aether/.env; set +a
    /opt/aether/venv/bin/python -m scripts.dmarc_summary            # latest 5
    /opt/aether/venv/bin/python -m scripts.dmarc_summary --limit 20
    /opt/aether/venv/bin/python -m scripts.dmarc_summary --fails-only
"""
from __future__ import annotations

import gzip
import io
import os
import sys
import zipfile
from datetime import datetime, timezone
from email import message_from_bytes
from xml.etree import ElementTree as ET


DMARC_BUCKET = os.environ.get("DMARC_R2_BUCKET", "aether-dmarc-reports")
PREFIX = "dmarc/"


def _client():
    # Lazy import so the parsing helpers below stay testable without boto3.
    import boto3
    from botocore.config import Config

    endpoint = os.environ.get("R2_ENDPOINT")
    key = os.environ.get("R2_ACCESS_KEY_ID")
    secret = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not (endpoint and key and secret):
        print(
            "R2 not configured (need R2_ENDPOINT / R2_ACCESS_KEY_ID / "
            "R2_SECRET_ACCESS_KEY in env).",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def _extract_xml(raw_eml: bytes) -> bytes | None:
    """Pull the DMARC XML out of a raw report email.

    The report is an attachment, usually application/zip or application/
    gzip but sometimes octet-stream — so we sniff magic bytes rather than
    trust the declared content-type.
    """
    msg = message_from_bytes(raw_eml)
    for part in msg.walk():
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        # zip: "PK\x03\x04"
        if payload[:4] == b"PK\x03\x04":
            try:
                with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                    name = zf.namelist()[0]
                    return zf.read(name)
            except Exception:
                continue
        # gzip: 0x1f 0x8b
        if payload[:2] == b"\x1f\x8b":
            try:
                return gzip.decompress(payload)
            except Exception:
                continue
        # bare XML fallback
        if payload.lstrip()[:5].lower() == b"<?xml" or payload.lstrip()[:9] == b"<feedback":
            return payload
    return None


def _txt(node, path: str, default: str = "") -> str:
    el = node.find(path)
    return el.text.strip() if (el is not None and el.text) else default


def _fmt_epoch(s: str) -> str:
    try:
        return datetime.fromtimestamp(int(s), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s or "?"


def summarize_report(xml_bytes: bytes, fails_only: bool) -> tuple[int, int]:
    """Print one report's summary. Returns (pass_msgs, fail_msgs)."""
    root = ET.fromstring(xml_bytes)
    org = _txt(root, "report_metadata/org_name", "?")
    rid = _txt(root, "report_metadata/report_id", "?")
    begin = _fmt_epoch(_txt(root, "report_metadata/date_range/begin"))
    end = _fmt_epoch(_txt(root, "report_metadata/date_range/end"))
    domain = _txt(root, "policy_published/domain", "?")
    policy = _txt(root, "policy_published/p", "?")

    rows = []
    pass_msgs = 0
    fail_msgs = 0
    for rec in root.findall("record"):
        ip = _txt(rec, "row/source_ip", "?")
        count = int(_txt(rec, "row/count", "0") or 0)
        disp = _txt(rec, "row/policy_evaluated/disposition", "?")
        dkim = _txt(rec, "row/policy_evaluated/dkim", "?")
        spf = _txt(rec, "row/policy_evaluated/spf", "?")
        hfrom = _txt(rec, "identifiers/header_from", "?")
        ok = dkim == "pass" or spf == "pass"
        if ok:
            pass_msgs += count
        else:
            fail_msgs += count
        rows.append((ok, ip, count, disp, dkim, spf, hfrom))

    if fails_only and fail_msgs == 0:
        return pass_msgs, fail_msgs

    print(f"\n── {org}  ·  {begin} → {end}  ·  report {rid}")
    print(f"   domain={domain} published-policy=p={policy}")
    for ok, ip, count, disp, dkim, spf, hfrom in rows:
        if fails_only and ok:
            continue
        mark = "PASS" if ok else "FAIL"
        print(
            f"   [{mark}] {ip:<39} x{count:<4} from={hfrom:<28} "
            f"dkim={dkim} spf={spf} disposition={disp}"
        )
    return pass_msgs, fail_msgs


def main(argv: list[str]) -> int:
    limit = 5
    fails_only = "--fails-only" in argv
    if "--limit" in argv:
        try:
            limit = int(argv[argv.index("--limit") + 1])
        except (ValueError, IndexError):
            print("--limit needs a number", file=sys.stderr)
            return 2

    c = _client()
    paginator = c.get_paginator("list_objects_v2")
    objs = []
    for page in paginator.paginate(Bucket=DMARC_BUCKET, Prefix=PREFIX):
        objs.extend(page.get("Contents") or [])
    if not objs:
        print(f"No reports in r2://{DMARC_BUCKET}/{PREFIX} yet.")
        return 0

    objs.sort(key=lambda o: o["LastModified"], reverse=True)
    objs = objs[:limit]
    print(f"Newest {len(objs)} of the captured reports in r2://{DMARC_BUCKET}/{PREFIX}:")

    total_pass = 0
    total_fail = 0
    parsed = 0
    for o in objs:
        body = c.get_object(Bucket=DMARC_BUCKET, Key=o["Key"])["Body"].read()
        xml = _extract_xml(body)
        if xml is None:
            print(f"\n── (could not extract XML from {o['Key']})")
            continue
        p, f = summarize_report(xml, fails_only)
        total_pass += p
        total_fail += f
        parsed += 1

    print(
        f"\n═══ {parsed} report(s): {total_pass} message(s) passed DMARC, "
        f"{total_fail} failed ═══"
    )
    if total_fail:
        print("    ↑ failing sources are misconfigured legit senders or spoofers — worth a look.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
