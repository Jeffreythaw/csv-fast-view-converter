from __future__ import annotations

import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from app.processor import process_csv


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


class TestXlsxOutput(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir)

    def test_semicolon_csv_creates_direct_two_sheet_xlsx(self) -> None:
        source = self.test_dir / "BlkB-FCU_16062026_01062026.csv"
        source.write_text(
            "DateTime;Time;Value1;Value2\n"
            "01/06/2026 00:00:00;00:00:00;1.2;3.4\n"
            "01/06/2026 00:15:00;00:15:00;5.6;7.8\n",
            encoding="utf-8",
        )
        output = self.test_dir / "job-id.xlsx"

        def progress(*_args, **_kwargs) -> None:
            return None

        result = process_csv(source, output, "xlsx", progress)

        self.assertEqual(result, output)
        self.assertTrue(output.is_file())
        self.assertFalse((self.test_dir / "conversion_report.txt").exists())
        self.assertEqual(list(self.test_dir.glob("*.zip")), [])

        with zipfile.ZipFile(output) as workbook_zip:
            workbook = ElementTree.fromstring(workbook_zip.read("xl/workbook.xml"))
            sheets = workbook.find(f"{{{MAIN_NS}}}sheets")
            self.assertIsNotNone(sheets)
            sheet_elements = list(sheets) if sheets is not None else []
            self.assertEqual([sheet.attrib["name"] for sheet in sheet_elements], ["Data", "Analysis"])

            relationships = ElementTree.fromstring(workbook_zip.read("xl/_rels/workbook.xml.rels"))
            relationship_targets = {
                relationship.attrib["Id"]: relationship.attrib["Target"]
                for relationship in relationships.findall(f"{{{PACKAGE_REL_NS}}}Relationship")
            }
            data_relationship_id = sheet_elements[0].attrib[f"{{{REL_NS}}}id"]
            data_target = relationship_targets[data_relationship_id].lstrip("/")
            if not data_target.startswith("xl/"):
                data_target = f"xl/{data_target}"
            data_sheet = ElementTree.fromstring(workbook_zip.read(data_target))
            first_data_row = data_sheet.find(
                f".//{{{MAIN_NS}}}sheetData/{{{MAIN_NS}}}row[@r='2']"
            )
            self.assertIsNotNone(first_data_row)
            cells = list(first_data_row) if first_data_row is not None else []
            self.assertEqual(len(cells), 4)

    def test_analysis_sheet_uses_operator_friendly_daily_sections(self) -> None:
        source = self.test_dir / "daily-acmv.csv"
        source.write_text(
            "DateTime;AHU 1 Run Status;AHU 1 CHW Valve Feedback;AHU 1 Supply Temp;"
            "AHU 1 Return Temp;AHU 1 VSD Feedback Hz;Chiller 1 Run Status;"
            "Chiller 1 Trip;Chiller 1 CHW Return Temp;Chiller 1 CHW Supply Temp;"
            "Chiller 1 CHW Flow gpm\n"
            "18/06/2026 00:00:00;0;0;14;25;0;1;0;12;7;1200\n"
            "18/06/2026 06:00:00;1;65;13;24;42;1;1;13;7;1250\n"
            "18/06/2026 06:30:00;1;70;13;23;45;1;0;13;7;1250\n"
            "18/06/2026 23:45:00;0;0;14;24;0;0;0;12;7;0\n",
            encoding="utf-8",
        )
        output = self.test_dir / "daily-acmv.xlsx"

        def progress(*_args, **_kwargs) -> None:
            return None

        process_csv(source, output, "xlsx", progress)

        with zipfile.ZipFile(output) as workbook_zip:
            analysis_xml = ElementTree.fromstring(workbook_zip.read("xl/worksheets/sheet2.xml"))
            visible_text = " ".join(
                node.text or ""
                for node in analysis_xml.findall(f".//{{{MAIN_NS}}}t")
            )
            for expected in [
                "Daily ACMV Operation Summary",
                "Equipment Running Summary",
                "Valve Opening (%)",
                "Temperature IN / OUT",
                "VSD / Frequency (Hz)",
                "Trip / Fault Timeline",
                "Chiller Cooling Load",
                "Trip started",
                "Recovered at",
                "Average RT",
                "Peak RT",
            ]:
                self.assertIn(expected, visible_text)


if __name__ == "__main__":
    unittest.main()
