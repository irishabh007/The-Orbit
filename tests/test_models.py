import unittest
from itertools import pairwise

from warden.models import (
    TRANSITIONS,
    InvalidTransition,
    NodeState,
    assert_transition,
    can_transition,
)


class TransitionTableTests(unittest.TestCase):
    def test_every_state_has_an_entry(self) -> None:
        self.assertEqual(set(TRANSITIONS), set(NodeState))

    def test_happy_path_is_legal(self) -> None:
        path = [NodeState.PENDING, NodeState.READY, NodeState.RUNNING, NodeState.SUCCEEDED]
        for src, dst in pairwise(path):
            self.assertTrue(can_transition(src, dst), f"{src} -> {dst}")

    def test_approval_branches(self) -> None:
        for dst in (NodeState.SUCCEEDED, NodeState.FAILED, NodeState.RUNNING):
            self.assertTrue(can_transition(NodeState.AWAITING_APPROVAL, dst))

    def test_retry_and_rollback_from_failed(self) -> None:
        self.assertTrue(can_transition(NodeState.FAILED, NodeState.READY))
        self.assertTrue(can_transition(NodeState.FAILED, NodeState.ROLLED_BACK))

    def test_replan_path(self) -> None:
        self.assertTrue(can_transition(NodeState.SUCCEEDED, NodeState.STALE))
        self.assertTrue(can_transition(NodeState.STALE, NodeState.PENDING))

    def test_illegal_transitions_raise(self) -> None:
        for src, dst in [
            (NodeState.PENDING, NodeState.SUCCEEDED),  # cannot skip running
            (NodeState.SUCCEEDED, NodeState.RUNNING),  # cannot silently re-run
            (NodeState.FAILED, NodeState.SUCCEEDED),
            (NodeState.RUNNING, NodeState.PENDING),
        ]:
            with self.assertRaises(InvalidTransition):
                assert_transition(src, dst)


if __name__ == "__main__":
    unittest.main()
