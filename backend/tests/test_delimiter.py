from __future__ import annotations

import unittest
from pathlib import Path
import shutil
import tempfile
from app.processor import detect_csv_properties

class TestCSVDelimiterDetection(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def write_csv(self, filename: str, content: str) -> Path:
        path = self.test_dir / filename
        path.write_text(content, encoding="utf-8")
        return path

    def test_semicolon_csv(self):
        content = (
            "DateTime;Time;Value1;Value2\n"
            "01/06/2026 00:00:00;00:00:00;1.2;3.4\n"
            "01/06/2026 00:15:00;00:15:00;5.6;7.8\n"
        )
        path = self.write_csv("semicolon.csv", content)
        encoding, delimiter, col_count, headers = detect_csv_properties(path)
        self.assertEqual(delimiter, ";")
        self.assertEqual(col_count, 4)
        self.assertEqual(headers, ["DateTime", "Time", "Value1", "Value2"])

    def test_comma_csv(self):
        content = (
            "DateTime,Time,Value1,Value2\n"
            "01/06/2026 00:00:00,00:00:00,1.2,3.4\n"
            "01/06/2026 00:15:00,00:15:00,5.6,7.8\n"
        )
        path = self.write_csv("comma.csv", content)
        encoding, delimiter, col_count, headers = detect_csv_properties(path)
        self.assertEqual(delimiter, ",")
        self.assertEqual(col_count, 4)
        self.assertEqual(headers, ["DateTime", "Time", "Value1", "Value2"])

    def test_tab_csv(self):
        content = (
            "DateTime\tTime\tValue1\tValue2\n"
            "01/06/2026 00:00:00\t00:00:00\t1.2\t3.4\n"
            "01/06/2026 00:15:00\t00:15:00\t5.6\t7.8\n"
        )
        path = self.write_csv("tab.csv", content)
        encoding, delimiter, col_count, headers = detect_csv_properties(path)
        self.assertEqual(delimiter, "\t")
        self.assertEqual(col_count, 4)
        self.assertEqual(headers, ["DateTime", "Time", "Value1", "Value2"])

    def test_semicolon_preferred_with_commas_in_text(self):
        content = (
            'DateTime;Time;Equipment Name;Load Value\n'
            '01/06/2026 00:00:00;00:00:00;"FCU, BlkA";10,5\n'
            '01/06/2026 00:15:00;00:15:00;"FCU, BlkB";12,8\n'
        )
        path = self.write_csv("preferred.csv", content)
        encoding, delimiter, col_count, headers = detect_csv_properties(path)
        self.assertEqual(delimiter, ";")
        self.assertEqual(col_count, 4)

    def test_validation_only_one_column_with_semicolon_header(self):
        # Header has semicolon but parsed as comma giving 1 column, should retry with semicolon.
        # However, if even semicolon gives 1 column, it raises an error. Let's test standard retry first.
        content = (
            "DateTime;Time;Value1;Value2\n" # Header has semicolon, but let's see if comma sniffing would fail
            # Sniffing with comma would yield 1 column, triggering the semicolon retry.
        )
        # Write a file that looks like a single comma-separated column but has semicolon:
        content = (
            "DateTime;Time;Value1;Value2\n"
            "row1\n"
            "row2\n"
        )
        path = self.write_csv("validation_retry.csv", content)
        encoding, delimiter, col_count, headers = detect_csv_properties(path)
        self.assertEqual(delimiter, ";")
        self.assertEqual(col_count, 4)

    def test_validation_raises_error(self):
        # Header has semicolon but even semicolon parsing yields <= 1 column.
        content = (
            '"DateTime;SemicolonOnlyInHeader"\n'
        )
        path = self.write_csv("validation_fail.csv", content)
        with self.assertRaises(ValueError) as ctx:
            detect_csv_properties(path)
        self.assertIn("CSV parsing failed", str(ctx.exception))

if __name__ == "__main__":
    unittest.main()
