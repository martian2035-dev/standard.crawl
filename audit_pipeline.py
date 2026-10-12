#!/usr/bin/env python3
"""先离线匹配历史库，再按优先级分片联网核验；云端输入不包含文件名等信息。"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import requests

from standard_audit import (Auditor, CODE_RE, Evidence, Fetcher, LocalStore, Observation,
                            ROOT, RequestBudgetExceeded, comparable, date_value, export_csv,
                            merge_observations, normalize, now, read_input, status_value)
from audit_excel import export_excel

PROJECTION_HEADERS = ["标准类型", "标准号", "原统计表行号", "检索优先级", "本地筛查原因"]


def sha256(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def supported(row):
    return row.get("标准类型") != "国际标准" and bool(CODE_RE.fullmatch(normalize(row["标准号"])))


class Baseline:
    def __init__(self, db):
        self.db = Path(db).resolve()
        self.hash = sha256(self.db)
        self.conn = sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        try:
            meta = dict(self.conn.execute("SELECT key,value FROM meta"))
            self.built_at = meta.get("built_at", "")
            self.count = self.conn.execute("SELECT COUNT(*) FROM standards").fetchone()[0]
            if self.conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("历史数据库完整性检查失败")
            manifest_path = self.db.parent / "manifest.json"
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text())
                if manifest.get("sha256") != self.hash or manifest.get("count_total") != self.count:
                    raise ValueError("历史数据库与 manifest 的哈希或记录数量不一致")
        except Exception:
            self.conn.close()
            raise

    def lookup(self, code):
        matches = self.conn.execute("SELECT * FROM standards WHERE standard_no_compact=?", (normalize(code),)).fetchall()
        observations = []
        for row in matches:
            values = {"标准名称": row["name"], "状态": status_value(row["status"]),
                      "发布日期": date_value(row["publish_date"]), "实施日期": date_value(row["implement_date"]),
                      "标准类型": row["category"], "行业": row["industry"], "地区": row["region"]}
            if row["is_adopted"]:
                values["是否采标"] = "采"
            ev = Evidence("baseline", "", self.built_at, "历史基线/standards.db", self.hash,
                          {"数据库行ID": row["id"], "来源CSV": row["source_file"], "标准号": row["standard_no"]})
            observations.append(Observation(normalize(code), {k: v for k, v in values.items() if v}, [], [], ev))
        return merge_observations(observations, []) if observations else None

    def close(self):
        self.conn.close()


def screen(row, baseline_result):
    if not supported(row):
        return 0, ["国际标准或标准号格式未覆盖"]
    if baseline_result is None:
        return 90, ["本地数据库未精确匹配"]
    values, reasons, score = baseline_result["values"], [], 10
    if "/T" in normalize(row["标准号"]) and "/" not in str(row["标准号"]):
        reasons.append("已规范推荐标准前缀斜杠；保留原表标准号")
    if values.get("状态") == "废止":
        reasons.append("本地记录废止，需核实替代链")
        score = max(score, 100)
    if baseline_result["conflicts"]:
        reasons.append("本地同标准号记录存在冲突")
        score = max(score, 95)
    for key in ("状态", "发布日期", "实施日期"):
        if not values.get(key):
            reasons.append("本地记录缺少" + key)
            score = max(score, 80)
    for key in ("状态", "发布日期", "实施日期", "标准名称"):
        original = row.get(key, "")
        original = date_value(original) if key.endswith("日期") else (status_value(original) if key == "状态" else original)
        if original and values.get(key) and comparable(original) != comparable(values[key]):
            reasons.append("原表与本地记录的" + key + "不一致")
            score = max(score, 85)
    if not reasons:
        reasons = ["本地历史匹配，按需联网抽核"]
    return score, reasons


def precheck(input_path, db, output_dir):
    headers, rows = read_input(input_path)
    input_hash = sha256(input_path)
    baseline = Baseline(db)
    store = LocalStore(output_dir)
    try:
        existing = {n: json.loads(d) for n, d in store.conn.execute("SELECT sheet_row,data FROM results WHERE input_hash=?", (input_hash,))}
        counts = {"本地匹配行数": 0, "本地废止行数": 0, "本地未匹配行数": 0, "未覆盖行数": 0}
        tasks, memo = [], {}
        for number, row in enumerate(rows, 2):
            code = normalize(row["标准号"])
            if code not in memo:
                memo[code] = baseline.lookup(code) if supported(row) else None
            result = memo[code]
            priority, reasons = screen(row, result)
            if supported(row):
                counts["本地匹配行数" if result else "本地未匹配行数"] += 1
                counts["本地废止行数"] += bool(result and result["values"].get("状态") == "废止")
            else:
                counts["未覆盖行数"] += 1
            # 原始表在本地筛出的冲突优先级随最小查询清单带入云端。
            priority = max(priority, int(row.get("检索优先级") or 0)) if supported(row) else 0
            if row.get("本地筛查原因"):
                reasons = list(dict.fromkeys(reasons + str(row["本地筛查原因"]).split("；")))
            tasks.append({"sheet_row": number, "code": code, "priority": priority, "reasons": reasons, "supported": supported(row)})
            if number in existing and not existing[number].get("baseline_only"):
                continue
            data = dict(result) if result else {"values": {}, "provenance": {}, "observations": [], "conflicts": [], "errors": []}
            data.update({"original": row, "baseline_only": True, "checked_at": baseline.built_at,
                         "review_notes": ["历史库仅作筛查依据，状态未联网复核；全量采集器按标准号新增，可能保留旧状态。"] + reasons,
                         "local_screen": {"priority": priority, "reasons": reasons}})
            if not supported(row):
                data.update({"skipped": True, "baseline_only": False, "review_notes": reasons})
            store.save_row(input_hash, number, data)
        evidence_dir = Path(output_dir) / "历史基线"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        destination = evidence_dir / "standards.db"
        if not destination.exists() or sha256(destination) != baseline.hash:
            shutil.copyfile(baseline.db, destination)
        export_csv(store, headers, rows, input_hash)
        summary = {"输入SHA256": input_hash, "输入行数": len(rows), "历史数据库SHA256": baseline.hash,
                   "历史数据库构建时间": baseline.built_at, "历史数据库记录数": baseline.count,
                   **counts, "待联网唯一标准数": len({t["code"] for t in tasks if t["supported"]}), "生成时间": now()}
        atomic_json(Path(output_dir) / "本地筛查摘要.json", summary)
        atomic_json(Path(output_dir) / "检索任务.json", tasks)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        return tasks, rows, summary
    finally:
        baseline.close()
        store.conn.close()


def prepare(args):
    tasks, rows, summary = precheck(args.input, args.db, args.output_dir)
    args.manifest_dir.mkdir(parents=True, exist_ok=True)
    target = args.manifest_dir / "standards.csv"
    with target.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=PROJECTION_HEADERS, lineterminator="\n")
        writer.writeheader()
        for task, row in zip(tasks, rows):
            writer.writerow({"标准类型": row.get("标准类型", ""), "标准号": row["标准号"],
                             "原统计表行号": task["sheet_row"], "检索优先级": task["priority"],
                             "本地筛查原因": "；".join(task["reasons"])})
    atomic_json(args.manifest_dir / "manifest.json", {"原表SHA256": sha256(args.input), "查询清单SHA256": sha256(target),
                "原表行数": len(rows), "历史数据库SHA256": summary["历史数据库SHA256"],
                "历史数据库构建时间": summary["历史数据库构建时间"], "生成时间": now()})
    if args.manifest_dir.resolve() == ROOT / "audit_inputs":
        atomic_json(ROOT / "local_audit" / "本地输入配置.json", {"原表路径": str(args.input.resolve())})
    print(f"已生成最小查询清单：{target}；不包含文件名、大小、原表备注或原有来源链接。", flush=True)


def run(args):
    manifest = json.loads(args.manifest.read_text())
    if sha256(args.input) != manifest["查询清单SHA256"] or sha256(args.db) != manifest["历史数据库SHA256"]:
        raise ValueError("查询清单或历史数据库与清单 manifest 不一致，请重新 prepare")
    tasks, rows, _ = precheck(args.input, args.db, args.output_dir)
    headers, _ = read_input(args.input)
    input_hash, store = sha256(args.input), LocalStore(args.output_dir)
    previous_summary = args.output_dir / "运行摘要.json"
    previous_chunks = json.loads(previous_summary.read_text()).get("累计分片数", 0) if previous_summary.exists() else 0
    try:
        records = {n: json.loads(d) for n, d in store.conn.execute("SELECT sheet_row,data FROM results WHERE input_hash=?", (input_hash,))}
        grouped = {}
        for task in tasks:
            if task["supported"]:
                grouped.setdefault(task["code"], []).append(task)
        def pending(group):
            return any(records[t["sheet_row"]].get("baseline_only") or (args.retry_failed and
                records[t["sheet_row"]].get("attempt_count", 1) < 2 and (
                records[t["sheet_row"]].get("errors") or records[t["sheet_row"]].get("conflicts") or
                records[t["sheet_row"]].get("review_notes") or
                not records[t["sheet_row"]].get("values", {}).get("状态"))) for t in group)
        queue = [g for g in grouped.values() if pending(g)]
        queue.sort(key=lambda g: (-max(t["priority"] for t in g), g[0]["sheet_row"]))
        queue = queue[:args.batch_size]
        fetcher = Fetcher(store, args.delay, args.timeout, args.retries, args.budget_seconds, args.max_requests)
        auditor = Auditor(store, fetcher, args.sources.split(","), retry_failed=args.retry_failed, source_policy="fallback")
        count = confirmed = 0
        stop_reason = "批次完成"
        try:
            for group in queue:
                code = group[0]["code"]
                print(f"[{count + 1}/{len(queue)}] 优先核验 {code}", flush=True)
                data = auditor.audit(code)
                for task in group:
                    number = task["sheet_row"]
                    row_data = dict(data)
                    baseline_values = records[number].get("baseline_comparison", records[number].get("values", {}))
                    row_data.update({"original": rows[number - 2], "baseline_comparison": baseline_values,
                                     "attempt_count": records[number].get("attempt_count", 0 if records[number].get("baseline_only") else 1) + 1,
                                     "local_screen": {"priority": task["priority"], "reasons": task["reasons"]}})
                    store.save_row(input_hash, number, row_data)
                    records[number] = row_data
                count += 1
                confirmed += bool(data["values"].get("状态"))
                if all(h in fetcher.blocked for h in ["std.samr.gov.cn", "openstd.samr.gov.cn", "hbba.sacinfo.org.cn", "dbba.sacinfo.org.cn"]):
                    stop_reason = "全部官方站点访问受限，停止并保留进度"
                    break
        except (RequestBudgetExceeded, KeyboardInterrupt) as exc:
            stop_reason = str(exc) or "用户中断"
        export_csv(store, headers, rows, input_hash)
        left = sum(any(records[t["sheet_row"]].get("baseline_only") for t in group) for group in grouped.values())
        pending_count = sum(pending(g) for g in grouped.values())
        chunks = previous_chunks + 1
        auto_continue = bool(pending_count and count and "全部官方站点" not in stop_reason and chunks < 100)
        state = "正在自动核验" if auto_continue else ("全表已处理（未确认项需复核）" if not pending_count else "已暂停，请查看停止原因")
        if pending_count and not count:
            stop_reason += "；本片无完整标准进展，暂停自动续跑"
        if chunks >= 100 and pending_count:
            stop_reason += "；已达 100 片上限"
        processed = [records[g[0]["sheet_row"]] for g in grouped.values() if not records[g[0]["sheet_row"]].get("baseline_only")]
        summary = {"本次联网标准数": count, "本次确认状态标准数": confirmed, "本次请求数": fetcher.request_count,
                   "剩余未联网唯一标准数": left, "停止原因": stop_reason, "生成时间": now(),
                   "剩余待处理唯一标准数": pending_count,
                   "原表SHA256": manifest["原表SHA256"], "查询清单SHA256": input_hash,
                   "历史数据库构建时间": manifest["历史数据库构建时间"], "来源策略": "官方优先按需补缺",
                   "本次限速秒数": args.delay, "累计分片数": chunks, "自动续跑": auto_continue, "任务状态": state,
                   "累计已处理唯一标准数": len(processed), "累计确认状态标准数": sum(bool(r.get("values", {}).get("状态")) for r in processed),
                   "暂停站点": sorted(fetcher.blocked)}
        atomic_json(args.output_dir / "运行摘要.json", summary)
        atomic_json(args.output_dir / "输入映射.json", manifest)
        export_excel(args.output_dir)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        return 0 if not queue or confirmed else 2
    finally:
        store.conn.close()


def merge(args):
    metadata = json.loads((args.result_dir / "输入映射.json").read_text())
    if sha256(args.input) != metadata["原表SHA256"]:
        raise ValueError("下载结果对应的原表 SHA256 不一致，拒绝合并")
    headers, rows = read_input(args.input)
    store = LocalStore(args.output_dir)
    try:
        for line in (args.result_dir / "核验记录.jsonl").read_text().splitlines():
            data = json.loads(line)
            original = data["original"]
            number = int(original["原统计表行号"])
            if number < 2 or number > len(rows) + 1 or normalize(original["标准号"]) != normalize(rows[number - 2]["标准号"]):
                raise ValueError("云端记录与本地原表标准号或行号不一致")
            data["original"] = rows[number - 2]
            data.pop("原表行号", None)
            store.save_row(metadata["原表SHA256"], number, data)
        # 证据按内容哈希命名；历史基线也随成果包保留，保证字段证据可追溯。
        for name in ("网页证据", "历史基线"):
            if (args.result_dir / name).exists():
                shutil.copytree(args.result_dir / name, args.output_dir / name, dirs_exist_ok=True)
        target, _ = export_csv(store, headers, rows, metadata["原表SHA256"])
        if (args.result_dir / "运行摘要.json").exists():
            shutil.copyfile(args.result_dir / "运行摘要.json", args.output_dir / "运行摘要.json")
        excel = export_excel(args.output_dir)
        print(f"已合并到本地原表：{excel}", flush=True)
    finally:
        store.conn.close()




def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="统计表离线筛查及分片联网核验")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "precheck", "run", "merge"):
        p = sub.add_parser(name)
        p.add_argument("input", type=Path)
        p.add_argument("--output-dir", type=Path, default=ROOT / "local_audit" / name)
        if name != "merge":
            p.add_argument("--db", type=Path, default=ROOT / "standard_db" / "standards.db")
        if name == "prepare":
            p.add_argument("--manifest-dir", type=Path, default=ROOT / "audit_inputs")
        if name == "run":
            p.add_argument("--manifest", type=Path, default=ROOT / "audit_inputs" / "manifest.json")
            p.add_argument("--batch-size", type=int, default=50)
            p.add_argument("--budget-seconds", type=float, default=1800)
            p.add_argument("--max-requests", type=int, default=250)
            p.add_argument("--delay", type=float, default=5)
            p.add_argument("--timeout", type=float, default=15)
            p.add_argument("--retries", type=int, default=2)
            p.add_argument("--sources", default="samr,national,industry,local")
            p.add_argument("--retry-failed", action="store_true")
        if name == "merge":
            p.add_argument("--result-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output_dir.resolve()
    if output == ROOT or any(output == ROOT / p or ROOT / p in output.parents for p in ("standard_db", ".crawl_state", ".git", "audit_inputs")):
        parser.error("结果目录不能覆盖仓库、历史数据库、进度或输入清单")
    if args.command == "run":
        if args.batch_size < 1 or args.budget_seconds <= 0 or args.max_requests < 1 or args.delay < 0 or args.timeout <= 0 or args.retries < 1:
            parser.error("批次数、请求数和预算必须为正，间隔不能为负")
        if any(s not in ("samr", "national", "industry", "local", "csres") for s in args.sources.split(",")):
            parser.error("来源参数不合法")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.command == "prepare":
            prepare(args)
        elif args.command == "precheck":
            precheck(args.input, args.db, args.output_dir)
        elif args.command == "run":
            return run(args)
        elif args.command == "merge":
            merge(args)
        return 0
    except requests.RequestException as exc:
        print("核验请求失败：" + type(exc).__name__, flush=True)
        return 1
    except (ValueError, OSError, sqlite3.Error) as exc:
        print("核验流水线失败：" + str(exc), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
