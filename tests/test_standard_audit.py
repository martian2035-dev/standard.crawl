"""Offline regression tests for standard audit."""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from standard_audit import (Auditor, Evidence, LocalStore, Observation, SOURCES,
                            date_value, display_code, export_csv, merge_observations,
                            national_matches, normalize, now, parse_detail, read_input, status_value)


def evidence(source="samr"):
    return Evidence(source, SOURCES[source][1] + "/test", now(), "evidence/test.txt", "sha")


def observation(code, values, replacements=(), source="samr", relation_text=()):
    return Observation(code, values, list(replacements), list(relation_text), evidence(source))


class AuditTests(unittest.TestCase):
    def test_normalization_keeps_version_and_part(self):
        self.assertEqual(normalize("ＧＢ／Ｔ 150.1—2011"), "GB/T150.1-2011")
        self.assertEqual(display_code("AQ3001-2005"), "AQ 3001-2005")
        self.assertEqual(display_code("AQ/T3001-2021"), "AQ/T 3001-2021")
        self.assertNotEqual(normalize("GB/T1-2008"), normalize("GB/T1-2009"))
        self.assertEqual(normalize("GBT 150.1-2011"), "GB/T150.1-2011")
        self.assertEqual(normalize("SYT 1-2020"), "SY/T1-2020")
        self.assertEqual(normalize("GBZ 2.1-2019"), "GBZ2.1-2019")

    def test_only_forward_relation(self):
        html = """<h1>标准编号：AQ 3001-2005</h1>
        <table><tr><td>标准状态：</td><td>废止</td></tr>
        <tr><td>替代情况：</td><td>代替AQ 3001-1999；被AQ/T 3001-2021代替</td></tr></table>"""
        result = parse_detail(html, "AQ3001-2005", evidence("csres"))
        self.assertEqual(result.replacements, ["AQ/T3001-2021"])
        self.assertEqual(result.values["状态"], "废止")

    def test_wrong_detail_identity_rejected(self):
        html = "<h1>标准号：AQ 3022-2008</h1><p>相关标准 AQ 3022-2009</p>"
        with self.assertRaises(ValueError):
            parse_detail(html, "AQ3022-2009", evidence())

    def test_navigation_is_not_status(self):
        html = """<dl><dt>标准号</dt><dd>AQ 1-2000</dd></dl>
        <div class='page-header'><h4>示例标准</h4><span class='s-status'>现行</span></div>
        <div class='timeline'>发布 即将实施 实施 废止</div>"""
        self.assertEqual(parse_detail(html, "AQ1-2000", evidence()).values["状态"], "现行")
        self.assertEqual(status_value("非现行"), "")

    def test_samr_panels_are_directional(self):
        html = """<dl><dt>标准号</dt><dd>GB/T 1-2000</dd></dl>
        <div><div class='panel-heading'>代替以下标准</div><div class='panel-body'>GB/T 1-1990</div></div>
        <div><div class='panel-heading'>被以下标准替代</div><div class='panel-body'>GB/T 1.1-2020（部分代替） GB/T 1.2-2020</div></div>
        <div><a>GB/T 999-2020</a></div>"""
        result = parse_detail(html, "GB/T1-2000", evidence())
        self.assertEqual(result.replacements, ["GB/T1.1-2020", "GB/T1.2-2020"])
        self.assertIn("部分代替", result.relation_text[0])

    def test_national_headers_not_positions(self):
        html = """<table><tr><th>标准号</th><th>语种</th><th>发布日期</th><th>状态</th><th>操作</th></tr>
        <tr><td>GB/T 1-2000</td><td></td><td>2000-01-01</td><td>废止</td><td onclick="showInfo('ABC123');"></td></tr>
        <tr><td>GB/T 11-2000</td><td></td><td>2000-02-01</td><td>现行</td><td onclick="showInfo('ABC234');"></td></tr>
        <tr><td>GB/T 1-2000</td><td>EN</td><td></td><td>发布</td><td onclick="showInfo('ABC456');"></td></tr></table>"""
        matches = national_matches(html, "GB/T1-2000", "https://openstd.samr.gov.cn/bzgk/std/std_list")
        self.assertEqual(len(matches), 1)
        self.assertIn("hcno=ABC123", matches[0][1])
        with self.assertRaises(ValueError):
            national_matches("请输入验证码", "GB/T1-2000", "https://example.test")

    def test_official_priority_preserves_conflict(self):
        result = merge_observations([
            observation("AQ1-2000", {"状态": "现行"}, source="csres"),
            observation("AQ1-2000", {"状态": "废止"}, source="industry")], [])
        self.assertEqual(result["values"]["状态"], "废止")
        self.assertTrue(result["conflicts"])
        self.assertEqual(len(result["provenance"]["状态"]), 2)

    def test_equal_authority_conflict_is_blank(self):
        result = merge_observations([
            observation("GB/T1-2000", {"状态": "现行"}, source="national"),
            observation("GB/T1-2000", {"状态": "废止"}, source="samr")], [])
        self.assertNotIn("状态", result["values"])
        self.assertFalse(any(e["selected"] for e in result["provenance"]["状态"]))

    def test_name_whitespace_is_not_a_conflict(self):
        result = merge_observations([
            observation("AQ1-2000", {"标准名称": "加油（气）站 安全规程"}),
            observation("AQ1-2000", {"标准名称": "加油(气)站安全规程"}, source="industry")], [])
        self.assertFalse(result["conflicts"])

    def test_replacement_conflict_is_not_guessed(self):
        result = merge_observations([
            observation("AQ1-2000", {}, ["AQ1-2020"]),
            observation("AQ1-2000", {}, ["AQ1-2021"], "csres")], [])
        self.assertEqual(result["replacements"], [])
        self.assertTrue(result["conflicts"])

    def test_dates_are_not_interchanged(self):
        html = "<h1>标准号：AQ 1-2000</h1><div class='title'>发布日期</div><div>2000-02-03</div><div class='title'>实施日期</div><div>2001-03-04</div>"
        values = parse_detail(html, "AQ1-2000", evidence()).values
        self.assertEqual(values["发布日期"], "2000-02-03")
        self.assertEqual(values["实施日期"], "2001-03-04")
        self.assertEqual(date_value("2020-02-30"), "")

    def test_chain_follows_to_current_and_excludes_future(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalStore(tmp)
            try:
                auditor = Auditor(store, Mock(), ["samr"])
                data = {
                    "AQ1-2000": merge_observations([observation("AQ1-2000", {"状态": "废止"}, ["AQ1-2010", "AQ2-2025"])], []),
                    "AQ1-2010": merge_observations([observation("AQ1-2010", {"状态": "废止"}, ["AQ1-2020"])], []),
                    "AQ1-2020": merge_observations([observation("AQ1-2020", {"状态": "现行", "发布日期": "2020-01-01"})], []),
                    "AQ2-2025": merge_observations([observation("AQ2-2025", {"状态": "即将实施"})], []),
                }
                auditor.lookup = lambda code, seed="": data[code]
                result = auditor.audit("AQ1-2000")
                self.assertEqual([v["标准号"] for v in result["current_versions"]], ["AQ1-2020"])
                self.assertTrue(result["review_notes"])
                self.assertEqual(len(result["replacement_chain"]), 4)
            finally:
                store.conn.close()

    def test_cycle_terminates_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalStore(tmp)
            try:
                auditor = Auditor(store, Mock(), ["samr"])
                data = merge_observations([observation("AQ1-2000", {"状态": "废止"}, ["AQ1-2000"])], [])
                auditor.lookup = lambda code, seed="": data
                self.assertIn("cycle", auditor.audit("AQ1-2000")["review_notes"][0])
            finally:
                store.conn.close()

    def test_cache_retry_and_export_preserve_duplicate_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalStore(tmp)
            try:
                data = merge_observations([observation("AQ1-2000", {"状态": "现行"})], [{"source": "csres", "message": "Request failed"}])
                store.save_lookup("k", data)
                self.assertIsNotNone(store.cached("k", 7))
                self.assertIsNone(store.cached("k", 7, retry_failed=True))
                rows = [{"标准号": "AQ1-2000", "文件名": "=危险公式"}, {"标准号": "AQ1-2000", "文件名": "另一个文件"}, {"标准号": "AQ2-2000", "文件名": "保留"}]
                store.save_row("hash", 2, data)
                store.save_row("hash", 3, data)
                target, _ = export_csv(store, ["标准号", "文件名"], rows, "hash")
                with target.open(encoding="utf-8-sig") as f:
                    exported = list(csv.DictReader(f))
                self.assertEqual(len(exported), 3)
                self.assertEqual(exported[0]["文件名"], "'=危险公式")
                self.assertEqual(exported[1]["文件名"], "另一个文件")
                self.assertEqual(exported[2]["Conclusion"], "Not collected")
                self.assertEqual(len((Path(tmp) / "audit-records.jsonl").read_text().splitlines()), 2)
            finally:
                store.conn.close()

    def test_input_headers_are_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.csv"
            path.write_text("标准号,标准号\nAQ1-2000,AQ2-2000\n")
            with self.assertRaises(ValueError):
                read_input(path)


if __name__ == "__main__":
    unittest.main()
