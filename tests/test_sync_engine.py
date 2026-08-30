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

from oiiaw.sync_engine import SyncEngine, _Watcher, conflict_backup_path
from oiiaw.cloud_status import CloudProbeError, CloudProbeTimeout


def make_config(tmp_path):
    return types.SimpleNamespace(
        local_vault=str(tmp_path / "local"),
        cloud_vault=str(tmp_path / "cloud"),
        sync_baseline=str(tmp_path / "baseline"),
        logs_dir=str(tmp_path / "logs"),
        ignored_dirs=set(),
        ignored_files=set(),
        ignore_patterns=[],
        cloud_probe_timeout=0.02,
        cloud_hydrate_timeout=0.02,
        hydrate_retry_initial=0.01,
        hydrate_retry_max=0.02,
        error_retry_initial=0.01,
        error_retry_max=0.02,
        cloud_confirm_retry=0.01,
        delete_grace_seconds=0,
        delete_batch_limit=20,
        delete_batch_window=60,
        protect_nonempty_from_zero=True,
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

    async def available(path):
        return True

    async def confirmed(path):
        return True

    monkeypatch.setattr(eng.cloud, "is_content_available", available)
    monkeypatch.setattr(eng.cloud, "is_content_in_sync", confirmed)
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

    event = asyncio.run(engine.sync_one("note.md"))

    # the loser (older mtime) can end up on either side depending on write
    # timing, so check both instead of assuming which one lost.
    all_files = os.listdir(engine.config.local_vault) + os.listdir(engine.config.cloud_vault)
    assert any("_CONFLICT_" in f for f in all_files)
    assert event[0] == "CONFLICT"
    assert event[1]["conflict_path"].endswith(".md")
    assert ".md_CONFLICT_" not in event[1]["conflict_path"]


def test_conflict_backup_path_keeps_extension_and_avoids_collision(tmp_path, monkeypatch):
    monkeypatch.setattr("oiiaw.sync_engine.time.strftime", lambda pattern: "20260829_120000")
    original = tmp_path / "note.md"

    first = conflict_backup_path(str(original))
    open(first, "wb").close()
    second = conflict_backup_path(str(original))

    assert first.endswith("note_CONFLICT_20260829_120000.md")
    assert second.endswith("note_CONFLICT_20260829_120000_2.md")


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


def test_empty_new_file_pushes(engine):
    local = os.path.join(engine.config.local_vault, "empty.md")
    open(local, "wb").close()

    event = asyncio.run(engine.sync_one("empty.md"))

    assert event == "PUSH_PENDING"
    assert not os.path.exists(os.path.join(engine.config.sync_baseline, "empty.md"))
    for root in (engine.config.local_vault, engine.config.cloud_vault):
        path = os.path.join(root, "empty.md")
        assert os.path.isfile(path)
        assert os.path.getsize(path) == 0


def test_empty_file_then_immediate_content_is_not_lost(engine):
    async def scenario():
        engine.loop = asyncio.get_running_loop()
        local = os.path.join(engine.config.local_vault, "draft.md")
        open(local, "wb").close()
        worker = asyncio.create_task(engine._worker())
        try:
            engine.enqueue("draft.md")
            await asyncio.sleep(0.005)
            with open(local, "w") as f:
                f.write("content added immediately after creating the note")
            engine.enqueue("draft.md")  # the watchdog event raised by that write

            cloud = os.path.join(engine.config.cloud_vault, "draft.md")
            for _ in range(50):
                if os.path.isfile(cloud):
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("the final content was never pushed")

            with open(cloud) as f:
                assert f.read() == "content added immediately after creating the note"
        finally:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker

    asyncio.run(scenario())


def test_cooldown_event_gets_a_delayed_retry(engine):
    async def scenario():
        engine.loop = asyncio.get_running_loop()
        engine.cooldown.start("note.md", 0)

        await engine.sync_one("note.md")
        assert "note.md" in engine._retry_handles

        await asyncio.sleep(0.03)
        assert "note.md" in engine.queued

    asyncio.run(scenario())


def test_unavailable_cloud_file_is_hydrated_and_synced(engine, monkeypatch):
    cloud = os.path.join(engine.config.cloud_vault, "offline.md")
    with open(cloud, "w") as f:
        f.write("cloud content that is not hydrated yet")

    async def unavailable(path):
        return False

    hydrated = []

    async def hydrate(path):
        hydrated.append(path)
        return True

    monkeypatch.setattr(engine.cloud, "is_content_available", unavailable)
    monkeypatch.setattr(engine.cloud, "hydrate", hydrate)

    event = asyncio.run(engine.sync_one("offline.md"))

    assert event == "PULL"
    assert hydrated == [cloud]
    assert "offline.md" not in engine.parked
    with open(os.path.join(engine.config.local_vault, "offline.md")) as f:
        assert f.read() == "cloud content that is not hydrated yet"


def test_failed_hydration_is_automatically_retried(engine, monkeypatch):
    cloud = os.path.join(engine.config.cloud_vault, "offline.md")
    with open(cloud, "w") as f:
        f.write("cloud content that is not hydrated yet")

    async def unavailable(path):
        return False

    async def hydration_pending(path):
        return False

    monkeypatch.setattr(engine.cloud, "is_content_available", unavailable)
    monkeypatch.setattr(engine.cloud, "hydrate", hydration_pending)

    async def scenario():
        engine.loop = asyncio.get_running_loop()
        event = await engine.sync_one("offline.md")
        assert event == "PARK"
        assert "offline.md" in engine.parked
        assert "offline.md" in engine._retry_handles

        await asyncio.sleep(0.02)
        assert "offline.md" not in engine.parked
        assert "offline.md" in engine.queued

    asyncio.run(scenario())


def test_success_after_cloud_retry_clears_red_error_state(engine):
    rel_path = "offline.md"
    content = "content is now available"
    for root in (engine.config.local_vault, engine.config.cloud_vault, engine.config.sync_baseline):
        with open(os.path.join(root, rel_path), "w") as f:
            f.write(content)
    engine.parked.add(rel_path)
    engine._hydrate_attempts[rel_path] = 2
    engine.status.record_event("PROBE_TIMEOUT", rel_path)

    asyncio.run(engine.sync_one(rel_path))

    assert engine.status.last_event["type"] == "RECOVERED"
    assert rel_path not in engine.parked


@pytest.mark.parametrize(
    ("exception", "event_type"),
    [
        (CloudProbeTimeout("stuck"), "PROBE_TIMEOUT"),
        (CloudProbeError("failed"), "PROBE_ERROR"),
    ],
)
def test_cloud_probe_failure_is_parked_without_blocking(engine, monkeypatch, exception, event_type):
    cloud = os.path.join(engine.config.cloud_vault, "offline.md")
    with open(cloud, "w") as f:
        f.write("cloud content")

    async def fail(path):
        raise exception

    monkeypatch.setattr(engine.cloud, "is_content_available", fail)

    async def scenario():
        engine.loop = asyncio.get_running_loop()
        try:
            event = await engine.sync_one("offline.md")

            assert event == event_type
            assert "offline.md" in engine.parked
            assert "offline.md" in engine._retry_handles
        finally:
            engine._cancel_retry("offline.md")

    asyncio.run(scenario())


def test_events_during_sync_are_coalesced_not_run_concurrently(engine):
    async def scenario():
        engine.loop = asyncio.get_running_loop()
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        calls = 0
        running = 0
        max_running = 0

        async def controlled_sync(rel_path):
            nonlocal calls, running, max_running
            calls += 1
            running += 1
            max_running = max(max_running, running)
            if calls == 1:
                first_started.set()
                await release_first.wait()
            running -= 1

        engine.sync_one = controlled_sync
        worker = asyncio.create_task(engine._worker())
        try:
            engine.enqueue("note.md")
            await first_started.wait()
            engine.enqueue("note.md")
            release_first.set()

            for _ in range(50):
                if calls == 2:
                    break
                await asyncio.sleep(0.01)
            assert calls == 2
            assert max_running == 1
        finally:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker

    asyncio.run(scenario())


def test_unexpected_file_error_is_automatically_retried(engine):
    async def scenario():
        engine.loop = asyncio.get_running_loop()
        calls = 0

        async def fail_once(rel_path):
            nonlocal calls
            calls += 1
            raise PermissionError("temporarily locked")

        engine.sync_one = fail_once
        worker = asyncio.create_task(engine._worker())
        try:
            engine.enqueue("locked.md")
            await engine.pending.join()

            assert "locked.md" in engine._retry_handles
            assert engine.status.error_count == 1

            for _ in range(50):
                if calls >= 2:
                    break
                await asyncio.sleep(0.002)
            assert calls >= 2
        finally:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker

    asyncio.run(scenario())


def test_success_after_file_error_clears_error_state(engine):
    async def scenario():
        engine.loop = asyncio.get_running_loop()
        calls = 0

        async def fail_then_recover(rel_path):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PermissionError("temporarily locked")

        engine.sync_one = fail_then_recover
        worker = asyncio.create_task(engine._worker())
        try:
            engine.enqueue("locked.md")
            for _ in range(50):
                if engine.status.last_event and engine.status.last_event["type"] == "RECOVERED":
                    break
                await asyncio.sleep(0.002)

            assert engine.status.last_event["type"] == "RECOVERED"
            assert "locked.md" not in engine._error_attempts
        finally:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker

    asyncio.run(scenario())


def test_file_rename_relays_old_and_new_paths():
    seen = []
    watcher = _Watcher(lambda path, root: seen.append((path, root)), "C:/vault")
    event = types.SimpleNamespace(is_directory=False, src_path="C:/vault/old.md", dest_path="C:/vault/new.md")

    watcher.on_moved(event)

    assert seen == [("C:/vault/old.md", "C:/vault"), ("C:/vault/new.md", "C:/vault")]


def test_renaming_an_empty_note_syncs_the_new_title(engine):
    old_rel = "old title.md"
    new_rel = "new title.md"
    for root in (engine.config.local_vault, engine.config.cloud_vault, engine.config.sync_baseline):
        open(os.path.join(root, old_rel), "wb").close()
    os.replace(
        os.path.join(engine.config.local_vault, old_rel),
        os.path.join(engine.config.local_vault, new_rel),
    )

    async def sync_rename():
        await asyncio.gather(engine.sync_one(old_rel), engine.sync_one(new_rel))
        await asyncio.sleep(0.02)
        await engine.sync_one(new_rel)

    asyncio.run(sync_rename())

    for root in (engine.config.local_vault, engine.config.cloud_vault, engine.config.sync_baseline):
        assert os.path.isfile(os.path.join(root, new_rel))
        assert not os.path.exists(os.path.join(root, old_rel))
    assert os.path.isfile(os.path.join(engine.config.cloud_vault, ".trash", old_rel))


def test_delete_moves_to_trash_instead_of_removing(engine):
    """Deletion is the one sync outcome nothing else backs up — a wrong
    judgment here has no undo. It should land in the vault's own .trash,
    not disappear via os.remove()."""
    local = os.path.join(engine.config.local_vault, "note.md")
    cloud = os.path.join(engine.config.cloud_vault, "note.md")
    baseline = os.path.join(engine.config.sync_baseline, "note.md")
    content = "content long enough to pass the tiny threshold check"
    for path in (local, cloud, baseline):
        with open(path, "w") as f:
            f.write(content)
    os.remove(local)  # local side genuinely deleted it, matching baseline still on cloud

    asyncio.run(engine.sync_one("note.md"))

    assert not os.path.exists(cloud)
    assert not os.path.exists(baseline)
    trashed = os.path.join(engine.config.cloud_vault, ".trash", "note.md")
    assert os.path.exists(trashed)
    with open(trashed) as f:
        assert f.read() == content


def test_directory_placeholder_is_never_processed_as_a_file(engine):
    rel = os.path.join("folder", "placeholder")
    cloud_dir = os.path.join(engine.config.cloud_vault, rel)
    baseline_dir = os.path.join(engine.config.sync_baseline, rel)
    os.makedirs(cloud_dir)
    os.makedirs(baseline_dir)

    event = asyncio.run(engine.sync_one(rel))

    assert event is None
    assert os.path.isdir(cloud_dir)
    assert os.path.isdir(baseline_dir)
    assert not os.path.exists(os.path.join(engine.config.cloud_vault, ".trash", rel))


def test_zero_byte_local_regression_never_overwrites_good_cloud(engine):
    local = os.path.join(engine.config.local_vault, "note.md")
    cloud = os.path.join(engine.config.cloud_vault, "note.md")
    baseline = os.path.join(engine.config.sync_baseline, "note.md")
    good = "important non-empty content"
    for path in (local, cloud, baseline):
        with open(path, "w") as f:
            f.write(good)
    open(local, "wb").close()

    event = asyncio.run(engine.sync_one("note.md"))

    assert event == "BLOCK_ZERO"
    assert open(cloud).read() == good
    assert open(baseline).read() == good


def test_zero_byte_cloud_regression_never_overwrites_good_local(engine):
    local = os.path.join(engine.config.local_vault, "note.md")
    cloud = os.path.join(engine.config.cloud_vault, "note.md")
    baseline = os.path.join(engine.config.sync_baseline, "note.md")
    good = "important non-empty content"
    for path in (local, cloud, baseline):
        with open(path, "w") as f:
            f.write(good)
    open(cloud, "wb").close()

    event = asyncio.run(engine.sync_one("note.md"))

    assert event == "BLOCK_ZERO"
    assert open(local).read() == good
    assert open(baseline).read() == good


def test_zero_nonzero_disagreement_without_baseline_is_preserved(engine):
    local = os.path.join(engine.config.local_vault, "note.md")
    cloud = os.path.join(engine.config.cloud_vault, "note.md")
    open(local, "wb").close()
    with open(cloud, "w") as f:
        f.write("remote content with no confirmed baseline")

    event = asyncio.run(engine.sync_one("note.md"))

    assert event == "BLOCK_ZERO"
    assert os.path.getsize(local) == 0
    assert open(cloud).read() == "remote content with no confirmed baseline"
    assert not os.path.exists(os.path.join(engine.config.sync_baseline, "note.md"))


def test_push_keeps_old_baseline_until_cloud_matches_and_is_confirmed(engine, monkeypatch):
    local = os.path.join(engine.config.local_vault, "note.md")
    cloud = os.path.join(engine.config.cloud_vault, "note.md")
    baseline = os.path.join(engine.config.sync_baseline, "note.md")
    for path in (local, cloud, baseline):
        with open(path, "w") as f:
            f.write("old confirmed version")
    with open(local, "w") as f:
        f.write("new local version that must survive")

    first = asyncio.run(engine.sync_one("note.md"))
    assert first == "PUSH_PENDING"
    assert open(baseline).read() == "old confirmed version"

    # Simulate iCloud replaying the previous server version after our copy.
    with open(cloud, "w") as f:
        f.write("old confirmed version")
    engine.cooldown._until.clear()
    second = asyncio.run(engine.sync_one("note.md"))
    assert second == "PUSH_PENDING"
    assert open(local).read() == "new local version that must survive"
    assert open(baseline).read() == "old confirmed version"

    engine.cooldown._until.clear()
    third = asyncio.run(engine.sync_one("note.md"))
    assert third == "RESOLVED"
    assert open(baseline).read() == "new local version that must survive"


def test_transient_zero_cloud_after_push_is_blocked_not_pulled(engine):
    local = os.path.join(engine.config.local_vault, "note.md")
    cloud = os.path.join(engine.config.cloud_vault, "note.md")
    baseline = os.path.join(engine.config.sync_baseline, "note.md")
    for path in (local, cloud, baseline):
        with open(path, "w") as f:
            f.write("old confirmed version")
    with open(local, "w") as f:
        f.write("new local version")

    assert asyncio.run(engine.sync_one("note.md")) == "PUSH_PENDING"
    open(cloud, "wb").close()
    engine.cooldown._until.clear()

    assert asyncio.run(engine.sync_one("note.md")) == "BLOCK_ZERO"
    assert open(local).read() == "new local version"
    assert open(baseline).read() == "old confirmed version"


def test_deleted_file_resurrection_is_suppressed_by_tombstone(engine):
    local = os.path.join(engine.config.local_vault, "note.md")
    cloud = os.path.join(engine.config.cloud_vault, "note.md")
    baseline = os.path.join(engine.config.sync_baseline, "note.md")
    content = "confirmed content"
    for path in (local, cloud, baseline):
        with open(path, "w") as f:
            f.write(content)
    os.remove(local)

    assert asyncio.run(engine.sync_one("note.md")) == "DELETE"
    with open(cloud, "w") as f:
        f.write(content)  # stale iCloud replay

    assert asyncio.run(engine.sync_one("note.md")) == "DELETE_REPLAY"
    assert not os.path.exists(local)
    assert not os.path.exists(cloud)


def test_mass_delete_fuse_stops_additional_deletions(engine):
    engine.config.delete_batch_limit = 1
    for name in ("one.md", "two.md"):
        for root in (engine.config.local_vault, engine.config.cloud_vault, engine.config.sync_baseline):
            with open(os.path.join(root, name), "w") as f:
                f.write("confirmed content")
        os.remove(os.path.join(engine.config.local_vault, name))

    assert asyncio.run(engine.sync_one("one.md")) == "DELETE"
    assert asyncio.run(engine.sync_one("two.md")) == "DELETE_FUSE"
    assert os.path.exists(os.path.join(engine.config.cloud_vault, "two.md"))


def test_ignore_patterns_excludes_matching_paths(tmp_path):
    cfg = make_config(tmp_path)
    cfg.ignore_patterns = ["*.canvas"]
    eng = SyncEngine(cfg, NullLogger())

    assert eng.is_tracked("notes/diagram.canvas") is False
    assert eng.is_tracked("notes/diagram.md") is True


def test_internal_atomic_copy_file_is_not_tracked(engine):
    assert engine.is_tracked("notes/note.md.oiiaw-tmp") is False


def test_missing_cloud_root_is_not_mistaken_for_remote_deletion(engine):
    local = os.path.join(engine.config.local_vault, "note.md")
    with open(local, "w") as f:
        f.write("must remain local while iCloud is unavailable")
    os.rmdir(engine.config.cloud_vault)

    with pytest.raises(FileNotFoundError, match="iCloud 폴더"):
        asyncio.run(engine.sync_one("note.md"))

    assert os.path.isfile(local)
    assert not os.path.exists(os.path.join(engine.config.local_vault, ".trash", "note.md"))
