"""Offline regression tests for audit delivery."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from openpyxl import load_workbook

from audit_excel import export_excel
from audit_pipeline import parse_args, precheck, prepare, run
from standard_audit import Fetcher, LocalStore
from test_audit_pipeline import make_fixture


class DeliveryTests(unittest.TestCase):


    def test_excel_keeps_dates_duplicate_rows_evidence_and_literal_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db, input_path = make_fixture(root)
            input_path.write_text(input_path.read_text(encoding="utf-8-sig").replace("甲.pdf", "=1+1"), encoding="utf-8-sig")
            precheck(input_path, db, root / "out")
            path = export_excel(root / "out")
            book = load_workbook(path)
            try:
                self.assertEqual(book.sheetnames, ["Audit results", "Field evidence", "Replacement relationships"])
                sheet = book.worksheets[0]
                self.assertEqual(sheet.max_row, 9)
                self.assertEqual(sheet["A6"].value, sheet["A7"].value)
                self.assertEqual(sheet["D6"].data_type, "s")
                self.assertIn("=1+1", sheet["D6"].value)
                self.assertEqual(sheet["C5"].number_format, "yyyy-mm-dd")
                self.assertEqual(sheet.freeze_panes, "D5")
                self.assertGreater(book["Field evidence"].max_row, 1)
                self.assertFalse(any(cell.data_type == "f" for s in book for row in s for cell in row))
            finally:
                book.close()

    def test_site_pause_survives_a_new_fetcher(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(directory)
            try:
                first = Fetcher(store, delay=0)
                first.pause("openstd.samr.gov.cn")
                second = Fetcher(store, delay=0)
                second.session.request = Mock()
                with self.assertRaisesRegex(ValueError, "is paused"):
                    second.fetch("national", "https://openstd.samr.gov.cn/test")
                second.session.request.assert_not_called()
            finally:
                store.conn.close()

    def test_no_progress_or_all_sources_paused_stops_automatic_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db, input_path = make_fixture(root)
            prepare(parse_args(["prepare", str(input_path), "--db", str(db), "--output-dir", str(root / "pre"),
                                "--manifest-dir", str(root / "inputs")]))
            output = root / "cloud"
            output.mkdir()
            hosts = ["std.samr.gov.cn", "openstd.samr.gov.cn", "hbba.sacinfo.org.cn", "dbba.sacinfo.org.cn"]
            (output / "site-pauses.json").write_text(json.dumps({h: time.time() + 3600 for h in hosts}))
            args = parse_args(["run", str(root / "inputs/standards.csv"), "--db", str(db),
                               "--manifest", str(root / "inputs/manifest.json"), "--output-dir", str(output)])
            with patch("audit_pipeline.Auditor") as factory:
                factory.return_value.audit.return_value = {"values": {}, "observations": [], "errors": [], "conflicts": [], "review_notes": []}
                self.assertEqual(run(args), 2)
            summary = json.loads((output / "run-summary.json").read_text())
            self.assertFalse(summary["auto_continue"])
            self.assertIn("All official sites", summary["stop_reason"])


if __name__ == "__main__":
    unittest.main()


def test_english_export_preserves_chinese_values_and_successor_dates(tmp_path):
    import csv
    import datetime as dt
    from standard_audit import Auditor, export_csv, merge_observations
    from test_standard_audit import observation

    store = LocalStore(tmp_path)
    try:
        auditor = Auditor(store, Mock(), ["samr"])
        records = {
            "GB1-2000": merge_observations([observation("GB1-2000", {
                "状态": "废止", "标准名称": "测试标准", "发布日期": "2000-01-01",
            }, ["GB1-2020"])], []),
            "GB1-2020": merge_observations([observation("GB1-2020", {
                "状态": "现行", "发布日期": "2020-02-03", "实施日期": "2021-04-05",
            })], []),
        }
        auditor.lookup = lambda code, seed="": records[code]
        original = {"标准号": "GB1-2000"}
        store.save_row("input", 2, {**auditor.audit("GB1-2000"), "original": original})
        path, _ = export_csv(store, list(original), [original], "input")
        with path.open(encoding="utf-8-sig", newline="") as f:
            row = next(csv.DictReader(f))
        assert all(name.isascii() for name in row if name not in original)
        assert row["Verified standard name"] == "测试标准"
        assert row["Verified status"] == "废止"
        assert row["Conclusion"] == "Verified by official sources"
        assert "2020-02-03" in row["Current version publication dates"]
        assert "2021-04-05" in row["Current version implementation dates"]
        book = load_workbook(export_excel(tmp_path))
        try:
            sheet = book["Audit results"]
            headers = {c.value: c.column for c in sheet[4]}
            assert sheet.cell(5, headers["Verified publication date"]).value == dt.datetime(2000, 1, 1)
            assert book["Replacement relationships"].max_row == 3
            assert all(name.isascii() for name in book.sheetnames)
        finally:
            book.close()
    finally:
        store.conn.close()


def test_translated_response_errors_still_pause_the_source(tmp_path):
    from standard_audit import Auditor

    store = LocalStore(tmp_path)
    try:
        fetcher = Fetcher(store, delay=0)
        auditor = Auditor(store, fetcher, ["samr"])
        auditor.query = Mock(side_effect=ValueError("Unexpected public service platform response structure"))
        result = auditor.lookup("GB1-2020")
        assert "std.samr.gov.cn" in fetcher.blocked
        assert result["errors"]
        assert not result["values"]
    finally:
        store.conn.close()
