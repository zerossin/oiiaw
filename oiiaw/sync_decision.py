"""Pure content-state decisions for the sync engine.

Provider hydration, stability waits and filesystem mutations belong to the
executor. This module only answers what three observed content hashes mean,
which makes every ambiguous state explicit and independently testable.
"""

from dataclasses import dataclass
from enum import Enum, auto


class SyncDecision(Enum):
    NOOP = auto()
    SEED_BASELINE = auto()
    PUSH_LOCAL = auto()
    PULL_CLOUD = auto()
    CONFIRM_MATCH = auto()
    REPLAY_LOCAL = auto()
    PRESERVE_CONFLICT = auto()
    DELETE_CLOUD = auto()
    DELETE_LOCAL = auto()
    HOLD_BASELINE = auto()


@dataclass(frozen=True)
class SyncObservation:
    local_hash: str | None
    cloud_hash: str | None
    baseline_hash: str | None
    cloud_is_local_generation: bool = False


def decide_sync(observation: SyncObservation) -> SyncDecision:
    """Return one deterministic action for an observed content state.

    `None` means that side is missing. A real zero-byte file still has a SHA256
    hash and is handled normally; zero-regression policy is an executor guard.
    """
    local = observation.local_hash
    cloud = observation.cloud_hash
    baseline = observation.baseline_hash

    if baseline is None:
        if local is None and cloud is None:
            return SyncDecision.NOOP
        if local is not None and cloud is None:
            return SyncDecision.PUSH_LOCAL
        if local is None and cloud is not None:
            return SyncDecision.PULL_CLOUD
        if local == cloud:
            return SyncDecision.SEED_BASELINE
        if observation.cloud_is_local_generation:
            return SyncDecision.REPLAY_LOCAL
        return SyncDecision.PRESERVE_CONFLICT

    if local is None and cloud is None:
        return SyncDecision.HOLD_BASELINE
    if local is None:
        # The local vault is the editing authority. Even if cloud changed while
        # the deletion was in flight, preserve it in trash rather than reviving
        # a path the user removed locally.
        return SyncDecision.DELETE_CLOUD
    if cloud is None:
        return (
            SyncDecision.DELETE_LOCAL
            if local == baseline
            else SyncDecision.PUSH_LOCAL
        )

    if local == cloud == baseline:
        return SyncDecision.NOOP
    if (
        local != cloud
        and cloud != baseline
        and observation.cloud_is_local_generation
    ):
        return SyncDecision.REPLAY_LOCAL
    if local != baseline and cloud == baseline:
        return SyncDecision.PUSH_LOCAL
    if cloud != baseline and local == baseline:
        return SyncDecision.PULL_CLOUD
    if local == cloud:
        return SyncDecision.CONFIRM_MATCH
    return SyncDecision.PRESERVE_CONFLICT
