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

import asyncio
import os
from unittest.mock import patch

import pytest

from oiiaw.cloud_status import CloudFilter, CloudProbe, CloudProbeError, CloudProbeTimeout, PlaceholderInfo, PinState


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


class FakeProcess:
    def __init__(self):
        self.alive = True
        self.terminated = False

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminated = True
        self.alive = False

    def join(self, timeout=None):
        pass

    def kill(self):
        self.alive = False


class FakeConnection:
    def __init__(self, response=None):
        self.response = response
        self.sent = []
        self.closed = False

    def send(self, value):
        self.sent.append(value)

    def poll(self, timeout=None):
        return self.response is not None

    def recv(self):
        return self.response

    def close(self):
        self.closed = True


def make_probe(connection, process, timeout=0.01):
    probe = CloudProbe(timeout_seconds=timeout)
    probe._connection = connection
    probe._process = process
    return probe


def test_isolated_probe_returns_worker_result():
    connection = FakeConnection(("ok", False))
    process = FakeProcess()
    probe = make_probe(connection, process)

    assert asyncio.run(probe.is_content_available("C:/cloud/offline.md")) is False
    assert connection.sent == ["C:/cloud/offline.md"]
    assert process.terminated is False
    probe.close()


def test_isolated_probe_timeout_terminates_stuck_worker():
    connection = FakeConnection()
    process = FakeProcess()
    probe = make_probe(connection, process)

    with pytest.raises(CloudProbeTimeout):
        asyncio.run(probe.is_content_available("C:/cloud/stuck.md"))

    assert process.terminated is True
    assert connection.closed is True
    assert probe._process is None


def test_isolated_probe_surfaces_worker_error():
    probe = make_probe(FakeConnection(("error", "provider failed")), FakeProcess())

    with pytest.raises(CloudProbeError, match="provider failed"):
        asyncio.run(probe.is_content_available("C:/cloud/broken.md"))
