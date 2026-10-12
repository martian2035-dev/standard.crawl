#!/usr/bin/env python3
"""Run monthly GitHub collection and audit jobs with explicit continuation checkpoints."""
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
        raise ValueError("Campaign must use YYYY-MM format")
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
        raise ValueError("Crawl paused with progress saved; check the sites and resume the same campaign. Skipped pages must be retried.")


def package_database(args):
    # Update the three monthly CSVs; include engineering data in the database when available.
    for name in CSV_FILES:
        if not Path(name).is_file():
            raise ValueError("Missing required CSV: " + name)
    out = Path(".github_release")
    manifest = build_db.build(Path.cwd(), out, quiet=True)
    if build_db.verify(out):
        raise ValueError("Database verification failed; refusing to publish")
    with (out / "standards.db").open("rb") as source, gzip.open(out / "standards.db.gz", "wb") as target:
        shutil.copyfileobj(source, target)
    manifest["campaign"] = args.campaign
    manifest["csv_sha256"] = {name: audit_pipeline.sha256(name) for name in CSV_FILES}
    audit_pipeline.atomic_json(out / "manifest.json", manifest)
    outputs(campaign=args.campaign)


def audit(args):
    if not INPUT.exists():
        raise ValueError("Missing public_standards.csv; prepare it locally with public_input.py prepare")
    validate_public(INPUT)
    meta_path = AUDIT_STATE / "campaign.json"
    baseline = AUDIT_STATE / "results/baseline/standards.db"
    if args.restore and not meta_path.exists():
        raise ValueError("Missing previous checkpoint; stopped to avoid treating partial results as a complete audit")
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta["campaign"] != args.campaign or meta["input_sha256"] != audit_pipeline.sha256(INPUT):
            raise ValueError("Campaign or input changed; refusing to mix old results. Omit restore_run_id to restart.")
        if meta.get("checkpoint_version") != 2:
            raise ValueError("Incompatible checkpoint format; omit restore_run_id to start a fresh audit")
        if meta["chunks"] >= 100:
            raise ValueError("Reached the 100-chunk limit; review audit progress")
    else:
        AUDIT_STATE.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(INPUT, AUDIT_STATE / INPUT.name)
        build_db.build(Path.cwd(), baseline.parent, quiet=True)
        if build_db.verify(baseline.parent):
            raise ValueError("Audit baseline database verification failed")
        audit_pipeline.prepare(audit_pipeline.parse_args([
            "prepare", str(INPUT), "--db", str(baseline),
            "--output-dir", str(AUDIT_STATE / "precheck"), "--manifest-dir", str(AUDIT_STATE / "inputs")]))
        shutil.rmtree(AUDIT_STATE / "precheck")
        meta = {"campaign": args.campaign, "input_sha256": audit_pipeline.sha256(INPUT), "chunks": 0, "checkpoint_version": 2}
        audit_pipeline.atomic_json(meta_path, meta)
    # Start with fresh caches each month; resume within a campaign. Baseline matches still require online checks.
    run_args = audit_pipeline.parse_args([
        "run", str(AUDIT_STATE / "inputs/standards.csv"),
        "--manifest", str(AUDIT_STATE / "inputs/manifest.json"), "--db", str(baseline),
        "--output-dir", str(AUDIT_STATE / "results"), "--batch-size", "100",
        "--budget-seconds", str(args.budget_seconds), "--max-requests", "600",
        "--delay", str(args.delay), "--retry-failed"])
    audit_pipeline.run(run_args)
    meta["chunks"] += 1
    audit_pipeline.atomic_json(meta_path, meta)
    result = json.loads((AUDIT_STATE / "results/run-summary.json").read_text(encoding="utf-8"))
    summary(result)
    left = result["pending_count"]
    next_step = "continue" if left and result["auto_continue"] else ("stop" if left else "done")
    outputs(campaign=args.campaign, next=next_step)
    if next_step == "stop":
        raise ValueError("Audit paused; review run-summary.json and resume with this run ID. Unconfirmed entries are not classified as current or withdrawn.")
    if next_step == "done":
        merged = AUDIT_STATE / "merged"
        audit_pipeline.merge(audit_pipeline.parse_args([
            "merge", str(INPUT), "--result-dir", str(AUDIT_STATE / "results"), "--output-dir", str(merged)]))
        report = Path("reports/standards-audited.xlsx")
        report.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(merged / "audit-results.xlsx", report)
        shutil.copyfile(merged / "audit-results.csv", "reports/audit-results.csv")
        audit_pipeline.atomic_json(Path("reports/audit-summary.json"), {**result, "campaign": args.campaign})
        release = Path(".github_release")
        release.mkdir(exist_ok=True)
        # Publish detailed evidence as a Release asset and commit summary reports to main.
        with tarfile.open(release / "audit-evidence.tar.gz", "w:gz") as archive:
            archive.add(AUDIT_STATE / "results", arcname="audit-evidence")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Monthly standards collection and audit for GitHub Actions")
    parser.add_argument("command", choices=("crawl", "database", "audit"))
    parser.add_argument("--campaign", default=os.environ.get("CAMPAIGN", ""))
    parser.add_argument("--budget-seconds", type=int, default=3600)
    parser.add_argument("--delay", type=float, default=5)
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args(argv)
    try:
        args.campaign = campaign(args.campaign)
        if args.budget_seconds <= 0 or args.delay < 0:
            raise ValueError("Time budget must be positive; delay cannot be negative")
        {"crawl": crawl, "database": package_database, "audit": audit}[args.command](args)
        return 0
    except (ValueError, OSError) as exc:
        print("Task failed: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
