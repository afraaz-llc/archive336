"""Where a channel's @handle comes from.

The AFRFX channel's detail page rendered its raw UC id
("UCmzj3lQbWvZIlT9GhQGe7EQ") where the other channel showed "@afraaz".
Two bugs stacked:

The add flow never gave the handle to the shared-pool row. It stripped
"handle" out of metadata_json - correctly, since it belongs in the
structured Channel.handle column - and then called ensure_channel
without passing it. Removed from one home, never written to the other.

And the value it would have stored was "@" + the display NAME, which is
a different string that agrees often enough to pass review. "AFRFX"
gives "@AFRFX" by luck. "Afraaz 🗿" gives "@Afraaz 🗿", which is not an
address anyone can visit.
"""
from __future__ import annotations

import pytest

from app.routes.youtube import _handle_from_input


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://www.youtube.com/@AFRFX", "@AFRFX"),
        ("youtube.com/@afraaz/videos", "@afraaz"),
        ("@AFRFX", "@AFRFX"),
        ("  @AFRFX  ", "@AFRFX"),
        ("youtube.com/@some.handle_x-1", "@some.handle_x-1"),
        # No handle present - the caller falls back to the scrape, and
        # must not invent one out of the id.
        ("UCmzj3lQbWvZIlT9GhQGe7EQ", ""),
        ("https://youtube.com/channel/UCQov0Qdzr7GpfUfpWhPzAuw", ""),
        ("", ""),
    ],
)
def test_handle_read_from_what_the_user_typed(raw, expected):
    assert _handle_from_input(raw) == expected


def test_a_display_name_is_not_a_handle():
    """The regression, stated as the rule it broke.

    The old code built the handle from the channel's display name. These
    are the owner's two real channels: one where that coincidence holds
    and one where it does not.
    """
    assert _handle_from_input("AFRFX") == "", "a bare name yields no handle"
    assert _handle_from_input("Afraaz 🗿") == "", "even less so with an emoji"
