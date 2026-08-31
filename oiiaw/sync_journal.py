"""Transactional per-path provenance and deletion state.

The baseline directory stores recoverable confirmed bytes. SQLite stores the
small amount of lineage that hashes alone cannot express: which generations
originated locally and which deletions must suppress provider resurrection.
"""

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Tombstone:
    path: str
    missing_side: str
    content_hash: str
    created_at: float


class SyncJournal:
    def __init__(self, db_path: str | None, legacy_json_path: str | None = None):
        self.db_path = db_path or ":memory:"
        if db_path:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.db_path,
            timeout=10,
            check_same_thread=False,
        )
        with self._lock, self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tombstones (
                    path_key TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    missing_side TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_generations (
                    path_key TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (path_key, content_hash)
                );
                CREATE INDEX IF NOT EXISTS local_generations_created
                    ON local_generations(created_at);
                """
            )
        self._migrate_legacy_json(legacy_json_path)

    def close(self):
        with self._lock:
            connection = self._connection
            self._connection = None
            if connection is not None:
                connection.close()

    def _execute(self, sql: str, parameters=()):
        if self._connection is None:
            raise RuntimeError("sync journal is closed")
        return self._connection.execute(sql, parameters)

    def _migrate_legacy_json(self, path: str | None):
        if not path or not os.path.isfile(path):
            return
        with self._lock:
            migrated = self._execute(
                "SELECT value FROM metadata WHERE key = 'legacy_json_migrated'"
            ).fetchone()
            if migrated:
                return
            try:
                with open(path, "r", encoding="utf-8") as stream:
                    state = json.load(stream)
            except (OSError, json.JSONDecodeError, AttributeError):
                state = {}
            now = time.time()

            def timestamp(value) -> float:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return now

            with self._connection:
                for path_key, item in (state.get("tombstones", {}) or {}).items():
                    if not isinstance(item, dict):
                        continue
                    rel_path = item.get("path")
                    missing_side = item.get("missing_side")
                    content_hash = item.get("hash")
                    if not all(isinstance(value, str) for value in (rel_path, missing_side, content_hash)):
                        continue
                    self._execute(
                        """INSERT OR REPLACE INTO tombstones
                           (path_key, path, missing_side, content_hash, created_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (path_key, rel_path, missing_side, content_hash, timestamp(item.get("time"))),
                    )
                for path_key, entries in (state.get("local_generations", {}) or {}).items():
                    if not isinstance(entries, list):
                        continue
                    for entry in entries:
                        if not isinstance(entry, dict) or not isinstance(entry.get("hash"), str):
                            continue
                        self._execute(
                            """INSERT OR REPLACE INTO local_generations
                               (path_key, content_hash, created_at) VALUES (?, ?, ?)""",
                            (path_key, entry["hash"], timestamp(entry.get("time"))),
                        )
                self._execute(
                    "INSERT OR REPLACE INTO metadata (key, value) VALUES ('legacy_json_migrated', ?)",
                    (str(now),),
                )

    def remember_local_generation(
        self,
        path_key: str,
        content_hash: str,
        *,
        ttl: float,
        limit: int,
        now: float | None = None,
    ):
        now = time.time() if now is None else now
        ttl = max(60.0, float(ttl))
        limit = max(2, int(limit))
        with self._lock, self._connection:
            self._execute(
                "DELETE FROM local_generations WHERE created_at < ?",
                (now - ttl,),
            )
            self._execute(
                """INSERT OR REPLACE INTO local_generations
                   (path_key, content_hash, created_at) VALUES (?, ?, ?)""",
                (path_key, content_hash, now),
            )
            self._execute(
                """DELETE FROM local_generations
                   WHERE path_key = ? AND content_hash NOT IN (
                       SELECT content_hash FROM local_generations
                       WHERE path_key = ? ORDER BY created_at DESC LIMIT ?
                   )""",
                (path_key, path_key, limit),
            )

    def is_local_generation(
        self,
        path_key: str,
        content_hash: str | None,
        *,
        ttl: float,
        now: float | None = None,
    ) -> bool:
        if not content_hash:
            return False
        now = time.time() if now is None else now
        with self._lock:
            row = self._execute(
                """SELECT 1 FROM local_generations
                   WHERE path_key = ? AND content_hash = ? AND created_at >= ?""",
                (path_key, content_hash, now - max(60.0, float(ttl))),
            ).fetchone()
        return row is not None

    def clear_local_generations(self, path_key: str):
        with self._lock, self._connection:
            self._execute("DELETE FROM local_generations WHERE path_key = ?", (path_key,))

    def get_tombstone(self, path_key: str) -> Tombstone | None:
        with self._lock:
            row = self._execute(
                """SELECT path, missing_side, content_hash, created_at
                   FROM tombstones WHERE path_key = ?""",
                (path_key,),
            ).fetchone()
        return Tombstone(*row) if row else None

    def put_tombstone(
        self,
        path_key: str,
        path: str,
        missing_side: str,
        content_hash: str,
        *,
        now: float | None = None,
    ):
        now = time.time() if now is None else now
        with self._lock, self._connection:
            self._execute(
                """INSERT OR REPLACE INTO tombstones
                   (path_key, path, missing_side, content_hash, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (path_key, path, missing_side, content_hash, now),
            )
            self._execute("DELETE FROM local_generations WHERE path_key = ?", (path_key,))

    def clear_tombstone(self, path_key: str) -> bool:
        with self._lock, self._connection:
            cursor = self._execute("DELETE FROM tombstones WHERE path_key = ?", (path_key,))
        return cursor.rowcount > 0
