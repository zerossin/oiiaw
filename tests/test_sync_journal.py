import json
import threading

from oiiaw.sync_journal import SyncJournal


def test_journal_persists_generations_and_tombstones_across_restart(tmp_path):
    db = tmp_path / "sync_state.db"
    first = SyncJournal(str(db))
    first.remember_local_generation("note.md", "hash-b", ttl=86400, limit=16, now=1000)
    first.put_tombstone("deleted.md", "deleted.md", "local", "deleted-hash", now=1001)
    first.close()

    restarted = SyncJournal(str(db))
    assert restarted.is_local_generation("note.md", "hash-b", ttl=86400, now=1002)
    tombstone = restarted.get_tombstone("deleted.md")
    assert tombstone.path == "deleted.md"
    assert tombstone.missing_side == "local"
    assert tombstone.content_hash == "deleted-hash"
    restarted.close()


def test_generation_history_is_bounded_and_expires(tmp_path):
    journal = SyncJournal(str(tmp_path / "sync_state.db"))
    for index in range(5):
        journal.remember_local_generation(
            "note.md", f"hash-{index}", ttl=100, limit=3, now=1000 + index,
        )

    assert not journal.is_local_generation("note.md", "hash-0", ttl=100, now=1005)
    assert not journal.is_local_generation("note.md", "hash-1", ttl=100, now=1005)
    assert journal.is_local_generation("note.md", "hash-4", ttl=100, now=1005)
    assert not journal.is_local_generation("note.md", "hash-4", ttl=100, now=1200)
    journal.close()


def test_tombstone_atomically_clears_local_generation_history(tmp_path):
    journal = SyncJournal(str(tmp_path / "sync_state.db"))
    journal.remember_local_generation("note.md", "hash-b", ttl=86400, limit=16)
    journal.put_tombstone("note.md", "note.md", "local", "hash-b")

    assert not journal.is_local_generation("note.md", "hash-b", ttl=86400)
    assert journal.clear_tombstone("note.md") is True
    assert journal.get_tombstone("note.md") is None
    journal.close()


def test_legacy_json_is_migrated_without_deleting_recovery_state(tmp_path):
    legacy = tmp_path / "sync_state.json"
    legacy.write_text(json.dumps({
        "tombstones": {
            "deleted.md": {
                "path": "Deleted.md",
                "missing_side": "local",
                "hash": "deleted-hash",
                "time": 1000,
            }
        },
        "local_generations": {
            "note.md": [{"hash": "hash-b", "time": 1001}]
        },
    }), encoding="utf-8")

    journal = SyncJournal(str(tmp_path / "sync_state.db"), str(legacy))

    assert journal.get_tombstone("deleted.md").content_hash == "deleted-hash"
    assert journal.is_local_generation("note.md", "hash-b", ttl=86400, now=1002)
    assert legacy.is_file()
    journal.close()


def test_malformed_legacy_timestamps_do_not_block_startup(tmp_path):
    legacy = tmp_path / "sync_state.json"
    legacy.write_text(json.dumps({
        "local_generations": {
            "note.md": [{"hash": "hash-b", "time": "not-a-number"}]
        }
    }), encoding="utf-8")

    journal = SyncJournal(str(tmp_path / "sync_state.db"), str(legacy))

    assert journal.is_local_generation("note.md", "hash-b", ttl=86400)
    journal.close()


def test_journal_can_be_constructed_on_tray_thread_and_used_by_engine_thread(tmp_path):
    journal = SyncJournal(str(tmp_path / "sync_state.db"))
    errors = []

    def engine_work():
        try:
            journal.remember_local_generation("note.md", "hash-b", ttl=86400, limit=16)
            journal.put_tombstone("deleted.md", "deleted.md", "local", "deleted-hash")
        except Exception as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    worker = threading.Thread(target=engine_work)
    worker.start()
    worker.join()

    assert errors == []
    assert journal.is_local_generation("note.md", "hash-b", ttl=86400)
    assert journal.get_tombstone("deleted.md").content_hash == "deleted-hash"
    journal.close()
