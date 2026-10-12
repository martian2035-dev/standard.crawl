#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
standard_crawl.py —— 标准信息采集器（断点续爬 + 进度持久化）

为什么这么设计
--------------
CNB 流水线单次最长执行时间由平台限制（当前环境实测 120 分钟，且**无法通过
配置修改**），而一次全量抓取约需 4~5 小时。因此采集器必须支持：

1. **页面游标持久化**：每个采集任务的进度写入 ``.crawl_state/state.json``，
   每抓到若干页就把 CSV 原子落盘。容器被 SIGKILL 也不会丢掉已抓到的数据。
2. **时间盒分片**：``--budget-seconds`` 到点后主动优雅退出，本片成果已落盘，
   下一片从游标继续。一次「采集行动(campaign)」可跨多次流水线完成。
3. **采集与构建解耦**：本脚本只产出 CSV + 进度状态，不生成 standard_db。

用法
----
    python standard_crawl.py                      # 跑一个分片（默认预算 4500 秒）
    python standard_crawl.py --new-campaign       # 开启新采集行动（游标归零）
    python standard_crawl.py --only industry --budget-seconds 60 --max-pages 5
    python standard_crawl.py --status             # 查看进度
    python standard_crawl.py --emit-next          # 输出下一步动作（配合 exports）

退出码
------
    0   本片正常结束（预算到点 / 全部采集完成）
    1   本片无任何进展，或超过最大分片数 —— 流水线应报错并停止链式调用
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

# 请求头（模拟浏览器，防止被拦截）
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Referer": "https://openstd.samr.gov.cn/",
}

BASE_URL = "https://openstd.samr.gov.cn/bzgk/gb/std_list_type"

# 国家标准分类
STD_TYPES = {
    "1": "强制性国家标准",
    "2": "推荐性国家标准",
    "3": "指导性技术文件",
}

NATIONAL_COLUMNS = [
    "标准类型", "序号", "标准号", "是否采标",
    "标准名称", "状态", "发布日期", "实施日期",
]
# 「代替标准」「主管部门」来自备案平台列表接口本身返回的 reviseStdCodes、chargeDept，不增加请求
SACINFO_EXTRA_COLUMNS = ["代替标准", "主管部门"]
SACINFO_COLUMNS = ["标准号", "标准名称", "行业领域", "状态", "批准日期", "实施日期"] + SACINFO_EXTRA_COLUMNS
SACINFO_COLUMNS_LOCAL = ["标准号", "标准名称", "省市区", "状态", "批准日期", "实施日期"] + SACINFO_EXTRA_COLUMNS
# 工程建设标准：国家工程建设标准化信息网（住房和城乡建设部标准定额研究所）的标准查询接口，
# 收录住建部管理的现行工程建设国标、行标及建工、城建产品标准；国家标准与行业标准备案平台不收录这些标准
CCSN_API = "https://www.ccsn.org.cn/newweb/api/StandardQuery.assx/queryStandard"
CCSN_REFERER = "https://www.ccsn.org.cn/newweb/standardQuery"
CCSN_COLUMNS = ["标准号", "标准名称", "标准类型", "专业领域", "状态", "发布日期", "实施日期",
                "代替标准", "主管部门"]


# --------------------------------------------------------------------------- #
# 采集任务定义
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Task:
    key: str
    label: str
    filename: str
    kind: str               # national | sacinfo | ccsn
    columns: tuple
    p1_type: str = ""       # national：分类编号
    api: str = ""           # sacinfo：接口地址
    place_column: str = ""  # sacinfo：行业领域 / 省市区


TASKS: list[Task] = [
    Task("national:1", "强制性国家标准", "国家公开标准信息.csv", "national",
         tuple(NATIONAL_COLUMNS), p1_type="1"),
    Task("national:2", "推荐性国家标准", "国家公开标准信息.csv", "national",
         tuple(NATIONAL_COLUMNS), p1_type="2"),
    Task("national:3", "指导性技术文件", "国家公开标准信息.csv", "national",
         tuple(NATIONAL_COLUMNS), p1_type="3"),
    Task("industry", "行业标准", "行业公开标准信息.csv", "sacinfo",
         tuple(SACINFO_COLUMNS),
         api="https://hbba.sacinfo.org.cn/stdQueryList", place_column="行业领域"),
    Task("local", "地方标准", "地方公开标准信息.csv", "sacinfo",
         tuple(SACINFO_COLUMNS_LOCAL),
         api="https://dbba.sacinfo.org.cn/stdQueryList", place_column="省市区"),
    Task("engineering", "工程建设标准", "工程建设标准信息.csv", "ccsn",
         tuple(CCSN_COLUMNS), api=CCSN_API),
]
TASK_BY_KEY = {t.key: t for t in TASKS}


# --------------------------------------------------------------------------- #
# 工具
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
    """带指数退避的网络请求封装，抵御偶发 503 / timeout。"""
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
            log("[自动重试] 网络波动或拦截 (%s)，%d 秒后第 %d 次重试..." % (exc, wait, attempt + 1))
            time.sleep(wait)
    raise RuntimeError("unreachable")  # pragma: no cover


# --------------------------------------------------------------------------- #
# 数据集：一个 CSV 对应一个 Dataset，负责去重与原子落盘
# --------------------------------------------------------------------------- #
class Dataset:
    """按「标准号」去重并更新，以原子替换的方式写 CSV。

    原子替换（临时文件 + os.replace）保证磁盘上的 CSV 任何时刻都是完整的，
    不会因为写到一半被杀而留下半个文件。

    同一标准号再次抓到时：内容有变化就更新已有行（标准的状态会随时间从「现行」变为
    「废止」「有更新版」，新增的列也靠这一步回填）；两条记录日期不同时保留日期较新的。
    """

    # 用来比较新旧的日期列：国家标准为「发布日期」，行业 / 地方标准为「批准日期」
    DATE_COLUMNS = ("发布日期", "批准日期")
    # 随抓取时刻变化、不代表标准本身变化的列，不参与「内容是否有变化」的判断
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
            raise ValueError("历史 CSV 无法读取，停止以免覆盖已有数据：%s" % self.path) from exc
        self.rows = df.to_dict("records")
        self.index = {}
        for pos, row in enumerate(self.rows):
            code = str(row.get("标准号", "")).strip()
            if code:
                self.index.setdefault(code, pos)

    def _differs(self, new: dict, old: dict) -> bool:
        return any(str(new.get(c, "") or "") != str(old.get(c, "") or "") for c in self.compare_columns)

    def _is_older(self, new: dict, old: dict) -> bool:
        """新抓到的记录日期早于已有记录（同号的旧版备案）时不覆盖。"""
        if not self.date_column:
            return False
        new_date = str(new.get(self.date_column, "") or "")
        old_date = str(old.get(self.date_column, "") or "")
        return bool(new_date and old_date and new_date < old_date)

    def add(self, row: dict) -> bool:
        """新标准号入库返回 True；已有标准号只在内容变化时更新该行，返回 False。"""
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
# 进度状态
# --------------------------------------------------------------------------- #
def blank_task_state() -> dict:
    return {
        "page": 0,               # 已成功落盘的最后一页（0 = 尚未开始）
        "total_pages": None,
        "finished": False,
        "pages_fetched": 0,      # 累计抓取页数（含跳过页）
        "rows_added": 0,         # 累计新增条数
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
        log("[警告] 进度文件损坏（%s），将重新开始" % exc)
        return blank_state(current_campaign_id())
    st.setdefault("tasks", {})
    for t in TASKS:  # 兼容后续新增任务
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
    """游标归零，开启一次全新的采集行动。

    为什么每月都从第 1 页重扫：列表按发布日期倒序，新增标准会插到最前面，
    只从上次断点往后扫会漏掉新条目。旧数据靠「标准号」去重，不会重复入库；
    已有标准的状态等内容有变化时，全量重扫会把对应行更新为最新值。
    """
    st = blank_state(campaign)
    save_state(state_dir, st)
    return st


# --------------------------------------------------------------------------- #
# 单页抓取
# --------------------------------------------------------------------------- #
def get_page_data_national(p1_type: str, page: int, page_size: int):
    """返回 (行列表 或 None, 总页数, 总条数)。None 表示这一页不可用。"""
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
            # 页面结构变化不能被误判为只有一页，从而发布不完整数据库。
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
        log("[错误] 国家标准 %s 第 %d 页抓取失败：%s"
            % (STD_TYPES.get(p1_type, p1_type), page, exc))
        return None, 0, 0


def get_page_data_sacinfo(task: Task, page: int, page_size: int):
    """返回 (行列表 或 None, 总页数)。None 表示这一页不可用。"""
    payload = {"current": page, "size": page_size, "key": "", "status": ""}
    try:
        resp = fetch_with_retry("POST", task.api, headers=HEADERS, data=payload)
        res = resp.json()
    except Exception as exc:
        log("[错误] %s 第 %d 页抓取失败：%s" % (task.label, page, exc))
        return None, 0

    if (not isinstance(res, dict) or not isinstance(res.get("records"), list)
            or not isinstance(res.get("pages"), int) or res["pages"] < 1
            or not res["records"] or any(not isinstance(r, dict) or not r.get("code") for r in res["records"])):
        log("[错误] %s 响应缺少有效分页或标准记录，停止本页" % task.label)
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
    """'2017-09-27T00:00:00' → '2017-09-27'；空值返回空串。"""
    return str(value or "")[:10]


def get_page_data_ccsn(task: Task, page: int, page_size: int):
    """返回 (行列表 或 None, 总页数)。None 表示这一页不可用。"""
    payload = {"gjz": "", "pageIndex": page, "pageSize": page_size}
    headers = dict(HEADERS, Referer=CCSN_REFERER)
    try:
        resp = fetch_with_retry("POST", task.api, headers=headers, data=payload, timeout=60)
        res = resp.json()
    except Exception as exc:
        log("[错误] %s 第 %d 页抓取失败：%s" % (task.label, page, exc))
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
# 时间预算
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
# 采集主循环
# --------------------------------------------------------------------------- #
def crawl_task(task: Task, st: dict, ds: Dataset, budget: Budget, args) -> str:
    """采集单个任务，直到 完成 / 预算耗尽 / 页数上限。返回结束原因。"""
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
                # 保证「每次分片必须前进」，否则链式调用会永久卡死在同一页
                st["skipped_pages"].append(page)
                log("[跳过] %s 第 %d 页连续 %d 次不可用，记录后继续"
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
            "\r⏳ [%s] 第 %d/%d 页 (%.1f%%) | 本库 %d 条 | 本片 %d 页 | 新增 %d | 剩余 %ds"
            % (task.label, st["page"], total, st["page"] / total * 100,
               len(ds), fetched, task_added, budget.remaining())
        )
        sys.stdout.flush()

        # 定期落盘：容器被强杀最多丢 checkpoint_every 页
        if fetched % args.checkpoint_every == 0:
            ds.save()
            save_state(args.state_dir, args.state)
            log()

        if st["page"] >= total:
            st["finished"] = True
            st["rows_added"] += task_added
            ds.save()
            save_state(args.state_dir, args.state)
            log("\n✅ [%s] 采集完成：%d 页，本库共 %d 条" % (task.label, st["page"], len(ds)))
            return "finished"

        page += 1
        time.sleep(args.delay)


def run_chunk(args) -> dict:
    state_dir: Path = args.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)

    if args.new_campaign:
        campaign = args.campaign or current_campaign_id()
        log("🚩 开启新的采集行动：%s（游标归零，全量重扫，靠标准号去重）" % campaign)
        st = start_new_campaign(state_dir, campaign)
    else:
        st = load_state(state_dir)
        if args.campaign and st.get("campaign") != args.campaign:
            log("🚩 采集行动切换：%s → %s，游标归零" % (st.get("campaign"), args.campaign))
            st = start_new_campaign(state_dir, args.campaign)

    args.state = st
    st["chunks"] = int(st.get("chunks", 0)) + 1
    save_state(state_dir, st)

    before_pages = {t.key: int(st["tasks"][t.key].get("pages_fetched", 0)) for t in TASKS}
    budget = Budget(args.budget_seconds)
    args.budget = budget

    log("=" * 74)
    log("标准信息采集器（断点续爬 + 进度持久化）")
    log("  采集行动 : %s" % st["campaign"])
    log("  分片序号 : 第 %d 片" % st["chunks"])
    log("  时间预算 : %d 秒" % args.budget_seconds)
    log("  开始时间 : %s" % now_iso())
    log("=" * 74)

    datasets: dict[str, Dataset] = {}
    tasks = [TASK_BY_KEY[k] for k in args.only] if args.only else TASKS
    reason = "finished"

    for task in tasks:
        tst = st["tasks"][task.key]
        if tst["finished"]:
            log("⏭️  [%s] 已完成，跳过" % task.label)
            continue
        if budget.expired():
            reason = "budget"
            break

        ds = datasets.get(task.filename)
        if ds is None:
            ds = Dataset(Path(task.filename), task.columns)
            datasets[task.filename] = ds
            log("📦 [%s] 载入历史记录 %d 条" % (task.filename, len(ds)))

        result = crawl_task(task, tst, ds, budget, args)
        if result in ("budget", "page-limit", "upstream-error"):
            reason = result
            break

    # 收尾：所有脏数据集落盘 + 状态落盘
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
    log("本片结束：%s" % reason)
    for task in TASKS:
        tst = st["tasks"][task.key]
        log("  %-18s %s/%s 页  完成=%-5s 累计新增=%d%s"
            % (task.label, tst["page"], tst["total_pages"], tst["finished"],
               tst["rows_added"],
               ("  跳过页=%s" % tst["skipped_pages"]) if tst["skipped_pages"] else ""))
    log("  本片抓取 : %d 页 / 新增 %d 条 / 更新 %d 条"
        % (pages_this_run, rows_this_run, rows_updated_this_run))
    log("  耗时     : %.1f 秒" % status["elapsed_seconds"])
    log("  下一步   : %s" % next_action)
    log("-" * 74)

    return status


# --------------------------------------------------------------------------- #
# 只读命令
# --------------------------------------------------------------------------- #
def print_status(args) -> int:
    st = load_state(args.state_dir)
    status_path = args.state_dir / STATUS_FILE
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}

    log("采集行动 : %s" % st.get("campaign"))
    log("状态更新 : %s" % st.get("updated_at"))
    log("累计分片 : %s" % st.get("chunks"))
    log("")
    log("%-20s %-10s %-10s %-8s %-8s %s"
        % ("任务", "已抓页", "总页数", "已完成", "新增条数", "跳过页"))
    for task in TASKS:
        tst = st["tasks"][task.key]
        log("%-20s %-10s %-10s %-8s %-8s %s"
            % (task.label, tst.get("page"), tst.get("total_pages"), tst.get("finished"),
               tst.get("rows_added"), tst.get("skipped_pages") or "-"))
    log("")
    if status:
        log("上一次分片：reason=%s  耗时=%ss  →  下一步=%s"
            % (status.get("reason"), status.get("elapsed_seconds"), status.get("next_action")))
    return 0


def emit_next(args) -> int:
    """输出 ##[set-output crawl_next=...]，供流水线 exports 使用。"""
    status_path = args.state_dir / STATUS_FILE
    if not status_path.exists():
        action = "crawl"
    else:
        action = json.loads(status_path.read_text(encoding="utf-8")).get("next_action", "crawl")
    print("##[set-output crawl_next=%s]" % action)
    return 0


# --------------------------------------------------------------------------- #
def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="标准信息采集器（断点续爬 + 进度持久化）")
    p.add_argument("--budget-seconds", type=int,
                   default=int(os.environ.get("CRAWL_BUDGET_SECONDS", 4500)),
                   help="本片时间预算（秒），默认 4500（75 分钟），留足提交/推送时间")
    p.add_argument("--checkpoint-every", type=int, default=25,
                   help="每抓多少页把 CSV 落盘一次，默认 25")
    p.add_argument("--page-size", type=int, default=50, help="每页条数，默认 50")
    p.add_argument("--delay", type=float, default=0.4, help="每页之间的间隔秒数，默认 0.4")
    p.add_argument("--page-retries", type=int, default=3,
                   help="单页连续失败多少次后跳过（保证分片有前进），默认 3")
    p.add_argument("--strict-pages", action="store_true",
                   help="单页重试耗尽后暂停并保留游标，不跳过失败页（GitHub 月度采集启用）")
    p.add_argument("--max-pages", type=int, default=0,
                   help="本片最多抓多少页（0=不限制），调试用")
    p.add_argument("--max-chunks", type=int, default=24,
                   help="一次采集行动最多允许的分片数，防止链式调用失控")
    p.add_argument("--only", action="append", choices=[t.key for t in TASKS],
                   help="只采集指定任务，可重复；默认全部")
    p.add_argument("--new-campaign", action="store_true",
                   help="开启新的采集行动（游标归零）")
    p.add_argument("--campaign", default="", help="指定采集行动标识，默认当前年月")
    p.add_argument("--state-dir", type=Path, default=STATE_DIR, help="进度状态目录")
    p.add_argument("--status", action="store_true", help="打印当前进度后退出")
    p.add_argument("--emit-next", action="store_true", help="输出下一步动作后退出")
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
        log("❌ 采集无法继续：本片没有取得任何进展（%s），或已超过最大分片数(%d)。"
            % (status["reason"], args.max_chunks))
        log("   请检查网络与上游站点可用性，然后重新触发 api_trigger_crawl。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
