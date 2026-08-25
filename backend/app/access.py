"""Access-check helpers for the shared-pool archive.

Every privacy tier on a Video has a rule for "can this user see it?"
This module is the single place that rule lives. Routes and worker-
dispatch code call into here rather than hand-rolling the join logic
inline, so changing the rule (or adding a new tier) is a one-file
edit.

Rules, as actually implemented:

  Access branches on Video.visibility (open / sealed), which is stamped
  once at capture from YouTube's privacy tier and then frozen. It does
  NOT re-read YouTube's current privacy, because the product promise is
  "we keep what we captured".

  open    (public, age_restricted at capture)
          -> any user with an active subscription to the channel.
             age_restricted counts as open: the age check happens at
             sync time on the worker, not at view time.

  sealed  (private, unlisted, members_only at capture)
          -> only users with an active ChannelOwnership on the channel.
             Unlisted is in here deliberately: it is link-only on
             YouTube, and handing it to every subscriber of a shared
             channel is exactly the over-exposure this module prevents.

  Two caveats worth knowing:

  * has_verified_membership() is implemented but NOT consulted by
    can_user_access_video. A members_only video is sealed, so today it
    is owner-only even for a verified member. That is the conservative
    direction, and it stays until the membership tier is really built.

  * visible_video_filter() adds one rule on top for listings: an "open"
    video that is private on YouTube right now AND has no stored file is
    treated as sealed. See that function for why.

  This summary used to describe a per-privacy-tier rule the code stopped
  implementing, and the tests encoded the docstring rather than the
  behaviour. That gap is part of how this module ended up wired to
  nothing while a listing endpoint leaked private titles.

"Active" everywhere means "no revoke/unsubscribe timestamp set, OR
the timestamp is set but the 30-day grace hasn't elapsed." We keep
the grace open via this helper so a user who flickers their
subscription off+on within 30 days never sees an access gap.

The functions here all take a SQLAlchemy session because the calling
code already has one; passing it is cheaper than re-opening.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import false as sa_false, or_, select, true as sa_true
from sqlalchemy.orm import Session

from app.models import (
    ChannelMembership,
    UserChannelVideo,
    ChannelOwnership,
    UserChannelSubscription,
    User,
    Video,
)


# How long after an unsubscribe / revocation the user retains access.
# Mirrors the deletion grace window so the data they'd be regaining
# access to is still on disk.
GRACE_PERIOD = timedelta(days=30)

# Privacy tiers that mean "not publicly visible on YouTube right now".
# Used to withhold rows we never actually captured - see
# visible_video_filter for why an uncaptured private video is owner-only
# even when its frozen capture-time visibility says "open".
_PRIVATE_NOW = ("private", "members", "members_only", "unlisted")


def _within_grace(ts: Optional[datetime], now: datetime) -> bool:
    """A nullable timestamp counts as 'still in grace' if it's NULL
    (never revoked) or the revocation happened less than GRACE_PERIOD
    ago. Returns True if access should still flow through."""
    if ts is None:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts) < GRACE_PERIOD


def is_channel_owner(
    db: Session,
    user_id: str,
    channel_id: str,
    *,
    now: Optional[datetime] = None,
    grace: bool = True,
) -> bool:
    """True if the user has an active ChannelOwnership row for this
    channel. Active ownership grants private-tier access.

    ``grace=True`` (the default) also accepts a revocation less than
    GRACE_PERIOD old, so a user who disconnects and reconnects does not
    lose access in between. Pass ``grace=False`` where a withdrawn
    authorization must take effect immediately - see visible_video_filter
    for the one case that needs it.
    """
    when = now or datetime.now(timezone.utc)
    row = (
        db.query(ChannelOwnership)
        .filter(
            ChannelOwnership.user_id == user_id,
            ChannelOwnership.channel_id == channel_id,
        )
        .one_or_none()
    )
    if row is None:
        return False
    if not grace:
        return row.revoked_at is None
    return _within_grace(row.revoked_at, when)


def has_active_subscription(
    db: Session,
    user_id: str,
    channel_id: str,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """True if the user has an active (or grace-period)
    UserChannelSubscription for this channel. Required for all
    non-private tiers; the channel owner gets access to private
    videos whether they subscribed or not, but for public content
    they still need to subscribe explicitly (because that's what
    triggers billing inclusion)."""
    when = now or datetime.now(timezone.utc)
    row = (
        db.query(UserChannelSubscription)
        .filter(
            UserChannelSubscription.user_id == user_id,
            UserChannelSubscription.channel_id == channel_id,
        )
        .one_or_none()
    )
    if row is None:
        return False
    return _within_grace(row.unsubscribed_at, when)


def has_verified_membership(
    db: Session,
    user_id: str,
    channel_id: str,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """True if the user has a verified, non-expired, non-revoked
    ChannelMembership row for this channel. Required for the
    members_only tier on top of the regular subscription check.

    No grace period on memberships - if YouTube says the membership
    lapsed, our access lapses immediately. We're not in the business
    of letting a non-paying user keep watching members-only content
    just because we have a 30-day buffer policy on subscriptions.
    """
    when = now or datetime.now(timezone.utc)
    row = (
        db.query(ChannelMembership)
        .filter(
            ChannelMembership.user_id == user_id,
            ChannelMembership.channel_id == channel_id,
        )
        .one_or_none()
    )
    if row is None:
        return False
    if row.revoked_at is not None:
        return False
    if row.expires_at is not None:
        exp = row.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= when:
            return False
    return True


def can_user_access_video(
    db: Session,
    user: User,
    video: Video,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """The all-in-one access check. Returns True iff the user has enough
    authority for the video's archive **visibility** on its channel.

    Branches on ``video.visibility`` (open/sealed), which is stamped at
    capture and frozen - not on YouTube's current privacy. So a video we
    archived while it was public stays accessible to subscribers even
    after the creator privates the source: we keep what we captured.
    'sealed' (private or members-only at capture) is owner-only.
    """
    if video.visibility == "open":
        # Any active subscriber. Owners need a subscription too for the
        # open set (it drives their billing inclusion; no subscription
        # means they haven't asked us to archive it for them).
        return has_active_subscription(
            db, user.id, video.channel_id, now=now
        )

    # 'sealed' - only the channel's authenticated owners.
    return is_channel_owner(db, user.id, video.channel_id, now=now)


def visible_video_filter(
    db: Session,
    user_id: str,
    channel_pk: str,
    *,
    now: Optional[datetime] = None,
):
    """A SQLAlchemy filter clause for "videos of this channel that this
    user may see in a listing".

    Use this instead of ``can_user_access_video`` when filtering a LIST -
    it pushes the rule into SQL rather than fetching every row and
    checking them one at a time, and, more importantly, it is impossible
    to forget a row.

    Returns a clause to hand to ``.filter()``. Callers must still scope
    the query to the channel themselves.

    The rule, in three parts:

    * Channel owners see everything on their own channel. No filter.
    * ``sealed`` (private / unlisted / members-only at capture) is
      owner-only, so a non-owner sees none of it.
    * ``open`` is visible to any active subscriber - EXCEPT when the
      video is private on YouTube *right now* and we hold no file.

    That last exception is the one worth explaining. "Open" is stamped at
    capture and frozen on purpose: we archived it while it was public, so
    a subscriber keeps access even after the creator makes it private -
    "we keep what we captured". That promise rests entirely on our having
    actually captured something. A row with no ``r2_key`` is not an
    archive; it is a title we scraped while the video was public, of a
    video that is private today. Showing that to a stranger leaks the
    creator's current privacy and hands back nothing archival in return,
    so it stays owner-only until there is a real file behind it.
    """
    when = now or datetime.now(timezone.utc)

    if is_channel_owner(db, user_id, channel_pk, now=when, grace=False):
        # Owners see their own channel in full - including sealed videos
        # and private-now rows we never captured. It is their content.
        #
        # grace=False is deliberate, and this is the only place we drop
        # the grace window. Elsewhere the 30 days exist so a user who
        # flickers a subscription off and on never loses their own data.
        # Sealed TITLES are different: the only reason we know those
        # videos exist is that an authenticated worker enumerated them
        # with the owner's credentials. Once that authorization is
        # withdrawn we cannot verify who is asking, so continuing to show
        # them would be the site standing behind a claim it can no longer
        # check. Nothing they actually captured disappears - that comes
        # back through the clause below, which keys off their own archive
        # rows rather than off ownership.
        return sa_true()

    if not has_active_subscription(db, user_id, channel_pk, now=when):
        # Not subscribed and not an owner: nothing at all.
        return sa_false()

    # "We keep what we captured" is scoped to the user who actually
    # captured it, which is UserChannelVideo - the per-user archive row.
    # The first version of this filter used `Video.r2_key IS NOT NULL`
    # instead, and that was wrong in a way that undid the whole fix:
    # r2_key belongs to the SHARED pool row and describes whichever
    # single subscriber archived the file, not the caller. So a row
    # mis-stamped "open" while private-now stayed hidden only until
    # ANYONE archived it, at which point it reappeared for every
    # subscriber. Verified by probe - the stranger went from seeing
    # nothing to seeing the private title the moment the owner synced.
    captured_by_caller = select(UserChannelVideo.video_id).where(
        UserChannelVideo.user_id == user_id
    )
    return (Video.visibility == "open") & (
        Video.privacy_current.notin_(_PRIVATE_NOW)
        | Video.youtube_id.in_(captured_by_caller)
    )


def billable_video_query(
    db: Session,
    user_id: str,
    *,
    now: Optional[datetime] = None,
):
    """Return a SQLAlchemy query that yields every Video the given
    user is billable for at ``now``. Used by the billing recompute
    to integrate byte-hours.

    A video is billable for a user when:
      - the user has an active (or grace) subscription to its channel
      - AND can access it under the privacy rules above
      - AND it has been synced (bytes_stored > 0)

    Returns the Query object, not the rows, so callers can add
    ``with_entities()`` / ``count()`` / iterator-stream-style chains.
    """
    when = now or datetime.now(timezone.utc)
    grace_cutoff = when - GRACE_PERIOD

    # Subscriptions: active or within grace
    subscribed_channels = select(UserChannelSubscription.channel_id).where(
        UserChannelSubscription.user_id == user_id,
        or_(
            UserChannelSubscription.unsubscribed_at.is_(None),
            UserChannelSubscription.unsubscribed_at > grace_cutoff,
        ),
    )

    owned_channels = select(ChannelOwnership.channel_id).where(
        ChannelOwnership.user_id == user_id,
        or_(
            ChannelOwnership.revoked_at.is_(None),
            ChannelOwnership.revoked_at > grace_cutoff,
        ),
    )

    return (
        db.query(Video)
        .filter(Video.bytes_stored.is_not(None), Video.bytes_stored > 0)
        .filter(
            or_(
                # Open: any active subscriber.
                (
                    (Video.visibility == "open")
                    & Video.channel_id.in_(subscribed_channels)
                ),
                # Sealed (private or members-only at capture): owner only.
                (
                    (Video.visibility == "sealed")
                    & Video.channel_id.in_(owned_channels)
                ),
            )
        )
    )
