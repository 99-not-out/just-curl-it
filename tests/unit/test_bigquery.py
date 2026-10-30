"""The bigquery handler's row conversion: BigQuery returns every cell as a
string inside {"f":[{"v":...}]}; the handler types them from the schema.
Run with:  PYTHONPATH=handlers/lib python3 -m unittest discover -s tests/unit
"""
import importlib.util
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "bq", os.path.join(REPO, "handlers", "lib", "bigquery", "handler.py"))
bq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bq)

SCHEMA = {"fields": [
    {"name": "id", "type": "INTEGER"},
    {"name": "score", "type": "FLOAT"},
    {"name": "ok", "type": "BOOLEAN"},
    {"name": "name", "type": "STRING"},
    {"name": "tags", "type": "STRING", "mode": "REPEATED"},
    {"name": "addr", "type": "RECORD", "fields": [
        {"name": "city", "type": "STRING"}, {"name": "zip", "type": "INTEGER"}]},
    {"name": "n", "type": "NUMERIC"},
]}
ROWS = [{"f": [
    {"v": "42"}, {"v": "1.5"}, {"v": "true"}, {"v": "ada"},
    {"v": [{"v": "a"}, {"v": "b"}]},
    {"v": {"f": [{"v": "Paris"}, {"v": "75001"}]}},
    {"v": "12.30"},
]}, {"f": [{"v": None}, {"v": None}, {"v": "false"}, {"v": ""}, {"v": []}, {"v": None}, {"v": None}]}]


class ConvertRows(unittest.TestCase):
    def test_columns_and_types(self):
        cols, types = bq.schema_columns(SCHEMA)
        self.assertEqual(cols, ["id", "score", "ok", "name", "tags", "addr", "n"])
        self.assertEqual(types[4], "STRING[]")
        self.assertEqual(types[5], "RECORD")

    def test_typed_values(self):
        rows = bq.convert_rows(SCHEMA, ROWS)
        self.assertEqual(rows[0][:4], [42, 1.5, True, "ada"])
        self.assertEqual(rows[0][4], ["a", "b"])
        self.assertEqual(rows[0][5], {"city": "Paris", "zip": 75001})
        self.assertEqual(rows[0][6], "12.30")          # NUMERIC stays a string: no precision loss

    def test_nulls_and_empties(self):
        rows = bq.convert_rows(SCHEMA, ROWS)
        self.assertEqual(rows[1], [None, None, False, "", [], None, None])


class StatementGate(unittest.TestCase):
    def test_select_allowed(self):
        self.assertIsNone(bq.refuse_statement("SELECT"))

    def test_ddl_dml_refused(self):
        for st in ("CREATE_TABLE", "INSERT", "DELETE", "MERGE", "DROP_TABLE", "SCRIPT", None):
            self.assertIsNotNone(bq.refuse_statement(st), st)


if __name__ == "__main__":
    unittest.main()
