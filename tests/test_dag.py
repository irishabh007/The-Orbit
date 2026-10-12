import unittest

from warden.dag import Graph, GraphError, NodeSpec


def n(id_: str, deps: tuple[str, ...] = (), consumes: tuple[str, ...] = (), produces: tuple[str, ...] = ()) -> NodeSpec:
    return NodeSpec(id_, "agent", deps, consumes, produces)


class GraphTests(unittest.TestCase):
    def test_valid_diamond(self) -> None:
        g = Graph([n("a"), n("b", ("a",)), n("c", ("a",)), n("d", ("b", "c"))])
        self.assertEqual(g.order[0], "a")
        self.assertEqual(g.order[-1], "d")
        self.assertEqual(g.levels(), [["a"], ["b", "c"], ["d"]])
        self.assertEqual(g.descendants("a"), {"b", "c", "d"})
        self.assertEqual(g.ancestors("d"), {"a", "b", "c"})

    def test_failure_replan_targets_must_be_upstream(self) -> None:
        Graph([n("a"), NodeSpec("b", "agent", ("a",), on_failure_replan=("a",))])
        with self.assertRaisesRegex(GraphError, "upstream"):
            Graph([n("a"), n("b"), NodeSpec("c", "agent", ("a",), on_failure_replan=("b",))])

    def test_cycle_is_rejected(self) -> None:
        with self.assertRaises(GraphError):
            Graph([n("a", ("c",)), n("b", ("a",)), n("c", ("b",))])

    def test_unknown_and_self_dependency(self) -> None:
        with self.assertRaises(GraphError):
            Graph([n("a", ("ghost",))])
        with self.assertRaises(GraphError):
            Graph([n("a", ("a",))])

    def test_duplicate_node_and_empty_graph(self) -> None:
        with self.assertRaises(GraphError):
            Graph([n("a"), n("a")])
        with self.assertRaises(GraphError):
            Graph([])

    def test_artifact_must_have_single_producer(self) -> None:
        with self.assertRaises(GraphError):
            Graph([n("a", produces=("x",)), n("b", produces=("x",))])

    def test_consumed_artifact_must_come_from_an_ancestor(self) -> None:
        # b and c are parallel siblings: c may not consume what b produces
        with self.assertRaises(GraphError):
            Graph([n("a"), n("b", ("a",), produces=("x",)), n("c", ("a",), consumes=("x",))])

    def test_external_inputs_are_allowed(self) -> None:
        Graph([n("a", consumes=("request",), produces=("spec",))])

    def test_mermaid_contains_edges_and_approval_marker(self) -> None:
        g = Graph([n("a"), NodeSpec("b", "agent", ("a",), requires_approval=True)])
        text = g.to_mermaid()
        self.assertIn("a --> b", text)
        self.assertIn("🔒", text)


if __name__ == "__main__":
    unittest.main()
