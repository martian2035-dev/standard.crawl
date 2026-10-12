"""Offline regression tests for audit pipeline."""
import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


from audit_pipeline import Baseline, merge, parse_args, precheck, prepare, run
from standard_audit import (Auditor, Evidence, Fetcher, LocalStore, Observation,
                            RequestBudgetExceeded, merge_observations, now)


def make_fixture(root):
    db = root / "baseline.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
      CREATE TABLE meta(key TEXT, value TEXT);
      INSERT INTO meta VALUES('built_at','2026-10-08 02:59:12+0800');
      CREATE TABLE standards(id INTEGER PRIMARY KEY, standard_no TEXT, standard_no_compact TEXT,
        name TEXT, status TEXT, publish_date TEXT, implement_date TEXT, category TEXT,
        industry TEXT, region TEXT, is_adopted INTEGER, source_file TEXT);
      INSERT INTO standards VALUES(1,'AQ 1-2000','AQ1-2000','旧标准','废止','2000-01-01',
        '2001-01-01','行业标准','','',0,'行业公开标准信息.csv');
      INSERT INTO standards VALUES(2,'GB 11-2020','GB11-2020','普通标准','现行','2020-01-01',
        '2021-01-01','国标强制','','',0,'国家公开标准信息.csv');
    """)
    conn.close()
    input_path = root / "input.csv"
    with input_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["标准号", "标准类型", "发布日期", "文件名"])
        writer.writeheader()
        writer.writerows([
            {"标准号": "GB 11-2020", "标准类型": "国标强制", "发布日期": "2020-01-01", "文件名": "普通文件.pdf"},
            {"标准号": "AQ 1-2000", "标准类型": "行业标准", "发布日期": "2001-01-01", "文件名": "甲.pdf"},
            {"标准号": "AQ 1-2000", "标准类型": "行业标准", "文件名": "乙.pdf"},
            {"标准号": "GB 99-2000", "标准类型": "国标强制", "文件名": "未匹配.pdf"},
            {"标准号": "ISO 1-2000", "标准类型": "国际标准", "文件名": "国际.pdf"},
        ])
    return db, input_path


class PipelineTests(unittest.TestCase):

    def test_prepare_is_offline_and_projected_input_has_no_file_information(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db, input_path = make_fixture(root)
            args = parse_args(["prepare", str(input_path), "--db", str(db), "--output-dir", str(root / "out"),
                               "--manifest-dir", str(root / "inputs")])
            with patch("requests.Session.request", side_effect=AssertionError("Offline screening must not access the network")):
                prepare(args)
            summary = json.loads((root / "out/precheck-summary.json").read_text())
            self.assertEqual(summary["baseline_matched_rows"], 3)
            self.assertEqual(summary["baseline_withdrawn_rows"], 2)
            self.assertEqual(summary["baseline_unmatched_rows"], 1)
            self.assertEqual(summary["unsupported_rows"], 1)
            self.assertEqual(summary["online_query_count"], 3)
            projection = (root / "inputs/standards.csv").read_text(encoding="utf-8-sig")
            self.assertNotIn("文件名", projection)
            self.assertNotIn("甲.pdf", projection)
            with (root / "out/audit-results.csv").open(encoding="utf-8-sig") as f:
                results = list(csv.DictReader(f))
            self.assertIn("No baseline match", results[3]["Conclusion"])
            self.assertEqual(results[4]["Conclusion"], "Unsupported")

    def test_unique_priority_resume_and_merge_preserve_original_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db, input_path = make_fixture(root)
            prepare(parse_args(["prepare", str(input_path), "--db", str(db), "--output-dir", str(root / "pre"),
                                "--manifest-dir", str(root / "inputs")]))
            args = parse_args(["run", str(root / "inputs/standards.csv"), "--db", str(db),
                               "--manifest", str(root / "inputs/manifest.json"), "--output-dir", str(root / "cloud"), "--batch-size", "1"])
            def result(code):
                ev = Evidence("samr", "https://std.samr.gov.cn/test", now(), "evidence/test.txt", "sha")
                data = merge_observations([Observation(code, {"状态": "现行"}, [], [], ev)], [])
                data.update({"current_versions": [], "replacement_chain": [], "review_notes": []})
                return data
            with patch("audit_pipeline.Auditor") as factory:
                factory.return_value.audit.side_effect = result
                self.assertEqual(run(args), 0)
                self.assertEqual(factory.return_value.audit.call_args.args[0], "AQ1-2000")
                self.assertEqual(factory.return_value.audit.call_count, 1)
            with patch("audit_pipeline.Auditor") as factory:
                factory.return_value.audit.side_effect = result
                self.assertEqual(run(args), 0)
                self.assertEqual(factory.return_value.audit.call_args.args[0], "GB99-2000")
            merge(parse_args(["merge", str(input_path), "--result-dir", str(root / "cloud"), "--output-dir", str(root / "merged")]))
            with (root / "merged/audit-results.csv").open(encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 5)
            self.assertEqual([rows[1]["文件名"], rows[2]["文件名"]], ["甲.pdf", "乙.pdf"])
            self.assertEqual([rows[1]["Verified status"], rows[2]["Verified status"]], ["现行", "现行"])
            input_path.write_text("标准号\nGB 99-2001\n")
            with self.assertRaisesRegex(ValueError, "SHA256"):
                merge(parse_args(["merge", str(input_path), "--result-dir", str(root / "cloud"), "--output-dir", str(root / "wrong")]))

    def test_request_budget_and_429_suspend_site(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(directory)
            try:
                fetcher = Fetcher(store, delay=0, max_requests=1)
                response = Mock(status_code=429, is_redirect=False, url="https://std.samr.gov.cn/test", content=b"limited")
                fetcher.session.request = Mock(return_value=response)
                with self.assertRaisesRegex(ValueError, "429"):
                    fetcher.fetch("samr", response.url)
                with self.assertRaises(ValueError):
                    fetcher.fetch("samr", response.url)
                self.assertEqual(fetcher.session.request.call_count, 1)
                self.assertEqual(len(list((Path(directory) / "evidence").rglob("*.txt"))), 1)
                with self.assertRaises(RequestBudgetExceeded):
                    fetcher.request("GET", "https://hbba.sacinfo.org.cn/test")
            finally:
                store.conn.close()

    def test_fallback_stops_after_complete_official_record(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(directory)
            try:
                auditor = Auditor(store, Mock(), ["samr", "national", "csres"], source_policy="fallback")
                observation = Observation("GB1-2020", {"标准名称": "例", "状态": "现行", "发布日期": "2020-01-01", "实施日期": "2021-01-01"},
                                          [], [], Evidence("samr", "https://std.samr.gov.cn/test", now(), "test", "sha"))
                auditor.query = Mock(return_value=[observation])
                auditor.lookup("GB1-2020")
                self.assertEqual(auditor.query.call_count, 1)
            finally:
                store.conn.close()

    def test_manifest_mismatch_and_readonly_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db, _ = make_fixture(root)
            baseline = Baseline(db)
            try:
                self.assertIsNone(baseline.lookup("AQ1-2001"))
                with self.assertRaises(sqlite3.OperationalError):
                    baseline.conn.execute("DELETE FROM standards")
            finally:
                baseline.close()
            (root / "manifest.json").write_text('{"sha256":"错误","count_total":2}')
            with self.assertRaisesRegex(ValueError, "manifest"):
                Baseline(db)



if __name__ == "__main__":
    unittest.main()
