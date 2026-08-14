"""
Event-driven three-way sync: local_vault <-> cloud_vault, with sync_baseline
as the last-known-good reference used to tell "which side actually changed"
apart from "these two just differ".

Concurrency model: a fixed pool of worker coroutines pulls relative paths
off one shared queue. A `pending` set dedupes — if a path is already queued
or being worked on, a repeat filesystem event is dropped instead of queued
again. This keeps memory bounded by `worker_count`, not by how many distinct
files have ever changed since startup (no per-file task/queue to grow
without limit, no idle-cleanup timers needed).

Anything that can't be acted on right now (cloud content not hydrated yet,
content too small to trust) gets an increasing backoff, not a fixed
every-N-seconds retry forever.

New files wait `stability_window` seconds and get re-checked for size
changes before being pushed/pulled, so a file mid-autosave doesn't get
copied half-written. A both-sides-changed conflict waits the longer
`stabilize_wait` before deciding — long enough for a still-active edit on
either side to finish — then re-hashes instead of trusting stale values.
After any successful push/pull, `cooldown_seconds` (or `big_file_cooldown`
for files over `big_file_threshold`) suppresses repeat events for that path
so autosave-heavy apps don't re-trigger a sync cycle every keystroke.
"""

import os
import time
import shutil
import hashlib
import fnmatch
import asyncio
from dataclasses import dataclass, field

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from .cloud_status import CloudFilter
from .status_file import StatusReporter


def sha256_of(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def atomic_copy(src: str, dst: str):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".oiiaw-tmp"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


@dataclass
class Backoff:
    base_seconds: float
    max_seconds: float
    _until: dict = field(default_factory=dict)
    _streak: dict = field(default_factory=dict)

    def is_active(self, key: str) -> bool:
        return self._until.get(key, 0) > time.time()

    def bump(self, key: str) -> float:
        n = self._streak.get(key, 0) + 1
        self._streak[key] = n
        delay = min(self.base_seconds * (2 ** (n - 1)), self.max_seconds)
        self._until[key] = time.time() + delay
        return delay

    def reset(self, key: str):
        self._streak.pop(key, None)
        self._until.pop(key, None)


@dataclass
class Cooldown:
    """After a successful push/pull, ignore repeat events for this path for
    a bit — otherwise autosave-on-every-keystroke apps re-trigger a sync
    cycle for the same file dozens of times a minute. Big files get a longer
    cooldown since they take longer to actually finish copying/uploading on
    the other side."""

    normal_seconds: float
    big_file_seconds: float
    big_file_threshold: int
    _until: dict = field(default_factory=dict)

    def is_active(self, key: str) -> bool:
        return self._until.get(key, 0) > time.time()

    def start(self, key: str, file_size: int):
        duration = self.big_file_seconds if file_size >= self.big_file_threshold else self.normal_seconds
        self._until[key] = time.time() + duration


class _Watcher(FileSystemEventHandler):
    """Forwards raw watchdog callbacks (which fire on a background thread)
    into the engine's asyncio loop."""

    def __init__(self, notify, root: str):
        self._notify = notify
        self._root = root

    def _relay(self, path: str):
        if path:
            self._notify(path, self._root)

    def on_created(self, event):
        if not event.is_directory:
            self._relay(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._relay(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self._relay(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._relay(event.src_path)
            self._relay(event.dest_path)


class SyncEngine:
    def __init__(self, config, logger, worker_count: int = 4):
        self.config = config
        self.log = logger
        self.cloud = CloudFilter()
        self.backoff = Backoff(config.backoff_seconds, config.backoff_max_seconds)
        self.cooldown = Cooldown(config.cooldown_seconds, config.big_file_cooldown, config.big_file_threshold)
        self.worker_count = worker_count
        self.pending: asyncio.Queue[str] = asyncio.Queue()
        self.queued: set[str] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.status = StatusReporter(getattr(config, "logs_dir", None))
        self.active = 0
        self._shutdown_event = asyncio.Event()

    # ── path helpers ──

    def is_tracked(self, rel_path: str) -> bool:
        name = os.path.basename(rel_path).lower()
        if name in self.config.ignored_files or name.endswith(".tmp"):
            return False
        parts = [p.lower() for p in rel_path.split(os.sep)]
        if any(p in self.config.ignored_dirs for p in parts):
            return False
        as_posix = rel_path.replace(os.sep, "/")
        return not any(fnmatch.fnmatch(as_posix, pattern) for pattern in self.config.ignore_patterns)

    def _relativize(self, abs_path: str, root: str) -> str | None:
        try:
            rel = os.path.normpath(os.path.relpath(abs_path, root))
        except ValueError:
            return None
        return None if rel.startswith("..") else rel

    def discover_tracked_paths(self) -> set[str]:
        found = set()

        def walk(base):
            for cur, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d.lower() not in self.config.ignored_dirs]
                for name in files:
                    rel = os.path.normpath(os.path.relpath(os.path.join(cur, name), base))
                    if self.is_tracked(rel):
                        found.add(rel)

        for root in (self.config.local_vault, self.config.cloud_vault, self.config.sync_baseline):
            if root and os.path.isdir(root):
                walk(root)
        return found

    # ── queue plumbing ──

    def enqueue(self, rel_path: str):
        if rel_path in self.queued:
            return
        self.queued.add(rel_path)
        self.pending.put_nowait(rel_path)

    def _on_fs_event(self, abs_path: str, root: str):
        rel = self._relativize(abs_path, root)
        if rel is None or not self.is_tracked(rel) or self.loop is None:
            return
        self.loop.call_soon_threadsafe(self.enqueue, rel)

    def start_watching(self) -> Observer:
        observer = Observer()
        for root in (self.config.local_vault, self.config.cloud_vault):
            if root and os.path.isdir(root):
                observer.schedule(_Watcher(self._on_fs_event, root), root, recursive=True)
        observer.start()
        return observer

    def request_shutdown(self):
        """Thread-safe: the tray icon calls this from a different thread
        than the one running this engine's asyncio loop."""
        if self.loop:
            self.loop.call_soon_threadsafe(self._shutdown_event.set)

    async def _worker(self):
        while True:
            rel_path = await self.pending.get()
            self.queued.discard(rel_path)
            self.active += 1
            try:
                event = await self.sync_one(rel_path)
                if event:
                    self.status.record_event(event, rel_path)
            except Exception as e:
                self.log.error("ERROR", f"{rel_path}: {e}")
                self.status.record_event("ERROR", rel_path)
            finally:
                self.active -= 1

    # ── three-way sync decision ──

    @staticmethod
    async def _settled(path: str, wait_seconds: float) -> bool:
        """True if `path`'s size is unchanged after waiting — i.e. it's not
        still being actively written (autosave, a slow copy, etc.)."""
        try:
            before = os.path.getsize(path)
        except OSError:
            return False
        await asyncio.sleep(wait_seconds)
        try:
            after = os.path.getsize(path)
        except OSError:
            return False
        return before == after

    def _start_cooldown(self, rel_path: str, synced_path: str):
        try:
            size = os.path.getsize(synced_path)
        except OSError:
            size = 0
        self.cooldown.start(rel_path, size)

    async def sync_one(self, rel_path: str):
        config = self.config
        if self.backoff.is_active(rel_path) or self.cooldown.is_active(rel_path):
            return

        local = os.path.join(config.local_vault, rel_path)
        cloud = os.path.join(config.cloud_vault, rel_path)
        baseline = os.path.join(config.sync_baseline, rel_path)
        local_exists, cloud_exists, baseline_exists = os.path.exists(local), os.path.exists(cloud), os.path.exists(baseline)

        if cloud_exists and not self.cloud.is_content_available(cloud):
            delay = self.backoff.bump(rel_path)
            self.log.info("WAIT", f"{rel_path} — cloud content not fully on disk yet, retry in {delay:.0f}s", level="verbose")
            return
        self.backoff.reset(rel_path)

        if not local_exists and not cloud_exists:
            if baseline_exists:
                os.remove(baseline)
            return

        if local_exists and not cloud_exists and not baseline_exists:
            if os.path.getsize(local) < config.tiny_threshold:
                delay = self.backoff.bump(rel_path)
                self.log.info("SKIP", f"{rel_path} too small — retry in {delay:.0f}s", level="verbose")
                return
            if not await self._settled(local, config.stability_window):
                self.log.info("WAIT", f"{rel_path} not settled yet, deferring", level="verbose")
                return
            atomic_copy(local, cloud)
            atomic_copy(local, baseline)
            self._start_cooldown(rel_path, local)
            self.log.success("PUSH", rel_path, level="verbose")
            return "PUSH"

        if cloud_exists and not local_exists and not baseline_exists:
            if os.path.getsize(cloud) < config.tiny_threshold:
                delay = self.backoff.bump(rel_path)
                self.log.info("SKIP", f"{rel_path} too small — retry in {delay:.0f}s", level="verbose")
                return
            if not await self._settled(cloud, config.stability_window):
                self.log.info("WAIT", f"{rel_path} not settled yet, deferring", level="verbose")
                return
            atomic_copy(cloud, local)
            atomic_copy(cloud, baseline)
            self._start_cooldown(rel_path, cloud)
            self.log.success("PULL", rel_path, level="verbose")
            return "PULL"

        if not local_exists and cloud_exists and baseline_exists:
            if sha256_of(cloud) == sha256_of(baseline):
                os.remove(cloud)
                os.remove(baseline)
                self.log.warn("DELETE", rel_path, level="verbose")
                return "DELETE"
            atomic_copy(cloud, local)
            atomic_copy(cloud, baseline)
            self._start_cooldown(rel_path, cloud)
            self.log.success("PULL", rel_path, level="verbose")
            return "PULL"

        if not cloud_exists and local_exists and baseline_exists:
            if sha256_of(local) == sha256_of(baseline):
                os.remove(local)
                os.remove(baseline)
                self.log.warn("DELETE", rel_path, level="verbose")
                return "DELETE"
            atomic_copy(local, cloud)
            atomic_copy(local, baseline)
            self._start_cooldown(rel_path, local)
            self.log.success("PUSH", rel_path, level="verbose")
            return "PUSH"

        if local_exists and cloud_exists and not baseline_exists:
            # both sides already exist with no baseline yet (fresh vault, or
            # a file that predates oiiaw) — seed it quietly if they already
            # match, only treat it as a real conflict if they don't.
            if sha256_of(local) == sha256_of(cloud):
                atomic_copy(local, baseline)
                return

        local_hash, cloud_hash, baseline_hash = sha256_of(local), sha256_of(cloud), sha256_of(baseline)
        if local_hash == cloud_hash == baseline_hash:
            return
        if local_hash != baseline_hash and cloud_hash == baseline_hash:
            atomic_copy(local, cloud)
            atomic_copy(local, baseline)
            self._start_cooldown(rel_path, local)
            self.log.success("PUSH", rel_path, level="verbose")
            return "PUSH"
        elif cloud_hash != baseline_hash and local_hash == baseline_hash:
            atomic_copy(cloud, local)
            atomic_copy(cloud, baseline)
            self._start_cooldown(rel_path, cloud)
            self.log.success("PULL", rel_path, level="verbose")
            return "PULL"
        else:
            # both sides changed — wait longer in case one is still mid-edit,
            # then decide with fresh hashes instead of the ones we started with.
            await asyncio.sleep(config.stabilize_wait)
            local_hash, cloud_hash = sha256_of(local), sha256_of(cloud)
            if local_hash is not None and local_hash == cloud_hash:
                atomic_copy(local, baseline)
                self._start_cooldown(rel_path, local)
                self.log.info("RESOLVED", f"{rel_path} — settled to the same content while waiting", level="verbose")
                return "RESOLVED"
            winner, loser = (local, cloud) if os.path.getmtime(local) >= os.path.getmtime(cloud) else (cloud, local)
            backup = f"{loser}_CONFLICT_{time.strftime('%Y%m%d_%H%M%S')}"
            shutil.copy2(loser, backup)
            atomic_copy(winner, local)
            atomic_copy(winner, cloud)
            atomic_copy(winner, baseline)
            self._start_cooldown(rel_path, winner)
            self.log.warn("CONFLICT", f"{rel_path} — kept newer, backed up loser", level="important")
            return "CONFLICT"

    # ── main entry ──

    async def run(self):
        self.log.init_log_file()
        self.loop = asyncio.get_running_loop()
        observer = self.start_watching()
        workers = [asyncio.create_task(self._worker()) for _ in range(self.worker_count)]
        try:
            for rel in self.discover_tracked_paths():
                self.enqueue(rel)
            while not self._shutdown_event.is_set():
                state = "syncing" if self.active > 0 or not self.pending.empty() else "idle"
                self.status.write(state, self.pending.qsize())
                self.log.flush()
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
        finally:
            for w in workers:
                w.cancel()
            observer.stop()
            observer.join()
