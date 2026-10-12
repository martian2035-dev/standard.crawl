#!/usr/bin/env python3
"""Screen a fixed baseline, then run prioritized online audit chunks with minimal input."""
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
                            ROOT, FIELD_LABELS, RequestBudgetExceeded, comparable, date_value, export_csv,
                            merge_observations, normalize, now, read_input, status_value)
from audit_excel import export_excel

PROJECTION_HEADERS = ["标准类型", "标准号", "Original worksheet row", "Search priority", "Screening notes"]


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
                raise ValueError("Baseline database integrity check failed")
            manifest_path = self.db.parent / "manifest.json"
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text())
                if manifest.get("sha256") != self.hash or manifest.get("count_total") != self.count:
                    raise ValueError("Baseline hash or row count differs from the manifest")
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
            ev = Evidence("baseline", "", self.built_at, "baseline/standards.db", self.hash,
                          {"database_row_id": row["id"], "source_csv": row["source_file"], "标准号": row["standard_no"]})
            observations.append(Observation(normalize(code), {k: v for k, v in values.items() if v}, [], [], ev))
        return merge_observations(observations, []) if observations else None

    def close(self):
        self.conn.close()


def screen(row, baseline_result):
    if not supported(row):
        return 0, ["International standard or unsupported identifier format"]
    if baseline_result is None:
        return 90, ["No exact match in the baseline database"]
    values, reasons, score = baseline_result["values"], [], 10
    if "/T" in normalize(row["标准号"]) and "/" not in str(row["标准号"]):
        reasons.append("Normalized the recommended-standard prefix slash; original identifier retained")
    if values.get("状态") == "废止":
        reasons.append("Baseline record is withdrawn; verify the replacement chain")
        score = max(score, 100)
    if baseline_result["conflicts"]:
        reasons.append("Conflicting baseline records for the same standard number")
        score = max(score, 95)
    for key in ("状态", "发布日期", "实施日期"):
        if not values.get(key):
            reasons.append("Baseline record is missing: " + FIELD_LABELS[key])
            score = max(score, 80)
    for key in ("状态", "发布日期", "实施日期", "标准名称"):
        original = row.get(key, "")
        original = date_value(original) if key.endswith("日期") else (status_value(original) if key == "状态" else original)
        if original and values.get(key) and comparable(original) != comparable(values[key]):
            reasons.append("Input and baseline field " + FIELD_LABELS[key] + " differs")
            score = max(score, 85)
    if not reasons:
        reasons = ["Baseline match; online verification pending"]
    return score, reasons


def precheck(input_path, db, output_dir):
    headers, rows = read_input(input_path)
    input_hash = sha256(input_path)
    baseline = Baseline(db)
    store = LocalStore(output_dir)
    try:
        existing = {n: json.loads(d) for n, d in store.conn.execute("SELECT sheet_row,data FROM results WHERE input_hash=?", (input_hash,))}
        counts = {"baseline_matched_rows": 0, "baseline_withdrawn_rows": 0, "baseline_unmatched_rows": 0, "unsupported_rows": 0}
        tasks, memo = [], {}
        for number, row in enumerate(rows, 2):
            code = normalize(row["标准号"])
            if code not in memo:
                memo[code] = baseline.lookup(code) if supported(row) else None
            result = memo[code]
            priority, reasons = screen(row, result)
            if supported(row):
                counts["baseline_matched_rows" if result else "baseline_unmatched_rows"] += 1
                counts["baseline_withdrawn_rows"] += bool(result and result["values"].get("状态") == "废止")
            else:
                counts["unsupported_rows"] += 1
            # Carry local conflict priorities into the minimal query list.
            priority = max(priority, int(row.get("Search priority") or 0)) if supported(row) else 0
            if row.get("Screening notes"):
                reasons = list(dict.fromkeys(reasons + str(row["Screening notes"]).split("；")))
            tasks.append({"sheet_row": number, "code": code, "priority": priority, "reasons": reasons, "supported": supported(row)})
            if number in existing and not existing[number].get("baseline_only"):
                continue
            data = dict(result) if result else {"values": {}, "provenance": {}, "observations": [], "conflicts": [], "errors": []}
            data.update({"original": row, "baseline_only": True, "checked_at": baseline.built_at,
                         "review_notes": ["The baseline is for screening only; status has not been verified online and may be stale."] + reasons,
                         "local_screen": {"priority": priority, "reasons": reasons}})
            if not supported(row):
                data.update({"skipped": True, "baseline_only": False, "review_notes": reasons})
            store.save_row(input_hash, number, data)
        evidence_dir = Path(output_dir) / "baseline"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        destination = evidence_dir / "standards.db"
        if not destination.exists() or sha256(destination) != baseline.hash:
            shutil.copyfile(baseline.db, destination)
        export_csv(store, headers, rows, input_hash)
        summary = {"input_sha256": input_hash, "input_rows": len(rows), "baseline_sha256": baseline.hash,
                   "baseline_built_at": baseline.built_at, "baseline_count": baseline.count,
                   **counts, "online_query_count": len({t["code"] for t in tasks if t["supported"]}), "generated_at": now()}
        atomic_json(Path(output_dir) / "precheck-summary.json", summary)
        atomic_json(Path(output_dir) / "search-tasks.json", tasks)
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
                             "Original worksheet row": task["sheet_row"], "Search priority": task["priority"],
                             "Screening notes": "；".join(task["reasons"])})
    atomic_json(args.manifest_dir / "manifest.json", {"original_sha256": sha256(args.input), "query_sha256": sha256(target),
                "original_rows": len(rows), "baseline_sha256": summary["baseline_sha256"],
                "baseline_built_at": summary["baseline_built_at"], "generated_at": now()})
    if args.manifest_dir.resolve() == ROOT / "audit_inputs":
        atomic_json(ROOT / "local_audit" / "local-input.json", {"original_path": str(args.input.resolve())})
    print(f"Prepared minimal query list: {target}; excludes filenames, sizes, private notes and original source URLs.", flush=True)


def run(args):
    manifest = json.loads(args.manifest.read_text())
    if sha256(args.input) != manifest["query_sha256"] or sha256(args.db) != manifest["baseline_sha256"]:
        raise ValueError("Query list or baseline differs from the manifest; run prepare again")
    tasks, rows, _ = precheck(args.input, args.db, args.output_dir)
    headers, _ = read_input(args.input)
    input_hash, store = sha256(args.input), LocalStore(args.output_dir)
    previous_summary = args.output_dir / "run-summary.json"
    previous_chunks = json.loads(previous_summary.read_text()).get("chunks", 0) if previous_summary.exists() else 0
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
        stop_reason = "Batch complete"
        try:
            for group in queue:
                code = group[0]["code"]
                print(f"[{count + 1}/{len(queue)}] Auditing by priority: {code}", flush=True)
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
                    stop_reason = "All official sites are restricted; stopped with progress saved"
                    break
        except (RequestBudgetExceeded, KeyboardInterrupt) as exc:
            stop_reason = str(exc) or "Interrupted by user"
        export_csv(store, headers, rows, input_hash)
        left = sum(any(records[t["sheet_row"]].get("baseline_only") for t in group) for group in grouped.values())
        pending_count = sum(pending(g) for g in grouped.values())
        chunks = previous_chunks + 1
        auto_continue = bool(pending_count and count and "All official sites" not in stop_reason and chunks < 100)
        state = "Audit in progress" if auto_continue else ("All supported entries processed (unconfirmed results need review)" if not pending_count else "Paused; review stop_reason")
        if pending_count and not count:
            stop_reason += "; no completed standards this chunk; automatic continuation paused"
        if chunks >= 100 and pending_count:
            stop_reason += "; reached the 100-chunk limit"
        processed = [records[g[0]["sheet_row"]] for g in grouped.values() if not records[g[0]["sheet_row"]].get("baseline_only")]
        summary = {"queried_this_run": count, "confirmed_this_run": confirmed, "requests_this_run": fetcher.request_count,
                   "unqueried_count": left, "stop_reason": stop_reason, "generated_at": now(),
                   "pending_count": pending_count,
                   "original_sha256": manifest["original_sha256"], "query_sha256": input_hash,
                   "baseline_built_at": manifest["baseline_built_at"], "source_policy": "official-first fallback",
                   "delay_seconds": args.delay, "chunks": chunks, "auto_continue": auto_continue, "task_status": state,
                   "processed_count": len(processed), "confirmed_count": sum(bool(r.get("values", {}).get("状态")) for r in processed),
                   "paused_hosts": sorted(fetcher.blocked)}
        atomic_json(args.output_dir / "run-summary.json", summary)
        atomic_json(args.output_dir / "input-mapping.json", manifest)
        export_excel(args.output_dir)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        return 0 if not queue or confirmed else 2
    finally:
        store.conn.close()


def merge(args):
    metadata = json.loads((args.result_dir / "input-mapping.json").read_text())
    if sha256(args.input) != metadata["original_sha256"]:
        raise ValueError("Downloaded results do not match the original SHA256; refusing to merge")
    headers, rows = read_input(args.input)
    store = LocalStore(args.output_dir)
    try:
        for line in (args.result_dir / "audit-records.jsonl").read_text().splitlines():
            data = json.loads(line)
            original = data["original"]
            number = int(original["Original worksheet row"])
            if number < 2 or number > len(rows) + 1 or normalize(original["标准号"]) != normalize(rows[number - 2]["标准号"]):
                raise ValueError("Remote result does not match the original standard number or row")
            data["original"] = rows[number - 2]
            data.pop("Input row", None)
            store.save_row(metadata["original_sha256"], number, data)
        # Keep content-addressed evidence and the fixed baseline with the result package for traceability.
        for name in ("evidence", "baseline"):
            if (args.result_dir / name).exists():
                shutil.copytree(args.result_dir / name, args.output_dir / name, dirs_exist_ok=True)
        target, _ = export_csv(store, headers, rows, metadata["original_sha256"])
        if (args.result_dir / "run-summary.json").exists():
            shutil.copyfile(args.result_dir / "run-summary.json", args.output_dir / "run-summary.json")
        excel = export_excel(args.output_dir)
        print(f"Merged into a local copy of the original workbook: {excel}", flush=True)
    finally:
        store.conn.close()




def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Offline screening and resumable online standards audit")
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
        parser.error("Output must not overwrite the repository, baseline, state or input manifests")
    if args.command == "run":
        if args.batch_size < 1 or args.budget_seconds <= 0 or args.max_requests < 1 or args.delay < 0 or args.timeout <= 0 or args.retries < 1:
            parser.error("Batch size, request count and budgets must be positive; delay cannot be negative")
        if any(s not in ("samr", "national", "industry", "local", "csres") for s in args.sources.split(",")):
            parser.error("Invalid source argument")
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
        print("Audit request failed: " + type(exc).__name__, flush=True)
        return 1
    except (ValueError, OSError, sqlite3.Error) as exc:
        print("Audit pipeline failed: " + str(exc), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
