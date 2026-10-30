"""The graphql handler's operation-type reader: it must find the top-level
operation keyword(s) of a document -- skipping comments, strings and
fragments -- because GET refuses anything that isn't a query (ADR 0005).
Run with:  PYTHONPATH=handlers/lib python3 -m unittest discover -s tests/unit
"""
import importlib.util
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "gql", os.path.join(REPO, "handlers", "lib", "graphql", "handler.py"))
gql = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gql)


class OperationTypes(unittest.TestCase):
    def test_shorthand_query(self):
        self.assertEqual(gql.operation_types("{ hello }"), [("query", None)])

    def test_named_query(self):
        self.assertEqual(gql.operation_types("query Q($a: Int) { hello(a: $a) }"), [("query", "Q")])

    def test_mutation(self):
        self.assertEqual(gql.operation_types("mutation M { setHello(v: \"x\") }"), [("mutation", "M")])

    def test_subscription(self):
        self.assertEqual(gql.operation_types("subscription { ticks }"), [("subscription", None)])

    def test_comment_before_keyword(self):
        doc = "# this is a mutation, honest\nquery Q { hello }"
        self.assertEqual(gql.operation_types(doc), [("query", "Q")])

    def test_keyword_inside_string_is_ignored(self):
        doc = 'query Q { search(term: "mutation { evil }") { id } }'
        self.assertEqual(gql.operation_types(doc), [("query", "Q")])

    def test_block_string_is_ignored(self):
        doc = 'query Q { f(x: """\nmutation {\n""") }'
        self.assertEqual(gql.operation_types(doc), [("query", "Q")])

    def test_fragment_is_not_an_operation(self):
        doc = "fragment F on T { a } query Q { ...F }"
        self.assertEqual(gql.operation_types(doc), [("query", "Q")])

    def test_multiple_operations(self):
        doc = "query A { a } mutation B { b }"
        self.assertEqual(gql.operation_types(doc), [("query", "A"), ("mutation", "B")])

    def test_empty_or_garbage(self):
        self.assertEqual(gql.operation_types(""), [])
        self.assertEqual(gql.operation_types("   # nothing\n"), [])


class GetAllowed(unittest.TestCase):
    def test_query_allowed(self):
        self.assertIsNone(gql.refuse_on_get("query Q { a }", None))

    def test_mutation_refused(self):
        self.assertIn("mutation", gql.refuse_on_get("mutation M { a }", None))

    def test_mixed_doc_with_query_op_selected(self):
        doc = "query A { a } mutation B { b }"
        self.assertIsNone(gql.refuse_on_get(doc, "A"))
        self.assertIn("mutation", gql.refuse_on_get(doc, "B"))

    def test_mixed_doc_without_selection_is_refused(self):
        self.assertIsNotNone(gql.refuse_on_get("query A { a } mutation B { b }", None))

    def test_unparseable_is_refused(self):
        # fail closed: if we can't see an operation, GET doesn't run it
        self.assertIsNotNone(gql.refuse_on_get("", None))


class TypeRefs(unittest.TestCase):
    def test_render_nested_type_ref(self):
        ref = {"kind": "NON_NULL", "name": None, "ofType": {
            "kind": "LIST", "name": None, "ofType": {
                "kind": "NON_NULL", "name": None, "ofType": {
                    "kind": "SCALAR", "name": "String", "ofType": None}}}}
        self.assertEqual(gql.render_type(ref), "[String!]!")


if __name__ == "__main__":
    unittest.main()
