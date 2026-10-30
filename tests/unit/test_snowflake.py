"""The snowflake handler's result typing: the SQL API returns every cell as
a string with a rowType describing the column.
Run with:  PYTHONPATH=handlers/lib python3 -m unittest discover -s tests/unit
"""
import importlib.util
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "sf", os.path.join(REPO, "handlers", "lib", "snowflake", "handler.py"))
sf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sf)

ROWTYPE = [
    {"name": "ID", "type": "fixed", "scale": 0},
    {"name": "AMT", "type": "fixed", "scale": 2},
    {"name": "R", "type": "real"},
    {"name": "OK", "type": "boolean"},
    {"name": "NAME", "type": "text"},
    {"name": "D", "type": "date"},
    {"name": "J", "type": "object"},
]
ROWS = [["42", "12.30", "1.5", "true", "ada", "20000", '{"a":1}'],
        [None, None, None, "false", "", None, None]]


class ConvertRows(unittest.TestCase):
    def test_columns_and_types(self):
        cols, types = sf.columns(ROWTYPE)
        self.assertEqual(cols, ["ID", "AMT", "R", "OK", "NAME", "D", "J"])
        self.assertEqual(types[0], "fixed")

    def test_typed(self):
        rows = sf.convert_rows(ROWTYPE, ROWS)
        self.assertEqual(rows[0][0], 42)            # fixed, scale 0 -> int
        self.assertEqual(rows[0][1], "12.30")       # fixed with scale -> string, no precision loss
        self.assertEqual(rows[0][2], 1.5)
        self.assertEqual(rows[0][3], True)
        self.assertEqual(rows[0][4], "ada")
        self.assertEqual(rows[0][5], "20000")       # dates are epoch-days as text; left alone
        self.assertEqual(rows[0][6], {"a": 1})      # object/array/variant parsed

    def test_nulls(self):
        self.assertEqual(sf.convert_rows(ROWTYPE, ROWS)[1], [None, None, None, False, "", None, None])


class DmlWarning(unittest.TestCase):
    def test_wrote_detects_dml_stats(self):
        self.assertTrue(sf.wrote({"numRowsInserted": 3}))
        self.assertTrue(sf.wrote({"numRowsDeleted": 1, "numRowsInserted": 0}))
        self.assertFalse(sf.wrote({"numRowsInserted": 0, "numRowsUpdated": 0}))
        self.assertFalse(sf.wrote({}))


if __name__ == "__main__":
    unittest.main()
