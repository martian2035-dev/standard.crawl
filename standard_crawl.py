#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect standards in resumable, time-bounded chunks.

Page cursors are persisted in .crawl_state/state.json and CSV files are saved
atomically at checkpoints. This command collects data; build_db.py builds SQLite.
A new monthly campaign rescans from page 1 to discover new and changed records.

Examples:
    python standard_crawl.py --new-campaign
    python standard_crawl.py --only industry --budget-seconds 60 --max-pages 5
    python standard_crawl.py --status
    python standard_crawl.py --emit-next

Exit codes: 0 for a completed chunk; 1 for no progress or the chunk limit.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests
from bs4 import BeautifulSoup

STATE_VERSION = 1
STATE_DIR = Path(".crawl_state")
STATE_FILE = "state.json"
STATUS_FILE = "status.json"

# Browser-compatible request headers.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Referer": "https://openstd.samr.gov.cn/",
}

BASE_URL = "https://openstd.samr.gov.cn/bzgk/gb/std_list_type"

# National standard categories.
STD_TYPES = {
    "1": "强制性国家标准",
    "2": "推荐性国家标准",
    "3": "指导性技术文件",
}

NATIONAL_COLUMNS = [
    "标准类型", "序号", "标准号", "是否采标",
    "标准名称", "状态", "发布日期", "实施日期",
]
# Replacement and authority fields come from reviseStdCodes and chargeDept without extra requests.
SACINFO_EXTRA_COLUMNS = ["代替标准", "主管部门"]
SACINFO_COLUMNS = ["标准号", "标准名称", "行业领域", "状态", "批准日期", "实施日期"] + SACINFO_EXTRA_COLUMNS
SACINFO_COLUMNS_LOCAL = ["标准号", "标准名称", "省市区", "状态", "批准日期", "实施日期"] + SACINFO_EXTRA_COLUMNS
# Engineering standards come from the ministry engineering standardization query API.
# This source includes engineering and construction product standards absent from other registries.
CCSN_API = "https://www.ccsn.org.cn/newweb/api/StandardQuery.assx/queryStandard"
CCSN_REFERER = "https://www.ccsn.org.cn/newweb/standardQuery"
CCSN_COLUMNS = ["标准号", "标准名称", "标准类型", "专业领域", "状态", "发布日期", "实施日期",
                "代替标准", "主管部门"]


# --------------------------------------------------------------------------- #
# Collection task definitions.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Task:
    key: str
    label: str
    filename: str
    kind: str               # national | sacinfo | ccsn
    columns: tuple
    p1_type: str = ""       # National category identifier.
    api: str = ""           # Registry API URL.
    place_column: str = ""  # Registry industry or region field.


TASKS: list[Task] = [
    Task("national:1", "Mandatory national standards", "国家公开标准信息.csv", "national",
         tuple(NATIONAL_COLUMNS), p1_type="1"),
    Task("national:2", "Recommended national standards", "国家公开标准信息.csv", "national",
         tuple(NATIONAL_COLUMNS), p1_type="2"),
    Task("national:3", "National guidance documents", "国家公开标准信息.csv", "national",
         tuple(NATIONAL_COLUMNS), p1_type="3"),
    Task("industry", "Industry standards", "行业公开标准信息.csv", "sacinfo",
         tuple(SACINFO_COLUMNS),
         api="https://hbba.sacinfo.org.cn/stdQueryList", place_column="行业领域"),
    Task("local", "Local standards", "地方公开标准信息.csv", "sacinfo",
         tuple(SACINFO_COLUMNS_LOCAL),
         api="https://dbba.sacinfo.org.cn/stdQueryList", place_column="省市区"),
    Task("engineering", "Engineering standards", "工程建设标准信息.csv", "ccsn",
         tuple(CCSN_COLUMNS), api=CCSN_API),
]
TASK_BY_KEY = {t.key: t for t in TASKS}


# --------------------------------------------------------------------------- #
# Utilities.
# --------------------------------------------------------------------------- #
def log(msg: str = "") -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def now_iso() -> str:
    return _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")


def current_campaign_id() -> str:
    return _dt.datetime.now().strftime("%Y-%m")


def format_date(epoch_millis) -> str:
    if not epoch_millis:
        return ""
    try:
        return _dt.datetime.fromtimestamp(int(epoch_millis) / 1000).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        return ""


def fetch_with_retry(method, url, max_retries=5, **kwargs):
    """Retry transient network failures with exponential backoff."""
    timeout = kwargs.pop("timeout", 15)
    for attempt in range(max_retries):
        try:
            if method.upper() == "GET":
                resp = requests.get(url, timeout=timeout, **kwargs)
            else:
                resp = requests.post(url, timeout=timeout, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as exc:
            if attempt == max_retries - 1:
                raise
            wait = 2 ** attempt
            log("[RETRY] Network error or restriction (%s); waiting %d seconds before retry %d..." % (exc, wait, attempt + 1))
            time.sleep(wait)
    raise RuntimeError("unreachable")  # pragma: no cover


# --------------------------------------------------------------------------- #
# One Dataset per CSV, with deduplication and atomic persistence.
# --------------------------------------------------------------------------- #
class Dataset:
    """Deduplicate and update by standard number, then save CSV atomically. Older dated records never replace newer ones."""

    # Compare publication dates for national records and approval dates for industry/local records.
    DATE_COLUMNS = ("发布日期", "批准日期")
    # Ignore fields that change with collection time rather than with the standard itself.
    VOLATILE_COLUMNS = ("序号",)

    def __init__(self, path: Path, columns: Iterable[str]):
        self.path = Path(path)
        self.columns = list(columns)
        self.rows: list[dict] = []
        self.index: dict[str, int] = {}
        self.dirty = False
        self.added = 0
        self.updated = 0
        self.date_column = next((c for c in self.DATE_COLUMNS if c in self.columns), "")
        self.compare_columns = [c for c in self.columns if c not in self.VOLATILE_COLUMNS]
        self.load()

    @property
    def codes(self) -> set[str]:
        return set(self.index)

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            df = pd.read_csv(self.path, dtype=str, encoding="utf-8-sig", keep_default_na=False)
        except Exception as exc:
            raise ValueError("Cannot read existing CSV; stopped to prevent data loss: %s" % self.path) from exc
        self.rows = df.to_dict("records")
        self.index = {}
        for pos, row in enumerate(self.rows):
            code = str(row.get("标准号", "")).strip()
            if code:
                self.index.setdefault(code, pos)

    def _differs(self, new: dict, old: dict) -> bool:
        return any(str(new.get(c, "") or "") != str(old.get(c, "") or "") for c in self.compare_columns)

    def _is_older(self, new: dict, old: dict) -> bool:
        """Prevent an older registration of the same standard from overwriting newer data."""
        if not self.date_column:
            return False
        new_date = str(new.get(self.date_column, "") or "")
        old_date = str(old.get(self.date_column, "") or "")
        return bool(new_date and old_date and new_date < old_date)

    def add(self, row: dict) -> bool:
        """Return True for a new standard; update changed existing rows and return False."""
        code = str(row.get("标准号", "")).strip()
        if not code:
            return False
        pos = self.index.get(code)
        if pos is None:
            self.index[code] = len(self.rows)
            self.rows.append(row)
            self.dirty = True
            self.added += 1
            return True
        old = self.rows[pos]
        if self._is_older(row, old) or not self._differs(row, old):
            return False
        self.rows[pos] = row
        self.dirty = True
        self.updated += 1
        return False

    def save(self) -> None:
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        pd.DataFrame(self.rows, columns=self.columns).fillna("").to_csv(
            tmp, index=False, encoding="utf-8-sig"
        )
        os.replace(tmp, self.path)
        self.dirty = False

    def __len__(self) -> int:
        return len(self.rows)


# --------------------------------------------------------------------------- #
# Progress state.
# --------------------------------------------------------------------------- #
def blank_task_state() -> dict:
    return {
        "page": 0,               # Last successfully persisted page; 0 means not started.
        "total_pages": None,
        "finished": False,
        "pages_fetched": 0,      # Total pages visited, including skipped pages.
        "rows_added": 0,         # Total newly added records.
        "failures": 0,
        "skipped_pages": [],
        "last_error": "",
        "updated_at": "",
    }


def blank_state(campaign: str) -> dict:
    now = now_iso()
    return {
        "version": STATE_VERSION,
        "campaign": campaign,
        "created_at": now,
        "updated_at": now,
        "chunks": 0,
        "tasks": {t.key: blank_task_state() for t in TASKS},
    }


def load_state(state_dir: Path) -> dict:
    path = state_dir / STATE_FILE
    if not path.exists():
        return blank_state(current_campaign_id())
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log("[WARNING] Corrupt progress file (%s); starting over" % exc)
        return blank_state(current_campaign_id())
    st.setdefault("tasks", {})
    for t in TASKS:  # Support tasks added after the checkpoint was created.
        st["tasks"].setdefault(t.key, blank_task_state())
    st.setdefault("campaign", current_campaign_id())
    st.setdefault("chunks", 0)
    return st


def save_state(state_dir: Path, st: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    st["updated_at"] = now_iso()
    tmp = state_dir / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, state_dir / STATE_FILE)


def save_status(state_dir: Path, status: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / (STATUS_FILE + ".tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, state_dir / STATUS_FILE)


def start_new_campaign(state_dir: Path, campaign: str) -> dict:
    """Reset page cursors for a full rescan that discovers new entries and updates existing statuses."""
    st = blank_state(campaign)
    save_state(state_dir, st)
    return st


# --------------------------------------------------------------------------- #
# Fetch one page.
# --------------------------------------------------------------------------- #
def get_page_data_national(p1_type: str, page: int, page_size: int):
    """Return (rows or None, total pages, total records). None means the page is unavailable."""
    params = {
        "p.p1": p1_type,
        "p.p90": "circulation_date",
        "p.p91": "desc",
        "pageSize": page_size,
        "page": page,
    }
    try:
        response = fetch_with_retry("GET", BASE_URL, headers=HEADERS, params=params)
        soup = BeautifulSoup(response.text, "html.parser")

        total_pages, total_records = 1, 0
        match = re.search(r"共\s*(\d+)\s*条标准\s*\d+\s*/\s*(\d+)", soup.get_text())
        if match:
            total_records = int(match.group(1))
            total_pages = int(match.group(2))

        table = None
        for candidate in soup.find_all("table"):
            headers = [th.get_text(strip=True) for th in candidate.find_all("th")]
            if "标准号" in headers and "标准名称" in headers:
                table = candidate
                break
        if table is None:
            return None, total_pages, total_records
        if not match:
            # Never interpret a page layout change as a single-page result and publish an incomplete database.
            return None, 0, 0

        rows = []
        for tr in table.find_all("tr")[1:]:
            cols = tr.find_all("td")
            if len(cols) < 7:
                continue
            rows.append({
                "标准类型": STD_TYPES[p1_type],
                "序号": cols[0].get_text(strip=True),
                "标准号": cols[1].get_text(strip=True),
                "是否采标": cols[2].get_text(strip=True),
                "标准名称": cols[3].get_text(strip=True),
                "状态": cols[4].get_text(strip=True),
                "发布日期": cols[5].get_text(strip=True),
                "实施日期": cols[6].get_text(strip=True),
            })
        if not rows and total_records:
            return None, total_pages, total_records
        return rows, total_pages, total_records
    except Exception as exc:
        log("[ERROR] National standard type %s page %d failed: %s"
            % (STD_TYPES.get(p1_type, p1_type), page, exc))
        return None, 0, 0


def get_page_data_sacinfo(task: Task, page: int, page_size: int):
    """Return (rows or None, total pages). None means the page is unavailable."""
    payload = {"current": page, "size": page_size, "key": "", "status": ""}
    try:
        resp = fetch_with_retry("POST", task.api, headers=HEADERS, data=payload)
        res = resp.json()
    except Exception as exc:
        log("[ERROR] %s page %d failed: %s" % (task.label, page, exc))
        return None, 0

    if (not isinstance(res, dict) or not isinstance(res.get("records"), list)
            or not isinstance(res.get("pages"), int) or res["pages"] < 1
            or not res["records"] or any(not isinstance(r, dict) or not r.get("code") for r in res["records"])):
        log("[ERROR] %s response has no valid pagination or standards; stopping this page" % task.label)
        return None, 0
    total_pages = res["pages"]
    rows = []
    for item in res.get("records") or []:
        rows.append({
            "标准号": item.get("code", ""),
            "标准名称": item.get("chName", ""),
            task.place_column: item.get("industry", ""),
            "状态": item.get("status", ""),
            "批准日期": format_date(item.get("issueDate")),
            "实施日期": format_date(item.get("actDate")),
            "代替标准": (item.get("reviseStdCodes") or "").strip(),
            "主管部门": (item.get("chargeDept") or "").strip(),
        })
    return rows, total_pages


def iso_date(value) -> str:
    """Extract YYYY-MM-DD from an ISO timestamp; return an empty string for an empty value."""
    return str(value or "")[:10]


def get_page_data_ccsn(task: Task, page: int, page_size: int):
    """Return (rows or None, total pages). None means the page is unavailable."""
    payload = {"gjz": "", "pageIndex": page, "pageSize": page_size}
    headers = dict(HEADERS, Referer=CCSN_REFERER)
    try:
        resp = fetch_with_retry("POST", task.api, headers=headers, data=payload, timeout=60)
        res = resp.json()
    except Exception as exc:
        log("[ERROR] %s page %d failed: %s" % (task.label, page, exc))
        return None, 0

    rows = []
    for item in res.get("List") or []:
        rows.append({
            "标准号": (item.get("StandardCode") or "").strip(),
            "标准名称": (item.get("StandardCNName") or "").strip(),
            "标准类型": (item.get("StandardType") or "").strip(),
            "专业领域": (item.get("AreaOrTrade") or "").strip(),
            "状态": (item.get("StandardState") or "").strip(),
            "发布日期": iso_date(item.get("PublishDate")),
            "实施日期": iso_date(item.get("PerformDate")),
            "代替标准": (item.get("ReplaceStandardCode") or "").strip(),
            "主管部门": (item.get("ApprovalDep") or "").strip(),
        })
    return rows, res.get("Pages") or 1


# --------------------------------------------------------------------------- #
# Time budget.
# --------------------------------------------------------------------------- #
class Budget:
    def __init__(self, seconds: float):
        self.seconds = float(seconds)
        self.start = time.monotonic()
        self.deadline = self.start + self.seconds

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline

    def remaining(self) -> int:
        return max(0, int(self.deadline - time.monotonic()))

    def elapsed(self) -> float:
        return time.monotonic() - self.start


# --------------------------------------------------------------------------- #
# Collection loop.
# --------------------------------------------------------------------------- #
def crawl_task(task: Task, st: dict, ds: Dataset, budget: Budget, args) -> str:
    """Collect one task until completion or a budget/page limit; return the stop reason."""
    page = int(st["page"]) + 1
    fetched = 0
    consecutive_failures = 0
    task_added = 0

    while True:
        if budget.expired():
            st["rows_added"] += task_added
            return "budget"
        if args.max_pages and fetched >= args.max_pages:
            st["rows_added"] += task_added
            return "page-limit"

        if task.kind == "national":
            rows, total_pages, _ = get_page_data_national(task.p1_type, page, args.page_size)
        elif task.kind == "ccsn":
            rows, total_pages = get_page_data_ccsn(task, page, args.page_size)
        else:
            rows, total_pages = get_page_data_sacinfo(task, page, args.page_size)

        if rows is None:
            consecutive_failures += 1
            st["failures"] += 1
            st["last_error"] = "page %d unavailable" % page
            if consecutive_failures >= args.page_retries:
                if getattr(args, "strict_pages", False):
                    st["rows_added"] += task_added
                    ds.save()
                    save_state(args.state_dir, args.state)
                    return "upstream-error"
                # Require progress to avoid an endless continuation loop on one page.
                st["skipped_pages"].append(page)
                log("[SKIP] %s page %d unavailable after %d attempts; recorded and continuing"
                    % (task.label, page, consecutive_failures))
                st["page"] = page
                st["pages_fetched"] += 1
                ds.save()
                save_state(args.state_dir, args.state)
                consecutive_failures = 0
                fetched += 1
                page += 1
                continue
            time.sleep(1)
            continue

        consecutive_failures = 0
        st["total_pages"] = total_pages or st.get("total_pages") or 1

        for row in rows:
            if ds.add(row):
                task_added += 1

        st["page"] = page
        st["pages_fetched"] += 1
        st["updated_at"] = now_iso()
        fetched += 1

        total = st["total_pages"] or 1
        sys.stdout.write(
            "\r⏳ [%s] page %d/%d (%.1f%%) | %d stored | %d pages this chunk | %d added | %ds remaining"
            % (task.label, st["page"], total, st["page"] / total * 100,
               len(ds), fetched, task_added, budget.remaining())
        )
        sys.stdout.flush()

        # Checkpoint periodically; a forced shutdown can lose at most checkpoint_every pages.
        if fetched % args.checkpoint_every == 0:
            ds.save()
            save_state(args.state_dir, args.state)
            log()

        if st["page"] >= total:
            st["finished"] = True
            st["rows_added"] += task_added
            ds.save()
            save_state(args.state_dir, args.state)
            log("\n[%s] Collection complete: %d pages, %d stored records" % (task.label, st["page"], len(ds)))
            return "finished"

        page += 1
        time.sleep(args.delay)


def run_chunk(args) -> dict:
    state_dir: Path = args.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)

    if args.new_campaign:
        campaign = args.campaign or current_campaign_id()
        log("Starting campaign %s (reset cursors, full rescan, deduplicate by standard number)" % campaign)
        st = start_new_campaign(state_dir, campaign)
    else:
        st = load_state(state_dir)
        if args.campaign and st.get("campaign") != args.campaign:
            log("Campaign changed: %s -> %s; cursors reset" % (st.get("campaign"), args.campaign))
            st = start_new_campaign(state_dir, args.campaign)

    args.state = st
    st["chunks"] = int(st.get("chunks", 0)) + 1
    save_state(state_dir, st)

    before_pages = {t.key: int(st["tasks"][t.key].get("pages_fetched", 0)) for t in TASKS}
    budget = Budget(args.budget_seconds)
    args.budget = budget

    log("=" * 74)
    log("Standards collector with persistent checkpoints")
    log("  Campaign: %s" % st["campaign"])
    log("  Chunk: %d" % st["chunks"])
    log("  Time budget: %d seconds" % args.budget_seconds)
    log("  Started at: %s" % now_iso())
    log("=" * 74)

    datasets: dict[str, Dataset] = {}
    tasks = [TASK_BY_KEY[k] for k in args.only] if args.only else TASKS
    reason = "finished"

    for task in tasks:
        tst = st["tasks"][task.key]
        if tst["finished"]:
            log("[%s] Already complete; skipping" % task.label)
            continue
        if budget.expired():
            reason = "budget"
            break

        ds = datasets.get(task.filename)
        if ds is None:
            ds = Dataset(Path(task.filename), task.columns)
            datasets[task.filename] = ds
            log("[%s] Loaded %d existing records" % (task.filename, len(ds)))

        result = crawl_task(task, tst, ds, budget, args)
        if result in ("budget", "page-limit", "upstream-error"):
            reason = result
            break

    # Flush dirty datasets and state at the end of the chunk.
    for ds in datasets.values():
        ds.save()
    save_state(state_dir, st)

    pages_this_run = sum(
        int(st["tasks"][t.key].get("pages_fetched", 0)) - before_pages[t.key] for t in TASKS
    )
    rows_this_run = sum(ds.added for ds in datasets.values())
    rows_updated_this_run = sum(ds.updated for ds in datasets.values())

    remaining = [t.key for t in tasks if not st["tasks"][t.key]["finished"]]
    finished = not remaining
    if reason == "upstream-error":
        next_action = "stop"
    elif finished:
        reason = "finished"
        next_action = "build"
    elif pages_this_run == 0:
        next_action = "stop"
        reason = reason if reason != "finished" else "no-progress"
    elif st["chunks"] >= args.max_chunks:
        next_action = "stop"
    else:
        next_action = "crawl"

    status = {
        "campaign": st["campaign"],
        "chunk": st["chunks"],
        "finished": finished,
        "reason": reason,
        "next_action": next_action,
        "pages_this_run": pages_this_run,
        "rows_added_this_run": rows_this_run,
        "rows_updated_this_run": rows_updated_this_run,
        "elapsed_seconds": round(budget.elapsed(), 1),
        "remaining_tasks": remaining,
        "tasks": st["tasks"],
        "generated_at": now_iso(),
    }
    save_status(state_dir, status)

    log("")
    log("-" * 74)
    log("Chunk finished: %s" % reason)
    for task in TASKS:
        tst = st["tasks"][task.key]
        log("  %-18s %s/%s pages  complete=%-5s total added=%d%s"
            % (task.label, tst["page"], tst["total_pages"], tst["finished"],
               tst["rows_added"],
               ("  skipped pages=%s" % tst["skipped_pages"]) if tst["skipped_pages"] else ""))
    log("  This chunk: %d pages / %d added / %d updated"
        % (pages_this_run, rows_this_run, rows_updated_this_run))
    log("  Elapsed: %.1f seconds" % status["elapsed_seconds"])
    log("  Next step: %s" % next_action)
    log("-" * 74)

    return status


# --------------------------------------------------------------------------- #
# Read-only commands.
# --------------------------------------------------------------------------- #
def print_status(args) -> int:
    st = load_state(args.state_dir)
    status_path = args.state_dir / STATUS_FILE
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}

    log("Campaign: %s" % st.get("campaign"))
    log("State updated: %s" % st.get("updated_at"))
    log("Total chunks: %s" % st.get("chunks"))
    log("")
    log("%-20s %-10s %-10s %-8s %-8s %s"
        % ("Task", "Pages", "Total pages", "Complete", "Added", "Skipped"))
    for task in TASKS:
        tst = st["tasks"][task.key]
        log("%-20s %-10s %-10s %-8s %-8s %s"
            % (task.label, tst.get("page"), tst.get("total_pages"), tst.get("finished"),
               tst.get("rows_added"), tst.get("skipped_pages") or "-"))
    log("")
    if status:
        log("Last chunk: reason=%s  elapsed=%ss  next=%s"
            % (status.get("reason"), status.get("elapsed_seconds"), status.get("next_action")))
    return 0


def emit_next(args) -> int:
    """Emit the crawl_next output for workflow continuation."""
    status_path = args.state_dir / STATUS_FILE
    if not status_path.exists():
        action = "crawl"
    else:
        action = json.loads(status_path.read_text(encoding="utf-8")).get("next_action", "crawl")
    print("##[set-output crawl_next=%s]" % action)
    return 0


# --------------------------------------------------------------------------- #
def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Standards collector with persistent checkpoints")
    p.add_argument("--budget-seconds", type=int,
                   default=int(os.environ.get("CRAWL_BUDGET_SECONDS", 4500)),
                   help="Chunk time budget in seconds; default 4500 (75 minutes), allowing time to commit and push")
    p.add_argument("--checkpoint-every", type=int, default=25,
                   help="Save CSV every N pages; default 25")
    p.add_argument("--page-size", type=int, default=50, help="Records per page; default 50")
    p.add_argument("--delay", type=float, default=0.4, help="Delay between pages in seconds; default 0.4")
    p.add_argument("--page-retries", type=int, default=3,
                   help="Skip a page after N consecutive failures; default 3")
    p.add_argument("--strict-pages", action="store_true",
                   help="Pause with cursor preserved after page retries are exhausted (enabled for monthly GitHub runs)")
    p.add_argument("--max-pages", type=int, default=0,
                   help="Maximum pages this chunk for debugging; 0 means unlimited")
    p.add_argument("--max-chunks", type=int, default=24,
                   help="Maximum chunks per campaign to bound automatic continuation")
    p.add_argument("--only", action="append", choices=[t.key for t in TASKS],
                   help="Collect only this task; may be repeated; defaults to all")
    p.add_argument("--new-campaign", action="store_true",
                   help="Start a new campaign and reset page cursors")
    p.add_argument("--campaign", default="", help="Campaign identifier; defaults to the current year and month")
    p.add_argument("--state-dir", type=Path, default=STATE_DIR, help="Progress state directory")
    p.add_argument("--status", action="store_true", help="Print progress and exit")
    p.add_argument("--emit-next", action="store_true", help="Print the next action and exit")
    args = p.parse_args(argv)
    args.state = {}
    args.budget = Budget(args.budget_seconds)
    return args


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.status:
        return print_status(args)
    if args.emit_next:
        return emit_next(args)

    status = run_chunk(args)

    if status["next_action"] == "stop":
        log("")
        log("Crawl cannot continue: no progress this chunk (%s) or chunk limit (%d) exceeded."
            % (status["reason"], args.max_chunks))
        log("   Check network and upstream availability, then resume the current campaign.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
