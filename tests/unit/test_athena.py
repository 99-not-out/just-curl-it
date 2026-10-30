"""The athena handler's result-set conversion: Athena returns every cell as a
VarCharValue, with a header row first on SELECT results.
Run with:  PYTHONPATH=handlers/lib python3 -m unittest discover -s tests/unit
"""
import importlib.util
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "athena", os.path.join(REPO, "handlers", "lib", "athena", "handler.py"))
athena = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(athena)

META = {"ColumnInfo": [{"Name": "id", "Type": "bigint"}, {"Name": "score", "Type": "double"},
                       {"Name": "ok", "Type": "boolean"}, {"Name": "name", "Type": "varchar"},
                       {"Name": "amt", "Type": "decimal"}]}
ROWS = [
    {"Data": [{"VarCharValue": "id"}, {"VarCharValue": "score"}, {"VarCharValue": "ok"},
              {"VarCharValue": "name"}, {"VarCharValue": "amt"}]},          # header row
    {"Data": [{"VarCharValue": "42"}, {"VarCharValue": "1.5"}, {"VarCharValue": "true"},
              {"VarCharValue": "ada"}, {"VarCharValue": "12.30"}]},
    {"Data": [{}, {}, {"VarCharValue": "false"}, {"VarCharValue": ""}, {}]},   # nulls are empty dicts
]


class ConvertRows(unittest.TestCase):
    def test_header_row_skipped_and_typed(self):
        rows = athena.convert_rows(META, ROWS, skip_header=True)
        self.assertEqual(rows[0], [42, 1.5, True, "ada", "12.30"])
        self.assertEqual(rows[1], [None, None, False, "", None])

    def test_columns_and_types(self):
        cols, types = athena.columns(META)
        self.assertEqual(cols, ["id", "score", "ok", "name", "amt"])
        self.assertEqual(types, ["bigint", "double", "boolean", "varchar", "decimal"])

    def test_header_detection(self):
        self.assertTrue(athena.first_row_is_header(META, ROWS))
        self.assertFalse(athena.first_row_is_header(META, ROWS[1:]))


if __name__ == "__main__":
    unittest.main()
