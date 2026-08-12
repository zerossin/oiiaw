"""
The struct layout / call sequence here was validated live against real files
in an actual iCloud vault before this test was written (a pinned file, a
genuinely-empty placeholder, a force-hydrated file, a plain non-placeholder
local file, and several still-offline files) — see project history for the
exact HRESULT/byte-count debugging session. These tests pin down the
decision logic (`is_content_available`'s byte-count comparison) so it can't
silently regress; they don't re-verify the raw ctypes plumbing, which needs
a real Windows cloud-sync folder to exercise at all.
"""

import os
from unittest.mock import patch

from oiiaw.cloud_status import CloudFilter, PlaceholderInfo, PinState


def make_filter():
    cf = CloudFilter.__new__(CloudFilter)  # skip __init__, no real DLL needed
    cf._available = True
    return cf


@patch("os.path.getsize")
def test_not_a_placeholder_is_available(getsize):
    cf = make_filter()
    with patch.object(cf, "get_placeholder_info", return_value=None):
        assert cf.is_content_available("C:/fake/plain.md") is True
    getsize.assert_not_called()


@patch("os.path.getsize", return_value=100)
def test_fully_hydrated_is_available(getsize):
    cf = make_filter()
    info = PlaceholderInfo(on_disk_bytes=100, validated_bytes=100, pin_state=PinState.PINNED, in_sync=True)
    with patch.object(cf, "get_placeholder_info", return_value=info):
        assert cf.is_content_available("C:/fake/full.md") is True


@patch("os.path.getsize", return_value=100)
def test_partially_hydrated_is_unavailable(getsize):
    cf = make_filter()
    info = PlaceholderInfo(on_disk_bytes=0, validated_bytes=0, pin_state=PinState.UNSPECIFIED, in_sync=True)
    with patch.object(cf, "get_placeholder_info", return_value=info):
        assert cf.is_content_available("C:/fake/partial.md") is False


@patch("os.path.getsize", return_value=0)
def test_genuinely_empty_file_is_available(getsize):
    # a real placeholder can legitimately have on_disk_bytes == real_size == 0
    # — there's nothing to fetch, so it must not be treated as "still pending".
    cf = make_filter()
    info = PlaceholderInfo(on_disk_bytes=0, validated_bytes=0, pin_state=PinState.UNPINNED, in_sync=True)
    with patch.object(cf, "get_placeholder_info", return_value=info):
        assert cf.is_content_available("C:/fake/empty.md") is True
