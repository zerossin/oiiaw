"""
Event-driven three-way sync: local_vault <-> cloud_vault, with sync_baseline
as the last-known-good reference used to tell "which side actually changed"
apart from "these two just differ".

Concurrency model: a fixed pool of worker coroutines pulls relative paths
off one shared queue. `queued` dedupes waiting work; `in_flight` plus `dirty`
serializes each path and coalesces events received while it is active. This
keeps memory bounded by the number of paths currently needing work, with no
per-file worker or idle-cleanup task.

Anything that can't be acted on right now (a file still being written or an
active cooldown) gets a scheduled retry. Offline cloud placeholders are
hydrated through an isolated helper process; failures are parked temporarily
and retried with bounded backoff.
Filesystem events that arrive while the same path is being processed are
coalesced into one follow-up pass instead of being dropped or run concurrently.

Every changed file waits `stability_window` seconds and must keep the same
size, mtime and hash before being pushed/pulled, so a file mid-autosave
doesn't get copied half-written. A both-sides-changed conflict waits the
longer `stabilize_wait` before deciding. A local push does not advance the
baseline until iCloud reports the copied bytes as confirmed; stale provider
echoes therefore cannot masquerade as legitimate remote edits.

Destructive paths have additional interlocks: directory placeholders are
never treated as files, non-empty baselines cannot regress to zero bytes,
deletions wait through a grace period, a persistent tombstone suppresses
cloud resurrection, and a rate fuse stops mass deletion batches.
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
import json
from collections import deque
from dataclasses import dataclass, field

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from .cloud_status import CloudProbe, CloudProbeError, CloudProbeTimeout
from .status_file import StatusReporter


def sha256_of(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    if not os.path.isfile(path):
        raise IsADirectoryError(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_copy(src: str, dst: str):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".oiiaw-tmp"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def trash_move(vault_root: str, rel_path: str):
    """Deletion is the one sync outcome nothing else backs up — PUSH/PULL
    just copy and CONFLICT keeps a backup, so a wrong DELETE judgment would
    otherwise lose a file with no way back. Moves it into the vault's own
    `.trash` (already excluded from sync, and where Obsidian users already
    look for deleted notes) instead of removing it outright."""
    src = os.path.join(vault_root, rel_path)
    dst = os.path.join(vault_root, ".trash", rel_path)
    if os.path.exists(dst):
        stem, ext = os.path.splitext(dst)
        dst = f"{stem}_{time.strftime('%Y%m%d_%H%M%S')}{ext}"
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)


def conflict_backup_path(path: str) -> str:
    """Keep the real extension so Markdown backups remain openable notes."""
    stem, ext = os.path.splitext(path)
    base = f"{stem}_CONFLICT_{time.strftime('%Y%m%d_%H%M%S')}"
    candidate = base + ext
    counter = 2
    while os.path.exists(candidate):
        candidate = f"{base}_{counter}{ext}"
        counter += 1
    return candidate


@dataclass
class Cooldown:
    """After a successful push/pull, defer repeat events for this path for a
    bit — otherwise autosave-on-every-keystroke apps re-trigger a sync cycle
    dozens of times a minute. Big files get a longer cooldown since they take
    longer to actually finish copying/uploading on the other side."""

    normal_seconds: float
    big_file_seconds: float
    big_file_threshold: int
    _until: dict = field(default_factory=dict)

    def is_active(self, key: str) -> bool:
        return self.remaining(key) > 0

    def remaining(self, key: str) -> float:
        return max(0.0, self._until.get(key, 0) - time.time())

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
        self.cloud = CloudProbe(
            getattr(config, "cloud_probe_timeout", 5),
            getattr(config, "cloud_hydrate_timeout", 30),
        )
        self.cooldown = Cooldown(config.cooldown_seconds, config.big_file_cooldown, config.big_file_threshold)
        self.worker_count = worker_count
        self.pending: asyncio.Queue[str] = asyncio.Queue()
        self.queued: set[str] = set()
        self.in_flight: set[str] = set()
        self.dirty: set[str] = set()
        self.parked: set[str] = set()
        self._hydrate_attempts: dict[str, int] = {}
        self._error_attempts: dict[str, int] = {}
        self._retry_handles: dict[str, asyncio.TimerHandle] = {}
        self._delete_candidates: dict[tuple[str, str], float] = {}
        self._recent_deletes: deque[float] = deque()
        self._delete_fuse_tripped = False
        self._safety_blocks: set[str] = set()
        self._state_path = None
        logs_dir = getattr(config, "logs_dir", None)
        if logs_dir:
            self._state_path = os.path.join(logs_dir, "sync_state.json")
        self._tombstones: dict[str, dict] = self._load_safety_state()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.status = StatusReporter(getattr(config, "logs_dir", None))
        self.active = 0
        self._shutdown_event = asyncio.Event()

    # ── path helpers ──

    @staticmethod
    def _state_key(rel_path: str) -> str:
        return os.path.normcase(os.path.normpath(rel_path))

    def _load_safety_state(self) -> dict[str, dict]:
        if not self._state_path:
            return {}
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            tombstones = data.get("tombstones", {})
            return tombstones if isinstance(tombstones, dict) else {}
        except (OSError, json.JSONDecodeError, AttributeError):
            return {}

    def _save_safety_state(self):
        if not self._state_path:
            return
        os.makedirs(os.path.dirname(self._state_path), exist_ok=True)
        tmp = self._state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"tombstones": self._tombstones}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._state_path)

    def _record_tombstone(self, rel_path: str, missing_side: str, content_hash: str):
        self._tombstones[self._state_key(rel_path)] = {
            "path": rel_path,
            "missing_side": missing_side,
            "hash": content_hash,
            "time": time.time(),
        }
        self._save_safety_state()

    def _clear_tombstone(self, rel_path: str):
        if self._tombstones.pop(self._state_key(rel_path), None) is not None:
            self._save_safety_state()

    def _deletion_ready(self, rel_path: str, missing_side: str) -> bool:
        grace = max(0.0, float(getattr(self.config, "delete_grace_seconds", 30)))
        key = (self._state_key(rel_path), missing_side)
        now = time.time()
        first_seen = self._delete_candidates.setdefault(key, now)
        remaining = grace - (now - first_seen)
        if remaining > 0:
            self._schedule_retry(rel_path, remaining)
            self.log.warn(
                "DELETE_WAIT",
                f"{rel_path} — {missing_side} disappearance must remain stable for {remaining:.0f}s",
                level="verbose",
            )
            return False
        self._delete_candidates.pop(key, None)
        return True

    def _allow_destructive_delete(self, rel_path: str) -> bool:
        if self._delete_fuse_tripped:
            return False
        now = time.time()
        window = max(1.0, float(getattr(self.config, "delete_batch_window", 60)))
        limit = max(1, int(getattr(self.config, "delete_batch_limit", 20)))
        while self._recent_deletes and now - self._recent_deletes[0] > window:
            self._recent_deletes.popleft()
        if len(self._recent_deletes) >= limit:
            self._delete_fuse_tripped = True
            self.log.error(
                "DELETE_FUSE",
                f"blocked mass deletion at {rel_path}: {limit} deletes within {window:.0f}s",
                level="important",
            )
            return False
        self._recent_deletes.append(now)
        return True

    def _is_zero_regression(self, candidate: str, baseline: str) -> bool:
        if not getattr(self.config, "protect_nonempty_from_zero", True):
            return False
        try:
            return os.path.getsize(baseline) > 0 and os.path.getsize(candidate) == 0
        except OSError:
            return False

    def _block_zero_regression(self, rel_path: str, side: str):
        self._safety_blocks.add(rel_path)
        delay = max(1.0, float(getattr(self.config, "stability_window", 3)))
        self._schedule_retry(rel_path, delay)
        self.log.error(
            "BLOCK_ZERO",
            f"{rel_path} — refused to replace a non-empty baseline with a zero-byte {side} file",
            level="important",
        )

    async def _cloud_is_confirmed(self, rel_path: str, cloud_path: str) -> bool:
        try:
            confirmed = await self.cloud.is_content_in_sync(cloud_path)
        except (CloudProbeTimeout, CloudProbeError) as exc:
            delay = self._park_for_retry(rel_path)
            self.log.error("PROBE", f"{rel_path} — {exc}; retrying in {delay:.0f}s", level="important")
            return False
        if confirmed:
            return True
        delay = max(1.0, float(getattr(self.config, "cloud_confirm_retry", 5)))
        self._schedule_retry(rel_path, delay)
        self.log.info(
            "CLOUD_WAIT",
            f"{rel_path} — waiting for iCloud to confirm the current bytes",
            level="verbose",
        )
        return False

    def is_tracked(self, rel_path: str) -> bool:
        name = os.path.basename(rel_path).lower()
        if name in self.config.ignored_files or name.endswith((".tmp", ".oiiaw-tmp")):
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

    def enqueue(self, rel_path: str, wake_parked: bool = False):
        if wake_parked:
            self.parked.discard(rel_path)
        elif rel_path in self.parked:
            return
        if rel_path in self.in_flight:
            self.dirty.add(rel_path)
            return
        if rel_path in self.queued:
            return
        self.queued.add(rel_path)
        self.pending.put_nowait(rel_path)

    def _schedule_retry(self, rel_path: str, delay: float, wake_parked: bool = False):
        """Coalesces delayed retries without losing the earliest wake-up.

        A retry re-enters the normal queue, so the same per-path serialization
        rules apply to timer wake-ups and filesystem events alike.
        """
        loop = self.loop or asyncio.get_running_loop()
        target = loop.time() + max(0.01, delay)
        current = self._retry_handles.get(rel_path)
        if current and not current.cancelled():
            if current.when() <= target:
                return
            current.cancel()

        def wake():
            self._retry_handles.pop(rel_path, None)
            self.enqueue(rel_path, wake_parked)

        self._retry_handles[rel_path] = loop.call_at(target, wake)

    def _cancel_retry(self, rel_path: str):
        handle = self._retry_handles.pop(rel_path, None)
        if handle:
            handle.cancel()

    def _park_for_retry(self, rel_path: str) -> float:
        attempt = self._hydrate_attempts.get(rel_path, 0) + 1
        self._hydrate_attempts[rel_path] = attempt
        initial = getattr(self.config, "hydrate_retry_initial", 15)
        maximum = getattr(self.config, "hydrate_retry_max", 300)
        delay = min(initial * (2 ** min(attempt - 1, 20)), maximum)
        self.parked.add(rel_path)
        self._schedule_retry(rel_path, delay, wake_parked=True)
        return delay

    def _hydration_succeeded(self, rel_path: str) -> bool:
        was_retrying = rel_path in self.parked or rel_path in self._hydrate_attempts
        self.parked.discard(rel_path)
        self._hydrate_attempts.pop(rel_path, None)
        return was_retrying

    def _retry_after_error(self, rel_path: str) -> float:
        """Retry unexpected per-file failures without stalling other files."""
        attempt = self._error_attempts.get(rel_path, 0) + 1
        self._error_attempts[rel_path] = attempt
        initial = getattr(self.config, "error_retry_initial", 5)
        maximum = getattr(self.config, "error_retry_max", 300)
        delay = min(initial * (2 ** min(attempt - 1, 20)), maximum)
        self._schedule_retry(rel_path, delay, wake_parked=True)
        return delay

    def _on_fs_event(self, abs_path: str, root: str):
        rel = self._relativize(abs_path, root)
        if rel is None or not self.is_tracked(rel) or self.loop is None:
            return
        self.loop.call_soon_threadsafe(self.enqueue, rel, True)

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
            self.in_flight.add(rel_path)
            self.active += 1
            try:
                was_retrying = rel_path in self._error_attempts
                event = await self.sync_one(rel_path)
                self._error_attempts.pop(rel_path, None)
                if event:
                    if isinstance(event, tuple):
                        event_type, details = event
                        self.status.record_event(event_type, rel_path, **details)
                    else:
                        self.status.record_event(event, rel_path)
                elif was_retrying:
                    self.log.success("RECOVERED", rel_path, level="verbose")
                    self.status.record_event("RECOVERED", rel_path)
            except Exception as e:
                delay = self._retry_after_error(rel_path)
                self.log.error("ERROR", f"{rel_path}: {e}; retrying in {delay:.0f}s")
                self.status.record_event("ERROR", rel_path)
            finally:
                self.active -= 1
                self.in_flight.discard(rel_path)
                self.pending.task_done()
                if rel_path in self.dirty:
                    self.dirty.discard(rel_path)
                    self.enqueue(rel_path, True)

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

    @staticmethod
    async def _content_settled(path: str, wait_seconds: float) -> bool:
        """Require bytes and metadata to remain identical across the window.

        Existing-file saves often truncate and rewrite in multiple steps. A
        size-only check can accept a transient zero-byte file or two different
        same-sized versions, so destructive copies use a full fingerprint.
        """
        try:
            before_stat = os.stat(path)
            before_hash = sha256_of(path)
        except OSError:
            return False
        await asyncio.sleep(wait_seconds)
        try:
            after_stat = os.stat(path)
            after_hash = sha256_of(path)
        except OSError:
            return False
        return (
            before_stat.st_size == after_stat.st_size
            and before_stat.st_mtime_ns == after_stat.st_mtime_ns
            and before_hash == after_hash
        )

    def _defer_unsettled(self, rel_path: str):
        delay = max(0.01, float(getattr(self.config, "stability_window", 3)))
        self._schedule_retry(rel_path, delay)
        self.log.info("WAIT", f"{rel_path} not settled yet, deferring", level="verbose")

    def _handle_tombstone(
        self,
        rel_path: str,
        local: str,
        cloud: str,
        local_exists: bool,
        cloud_exists: bool,
    ):
        tombstone = self._tombstones.get(self._state_key(rel_path))
        if not tombstone:
            return None

        missing_side = tombstone.get("missing_side")
        source_exists = local_exists if missing_side == "local" else cloud_exists
        if source_exists:
            # The side that initiated the deletion has intentionally restored
            # the path. It is now a normal new/changed file again.
            self._clear_tombstone(rel_path)
            return None

        target = cloud if missing_side == "local" else local
        target_root = self.config.cloud_vault if missing_side == "local" else self.config.local_vault
        target_exists = cloud_exists if missing_side == "local" else local_exists
        if not target_exists:
            return "TOMBSTONE"

        target_hash = sha256_of(target)
        if target_hash != tombstone.get("hash"):
            self._safety_blocks.add(rel_path)
            self.log.error(
                "TOMBSTONE_CONFLICT",
                f"{rel_path} — a different file reappeared after deletion; preserved for review",
                level="important",
            )
            return "TOMBSTONE_CONFLICT"

        trash_move(target_root, rel_path)
        self.log.warn("DELETE_REPLAY", f"{rel_path} — suppressed cloud resurrection", level="verbose")
        return "DELETE_REPLAY"

    def _start_cooldown(self, rel_path: str, synced_path: str):
        try:
            size = os.path.getsize(synced_path)
        except OSError:
            size = 0
        self.cooldown.start(rel_path, size)

    def _push_pending_confirmation(self, rel_path: str, local: str, cloud: str):
        """Copy local bytes out, but keep the old baseline until iCloud ACKs.

        If iCloud replays a stale/empty version before confirmation, the old
        baseline still identifies it as a cloud-side divergence instead of a
        legitimate remote edit that may overwrite the user's local document.
        """
        atomic_copy(local, cloud)
        self._start_cooldown(rel_path, local)
        delay = max(1.0, float(getattr(self.config, "cloud_confirm_retry", 5)))
        self._schedule_retry(rel_path, delay)
        self.log.success("PUSH_PENDING", f"{rel_path} — waiting for iCloud confirmation", level="verbose")

    def _preserve_remote_conflict_and_push(self, rel_path: str, local: str, cloud: str):
        """Local vault is the editing authority; preserve remote before push."""
        backup = conflict_backup_path(local)
        shutil.copy2(cloud, backup)
        backup_rel = os.path.normpath(os.path.relpath(backup, self.config.local_vault))
        self._push_pending_confirmation(rel_path, local, cloud)
        self.log.warn(
            "CONFLICT",
            f"{rel_path} — kept local, preserved remote as {backup_rel}",
            level="important",
        )
        return "CONFLICT", {"conflict_path": backup_rel}

    async def sync_one(self, rel_path: str):
        config = self.config
        # A missing iCloud mount is not the same as every cloud file being
        # deleted. Refuse all decisions until the root comes back.
        if not os.path.isdir(config.cloud_vault):
            raise FileNotFoundError(f"iCloud 폴더를 기다리는 중: {config.cloud_vault}")
        blocked_for = self.cooldown.remaining(rel_path)
        if blocked_for > 0:
            self._schedule_retry(rel_path, blocked_for)
            return
        self._cancel_retry(rel_path)

        local = os.path.join(config.local_vault, rel_path)
        cloud = os.path.join(config.cloud_vault, rel_path)
        baseline = os.path.join(config.sync_baseline, rel_path)
        local_exists, cloud_exists, baseline_exists = os.path.exists(local), os.path.exists(cloud), os.path.exists(baseline)

        # Cloud Files can occasionally report directory placeholders as file
        # events. Never let a directory reach hashing, copying, or deletion.
        for path in (local, cloud, baseline):
            if os.path.exists(path) and not os.path.isfile(path):
                self.parked.discard(rel_path)
                self._safety_blocks.discard(rel_path)
                self.log.info("SKIP_DIR", rel_path, level="verbose")
                return

        if local_exists:
            self._delete_candidates.pop((self._state_key(rel_path), "local"), None)
        if cloud_exists:
            self._delete_candidates.pop((self._state_key(rel_path), "cloud"), None)

        if cloud_exists:
            try:
                cloud_available = await self.cloud.is_content_available(cloud)
            except CloudProbeTimeout as exc:
                delay = self._park_for_retry(rel_path)
                self.log.error("TIMEOUT", f"{rel_path} — {exc}; retrying in {delay:.0f}s", level="important")
                return "PROBE_TIMEOUT"
            except CloudProbeError as exc:
                delay = self._park_for_retry(rel_path)
                self.log.error("PROBE", f"{rel_path} — {exc}; retrying in {delay:.0f}s", level="important")
                return "PROBE_ERROR"
            if not cloud_available:
                try:
                    cloud_available = await self.cloud.hydrate(cloud)
                except (CloudProbeTimeout, CloudProbeError) as exc:
                    cloud_available = False
                    self.log.warn("HYDRATE", f"{rel_path} — {exc}", level="verbose")
                if not cloud_available:
                    delay = self._park_for_retry(rel_path)
                    self.log.info("PARK", f"{rel_path} — cloud content is offline; retrying in {delay:.0f}s", level="verbose")
                    return "PARK"
                self.log.info("HYDRATE", f"{rel_path} — downloaded from cloud", level="verbose")
            if self._hydration_succeeded(rel_path):
                self.log.success("RECOVERED", f"{rel_path} — iCloud file is available", level="verbose")
                self.status.record_event("RECOVERED", rel_path)
        else:
            self._hydration_succeeded(rel_path)

        tombstone_event = self._handle_tombstone(rel_path, local, cloud, local_exists, cloud_exists)
        if tombstone_event:
            return tombstone_event

        if not local_exists and not cloud_exists:
            if baseline_exists:
                # Baseline is the final recoverable copy. Never erase it merely
                # because both watched paths disappeared in the same scan.
                self._safety_blocks.add(rel_path)
                self.log.error(
                    "BASELINE_HELD",
                    f"{rel_path} — both sides missing; preserved baseline for recovery",
                    level="important",
                )
                return "BASELINE_HELD"
            return


        if baseline_exists:
            if local_exists and self._is_zero_regression(local, baseline):
                self._block_zero_regression(rel_path, "local")
                return "BLOCK_ZERO"
            if cloud_exists and self._is_zero_regression(cloud, baseline):
                self._block_zero_regression(rel_path, "cloud")
                return "BLOCK_ZERO"
        elif local_exists and cloud_exists:
            # With no confirmed baseline, a zero/non-zero disagreement is too
            # ambiguous to resolve automatically. Preserve both until review.
            local_size, cloud_size = os.path.getsize(local), os.path.getsize(cloud)
            if (local_size == 0) != (cloud_size == 0):
                side = "local" if local_size == 0 else "cloud"
                self._block_zero_regression(rel_path, side)
                return "BLOCK_ZERO"
        self._safety_blocks.discard(rel_path)

        if local_exists and not cloud_exists and not baseline_exists:
            if not await self._content_settled(local, config.stability_window):
                self._defer_unsettled(rel_path)
                return
            self._push_pending_confirmation(rel_path, local, cloud)
            return "PUSH_PENDING"

        if cloud_exists and not local_exists and not baseline_exists:
            if not await self._cloud_is_confirmed(rel_path, cloud):
                return "CLOUD_WAIT"
            if not await self._content_settled(cloud, config.stability_window):
                self._defer_unsettled(rel_path)
                return
            atomic_copy(cloud, local)
            atomic_copy(cloud, baseline)
            self._start_cooldown(rel_path, cloud)
            self.log.success("PULL", rel_path, level="verbose")
            return "PULL"

        if not local_exists and cloud_exists and baseline_exists:
            cloud_hash, baseline_hash = sha256_of(cloud), sha256_of(baseline)
            if cloud_hash == baseline_hash:
                if not await self._cloud_is_confirmed(rel_path, cloud):
                    return "CLOUD_WAIT"
                if not await self._content_settled(cloud, config.stability_window):
                    self._defer_unsettled(rel_path)
                    return
                if not self._deletion_ready(rel_path, "local"):
                    return "DELETE_WAIT"
                if not self._allow_destructive_delete(rel_path):
                    return "DELETE_FUSE"
                self._record_tombstone(rel_path, "local", cloud_hash)
                trash_move(config.cloud_vault, rel_path)
                os.remove(baseline)
                self.log.warn("DELETE", rel_path, level="verbose")
                return "DELETE"
            if not await self._cloud_is_confirmed(rel_path, cloud):
                return "CLOUD_WAIT"
            if not await self._content_settled(cloud, config.stability_window):
                self._defer_unsettled(rel_path)
                return
            atomic_copy(cloud, local)
            atomic_copy(cloud, baseline)
            self._start_cooldown(rel_path, cloud)
            self.log.success("PULL", rel_path, level="verbose")
            return "PULL"

        if not cloud_exists and local_exists and baseline_exists:
            local_hash, baseline_hash = sha256_of(local), sha256_of(baseline)
            if local_hash == baseline_hash:
                if not await self._content_settled(local, config.stability_window):
                    self._defer_unsettled(rel_path)
                    return
                if not self._deletion_ready(rel_path, "cloud"):
                    return "DELETE_WAIT"
                if not self._allow_destructive_delete(rel_path):
                    return "DELETE_FUSE"
                self._record_tombstone(rel_path, "cloud", local_hash)
                trash_move(config.local_vault, rel_path)
                os.remove(baseline)
                self.log.warn("DELETE", rel_path, level="verbose")
                return "DELETE"
            if not await self._content_settled(local, config.stability_window):
                self._defer_unsettled(rel_path)
                return
            self._push_pending_confirmation(rel_path, local, cloud)
            return "PUSH_PENDING"

        if local_exists and cloud_exists and not baseline_exists:
            # both sides already exist with no baseline yet (fresh vault, or
            # a file that predates oiiaw) — seed it quietly if they already
            # match, only treat it as a real conflict if they don't.
            if not await self._cloud_is_confirmed(rel_path, cloud):
                return "CLOUD_WAIT"
            local_hash, cloud_hash = sha256_of(local), sha256_of(cloud)
            if local_hash == cloud_hash:
                if not await self._content_settled(cloud, config.stability_window):
                    self._defer_unsettled(rel_path)
                    return
                atomic_copy(local, baseline)
                return
            local_ok, cloud_ok = await asyncio.gather(
                self._content_settled(local, config.stabilize_wait),
                self._content_settled(cloud, config.stabilize_wait),
            )
            if not local_ok or not cloud_ok:
                self._defer_unsettled(rel_path)
                return
            return self._preserve_remote_conflict_and_push(rel_path, local, cloud)

        local_hash, cloud_hash, baseline_hash = sha256_of(local), sha256_of(cloud), sha256_of(baseline)
        if local_hash == cloud_hash == baseline_hash:
            return
        if local_hash != baseline_hash and cloud_hash == baseline_hash:
            if not await self._content_settled(local, config.stability_window):
                self._defer_unsettled(rel_path)
                return
            # Re-read after the wait; the branch may have changed meanwhile.
            if sha256_of(local) != local_hash or sha256_of(cloud) != cloud_hash:
                self._defer_unsettled(rel_path)
                return
            self._push_pending_confirmation(rel_path, local, cloud)
            return "PUSH_PENDING"
        elif cloud_hash != baseline_hash and local_hash == baseline_hash:
            if not await self._cloud_is_confirmed(rel_path, cloud):
                return "CLOUD_WAIT"
            if not await self._content_settled(cloud, config.stability_window):
                self._defer_unsettled(rel_path)
                return
            if sha256_of(cloud) != cloud_hash or sha256_of(local) != local_hash:
                self._defer_unsettled(rel_path)
                return
            atomic_copy(cloud, local)
            atomic_copy(cloud, baseline)
            self._start_cooldown(rel_path, cloud)
            self.log.success("PULL", rel_path, level="verbose")
            return "PULL"
        else:
            if not await self._cloud_is_confirmed(rel_path, cloud):
                return "CLOUD_WAIT"
            local_ok, cloud_ok = await asyncio.gather(
                self._content_settled(local, config.stabilize_wait),
                self._content_settled(cloud, config.stabilize_wait),
            )
            if not local_ok or not cloud_ok:
                self._defer_unsettled(rel_path)
                return
            local_hash, cloud_hash = sha256_of(local), sha256_of(cloud)
            if local_hash == cloud_hash:
                atomic_copy(local, baseline)
                self._start_cooldown(rel_path, local)
                self.log.info("RESOLVED", f"{rel_path} — settled to the same content while waiting", level="verbose")
                return "RESOLVED"
            return self._preserve_remote_conflict_and_push(rel_path, local, cloud)

    # ── main entry ──

    async def run(self):
        self.log.init_log_file()
        self.loop = asyncio.get_running_loop()
        if not os.path.isdir(self.config.cloud_vault):
            raise FileNotFoundError(f"iCloud 폴더를 기다리는 중: {self.config.cloud_vault}")
        observer = self.start_watching()
        workers = [asyncio.create_task(self._worker()) for _ in range(self.worker_count)]
        try:
            for rel in self.discover_tracked_paths():
                self.enqueue(rel)
            while not self._shutdown_event.is_set():
                if not os.path.isdir(self.config.cloud_vault):
                    raise FileNotFoundError(f"iCloud 폴더 연결이 끊김: {self.config.cloud_vault}")
                if self._delete_fuse_tripped or self._safety_blocks:
                    state = "error"
                else:
                    state = "syncing" if self.active > 0 or not self.pending.empty() else "idle"
                self.status.write(state, self.pending.qsize(), len(self.parked))
                self.log.flush()
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
        finally:
            for w in workers:
                w.cancel()
            for handle in self._retry_handles.values():
                handle.cancel()
            self._retry_handles.clear()
            self.cloud.close()
            observer.stop()
            observer.join()
