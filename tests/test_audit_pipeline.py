"""独立核验的离线集成测试：历史基线、分片续跑、行号映射与请求预算。"""
import csv
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from audit_pipeline import ROOT, Baseline, merge, parse_args, precheck, prepare, run
from standard_audit import (Auditor, Evidence, Fetcher, LocalStore, Observation,
                            RequestBudgetExceeded, merge_observations, now)
from scripts import audit_artifacts


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
    def test_attachment_missing_is_allowed_only_for_first_run(self):
        env = {"CNB_COMMIT": "a" * 40, "CNB_REPO_SLUG": "test/repo", "CNB_TOKEN": "测试令牌", "AUDIT_RESTORE_COMMIT": "", "CNB_EVENT": "push"}
        response = Mock(status_code=200)
        response.json.return_value = []
        with patch.dict(os.environ, env), patch("scripts.audit_artifacts.requests.get", return_value=response) as request:
            audit_artifacts.restore()
            self.assertEqual(request.call_args.kwargs["headers"]["Accept"], "application/vnd.cnb.api+json")
            with patch.dict(os.environ, {"CNB_EVENT": "api_trigger_audit_continue"}):
                with self.assertRaisesRegex(ValueError, "没有找到前片成果"):
                    audit_artifacts.restore()
            with patch.dict(os.environ, {"AUDIT_RESTORE_COMMIT": "b" * 40}):
                with self.assertRaisesRegex(ValueError, "没有核验附件"):
                    audit_artifacts.restore()
            response.status_code = 403
            with self.assertRaisesRegex(ValueError, "停止以免丢失进度"):
                audit_artifacts.restore()

    def test_prepare_is_offline_and_projected_input_has_no_file_information(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db, input_path = make_fixture(root)
            args = parse_args(["prepare", str(input_path), "--db", str(db), "--output-dir", str(root / "out"),
                               "--manifest-dir", str(root / "inputs")])
            with patch("requests.Session.request", side_effect=AssertionError("离线筛查不能访问网络")):
                prepare(args)
            summary = json.loads((root / "out/本地筛查摘要.json").read_text())
            self.assertEqual(summary["本地匹配行数"], 3)
            self.assertEqual(summary["本地废止行数"], 2)
            self.assertEqual(summary["本地未匹配行数"], 1)
            self.assertEqual(summary["未覆盖行数"], 1)
            self.assertEqual(summary["待联网唯一标准数"], 3)
            projection = (root / "inputs/standards.csv").read_text(encoding="utf-8-sig")
            self.assertNotIn("文件名", projection)
            self.assertNotIn("甲.pdf", projection)
            with (root / "out/标准信息核验结果.csv").open(encoding="utf-8-sig") as f:
                results = list(csv.DictReader(f))
            self.assertIn("未匹配", results[3]["核验结论"])
            self.assertEqual(results[4]["核验结论"], "未覆盖")

    def test_unique_priority_resume_and_merge_preserve_original_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db, input_path = make_fixture(root)
            prepare(parse_args(["prepare", str(input_path), "--db", str(db), "--output-dir", str(root / "pre"),
                                "--manifest-dir", str(root / "inputs")]))
            args = parse_args(["run", str(root / "inputs/standards.csv"), "--db", str(db),
                               "--manifest", str(root / "inputs/manifest.json"), "--output-dir", str(root / "cloud"), "--batch-size", "1"])
            def result(code):
                ev = Evidence("samr", "https://std.samr.gov.cn/test", now(), "网页证据/test.txt", "sha")
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
            with (root / "merged/标准信息核验结果.csv").open(encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 5)
            self.assertEqual([rows[1]["文件名"], rows[2]["文件名"]], ["甲.pdf", "乙.pdf"])
            self.assertEqual([rows[1]["核验状态"], rows[2]["核验状态"]], ["现行", "现行"])
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
                self.assertEqual(len(list((Path(directory) / "网页证据").rglob("*.txt"))), 1)
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

    def test_cnb_audit_is_separate_from_data_commit_and_deployment(self):
        config = yaml.safe_load((ROOT / ".cnb.yml").read_text())
        audit = config["codex/standard-table-audit"]["push"]
        self.assertEqual(audit, config["main"]["api_trigger_audit"])
        scripts = json.dumps(audit, ensure_ascii=False)
        for forbidden in ("api_trigger_vps", "publish_db", "commit_back", "sync_latest", "web_secrets"):
            self.assertNotIn(forbidden, scripts)
        self.assertEqual(audit[0]["env"]["AUDIT_DELAY"], "5")
        upload = next(s for s in audit[0]["endStages"] if s.get("image"))
        self.assertEqual(upload["settings"]["ttl"], 7)
        self.assertEqual(audit[0]["endStages"][-1]["options"]["event"], "api_trigger_audit_continue")
        self.assertNotIn("AUDIT_NEXT", audit[0]["endStages"][-1].get("env", {}))
        self.assertNotIn("AUDIT_UPLOADED_FILES", audit[0]["endStages"][-1].get("env", {}))
        initialization = next(s for s in audit[0]["stages"] if "audit_uploaded" in s.get("exports", {}))
        self.assertEqual(initialization["exports"]["audit_next"], "AUDIT_NEXT")
        self.assertEqual(audit, config["codex/standard-table-audit"]["api_trigger_audit_continue"])
        yaml.safe_load((ROOT / ".cnb/web_trigger.yml").read_text())


if __name__ == "__main__":
    unittest.main()
