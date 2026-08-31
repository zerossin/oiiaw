import pytest

from oiiaw.sync_decision import SyncDecision, SyncObservation, decide_sync


@pytest.mark.parametrize(
    ("local", "cloud", "baseline", "known_local", "expected"),
    [
        (None, None, None, False, SyncDecision.NOOP),
        ("A", None, None, False, SyncDecision.PUSH_LOCAL),
        (None, "A", None, False, SyncDecision.PULL_CLOUD),
        ("A", "A", None, False, SyncDecision.SEED_BASELINE),
        ("B", "A", None, False, SyncDecision.PRESERVE_CONFLICT),
        ("B", "A", None, True, SyncDecision.REPLAY_LOCAL),
        (None, None, "A", False, SyncDecision.HOLD_BASELINE),
        (None, "A", "A", False, SyncDecision.DELETE_CLOUD),
        (None, "B", "A", False, SyncDecision.DELETE_CLOUD),
        ("A", None, "A", False, SyncDecision.DELETE_LOCAL),
        ("B", None, "A", False, SyncDecision.PUSH_LOCAL),
        ("A", "A", "A", False, SyncDecision.NOOP),
        ("B", "A", "A", False, SyncDecision.PUSH_LOCAL),
        ("A", "B", "A", False, SyncDecision.PULL_CLOUD),
        ("B", "B", "A", False, SyncDecision.CONFIRM_MATCH),
        ("C", "B", "A", False, SyncDecision.PRESERVE_CONFLICT),
        ("C", "B", "A", True, SyncDecision.REPLAY_LOCAL),
        # A known generation equal to the baseline is an ordinary local edit,
        # not a stale replay. This keeps event semantics precise.
        ("C", "B", "B", True, SyncDecision.PUSH_LOCAL),
    ],
)
def test_decision_table(local, cloud, baseline, known_local, expected):
    observation = SyncObservation(local, cloud, baseline, known_local)
    assert decide_sync(observation) is expected


def test_zero_byte_hash_is_content_not_missing():
    empty_hash = "e3b0c442"
    observation = SyncObservation(empty_hash, empty_hash, None)
    assert decide_sync(observation) is SyncDecision.SEED_BASELINE
