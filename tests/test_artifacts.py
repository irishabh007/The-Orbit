import tempfile
import unittest
from pathlib import Path

from warden.artifacts import ArtifactStore


class ArtifactStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "artifacts"
        self.store = ArtifactStore(self.root)

    def _build_chain(self) -> None:
        self.store.put("spec.md", "v1 spec", kind="requirements")
        self.store.put("design.md", "design", kind="design", inputs=["spec.md"])
        self.store.put("app.py", "code", kind="code", inputs=["design.md"])
        self.store.put("README.md", "docs", kind="doc", inputs=["spec.md"])

    def test_versions_increment_on_change(self) -> None:
        a = self.store.put("spec.md", "one").artifact
        b = self.store.put("spec.md", "two").artifact
        self.assertEqual((a.version, b.version), (1, 2))
        self.assertEqual(b.id, "spec.md@v2")
        self.assertEqual(self.store.read_text(self.store.get("spec.md", 1)), "one")

    def test_identical_content_is_a_noop(self) -> None:
        first = self.store.put("spec.md", "same")
        again = self.store.put("spec.md", "same")
        self.assertTrue(first.created)
        self.assertFalse(again.created)
        self.assertEqual(len(self.store.history("spec.md")), 1)

    def test_unknown_input_is_rejected(self) -> None:
        with self.assertRaises(KeyError):
            self.store.put("design.md", "x", inputs=["missing.md"])
        self.assertIsNone(self.store.latest("design.md"))

    def test_binary_content(self) -> None:
        art = self.store.put("logo.bin", b"\x00\x01\x02").artifact
        self.assertEqual(self.store.read_bytes(art), b"\x00\x01\x02")

    def test_fresh_chain_has_nothing_stale(self) -> None:
        self._build_chain()
        self.assertEqual(self.store.stale(), set())

    def test_change_marks_only_downstream_stale(self) -> None:
        self._build_chain()
        self.store.put("spec.md", "v2 spec")
        # design and README were built from spec; app.py is built from design (transitive)
        self.assertEqual(self.store.stale(), {"design.md", "README.md", "app.py"})

    def test_rebuilding_a_node_clears_its_staleness_but_not_its_dependents(self) -> None:
        self._build_chain()
        self.store.put("spec.md", "v2 spec")
        self.store.put("design.md", "new design", inputs=["spec.md"])
        self.assertEqual(self.store.stale(), {"README.md", "app.py"})

    def test_rebuild_with_identical_output_does_not_invalidate_downstream(self) -> None:
        self._build_chain()
        self.store.put("spec.md", "v2 spec")
        result = self.store.put("design.md", "design", inputs=["spec.md"])  # same content
        self.assertFalse(result.created)
        self.assertEqual(len(self.store.history("design.md")), 1)
        # design was re-validated against the new spec and its hash is unchanged, so
        # app.py (built from design) stays valid. Only README is still outdated.
        self.assertEqual(self.store.stale(), {"README.md"})

    def test_dependents_is_transitive(self) -> None:
        self._build_chain()
        self.assertEqual(self.store.dependents("spec.md"), {"design.md", "app.py", "README.md"})
        self.assertEqual(self.store.dependents("app.py"), set())

    def test_restore_republishes_old_content_as_new_version(self) -> None:
        self.store.put("spec.md", "one")
        self.store.put("spec.md", "two")
        art = self.store.restore("spec.md", 1)
        self.assertEqual(art.version, 3)
        self.assertEqual(self.store.read_text(art), "one")
        self.assertEqual(len(self.store.history("spec.md")), 3)  # history is never rewritten

    def test_drop_removes_from_index(self) -> None:
        self.store.put("spec.md", "one")
        self.store.drop("spec.md")
        self.assertIsNone(self.store.latest("spec.md"))

    def test_persists_across_instances(self) -> None:
        self._build_chain()
        reopened = ArtifactStore(self.root)
        self.assertEqual(reopened.names(), ["README.md", "app.py", "design.md", "spec.md"])
        latest = reopened.latest("app.py")
        assert latest is not None
        self.assertEqual(reopened.read_text(latest), "code")
        self.assertEqual(reopened.stale(), set())


if __name__ == "__main__":
    unittest.main()
