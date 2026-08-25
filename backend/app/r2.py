"""Cloudflare R2 client.

R2 speaks the S3 API, so we use boto3 with R2's endpoint. Credentials and
endpoint are read from the environment (loaded via systemd EnvironmentFile
in production, or .env locally if the user sets it up).

Returns ``None`` if R2 isn't configured — callers can use that to fall back
to local-only behavior in dev.

R2 ops billing (Phase B):
    Every public helper that touches R2 takes a keyword-only ``subject``
    argument naming the billing target — either a real ``users.id`` or
    the ``ops_ledger.PLATFORM`` sentinel for platform-fixed ops
    (Litestream, cron, admin tooling). The helper records the count
    of Class A / Class B operations it just issued against R2 via
    ``ops_ledger.record_op``, in a separate DB transaction so a failing
    record never poisons the caller's transaction and a failing caller
    never loses ops data.

    Free ops (DeleteObject, DeleteBucket, AbortMultipartUpload) are NOT
    recorded — Cloudflare doesn't bill them. ``delete_keys`` is therefore
    free of subject-tracking too. See docs/CLOUDFLARE_AUDIT.md §2 for
    the full S3 → Class A/B/free mapping.

    Presigned URLs (presign_get / presign_put) are handled in Phase C
    of the R2 ops billing redesign — see the docstrings on those
    functions for the deferred status.
"""
from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import boto3
import requests
from boto3.s3.transfer import TransferConfig
from botocore.config import Config


log = logging.getLogger("archive336.r2")

_client = None
_bucket: Optional[str] = None

# Multipart upload chunking — see Phase F of the R2 ops billing
# redesign. boto3.upload_file splits any file ≥ THRESHOLD into chunks
# of CHUNKSIZE and issues:
#   1 × CreateMultipartUpload (Class A)
#   N × UploadPart            (Class A, one per chunk)
#   1 × CompleteMultipartUpload (Class A)
# Files below the threshold are single PutObject = 1 Class A.
#
# boto3's defaults are 8 MiB for both, which would cost ~128 Class A
# ops per GB uploaded. We bump to 64 MiB which costs ~16 ops/GB
# (8× fewer ops, same wall-clock since R2's upload throughput is the
# bottleneck, not the request rate). The same constants are used to
# estimate the op count for the ledger so the recorded charge matches
# what Cloudflare actually bills.
#
# R2 hard limits (for reference, we don't exceed them):
#   - Max parts per multipart upload: 10,000
#   - Min part size: 5 MiB (except the last part)
#   - Max object size: 5 TiB
# At 64 MiB/part the max single-object upload is 640 GiB, comfortable
# above any video file we'd archive.
_MULTIPART_THRESHOLD_BYTES = 64 * 1024 * 1024
_MULTIPART_CHUNKSIZE_BYTES = 64 * 1024 * 1024

# Shared TransferConfig instance passed into every upload_file call so
# boto3 actually honors the constants above. Concurrency stays at the
# boto3 default (10) — fine for our throughput, and changing it
# wouldn't affect op counts.
_TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=_MULTIPART_THRESHOLD_BYTES,
    multipart_chunksize=_MULTIPART_CHUNKSIZE_BYTES,
)


# ---- Metadata size accounting --------------------------------------------
# Cloudflare R2 bills storage on (bytes + metadata_bytes) × hours. The
# "metadata_bytes" portion is the size of the user-defined headers stored
# with each object plus a small implicit per-object overhead R2 keeps
# (ETag, LastModified, ContentLength, internal versioning markers).
#
# Cloudflare doesn't publish the exact byte accounting, but observed
# billing rounds to roughly:
#   per-object implicit overhead  ~80 bytes
#   each user-set header           name_bytes + value_bytes
#
# This is best-effort precision based on what we send + a conservative
# implicit constant. If R2's real accounting includes a few more bytes
# per object that we can't see, the difference is sub-cent and the
# platform absorbs it. Was 256 bytes flat before this — over-billed by
# ~100-150 bytes per object as platform-protective. Now precise.
METADATA_OBJECT_OVERHEAD_BYTES = 80


def metadata_bytes_for(
    content_type: Optional[str] = None,
    custom_meta: Optional[Dict[str, str]] = None,
) -> int:
    """Compute the metadata header size R2 stores + bills for this object.

    Sums:
      - METADATA_OBJECT_OVERHEAD_BYTES (R2-internal stuff we can't see)
      - Content-Type header name (12) + value bytes (if set)
      - Each x-amz-meta-* header (12 + key length) + value bytes
    """
    size = METADATA_OBJECT_OVERHEAD_BYTES
    if content_type:
        size += len("Content-Type") + len(content_type)
    if custom_meta:
        for k, v in custom_meta.items():
            size += len("x-amz-meta-") + len(k) + len(v)
    return size


def _load() -> None:
    global _client, _bucket
    if _client is not None:
        return

    # Object storage is S3-compatible, so the provider is swappable: Cloudflare
    # R2 today, Backblaze B2 after the storage migration. Prefer the
    # provider-neutral STORAGE_* vars and fall back to the legacy R2_* names so
    # nothing changes until STORAGE_* is set at cutover. The DMARC bucket and
    # Litestream backups keep their own R2_* / R2_BACKUP_* creds, so this only
    # moves the video/thumbnail bucket when STORAGE_* points at B2.
    endpoint = os.environ.get("STORAGE_ENDPOINT") or os.environ.get("R2_ENDPOINT")
    key = os.environ.get("STORAGE_ACCESS_KEY_ID") or os.environ.get("R2_ACCESS_KEY_ID")
    secret = os.environ.get("STORAGE_SECRET_ACCESS_KEY") or os.environ.get(
        "R2_SECRET_ACCESS_KEY"
    )
    bucket = os.environ.get("STORAGE_BUCKET") or os.environ.get("R2_BUCKET")
    # R2 ignores the SigV4 region ("auto"); B2 signs against its real region
    # (e.g. us-west-004), so make it overridable with an R2-safe default.
    region = os.environ.get("STORAGE_REGION") or "auto"

    if not (endpoint and key and secret and bucket):
        return

    _client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        region_name=region,
        config=Config(signature_version="s3v4"),
    )
    _bucket = bucket


def client():
    """Return the boto3 S3 client, or None if object storage isn't configured."""
    _load()
    return _client


def bucket() -> Optional[str]:
    """Return the object-storage bucket name, or None if not configured."""
    _load()
    return _bucket


def _estimate_class_a_ops_for_upload(file_size_bytes: int) -> int:
    """How many Class A ops boto3.upload_file will issue for this size.

    Single PutObject when below ``_MULTIPART_THRESHOLD_BYTES``,
    otherwise CreateMultipartUpload + N × UploadPart + CompleteMultipartUpload.
    See module docstring for the assumptions.
    """
    if file_size_bytes < _MULTIPART_THRESHOLD_BYTES:
        return 1
    parts = math.ceil(file_size_bytes / _MULTIPART_CHUNKSIZE_BYTES)
    return parts + 2  # Create + N parts + Complete


def _record(subject: str, op_class: str, count: int = 1) -> None:
    """Record ``count`` ops of ``op_class`` against ``subject`` on the
    configured bucket. Uses a fresh DB session so a caller's
    transaction (or lack of one) doesn't affect billing data.

    Best-effort: any failure is logged and swallowed. ``ops_ledger.record_op``
    already swallows its own DB errors; this wrapper additionally guards
    against import / session-construction failures.
    """
    if _bucket is None:
        return  # R2 not configured; nothing to bill against.
    try:
        from app import ops_ledger
        from app.db import SessionLocal
    except Exception:
        log.exception("r2._record: import failed; op not recorded")
        return
    db = SessionLocal()
    try:
        ops_ledger.record_op(
            db, subject=subject, bucket=_bucket, op_class=op_class, count=count
        )
    finally:
        try:
            db.close()
        except Exception:
            pass


def upload_file(
    local_path: str,
    key: str,
    content_type: Optional[str] = None,
    *,
    subject: str,
) -> int:
    """Upload a local file to R2 at the given key. Raises if R2 isn't configured.

    Records Class A ops against ``subject`` based on the file size and
    boto3's multipart thresholds (see ``_estimate_class_a_ops_for_upload``).
    Pass ``ops_ledger.PLATFORM`` for platform-fixed uploads (admin
    tooling), or a real ``users.id`` for user-driven uploads (worker
    pulling a video, etc.).

    Returns the metadata_bytes R2 will bill for this object's headers
    (Content-Type + implicit overhead). Pass to storage_ledger.record_object.
    """
    c = client()
    if c is None or _bucket is None:
        raise RuntimeError("R2 is not configured (missing env vars)")
    extra = {"ContentType": content_type} if content_type else None
    c.upload_file(
        local_path, _bucket, key, ExtraArgs=extra, Config=_TRANSFER_CONFIG
    )
    # File size determines whether boto3 went multipart and how many parts.
    try:
        file_size = os.path.getsize(local_path)
    except OSError:
        file_size = 0  # Defensive — should never happen post-successful-upload.
    _record(subject, "A", _estimate_class_a_ops_for_upload(file_size))
    return metadata_bytes_for(content_type=content_type)


def download_to_r2(
    source_url: str,
    key: str,
    content_type: Optional[str] = None,
    timeout_seconds: float = 30.0,
    *,
    subject: str,
) -> "Tuple[int, int]":
    """Fetch the bytes at ``source_url`` and upload them to R2 at ``key``.

    Returns ``(bytes_uploaded, metadata_bytes)`` — pass both into
    storage_ledger.record_object so the byte-hour billing math has
    real metadata size instead of a 256-byte over-estimate.

    Used to archive YouTube CDN images (per-video thumbnails, channel
    avatars) so the bytes survive YouTube channel changes/deletions.
    Raises requests.HTTPError on a non-2xx fetch and RuntimeError if
    R2 isn't configured. Caller is responsible for retry/backoff.

    Always issues a single PutObject (no multipart) since we hold the
    bytes in memory — that's 1 Class A op, recorded against ``subject``.
    """
    c = client()
    if c is None or _bucket is None:
        raise RuntimeError("R2 is not configured (missing env vars)")

    resp = requests.get(source_url, timeout=timeout_seconds)
    resp.raise_for_status()
    data = resp.content

    # If the caller didn't pin a content-type, fall back to the response's
    # Content-Type header, then to a sensible default (image/jpeg).
    ct = (
        content_type
        or resp.headers.get("Content-Type")
        or "image/jpeg"
    ).split(";")[0].strip()

    c.put_object(Bucket=_bucket, Key=key, Body=data, ContentType=ct)
    _record(subject, "A", 1)
    return len(data), metadata_bytes_for(content_type=ct)


def presign_get(
    key: str,
    expires_in: int = 300,
    filename: Optional[str] = None,
    content_type: Optional[str] = None,
    *,
    subject: str,
    proxy: bool = False,
) -> str:
    """Return a signed URL the user can fetch directly from R2.

    OPS BILLING (Phase C): the actual GET happens client-side via the
    signed URL — boto3 never sees the result, so we can't record at op
    time the way ``head()`` does. Instead we optimistically record
    +1 Class B against ``subject`` at presign time. This will OVER-count
    by whatever fraction of presigned URLs the user abandons without
    fetching (typical browser flow: user clicks Download, decides not
    to save the file → minted URL, no GET). Phase E's reconciliation
    against Cloudflare's per-bucket totals will surface the drift, and
    if it's material we'll switch to a client-pings-us-after-fetch
    pattern. Until then: optimistic.

    If ``filename`` is set we bake an attachment Content-Disposition
    into the URL so the browser saves the file with that name (rather
    than the raw R2 object key like 'video.mp4', which is the default).
    Same trick we use for the download-parts flow - the bytes still go
    straight from R2 to the user, but the suggested save name is the
    video's actual title.
    """
    c = client()
    if c is None or _bucket is None:
        raise RuntimeError("R2 is not configured (missing env vars)")
    params: Dict[str, Any] = {"Bucket": _bucket, "Key": key}
    if filename:
        # Quote the filename so embedded quotes/semicolons don't break
        # the header. Stick to ASCII for max browser compatibility -
        # the caller is expected to have already sanitized via
        # _sanitize_filename or similar.
        params["ResponseContentDisposition"] = (
            f'attachment; filename="{filename}"'
        )
    if content_type:
        params["ResponseContentType"] = content_type
    url = c.generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=expires_in,
    )
    _record(subject, "B", 1)
    # For large downloads (video files), optionally route the client through the
    # Cloudflare egress proxy instead of straight to B2: same presigned path +
    # query (the SigV4 signature signs `host`, which the proxy restores when it
    # forwards to B2), only the host swapped to STORAGE_PROXY_HOST. B2 ->
    # Cloudflare -> client is free egress (Bandwidth Alliance). If the proxy
    # host isn't configured we fall back to the direct B2 URL, so this is safe
    # to ship before the Worker / DNS exist.
    if proxy:
        # NOTE: the Worker at workers/b2-egress-proxy rewrites this host
        # back to the real B2 endpoint before forwarding, and SigV4 signs
        # Host - so its B2_HOST var (wrangler.jsonc) must match whatever
        # STORAGE_ENDPOINT points at here. Changing region/bucket/provider
        # without redeploying that Worker 403s every proxied download while
        # direct URLs keep working. Change both together.
        proxy_host = os.environ.get("STORAGE_PROXY_HOST")
        if proxy_host:
            parts = urlsplit(url)
            url = urlunsplit(("https", proxy_host, parts.path, parts.query, ""))
    return url


def presign_put(
    key: str,
    expires_in: int = 3600,
    content_type: Optional[str] = None,
    *,
    subject: str,
) -> str:
    """Return a signed URL the client can PUT directly to R2.

    OPS BILLING (Phase C): same shape as ``presign_get`` — we record
    +1 Class A against ``subject`` at presign time, optimistically.
    A presigned PUT URL that the client never uses still consumes a
    counter row here; reconciliation surfaces the drift.

    Used by worker clients to upload .mp4 files without proxying bytes
    through our origin (free egress + lower latency for the client).
    Default TTL is 1 hour, which should be plenty for a single video.
    """
    c = client()
    if c is None or _bucket is None:
        raise RuntimeError("R2 is not configured (missing env vars)")
    params = {"Bucket": _bucket, "Key": key}
    if content_type:
        params["ContentType"] = content_type
    url = c.generate_presigned_url(
        "put_object",
        Params=params,
        ExpiresIn=expires_in,
    )
    _record(subject, "A", 1)
    return url


def bucket_stats(*, subject: str) -> Optional[dict]:
    """Walk the bucket and return total object count + total bytes.

    Used by /api/admin/system to surface what R2 actually thinks is
    stored, separate from what our DB thinks. Drift between the two
    is a useful signal — orphaned objects (in R2 but no DB row) cost
    money silently; missing objects (DB row but no R2) mean a broken
    upload that should retry.

    Each ListObjectsV2 page is one Class A op (LIST is Class A and
    paginates at 1000 keys per request), counted against ``subject``.
    Admin tooling that calls this should pass ``ops_ledger.PLATFORM``
    — listing the bucket is platform infrastructure, never user-driven.

    Returns None if R2 isn't configured. Cheap at hundreds of objects;
    if the bucket grows past tens of thousands, switch to a cached
    nightly walk.
    """
    c = client()
    if c is None or _bucket is None:
        return None

    paginator = c.get_paginator("list_objects_v2")
    total_bytes = 0
    total_objects = 0
    pages = 0
    for page in paginator.paginate(Bucket=_bucket):
        pages += 1
        for obj in page.get("Contents", []) or []:
            total_objects += 1
            total_bytes += int(obj.get("Size", 0))

    _record(subject, "A", pages)

    return {
        "objects": total_objects,
        "bytes": total_bytes,
    }


def delete_keys(keys: list[str]) -> int:
    """Delete the given keys from R2. Batches per S3's 1000-key limit
    on delete_objects. Returns the count actually requested for delete
    (not necessarily the count that existed). Silently skips if R2
    isn't configured - caller should treat that as 'no R2 cleanup
    needed' rather than an error.

    OPS BILLING: DeleteObject / DeleteObjects are FREE on R2 — see
    docs/CLOUDFLARE_AUDIT.md §2 — so we don't take a subject and
    don't record anything here. Calling this is always free.
    """
    c = client()
    if c is None or _bucket is None or not keys:
        return 0
    deleted = 0
    for i in range(0, len(keys), 1000):
        chunk = keys[i : i + 1000]
        c.delete_objects(
            Bucket=_bucket,
            Delete={
                "Objects": [{"Key": k} for k in chunk],
                "Quiet": True,
            },
        )
        deleted += len(chunk)
    return deleted


def head(key: str, *, subject: str) -> Optional[dict]:
    """Return the HEAD metadata for a key, or None if missing.

    HeadObject is one Class B op, recorded against ``subject``. 404s
    are also billed (Cloudflare bills HEAD regardless of result), so
    the record happens whether or not the object exists.
    """
    c = client()
    if c is None or _bucket is None:
        raise RuntimeError("R2 is not configured (missing env vars)")
    try:
        result = c.head_object(Bucket=_bucket, Key=key)
        _record(subject, "B", 1)
        return result
    except c.exceptions.ClientError as e:
        _record(subject, "B", 1)
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
