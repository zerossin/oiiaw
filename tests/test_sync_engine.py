"""
Regression test for a real bug found tonight: running oiiaw against an
already-synced vault with no sync_baseline yet flagged every single
already-identical file as a conflict, spamming `_CONFLICT_<timestamp>`
backup files everywhere. Root cause: the "baseline missing" case fell
through to the final all-three-hashes branch, where a missing baseline
hash (None) never equals either real hash, so it always looked like both
sides had diverged.
"""

import asyncio
import os
import types

import pytest

from oiiaw.sync_engine import SyncEngine


def make_config(tmp_path):
    return types.SimpleNamespace(
        local_vault=str(tmp_path / "local"),
        cloud_vault=str(tmp_path / "cloud"),
        sync_baseline=str(tmp_path / "baseline"),
        ignored_dirs=set(),
        ignored_files=set(),
        ignore_patterns=[],
        tiny_threshold=8,
        backoff_seconds=30,
        backoff_max_seconds=300,
        cooldown_seconds=0.01,
        big_file_threshold=102400,
        big_file_cooldown=0.01,
        stability_window=0.01,
        stabilize_wait=0.01,
    )


class NullLogger:
    def info(self, *a, **k): pass
    def warn(self, *a, **k): pass
    def error(self, *a, **k): pass
    def success(self, *a, **k): pass


@pytest.fixture
def engine(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    for d in (cfg.local_vault, cfg.cloud_vault, cfg.sync_baseline):
        os.makedirs(d)
    eng = SyncEngine(cfg, NullLogger())
    monkeypatch.setattr(eng.cloud, "is_content_available", lambda path: True)
    return eng


def test_matching_files_with_no_baseline_seed_quietly(engine, tmp_path):
    local = os.path.join(engine.config.local_vault, "note.md")
    cloud = os.path.join(engine.config.cloud_vault, "note.md")
    with open(local, "w") as f:
        f.write("same content, long enough to pass the tiny threshold")
    with open(cloud, "w") as f:
        f.write("same content, long enough to pass the tiny threshold")

    asyncio.run(engine.sync_one("note.md"))

    baseline = os.path.join(engine.config.sync_baseline, "note.md")
    assert os.path.exists(baseline)
    assert os.listdir(engine.config.local_vault) == ["note.md"]
    assert os.listdir(engine.config.cloud_vault) == ["note.md"]


def test_genuinely_different_files_with_no_baseline_are_a_real_conflict(engine):
    local = os.path.join(engine.config.local_vault, "note.md")
    cloud = os.path.join(engine.config.cloud_vault, "note.md")
    with open(local, "w") as f:
        f.write("local version, long enough to pass the tiny threshold")
    with open(cloud, "w") as f:
        f.write("cloud version, long enough to pass the tiny threshold, but different")

    asyncio.run(engine.sync_one("note.md"))

    # the loser (older mtime) can end up on either side depending on write
    # timing, so check both instead of assuming which one lost.
    all_files = os.listdir(engine.config.local_vault) + os.listdir(engine.config.cloud_vault)
    assert any("_CONFLICT_" in f for f in all_files)


def test_settled_true_when_size_unchanged(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("hello")
    assert asyncio.run(SyncEngine._settled(str(p), 0.01)) is True


def test_settled_false_when_size_changes_during_wait(tmp_path, monkeypatch):
    p = tmp_path / "f.txt"
    p.write_text("hello")
    sizes = iter([5, 999])
    monkeypatch.setattr(os.path, "getsize", lambda path: next(sizes))
    assert asyncio.run(SyncEngine._settled(str(p), 0.01)) is False


def test_cooldown_starts_after_successful_push(engine):
    local = os.path.join(engine.config.local_vault, "note.md")
    with open(local, "w") as f:
        f.write("first version, long enough to pass the tiny threshold")

    asyncio.run(engine.sync_one("note.md"))

    assert engine.cooldown.is_active("note.md") is True


def test_ignore_patterns_excludes_matching_paths(tmp_path):
    cfg = make_config(tmp_path)
    cfg.ignore_patterns = ["*.canvas"]
    eng = SyncEngine(cfg, NullLogger())

    assert eng.is_tracked("notes/diagram.canvas") is False
    assert eng.is_tracked("notes/diagram.md") is True
