import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from warden.fsutil import atomic_write


class AtomicWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "state.json"
        self.sleeps: list[float] = []

    def write(self, text: str) -> None:
        atomic_write(self.path, text, sleep=self.sleeps.append)

    def test_writes_and_replaces_without_leaving_temp_files(self) -> None:
        self.write("one")
        self.write("two")
        self.assertEqual(self.path.read_text("utf-8"), "two")
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["state.json"])

    def test_retries_when_windows_briefly_locks_the_target(self) -> None:
        # antivirus / indexer / sync clients hold files open for a moment: WinError 5
        real_replace = os.replace
        failures = [PermissionError(13, "Access is denied")] * 2

        def flaky(src: str, dst: str) -> None:
            if failures:
                raise failures.pop()
            real_replace(src, dst)

        with mock.patch("warden.fsutil.os.replace", side_effect=flaky):
            self.write("saved")
        self.assertEqual(self.path.read_text("utf-8"), "saved")
        self.assertEqual(len(self.sleeps), 2)

    def test_gives_up_after_bounded_attempts_and_cleans_up(self) -> None:
        with mock.patch("warden.fsutil.os.replace", side_effect=PermissionError(13, "Access is denied")), \
                self.assertRaises(PermissionError):
            self.write("never")
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.iterdir()), [])  # temp file removed
        self.assertLess(sum(self.sleeps), 3.0)  # bounded wait
