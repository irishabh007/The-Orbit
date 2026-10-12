import unittest

from warden.bundle import BundleError, is_bundle, pack, path_problem, unpack


class BundleTests(unittest.TestCase):
    def test_round_trip_is_deterministic(self) -> None:
        files = {"b.py": "print(2)\n", "a/c.py": "x = 1\n"}
        text = pack(files)
        self.assertEqual(unpack(text), files)
        self.assertEqual(pack(dict(reversed(list(files.items())))), text)  # order-independent
        self.assertEqual(unpack(text.encode("utf-8")), files)

    def test_is_bundle_by_name(self) -> None:
        self.assertTrue(is_bundle("code.bundle"))
        self.assertFalse(is_bundle("spec.json"))

    def test_unsafe_paths_are_rejected(self) -> None:
        for bad in ("/etc/passwd", "../up.py", "a/../../b.py", "C:/x.py", "a\\b.py", "", "a//b.py"):
            with self.subTest(path=bad):
                self.assertIsNotNone(path_problem(bad))
                with self.assertRaises(BundleError):
                    pack({bad: "x"})
        self.assertIsNone(path_problem("app/main.py"))

    def test_malformed_bundle_is_rejected(self) -> None:
        for bad in ("not json", "[]", '{"files": []}', '{"files": {"a.py": 1}}', '{"files": {"../a": "x"}}'):
            with self.subTest(bundle=bad), self.assertRaises(BundleError):
                unpack(bad)
