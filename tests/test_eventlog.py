import json
import tempfile
import threading
import unittest
from pathlib import Path

from warden.eventlog import GENESIS_HASH, EventLog, EventLogCorrupt
from warden.models import EventType


def fixed_clock() -> str:
    return "2026-01-01T00:00:00+00:00"


class EventLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "runs" / "events.jsonl"
        self.log = EventLog(self.path, clock=fixed_clock)

    def _seed(self, n: int = 3) -> None:
        for i in range(n):
            self.log.append("r1", EventType.NOTE, "orchestrator", data={"i": i})

    def test_chain_links_and_verifies(self) -> None:
        self._seed(3)
        events = self.log.read_all()
        self.assertEqual([e.seq for e in events], [1, 2, 3])
        self.assertEqual(events[0].prev_hash, GENESIS_HASH)
        self.assertEqual(events[1].prev_hash, events[0].hash)
        result = self.log.verify()
        self.assertTrue(result.ok)
        self.assertEqual(result.count, 3)

    def test_empty_log_verifies(self) -> None:
        self.assertTrue(self.log.verify().ok)

    def test_hash_is_deterministic(self) -> None:
        other = EventLog(Path(self._tmp.name) / "other.jsonl", clock=fixed_clock)
        a = self.log.append("r1", EventType.NOTE, "x", data={"k": 1})
        b = other.append("r1", EventType.NOTE, "x", data={"k": 1})
        self.assertEqual(a.hash, b.hash)

    def test_edited_event_is_detected(self) -> None:
        self._seed(3)
        lines = self.path.read_text().splitlines()
        tampered = json.loads(lines[1])
        tampered["data"]["i"] = 999
        lines[1] = json.dumps(tampered, sort_keys=True)
        self.path.write_text("\n".join(lines) + "\n")
        result = EventLog(self.path).verify()
        self.assertFalse(result.ok)
        self.assertEqual(result.bad_seq, 2)

    def test_deleted_event_is_detected(self) -> None:
        self._seed(4)
        lines = self.path.read_text().splitlines()
        del lines[1]
        self.path.write_text("\n".join(lines) + "\n")
        result = EventLog(self.path).verify()
        self.assertFalse(result.ok)
        self.assertEqual(result.bad_seq, 3)

    def test_corrupt_file_fails_loudly_on_open(self) -> None:
        self._seed(1)
        with self.path.open("a") as fh:
            fh.write("not json\n")
        with self.assertRaises(EventLogCorrupt):
            EventLog(self.path)

    def test_resumes_chain_after_restart(self) -> None:
        self._seed(2)
        reopened = EventLog(self.path, clock=fixed_clock)
        third = reopened.append("r1", EventType.NOTE, "orchestrator")
        self.assertEqual(third.seq, 3)
        self.assertTrue(reopened.verify().ok)

    def test_lineage_follows_caused_by(self) -> None:
        e1 = self.log.append("r1", EventType.RUN_STARTED, "orchestrator")
        e2 = self.log.append("r1", EventType.NOTE, "agent:spec", caused_by=[e1.seq])
        self.log.append("r1", EventType.NOTE, "unrelated")
        e4 = self.log.append("r1", EventType.APPROVAL_DECIDED, "human:alice", caused_by=[e2.seq])
        chain = self.log.lineage(e4.seq)
        self.assertEqual([e.seq for e in chain], [1, 2, 4])

    def test_caused_by_must_reference_earlier_events(self) -> None:
        with self.assertRaises(ValueError):
            self.log.append("r1", EventType.NOTE, "x", caused_by=[5])
        self.assertEqual(self.log.read_all(), [])

    def test_unserializable_data_writes_nothing(self) -> None:
        with self.assertRaises(TypeError):
            self.log.append("r1", EventType.NOTE, "x", data={"bad": object()})
        self.assertEqual(self.log.read_all(), [])
        self.log.append("r1", EventType.NOTE, "x")  # log still usable
        self.assertTrue(self.log.verify().ok)

    def test_filter_by_run(self) -> None:
        self.log.append("a", EventType.NOTE, "x")
        self.log.append("b", EventType.NOTE, "x")
        self.assertEqual(len(self.log.read_all(run_id="a")), 1)

    def test_parallel_appends_keep_chain_intact(self) -> None:
        def worker(n: int) -> None:
            for i in range(25):
                self.log.append("r1", EventType.NOTE, f"agent:{n}", data={"i": i})

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        result = self.log.verify()
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.count, 200)


if __name__ == "__main__":
    unittest.main()
