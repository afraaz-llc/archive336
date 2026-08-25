from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    email: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    # Email verification. False until the user clicks the link we
    # email on signup. Existing users from before this column existed
    # are grandfathered in via the ALTER TABLE default.
    email_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    # Admin role. Gates /api/admin/* endpoints and the /admin UI.
    # Set manually via SQL; we don't expose a "promote user" endpoint
    # (would itself need an admin to call it, infinite-regress risk).
    is_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    # Billing — managed by Stripe. payment_status drives the 402 gate:
    #   none      — never added a card. Can browse, can't sync/import.
    #   active    — has a working card. Full access.
    #   past_due  — last invoice payment failed; given grace period to update card.
    #   canceled  — user explicitly removed their card / closed account.
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, unique=True, index=True
    )
    payment_status: Mapped[str] = mapped_column(
        String, default="none", nullable=False, index=True
    )
    last_billed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set when the user cancels their plan. Doubles as the grace clock:
    # all their channels are moved into the 30-day removed-grace window at
    # this instant (billing stops), and resubscribing before the purge
    # restores channels whose removed_at >= this AND back-charges the
    # storage held during the grace window. NULL = not canceled.
    plan_canceled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Per-user override for the storage cost markup applied at billing
    # time. NULL = use the default (2.0×, defined in app.billing). Lets
    # admins do per-customer pricing (volume deals, penalty rates, A/B
    # tiered pricing) without touching code. See
    # docs/STORAGE_BILLING_DESIGN.md.
    storage_cost_multiplier_override: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )

    # Membership tier - drives which features the user sees and which
    # billing math runs. Valid values are in app/tiers.py. Default
    # basic, which is the only tier wired today.
    tier: Mapped[str] = mapped_column(
        String, default="basic", nullable=False, index=True
    )
    # Admin-only impersonation: when set, effective_tier returns this
    # instead of `tier` so a single admin can develop + test against
    # any tier without making fake accounts. Stays sticky across
    # sessions; the dev-page toggle is the only thing that writes here.
    # NEVER flipped by normal product code - only by /api/dev endpoints.
    tier_override: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    sessions: Mapped[List["UserSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def effective_tier(self) -> str:
        """tier_override (admin impersonation) takes precedence; falls
        back to the real tier column. Mirrors app.tiers.effective_tier()
        but as a property so UserOut.from_attributes can read it.
        """
        # Local import to avoid models.py <-> tiers.py circular at load.
        from app.tiers import is_valid_tier
        if self.tier_override and is_valid_tier(self.tier_override):
            return self.tier_override
        return self.tier


class UserSession(Base):
    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Captured at login time so the Sessions panel in Settings can show
    # the user what device + rough origin each session was started on.
    # Nullable: rows created before this column was added show as
    # 'Unknown device' until the user signs in fresh on that device.
    user_agent: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Touched on heartbeat-style endpoints (specifically /sync-jobs/claim,
    # which the worker app polls every few seconds while running). Used
    # by the worker-status endpoint to tell the UI whether the worker
    # for this user is currently up - if a worker session's last_seen_at
    # is older than ~30s the UI shows "worker app inactive" instead of
    # a stalled progress bar.
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="sessions")


class PasswordResetToken(Base):
    """One-time password-reset tokens issued by /api/auth/forgot-password.

    The plaintext token is sent to the user's email; only its SHA-256
    hash is stored here. So a database leak doesn't let an attacker
    redeem outstanding tokens. Valid for 1 hour and one-time-use
    (used_at gets stamped on redemption).
    """

    __tablename__ = "password_reset_tokens"

    token_hash: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class EmailVerificationToken(Base):
    """One-time tokens for verifying a user's email address.

    Same shape as PasswordResetToken — plaintext in the email link,
    SHA-256 hash in the DB, one-time use, expires after 7 days
    (longer than reset because verification is less time-sensitive).

    The endpoint that redeems these does NOT require an active
    session — having the token is itself proof you control the
    inbox. Lets users click the verify link from any device.
    """

    __tablename__ = "email_verification_tokens"

    token_hash: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False, index=True
    )


class UserYouTubeSettings(Base):
    """Per-user YouTube settings blob.

    Stored as opaque JSON — the frontend owns the schema. Backend just
    persists what it's given and hands it back. One row per user.
    """

    __tablename__ = "user_youtube_settings"

    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    settings_json: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class UserUiPrefs(Base):
    """Per-user UI preferences blob (sidebar collapsed state + layout, …).

    Same opaque-JSON pattern as UserYouTubeSettings: the frontend owns the
    schema, the backend just persists + hands it back. One row per user.
    Tying these to the account (not just localStorage) lets a user's
    sidebar state follow them across devices. New table, so create_all
    materializes it - no migration.
    """

    __tablename__ = "user_ui_prefs"

    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    prefs_json: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class WorkerYoutubeConnection(Base):
    """What a Basic user's own worker app reports about its YouTube
    connection (cookies in the app's embedded webview).

    Basic users sync through their own worker app rather than OAuth, so the
    website's Connections tab mirrors this row for them instead of
    UserGoogleConnection. One row per user.
    """

    __tablename__ = "worker_youtube_connections"

    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    connected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cookie_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    channel_title: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class UserChannel(Base):
    """A YouTube channel a user has added to their dashboard.

    The full Channel object (including per-channel settings) is stored as JSON
    so the frontend owns the schema. Composite PK on (user_id, channel_id).

    ``google_user_id`` records which connected Google account this channel
    was imported from, so the worker can pick the right OAuth token to
    refresh when syncing. Nullable to keep older rows valid; backfilled
    on migration to whichever Google account the user had connected at
    the time.
    """

    __tablename__ = "user_channels"

    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    channel_id: Mapped[str] = mapped_column(String, primary_key=True)
    google_user_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, index=True
    )
    data_json: Mapped[str] = mapped_column(String, nullable=False)
    # Archived channel avatar bytes - we copy YouTube's CDN image to R2
    # at avatars/{channel_id}.jpg so the avatar survives YouTube channel
    # deletion. avatarUrl in data_json is YouTube's URL; the response
    # serializer swaps it for a signed R2 URL when this key is set.
    avatar_r2_key: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    # Soft-delete timestamp. NULL = active. Set when the user removes
    # the channel; daily purge cron hard-deletes rows where this is
    # older than the grace window. Metering/billing skips rows where
    # this is non-null so the user isn't charged for storage they've
    # asked us to drop.
    removed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class UserChannelVideo(Base):
    """A discovered video for a channel a user is tracking.

    The full Video object is stored as JSON so the frontend owns the schema.
    Composite PK on (user_id, channel_id, video_id).
    """

    __tablename__ = "user_channel_videos"

    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    channel_id: Mapped[str] = mapped_column(String, primary_key=True)
    video_id: Mapped[str] = mapped_column(String, primary_key=True)
    data_json: Mapped[str] = mapped_column(String, nullable=False)
    # Archived thumbnail bytes - we copy YouTube's CDN thumbnail to R2
    # at thumbnails/{video_id}.jpg so the thumbnail survives YouTube
    # changes / deletion. thumbnailUrl in data_json is YouTube's URL;
    # the response serializer swaps it for a signed R2 URL when this
    # key is set. thumbnail_size_bytes is for storage accounting.
    thumbnail_r2_key: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    thumbnail_size_bytes: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    # Content-addressing for the current thumbnail bytes in R2. Used by
    # the metadata-rescan engine to detect actual image changes without
    # blindly trusting URL comparisons (YouTube's thumbnail path stays
    # the same when the creator updates the thumbnail - only the `sqp=`
    # signature query param changes - so URL comparison misses real
    # updates AND treats CDN cache-busts as changes when they aren't).
    #
    # On rescan we HEAD the URL first: if etag or content-length matches
    # stored, skip entirely. If they don't match, GET the bytes and
    # compare sha256 - only THAT is authoritative for "did the image
    # change". Snapshots only happen when sha256 differs.
    thumbnail_sha256: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    thumbnail_etag: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    thumbnail_content_length: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
    # When this video was most recently scanned by the channel-level
    # metadata refresh cron. Null = never refreshed (only the initial
    # metadata captured at archive time). Used to bound the
    # "current value last confirmed" timespan in the history view -
    # the current value in data_json is implicitly active up to this
    # timestamp.
    last_metadata_sync_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When this video's comments were most recently rescanned. Same
    # role as last_metadata_sync_at but for the comments pipeline,
    # which runs on its own independent cadence (commentsRefreshFrequency
    # in channel settings).
    last_comments_sync_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class VideoFieldSnapshot(Base):
    """Historical record of a previous value of a versioned video field.

    Stored ONLY when a field's value changes during a metadata rescan.
    The CURRENT value continues to live in UserChannelVideo.data_json -
    snapshots represent superseded prior states with the timespan they
    were active for.

    Combine snapshot rows ordered by captured_at ASC with the current
    value from data_json to get the full history of a field. The
    last_metadata_sync_at column on UserChannelVideo bounds the most
    recent confirmation of the current value.

    Versioned fields: title, description, thumbnail, tags, privacy,
    captionLanguages. Stats (viewCount, likeCount) are NOT versioned
    here - they get their own time-series snapshots when implemented.

    For thumbnail snapshots, r2_key points at the actual image bytes
    stored at the time the snapshot was captured. Old thumbnails are
    never deleted from R2 - the user paid to archive them, so they
    stay downloadable forever (or until the user manually removes them).
    """

    __tablename__ = "video_field_snapshots"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)

    # We don't FK to UserChannelVideo directly because SQLite's composite
    # FK story is awkward; we store all three parts and CASCADE via the
    # user_id FK. Deleting a user wipes their snapshots like everything
    # else.
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    video_id: Mapped[str] = mapped_column(String, nullable=False, index=True)

    # Which field this snapshot tracks. One of: 'title', 'description',
    # 'thumbnail', 'tags', 'privacy', 'captionLanguages'. Adding new
    # versioned fields means adding a new string here and updating the
    # rescan diff in the worker.
    field: Mapped[str] = mapped_column(String, nullable=False, index=True)

    # The value at the time this snapshot was captured, JSON-encoded so
    # we can round-trip lists (tags, caption languages) and scalars
    # (title, description) through the same column. Decoded based on
    # `field` at the application layer.
    value_json: Mapped[str] = mapped_column(Text, nullable=False)

    # For thumbnail snapshots only: the R2 key holding the actual image
    # bytes for THIS version. Null for non-thumbnail fields.
    r2_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # When we first observed this value (either the original archive
    # time for the initial state, or the rescan time when the previous
    # snapshot was superseded).
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # The last rescan that confirmed this value was still in effect
    # before a change was detected. captured_at == last_seen_at means
    # the value flipped on the same scan we first saw it.
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # When the new value replaced this one. Always set on a snapshot
    # row (current values don't get rows; they live in data_json).
    superseded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class ChannelFieldSnapshot(Base):
    """Historical record of a channel-level field over time, the channel
    analogue of VideoFieldSnapshot.

    Two shapes share this table, discriminated by ``field``:
      - Change history ('about', 'avatar'): a row is written when the value
        changes; it holds the PRIOR value with the span it was live. Current
        value stays in UserChannel.data_json. For 'avatar', r2_key points at
        the preserved old image bytes.
      - Time-series ('stats'): a row is written every refresh (a point
        sample of subscriber/video/view counts), building a graph over time.

    Written by the channel-info refresh in the metadata rescan cron, gated on
    the per-field save/history toggles. Never deleted (the user archived it).
    """

    __tablename__ = "channel_field_snapshots"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # YouTube UC channel id (mirrors UserChannel.channel_id).
    channel_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # 'about' | 'avatar' | 'stats'.
    field: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # JSON-encoded value: the old description string ('about'), the old
    # {url, sha256} ('avatar'), or the {subscriberCount, videoCount,
    # totalViews} reading ('stats').
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    # For 'avatar' only: R2 key holding the preserved old image bytes.
    r2_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # When this value/reading was captured, and (for change-history) when it
    # was superseded. For 'stats' time-series rows the two are equal.
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    superseded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class VideoComment(Base):
    """One comment on one video. Top-level comments and replies live in
    the same table - replies set parent_comment_id, top-level set it to
    None.

    The whole archive philosophy applies: we never delete rows here.
    When YouTube removes a comment (author-deleted, uploader-removed,
    moderator-held, channel-terminated), we set deleted_at to the time
    we noticed it was gone and keep the row otherwise intact. The
    "recently deleted" channel-wide feed is the killer feature this
    schema enables.

    Edits: tracked via the text_hash column. On rescan, if a comment's
    yt-API text has a different hash than what we stored, we bump
    is_edited and overwrite the text. (Full prior-text history is a
    later concern - for now we just know the comment changed.)

    The comment ID from YouTube is globally unique and stable, so we use
    it as the PK. Replies have hierarchical IDs like
    "<parent_id>.<reply_id>"; we store the full ID and split out
    parent_comment_id for fast thread queries.
    """

    __tablename__ = "video_comments"

    # YouTube's globally-unique comment ID. Top-level look like
    # "UgxF1qVZv..." (24-26 chars); replies like "UgxF1qVZv....abc123" (50+).
    id: Mapped[str] = mapped_column(String, primary_key=True)

    # Scope. user_id is the ARCHIVE336 user who owns the archive; CASCADE
    # on user delete drops the comments along with everything else.
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    video_id: Mapped[str] = mapped_column(String, nullable=False, index=True)

    # Threading. NULL for top-level comments; otherwise the id of the
    # comment this is a reply to. Indexed so "load replies for thread X"
    # is a clean WHERE.
    parent_comment_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, index=True
    )

    # Author display name + their channel ID (so users can click through
    # to see other comments by the same person across the archive).
    author: Mapped[str] = mapped_column(String, nullable=False)
    author_channel_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, index=True
    )

    # Comment body. Stored as TEXT - YouTube allows up to 10k chars per
    # comment. Plus a SHA-256 of the body for change detection on
    # rescan (text edited => hash changes => we bump is_edited).
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String, nullable=False)

    # Engagement + flags. like_count we re-read on every rescan (it
    # changes over time and is interesting historically; full
    # time-series is a later concern). is_edited / is_pinned /
    # is_by_uploader come from the API directly.
    like_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_edited: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    is_pinned: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    is_by_uploader: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # YouTube's "heart" feature - whether the channel owner hearted
    # this comment. Worth tracking - hearted comments are
    # editorially-endorsed and tend to be the ones people care about.
    viewer_rating_like: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    # When the comment was originally posted (absolute timestamp from
    # YouTube). published_at is what YouTube returns; we capture it
    # at first observation and don't update on later rescans.
    published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When YouTube reports a more-recent timestamp (edit time, etc).
    # Surfaced by the API as "updatedAt" - distinct from our
    # last_seen_at which is when WE confirmed it was still there.
    updated_at_remote: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Our-side lifecycle timestamps for the soft-delete + diff model.
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False, index=True
    )
    # NULL = still visible on YouTube last time we checked. Non-null =
    # the timestamp of the rescan that first noticed it was gone.
    # Composite index with channel_id powers the "recently deleted"
    # channel feed.
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class UsageRecord(Base):
    """One snapshot of a user's storage consumption.

    The metering cron writes one of these per user per day (the bucket is
    just the calendar day in UTC). At billing time we accumulate across
    rows since `User.last_billed_at`, multiply by the price, and create a
    Stripe invoice when the total crosses the threshold or the annual
    fallback fires.

    Storing the raw bytes (not the dollar amount) means we can change
    pricing or markup later and recompute historical bills cleanly.
    """

    __tablename__ = "usage_records"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The day this record covers (truncated to 00:00 UTC). One row per
    # (user, day) — re-running the cron the same day overwrites.
    day: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    bytes_stored: Mapped[int] = mapped_column(Integer, nullable=False)
    # Whether this record's accrued cost has already been included in a
    # Stripe invoice. Flipped to True once the billing cron rolls it up.
    billed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class UserGoogleConnection(Base):
    """OAuth tokens for a user's connected Google/YouTube account.

    Composite PK on (user_id, google_user_id) — a single ARCHIVE336 user can
    connect multiple Google accounts so they can archive channels across
    accounts (personal + brand, or multiple separate accounts).

    Tokens are stored encrypted at rest using Fernet — see app.encryption.
    The encryption key lives in ARCHIVE336_FERNET_KEY in the .env, which is
    mode 600. So a database dump alone doesn't expose the tokens; an
    attacker needs the env file too.

    The ``google_user_id`` is the stable Google account identifier (the
    'sub' claim from the id_token). ``google_email`` is the address the
    user authed with — useful for showing "Connected as: x@y.com" in the
    UI but not used as a primary key (emails can change).
    """

    __tablename__ = "user_google_connections"

    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    google_user_id: Mapped[str] = mapped_column(String, primary_key=True)
    google_email: Mapped[str] = mapped_column(String, nullable=False)
    youtube_channel_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    youtube_channel_title: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Encrypted with Fernet. Stored as the URL-safe base64 ciphertext bytes
    # decoded to ASCII strings.
    access_token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    access_token_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    scopes: Mapped[str] = mapped_column(Text, nullable=False)

    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
    # Set when a token refresh fails with an unrecoverable error (Google
    # revoked our token, user disconnected us in their Google account
    # security settings, refresh token expired, etc). The row stays in
    # the table so the UI can show 'reconnect' and we don't lose the
    # connection's history - we just stop using it for API calls until
    # the user re-auths. Cleared back to NULL when they reconnect.
    disconnected_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Short tag from the OAuth provider ('invalid_grant', etc) for logs.
    # Not user-facing - just helps us debug repeat occurrences.
    disconnect_reason: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )


class SyncJob(Base):
    """A queued/running/completed video sync job.

    The worker picks up rows where status == 'pending' (oldest first), runs
    yt-dlp to fetch the .mp4, uploads it to R2, and updates the linked
    UserChannelVideo row to status='archived' with the R2 key as localPath.

    Possible status values: pending | running | done | failed.
    """

    __tablename__ = "sync_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    video_id: Mapped[str] = mapped_column(String, nullable=False, index=True)

    status: Mapped[str] = mapped_column(
        String, default="pending", nullable=False, index=True
    )
    # What the worker should do for this job. 'video' = download the
    # mp4 + probe + (also) any captions. 'captions' = captions-only,
    # used to backfill caption files onto already-archived videos
    # without re-downloading the mp4. Default 'video' preserves the
    # pre-captions row shape for older sync_jobs.
    kind: Mapped[str] = mapped_column(
        String, default="video", nullable=False
    )
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    r2_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Client orchestration: which user's worker claimed this row, and when
    # they last heartbeated. Stale claims (heartbeat older than threshold)
    # get reaped back to pending so a different client can pick them up.
    claimed_by: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # Queued exactly once, enforced by the database rather than by
        # every caller remembering to read-then-write. Four call sites
        # create video jobs and each had its own dedupe; the sweep runs in
        # a separate process from the API, so its in-flight snapshot can
        # go stale between the read and the commit, and that window widens
        # with queue depth. The loser of that race now writes nothing
        # instead of creating a second job and spending the user's
        # bandwidth - and their storage bill - twice.
        #
        # `kind` is part of the key so a pending captions job does not mask
        # the video job for the same video.
        #
        # Partial: only ACTIVE rows are unique. Terminal rows accumulate on
        # purpose - the give-up counter in auto_download reads them.
        Index(
            "uniq_sync_jobs_active",
            "user_id",
            "video_id",
            "kind",
            unique=True,
            sqlite_where=text("status IN ('pending','running')"),
        ),
        # The claim query filters (user_id, status, kind) and takes the
        # oldest. Without this it fell back to the single-column status
        # index and sorted every pending row - fine at ten, not at a
        # 20,000-video back catalogue.
        Index(
            "ix_sync_jobs_claim",
            "user_id",
            "status",
            "kind",
            "created_at",
        ),
    )


class AccountDeletionToken(Base):
    """One-time token issued by /api/auth/me/request-delete and
    redeemed at /api/auth/me/confirm-delete. The user clicks a link
    in their email to consent to the actual deletion, after which we
    run the charge + cleanup + send the post-delete email.

    Snapshots charge_amount_cents at request time so the user is
    charged the same number they accepted in the email. Storage might
    accrue a few more bytes between request and confirm, but they
    agreed to a specific dollar amount.
    """

    __tablename__ = "account_deletion_tokens"

    token_hash: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    charge_amount_cents: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    export_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False, index=True
    )


class EmailSendLog(Base):
    """One row per Resend send so the admin Resend card can show how
    much of the free tier we've burned (3,000/mo, 100/day).

    type values: 'verification' | 'password_reset' (extend as we add
    more transactional templates). Failures don't insert; we only log
    on a clean send so the count matches Stripe-side reality.
    """

    __tablename__ = "email_send_log"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    to_email: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False, index=True
    )


class ErrorLog(Base):
    """Captured errors - both backend exceptions and frontend uncaught
    errors. Surfaces in the /dev admin page so we can debug user issues
    after the fact instead of asking them to reproduce.

    source values:
      'server' - uncaught exception from a FastAPI route handler
      'client' - uncaught JS error reported via /api/errors POST

    user_id may be NULL for client errors that fired before login or
    server errors on unauthenticated routes. ON DELETE SET NULL keeps
    the log row around even if the user is later deleted.
    """

    __tablename__ = "error_log"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[Optional[str]] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source: Mapped[str] = mapped_column(String, nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    stack: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    request_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    request_method: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False, index=True
    )


class StorageObject(Base):
    """Authoritative ledger of every R2 object we've ever uploaded for a
    user, including when it was uploaded and (eventually) when it was
    deleted.

    This table is the *billing source of truth* for storage. Every
    object in R2 should correspond to exactly one row here with
    deleted_at IS NULL; every soft-deleted object becomes a row with a
    non-null deleted_at that we keep forever for historical billing
    audits and dispute resolution.

    The bill cron integrates `bytes * (deleted_at - uploaded_at)` per
    user per billing period to get byte-hours, then applies the user's
    per-tier storage price. Rates are per GB-month over decimal GB
    (10^9 bytes) and an average month of 730.485 hours.

    Deliberately no numbers here: this docstring used to quote
    Cloudflare's $0.015/GB-month and a flat 2.0x markup, both of which
    were wrong after the move to Backblaze ($0.007 cost) and the
    2026-06-04 per-tier re-pricing (Basic $0.020, Creator $0.010, Studio
    $0.0075). All constants live in app.billing - read them there rather
    than trusting a copy. See docs/STORAGE_BILLING_DESIGN.md for the math.

    Invariants enforced by code (not the DB):
      1. Insert AFTER a successful R2 PUT. Never before.
      2. On delete, issue R2 DELETE first, THEN flip deleted_at. If
         the R2 DELETE fails, retry; do NOT flip deleted_at. If R2
         DELETE succeeds but the DB update fails, reconciliation will
         detect the orphan within 24h and flip deleted_at retroactively.
      3. Rows are immutable except for the one-time deleted_at flip.
      4. UNIQUE(r2_key) catches duplicate-record races; the second
         worker to try should be told "job already done."
    """

    __tablename__ = "storage_objects"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The R2 key (e.g. "users/{user_id}/videos/{video_id}/video.mp4").
    # Indexed but NOT unique across the table — a single key may have
    # multiple rows over time (one per "lifecycle"), where every
    # historical row has deleted_at set and at most one open (active)
    # row has deleted_at NULL. The partial unique index in
    # __table_args__ enforces "at most one active row per key", which
    # is the correct invariant for thumbnail rotations and any future
    # case where the same R2 key gets rewritten with new content.
    r2_key: Mapped[str] = mapped_column(String, nullable=False, index=True)

    # Body size as we recorded it at upload time. We use the local file
    # size we uploaded (we control it) rather than R2's PUT response
    # because R2 occasionally returns slightly different values for
    # multipart uploads; reconciliation catches any future mismatch.
    bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    # Flat over-estimate of R2 metadata overhead (Content-Type header
    # etc.). Cloudflare charges for `bytes + metadata_bytes`. The real
    # number is ~100-200 bytes per object; we use 256 as a deliberate
    # over-bill so we never under-charge. <0.001% of cost on video-sized
    # objects; insignificant on the bottom line.
    metadata_bytes: Mapped[int] = mapped_column(
        Integer, default=256, nullable=False
    )

    # 'video' | 'thumbnail' | 'caption' | 'avatar' | 'snapshot'. Stored
    # as string (not SQL ENUM) so adding a new kind doesn't require a
    # migration. Used for admin breakdowns + reconciliation grouping.
    kind: Mapped[str] = mapped_column(String, nullable=False, index=True)

    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # At most one ACTIVE (non-deleted) row per r2_key. Historical
        # rows with deleted_at set can pile up at the same key — that's
        # how we track thumbnail rotations and other in-place rewrites
        # without losing billing history.
        Index(
            "uniq_storage_active_r2_key",
            "r2_key",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
        ),
        # "What does this user currently have in R2?" - the most common
        # query, used by the live-status endpoint + reconciliation.
        Index("ix_storage_objects_user_deleted", "user_id", "deleted_at"),
        # "What was this user storing during period X to Y?" - used by
        # the bill cron's byte-hour integral.
        Index("ix_storage_objects_user_uploaded", "user_id", "uploaded_at"),
        # Reconciliation: walk all objects ever touched in a period.
        Index("ix_storage_objects_uploaded_deleted", "uploaded_at", "deleted_at"),
    )


# When an R2 operation is platform-driven (Litestream backups, the
# reconciliation cron, the bill cron itself, admin tooling) we attribute
# it to this sentinel subject rather than a real user. Chosen to be
# obviously-not-a-UUID so it can't collide with a real user_id.
# See docs/CLOUDFLARE_AUDIT.md and the R2 ops audit phase B for the
# attribution rules each call site follows.
R2_OPS_PLATFORM_SUBJECT = "__platform__"


class R2OperationLog(Base):
    """Daily rollup of R2 Class A and Class B operations, per billing
    subject (user_id or the ``__platform__`` sentinel) per bucket.

    Phase A of the R2 ops billing redesign — the operations-side sibling
    of ``StorageObject``. Where StorageObject answers "how many byte-
    hours did this user store?", this table answers "how many writes/
    reads did this user drive against R2?". The bill cron will multiply
    these counts by Cloudflare's per-million rate (see billing.py
    ``R2_CLASS_A_USD_PER_MILLION`` / ``R2_CLASS_B_USD_PER_MILLION``)
    and the user's storage markup (default 2.0×) to produce the
    operations line on each user's invoice.

    Grain: ONE row per ``(subject, bucket, op_class, day)``. Increments
    flow in via INSERT ... ON CONFLICT DO UPDATE so concurrent callers
    don't race on the row, and so the table stays bounded at
    ``users × buckets × 2 op_classes × days`` instead of one row per
    R2 call. For 100 users that's a few hundred rows per day; nothing.

    The ``subject`` column is intentionally NOT a foreign key — it
    holds either a real ``users.id`` value or the
    ``R2_OPS_PLATFORM_SUBJECT`` sentinel string. A FK would force one
    of two ugly choices: insert a fake "platform" user row (collision
    risk if a real signup ever picks that id) or make user_id nullable
    and fight SQLite's NULL-in-unique-index semantics. Plain string is
    cleanest.

    Op-class assignment (mapping the S3 API name we called to "A" or
    "B" or "free") is the caller's responsibility — every call site
    in r2.py knows which op it just issued. The mapping itself is the
    one in docs/CLOUDFLARE_AUDIT.md §2. Free ops (DeleteObject,
    AbortMultipartUpload, DeleteBucket) are not recorded at all
    because they don't appear on the bill.

    Invariants enforced by code (not the DB):
      1. Always written AFTER the R2 op completes (success or failure).
         Cloudflare bills retries too, so we count attempts, not
         successes.
      2. Row is mutable (count grows monotonically through the day).
         Never decrement — corrections go through a reconciliation
         row, not by subtracting here.
      3. The ``day`` column is the UTC date the op occurred, with
         time truncated to 00:00:00 UTC. The recorder enforces this.
      4. Once a day is closed (we've billed it), rows older than
         the billing-cutoff are immutable for audit purposes. The
         billing cron does not write to old days.
    """

    __tablename__ = "r2_operation_log"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    # Either a real users.id (36-char UUID) or the R2_OPS_PLATFORM_SUBJECT
    # sentinel string. See class docstring.
    subject: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # 'aether-archive-tool' (user content) or 'aether-archive-backups'
    # (Litestream replicas). Stored verbatim so reconciliation against
    # Cloudflare's per-bucket totals is trivial.
    bucket: Mapped[str] = mapped_column(String, nullable=False)
    # 'A' (writes/lists/multipart) or 'B' (reads/heads). See the audit
    # doc for the full S3-API → class mapping. Free ops aren't logged.
    op_class: Mapped[str] = mapped_column(String, nullable=False)
    # UTC calendar day, time component truncated to 00:00:00 UTC.
    day: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Running count for the day. Grown by the recorder via UPSERT.
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # When the last increment landed. Mostly for debugging racy writes.
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )

    __table_args__ = (
        # The UPSERT target. One row per (subject, bucket, op_class, day).
        Index(
            "uniq_r2_op_log_subject_bucket_class_day",
            "subject",
            "bucket",
            "op_class",
            "day",
            unique=True,
        ),
        # "All ops for this user this period" — bill cron's hot query.
        Index("ix_r2_op_log_subject_day", "subject", "day"),
        # "All ops on this day across the account" — reconciliation
        # cross-check against Cloudflare's per-bucket totals.
        Index("ix_r2_op_log_day", "day"),
    )


class ReconciliationLog(Base):
    """Audit trail for every reconciliation action the safety-net cron
    takes (orphan deletes, phantom marks, drift fixes).

    Lets admins see, for any user, exactly what corrections we've made
    to our own books and when. Critical for dispute resolution: "you
    charged me $X but I only stored Y" is answered by joining this
    table against StorageObject history.

    See docs/STORAGE_BILLING_DESIGN.md → Reconciliation.
    """

    __tablename__ = "reconciliation_log"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    # Nullable so we can also log account-wide actions (e.g., bucket-
    # level drift checks that don't tie to a specific user).
    user_id: Mapped[Optional[str]] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # 'delete_orphan' | 'mark_phantom' | 'fix_drift' | other actions
    # we add later.
    action: Mapped[str] = mapped_column(String, nullable=False, index=True)
    r2_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Free-form JSON for action-specific details (db_bytes vs r2_bytes
    # for drift, multipart upload id for orphan parts, etc.).
    details_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ran_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False, index=True
    )
    # Whether this action triggered an engineering alert (phantoms and
    # drift always alert; orphan deletes don't because they're expected
    # background noise).
    alerted: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )


class StripeAuditLog(Base):
    """Append-only log of every Stripe webhook event we receive.

    Two jobs:

      1. **Idempotency** — Stripe will redeliver an event if our 200
         response is slow or lost. The ``stripe_event_id`` unique
         constraint lets the webhook handler skip events it's already
         processed (Stripe IDs are stable).

      2. **Audit trail** — for any user dispute ("you charged me $X"),
         we can join this against the billing tables and replay the
         exact event payload Stripe sent. Stripe's own Events API only
         keeps 30 days; we keep forever.

    Also lets the admin Stack tab surface system-level events that
    aren't tied to a user (payouts, fraud warnings) in one place
    instead of grepping logs.
    """

    __tablename__ = "stripe_audit_log"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    # The ``evt_xxx`` id from Stripe. Unique - same event redelivered
    # is a no-op so we don't double-flip payment_status etc.
    stripe_event_id: Mapped[str] = mapped_column(
        String, nullable=False, unique=True, index=True
    )
    # E.g. 'invoice.paid', 'payout.failed', 'radar.early_fraud_warning.created'.
    event_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False, index=True
    )
    # Nullable: system events (payout.*, dispute.*) have no customer.
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, index=True
    )
    # Nullable for the same reason + for events on customers we don't
    # have a row for (deleted users, cross-environment test events).
    user_id: Mapped[Optional[str]] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Full Stripe event payload, JSON-serialized. Heavy but invaluable
    # for forensic work; we'd rather pay the bytes than lose evidence.
    payload_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Did our handler actually do something (vs. log-and-ignore)?
    # Helps with "we received the event but did nothing" debugging.
    handled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # Free-form ops note, e.g. "auto-refunded charge ch_xxx" or
    # "unknown customer cus_xxx - skipped".
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# ============================================================
# Shared-pool archive model (v2)
# ------------------------------------------------------------
# Replaces the per-user UserChannel / UserChannelVideo / StorageObject
# trio for archived content. Key shift: one physical R2 file per
# YouTube video globally; users get pointers via subscription rows;
# each user is billed as if they were the sole storer of those bytes.
#
# Coexists with the old tables until the cutover migration. New
# models below intentionally do NOT have ForeignKey relationships to
# the legacy tables so we can drop those cleanly later.
#
# See the chat-level design doc and the project_tier_architecture
# memory for the full motivation; quick recap:
#   - public/unlisted/age_restricted videos: any subscribed user
#   - members_only: subscribed user with verified ChannelMembership
#   - private: only users in active ChannelOwnership
# ============================================================


class Channel(Base):
    """Global YouTube channel record — one row per real channel,
    regardless of how many platform users subscribe to it.

    youtube_id is the source-of-truth identifier (UCxxxxx…). The
    handle and title we mirror from YouTube periodically so the
    dashboard doesn't have to round-trip on every render.

    PubSubHubbub lease columns are populated when we subscribe to
    push notifications for new uploads. NULL = not subscribed yet.
    A daily cron renews leases nearing expiry (the YouTube hub gives
    10-day leases).
    """

    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    youtube_id: Mapped[str] = mapped_column(
        String, unique=True, nullable=False, index=True
    )
    handle: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    thumbnail_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    pubsub_lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pubsub_last_renewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # YouTube-side rich info — only the fields that are the same for
    # every subscriber (country, joinedAt, links, subscriberCount,
    # totalViews, etc.). Per-user state (settings, addedAt) lives on
    # UserChannelSubscription instead so two subscribers to the same
    # channel can have independent preferences.
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Cached profile-picture bytes for this channel. We download the
    # avatar once to R2 so it survives YouTube channel deletion + so
    # the frontend doesn't hit the YouTube CDN on every load. NULL
    # = no archived avatar (frontend falls back to thumbnail_url).
    avatar_r2_key: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )


# Privacy tiers a video can be in. String enum stored as plain text
# columns; we don't use a real DB enum because SQLite (dev) is fussy
# about altering enums and we want the freedom to add tiers later
# without a migration ceremony.
VIDEO_PRIVACY_TIERS = frozenset(
    ["public", "unlisted", "age_restricted", "members_only", "private"]
)

# Our archive's own visibility, distinct from YouTube's privacy. Stamped
# once when we capture a video and frozen thereafter: a video we grabbed
# while it was public stays "open" in the archive even if YouTube later
# privates the source. "open" = any active subscriber can watch it here;
# "sealed" = only the authenticated channel owner. Members-only collapses
# into sealed (treated the same as private).
VIDEO_VISIBILITY = frozenset(["open", "sealed"])
# Only fully-public videos earn ``open`` (any subscriber can view).
# Unlisted is link-only on YouTube, so we keep it ``sealed`` (owner-only) -
# the safe side of "never over-expose". age_restricted is still public,
# just age-gated.
_OPEN_AT_CAPTURE = frozenset(["public", "age_restricted"])


def visibility_for_privacy(privacy: str) -> str:
    """Map a YouTube privacy tier to our archive visibility at capture
    time. Public (and age-gated public) is ``open``; unlisted, members-only
    and private are ``sealed``."""
    return "open" if privacy in _OPEN_AT_CAPTURE else "sealed"


class Video(Base):
    """Global YouTube video record — one row per real video.

    Files are stored in R2 keyed by ``id`` (our cuid) and shared across
    all subscribers. ``r2_key`` is NULL when the video is tracked but
    not yet downloaded (members-only without a capable worker,
    age-restricted before an age-verified worker picks it up, etc.).

    Two privacy columns: ``privacy_at_discovery`` is the snapshot of
    YouTube's privacy at the moment we first saw the video; it never
    changes after creation. ``privacy_current`` mirrors whatever
    YouTube reports now and updates on each resync. The split lets us
    say "we archived this when it was public; the channel owner has
    since made it private — the archive copy is still here because we
    captured it back then" without lying about either fact.
    """

    __tablename__ = "videos"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    channel_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    youtube_id: Mapped[str] = mapped_column(
        String, unique=True, nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    thumbnail_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    duration_seconds: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )

    privacy_at_discovery: Mapped[str] = mapped_column(String, nullable=False)
    privacy_current: Mapped[str] = mapped_column(
        String, nullable=False, index=True
    )
    # Our archive's own access tier (open/sealed), stamped at capture from
    # privacy_at_discovery and then frozen. This - not privacy_current -
    # governs who can access the archived copy, so a video captured while
    # public stays accessible even after YouTube privates the source.
    # See visibility_for_privacy().
    visibility: Mapped[str] = mapped_column(
        String, nullable=False, server_default="open", default="open", index=True
    )

    r2_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    bytes_stored: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    synced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False, index=True
    )
    # Rich YouTube + sync metadata blob (viewCount, tags, captionLanguages,
    # videoResolution, videoCodec, audioCodec, etc.). Mirrors what the
    # legacy UserChannelVideo.data_json held so the YouTube page renders
    # the same set of fields after the read-route cutover. Schema is
    # the same shape as the legacy frontend payload, no new keys
    # invented here.
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ChannelOwnership(Base):
    """A user has authenticated as the owner of a channel.

    Active rows (both revoke columns NULL) are what lets the worker
    discover and sync the channel's sealed (private / unlisted /
    members-only) videos, and what makes already-archived sealed
    videos visible to this user. That is a PERMISSION, and permission
    is the whole of what these rows decide.

    They do not decide when the storage bill STOPS. billing.py's
    compute_user_byte_hours_v2 reads ownership to answer whose sealed
    bytes these are and, from ``authenticated_at``, when this user
    first held them - that is the only boundary ownership sets. What
    CLOSES the window is the user's own tracking row for the channel:
    UserChannelSubscription.unsubscribed_at, the same instant the open
    tier stops at, or legacy UserChannel.removed_at for an owner who
    never subscribed. A user with neither row is billed nothing here,
    because ownership on its own is a permission, not a request to
    hold files.
    Neither revoke column appears in that arithmetic. The rule is that
    we bill for what we store, for as long as we store it: revoking
    stops new sealed videos arriving but deletes nothing, so Backblaze
    keeps charging us by the hour for everything already held.
    Removing the channel is the action that stops the bill, because
    that is what soft-deletes the storage and starts the 30-day grace
    before purge.

    Multi-owner is supported for shared-team channels - three
    different users can all authenticate as the same @ChannelHandle
    and all get sealed-tier visibility. Each is billed independently.

    Two separate revoke columns, because they mean different things:

    ``revoked_at`` is machine bookkeeping. ensure_ownership() in
    archive.py clears it on every worker ownership report (except when
    user_revoked_at is set, which is the whole point of that column),
    so on its own it cannot hold a decision the user made.

    ``user_revoked_at`` is the human's decision, and only an explicit
    re-authenticate clears it. See its own comment below.

    Revoking deletes nothing. The row is kept, and so are the files it
    was used to fetch: nothing keys deletion off these rows. (The one
    purge in the codebase, scripts/purge_removed.py, walks legacy
    UserChannel.removed_at and never reads ownership.) Revoking stops
    NEW sealed videos being discovered; everything already archived
    stays archived, viewable, downloadable - and billable, per the
    second paragraph above.

    Read-access enforcement: access.py defines can_user_access_video()
    against these rows, but it has no production callers - it is not
    wired into any serving path. Do not assume ownership gates video
    reads today. The only place ownership currently changes behaviour
    is sealed-video discovery, the bytes/cost split in archive.py, and
    in billing.py deciding which user a sealed video's bytes are
    attributed to and, via ``authenticated_at``, when that user's
    metered window opens. Ownership never ends the window; removing
    the channel is what does that.
    """

    __tablename__ = "channel_ownerships"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    channel_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    google_user_id: Mapped[str] = mapped_column(String, nullable=False)
    authenticated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # The user's own deliberate revocation, kept separate from
    # revoked_at because revoked_at cannot survive: ensure_ownership()
    # clears it whenever the worker reports ownership, which the
    # desktop app does on every launch. A revoke button backed by
    # revoked_at alone would quietly undo itself within minutes and
    # the user would think they had turned something off that was
    # still running. This flag is sticky against that routine machine
    # chatter - only an explicit re-authenticate clears it.
    user_revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index(
            "ix_channel_ownerships_user_channel",
            "user_id",
            "channel_id",
            unique=True,
        ),
    )


class UserChannelSubscription(Base):
    """A user has asked us to archive a channel for them.

    Source of truth for billing inclusion + public-tier access. Once
    the row exists with NULL ``unsubscribed_at``, the user pays the
    per-byte-hour rate on every video in that channel they can see
    (public for everyone, private if they also have a
    ChannelOwnership row, members_only if they have a verified
    ChannelMembership row).

    Unsubscribe sets ``unsubscribed_at`` and stops their billing - for
    both tiers, since this column now closes the sealed window as well
    as the open one whenever the row exists (an owner who never
    subscribed is closed by legacy UserChannel.removed_at instead; see
    ChannelOwnership); the
    physical R2 files stick around as long as ANY active subscriber
    references them, or for a 30-day grace if they were the last one.
    A second subscribe within the grace window just clears the column.
    """

    __tablename__ = "user_channel_subscriptions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    subscribed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    unsubscribed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # Per-user, per-channel preferences (the things the YouTube page's
    # channel-detail settings sheet writes). Same shape as the old
    # UserChannel.data_json's "settings" sub-object so the frontend
    # round-trips through this column unchanged.
    settings_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Most-recent successful sync timestamp for this (user, channel)
    # pair. Mirrors the legacy UserChannel.lastSyncedAt field.
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index(
            "ix_user_channel_subs_user_channel",
            "user_id",
            "channel_id",
            unique=True,
        ),
    )


class ChannelMembership(Base):
    """A user has proven an active paid membership to a channel.

    Stub at launch — we don't have a verification path yet. The future
    flow: OAuth scope that lets us query YouTube for the user's
    channel memberships, then we mirror an entry per active membership
    here. Membership tokens expire; ``expires_at`` is the verification
    expiry, after which we'd re-check before granting access.

    Required to view ``members_only`` videos. Without an active row
    here for (user, channel), members-only content for that channel
    is invisible to the user — both in listings and in the archive
    UI, exactly as on YouTube itself.
    """

    __tablename__ = "channel_memberships"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    channel_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    google_user_id: Mapped[str] = mapped_column(String, nullable=False)
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    __table_args__ = (
        Index(
            "ix_channel_memberships_user_channel",
            "user_id",
            "channel_id",
            unique=True,
        ),
    )


class SupportMessage(Base):
    """One message in a user's support conversation.

    A single table rather than threads-plus-messages: a user has one
    ongoing conversation with the maintainer, and `from_staff` is enough
    to tell the two sides apart. Tickets would model a support desk with
    a rota; this models one person answering their own users, which is
    what the product actually is and says it is.

    ``snapshot_json`` is the reason this is worth building rather than
    buying. Every question a user asks about a backup tool - "why is it
    not syncing", "why does it say I need to authenticate" - is
    unanswerable without their account state, and unaskable by them
    because they cannot see it. Captured server-side at send time, so it
    describes the moment they hit send rather than the moment it is
    read, which for an intermittent fault is the difference between a
    diagnosis and a shrug.
    """

    __tablename__ = "support_messages"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # bug | feature | question. Free-form string, not an enum, so adding
    # a kind never needs a migration on a database with no Alembic.
    kind: Mapped[str] = mapped_column(String, nullable=False, default="question")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    from_staff: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Only on user messages, and only ever read by the maintainer.
    snapshot_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False, index=True
    )
