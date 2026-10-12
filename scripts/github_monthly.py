#!/usr/bin/env python3
"""GitHub 月度任务：复用采集器与核验器，显式保存下一片所需状态。"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import re
import shutil
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import audit_pipeline
import build_db
import standard_crawl
from scripts.public_input import PUBLIC_INPUT, validate_public

CSV_FILES = ["国家公开标准信息.csv", "行业公开标准信息.csv", "地方公开标准信息.csv"]
CRAWL_TASKS = ["national:1", "national:2", "national:3", "industry", "local"]
CRAWL_STATE = Path(".github_crawl_state")
AUDIT_STATE = Path(".github_audit_work")
INPUT = Path(PUBLIC_INPUT)


def campaign(value=""):
    value = value or dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("月份必须为 YYYY-MM")
    return value


def outputs(**values):
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as f:
            for key, value in values.items():
                f.write(f"{key}={value}\n")
    print(json.dumps(values, ensure_ascii=False), flush=True)


def summary(data):
    text = "```json\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n```\n"
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text)


def crawl(args):
    options = ["--campaign", args.campaign, "--state-dir", str(CRAWL_STATE),
               "--strict-pages", "--budget-seconds", str(args.budget_seconds),
               "--delay", str(args.delay), "--max-chunks", "100"]
    for task in CRAWL_TASKS:
        options += ["--only", task]
    status = standard_crawl.run_chunk(standard_crawl.parse_args(options))
    summary(status)
    skipped = any(status["tasks"][task]["skipped_pages"] for task in CRAWL_TASKS)
    next_step = "stop" if skipped else status["next_action"]
    outputs(campaign=args.campaign, next=next_step)
    if next_step == "stop":
        raise ValueError("采集暂停，已保存进度；检查站点后手动运行同一月份。跳过页必须重新采集。")


def package_database(args):
    # GitHub 仅更新用户要求的三份数据；工程标准 CSV 如存在，构建器仍会纳入。
    for name in CSV_FILES:
        if not Path(name).is_file():
            raise ValueError("缺少必需的 CSV：" + name)
    out = Path(".github_release")
    manifest = build_db.build(Path.cwd(), out, quiet=True)
    if build_db.verify(out):
        raise ValueError("数据库校验失败，拒绝发布")
    with (out / "standards.db").open("rb") as source, gzip.open(out / "standards.db.gz", "wb") as target:
        shutil.copyfileobj(source, target)
    manifest["campaign"] = args.campaign
    manifest["csv_sha256"] = {name: audit_pipeline.sha256(name) for name in CSV_FILES}
    audit_pipeline.atomic_json(out / "manifest.json", manifest)
    outputs(campaign=args.campaign)


def audit(args):
    if not INPUT.exists():
        raise ValueError("缺少公开标准查询清单.csv，请在本地使用 public_input.py prepare 准备")
    validate_public(INPUT)
    meta_path = AUDIT_STATE / "campaign.json"
    baseline = AUDIT_STATE / "results/历史基线/standards.db"
    if args.restore and not meta_path.exists():
        raise ValueError("续片缺少前片检查点，停止以免把部分结果当成全表结果")
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta["campaign"] != args.campaign or meta["input_sha256"] != audit_pipeline.sha256(INPUT):
            raise ValueError("月份或原表已变化，拒绝混用旧核验结果；不填 restore_run_id 可重新开始")
        if meta["chunks"] >= 100:
            raise ValueError("达到 100 片上限，请检查核验进度")
    else:
        AUDIT_STATE.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(INPUT, AUDIT_STATE / INPUT.name)
        build_db.build(Path.cwd(), baseline.parent, quiet=True)
        if build_db.verify(baseline.parent):
            raise ValueError("核验基线数据库校验失败")
        audit_pipeline.prepare(audit_pipeline.parse_args([
            "prepare", str(INPUT), "--db", str(baseline),
            "--output-dir", str(AUDIT_STATE / "precheck"), "--manifest-dir", str(AUDIT_STATE / "inputs")]))
        shutil.rmtree(AUDIT_STATE / "precheck")
        meta = {"campaign": args.campaign, "input_sha256": audit_pipeline.sha256(INPUT), "chunks": 0}
        audit_pipeline.atomic_json(meta_path, meta)
    # 每月新建整个缓存；同月续片才复用。历史库命中也必须联网核验。
    run_args = audit_pipeline.parse_args([
        "run", str(AUDIT_STATE / "inputs/standards.csv"),
        "--manifest", str(AUDIT_STATE / "inputs/manifest.json"), "--db", str(baseline),
        "--output-dir", str(AUDIT_STATE / "results"), "--batch-size", "100",
        "--budget-seconds", str(args.budget_seconds), "--max-requests", "600",
        "--delay", str(args.delay), "--retry-failed"])
    audit_pipeline.run(run_args)
    meta["chunks"] += 1
    audit_pipeline.atomic_json(meta_path, meta)
    result = json.loads((AUDIT_STATE / "results/运行摘要.json").read_text(encoding="utf-8"))
    summary(result)
    left = result["剩余待处理唯一标准数"]
    next_step = "continue" if left and result["自动续跑"] else ("stop" if left else "done")
    outputs(campaign=args.campaign, next=next_step)
    if next_step == "stop":
        raise ValueError("核验暂停；检查运行摘要，使用本次运行 ID 恢复。未确认项不会标成现行或废止。")
    if next_step == "done":
        merged = AUDIT_STATE / "merged"
        audit_pipeline.merge(audit_pipeline.parse_args([
            "merge", str(INPUT), "--result-dir", str(AUDIT_STATE / "results"), "--output-dir", str(merged)]))
        report = Path("reports/标准信息统计表_已核验.xlsx")
        report.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(merged / "标准信息核验结果.xlsx", report)
        shutil.copyfile(merged / "标准信息核验结果.csv", "reports/标准信息核验结果.csv")
        audit_pipeline.atomic_json(Path("reports/核验摘要.json"), {**result, "月份": args.campaign})
        release = Path(".github_release")
        release.mkdir(exist_ok=True)
        # 详细证据放在 Release，表格和摘要也回写 main，便于直接下载。
        with tarfile.open(release / "核验证据.tar.gz", "w:gz") as archive:
            archive.add(AUDIT_STATE / "results", arcname="核验证据")


def main(argv=None):
    parser = argparse.ArgumentParser(description="GitHub 月度标准数据与表格核验")
    parser.add_argument("command", choices=("crawl", "database", "audit"))
    parser.add_argument("--campaign", default=os.environ.get("CAMPAIGN", ""))
    parser.add_argument("--budget-seconds", type=int, default=3600)
    parser.add_argument("--delay", type=float, default=5)
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args(argv)
    try:
        args.campaign = campaign(args.campaign)
        if args.budget_seconds <= 0 or args.delay < 0:
            raise ValueError("时间预算须为正，间隔不能为负")
        {"crawl": crawl, "database": package_database, "audit": audit}[args.command](args)
        return 0
    except (ValueError, OSError) as exc:
        print("任务失败：" + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
