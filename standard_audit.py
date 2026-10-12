#!/usr/bin/env python3
"""按统计表逐项核验标准，结果、逐字段来源和网页证据仅存放在本地。"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

VERSION = 3
ROOT = Path(__file__).resolve().parent
TZ = dt.timezone(dt.timedelta(hours=8))
SOURCES = {
    "baseline": ("本地历史标准数据库", "", 40),
    "samr": ("全国标准信息公共服务平台", "https://std.samr.gov.cn", 100),
    "national": ("国家标准全文公开系统", "https://openstd.samr.gov.cn", 100),
    "industry": ("官方行业标准备案平台", "https://hbba.sacinfo.org.cn", 100),
    "local": ("官方地方标准备案平台", "https://dbba.sacinfo.org.cn", 100),
    "csres": ("工标网", "http://www.csres.com", 60),
}
ALLOWED_HOSTS = {urlparse(v[1]).hostname for v in SOURCES.values() if v[1]} | {"csres.com"}
CODE_RE = re.compile(r"(?<![A-Z0-9])(?:[A-Z]{2,8}\d{0,4}(?:\s*/\s*[TZ])?|T\s*/\s*[A-Z0-9]+)\s*\d+(?:\.\d+)*\s*[-—–－]\s*\d{4}(?!\d)", re.I)
FIELDS = ["标准类型", "标准名称", "状态", "发布日期", "实施日期", "废止日期", "是否采标", "行业", "行业分类", "地区",
          "中国标准分类号", "国际标准分类号", "主管部门", "归口部门", "归口单位", "发布单位"]
ALIASES = {
    "中文标准名称": "标准名称", "标准状态": "状态", "标准编号": "标准号",
    "批准日期": "发布日期", "作废日期": "废止日期",
    "中国标准分类号（CCS）": "中国标准分类号", "国际标准分类号（ICS）": "国际标准分类号",
    "批准发布部门": "发布单位", "技术归口": "归口部门",
}


def now():
    return dt.datetime.now(TZ).isoformat(timespec="seconds")


def normalize(code):
    value = unicodedata.normalize("NFKC", str(code or "")).upper()
    value = re.sub(r"[—–−－]", "-", value)
    value = re.sub(r"\s+", "", value)
    # 统计表常把 GB/T、SY/T 等推荐标准前缀写成 GBT、SYT。
    # 仅补全明确的固定前缀；不改年份、编号、分册号，也不猜 Q/、GBZ 等前缀。
    value = re.sub(r"^(GBT|SYT|SHT|HGT|JBT|NBT|DLT|JGJT|CJJT|YBT|YST|SJT|HJT)(?=\d)",
                   lambda m: m.group(1)[:-1] + "/T", value)
    return value


def codes_in(text):
    return list(dict.fromkeys(normalize(m.group()) for m in CODE_RE.finditer(text)))


def display_code(code):
    # 行业接口检索对前缀后的空格敏感；存储和匹配仍用无空格的规范化键。
    return re.sub(r"^([A-Z]+(?:/[TZ])?)(?=\d)", r"\1 ", code) if not code.startswith("DB") else code


def comparable(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value)))


def date_value(value):
    if isinstance(value, (int, float)) and value > 10000000000:
        return dt.datetime.fromtimestamp(value / 1000, TZ).date().isoformat()
    m = re.search(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", str(value or ""))
    if m:
        try:
            return dt.date(*map(int, m.groups())).isoformat()
        except ValueError:
            pass
    return ""


def status_value(value):
    text = str(value or "").strip()
    for status in ("即将实施", "废止", "现行", "暂行"):
        if text == status:
            return status
    return {"作废": "废止", "已废止": "废止", "已被代替": "废止", "被代替": "废止", "有效": "现行"}.get(text, "")


@dataclass
class Evidence:
    source: str
    url: str
    fetched_at: str
    snapshot: str
    sha256: str
    request: dict = field(default_factory=dict)


@dataclass
class Observation:
    code: str
    values: dict
    replacements: list[str]
    relation_text: list[str]
    evidence: Evidence


def label_pairs(soup):
    """只读取标签和值的结构，不从导航或整页关键词猜测字段。"""
    pairs = []
    for label in soup.select("dt, div.title"):
        value = label.find_next_sibling()
        if value:
            pairs.append((label.get_text(" ", strip=True), value.get_text(" ", strip=True)))
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"], recursive=False)
        if len(cells) == 2:
            pairs.append((cells[0].get_text(" ", strip=True), cells[1].get_text(" ", strip=True)))
    # 工标网和公开网的“标签：值”通常位于一个单独的 td 内。
    for tag in soup.find_all(["td", "li", "h1", "p"]):
        text = tag.get_text(" ", strip=True)
        match = re.match(r"^([^：:]{2,24})[：:]\s*(.+)$", text)
        if match:
            pairs.append(match.groups())
    return [(k.strip().rstrip("：:"), v.strip()) for k, v in pairs]


def parse_detail(html, requested, evidence):
    soup = BeautifulSoup(html, "html.parser")
    for popup in soup.select(".icsInfoPop, script, style"):
        popup.decompose()
    pairs = label_pairs(soup)
    identity = [v for k, v in pairs if k in ("标准号", "标准编号")]
    # 必须由详情字段精确确认身份，不能仅因页面提到了目标标准就采信。
    if not any(normalize(v) == requested or codes_in(v) == [requested] for v in identity):
        raise ValueError("详情页标准号不匹配或缺少标准号字段")
    values = {}
    replacements, relations = [], []
    for label, value in pairs:
        key = ALIASES.get(label, label)
        if key in FIELDS:
            if key.endswith("日期"):
                value = date_value(value)
            elif key == "状态":
                value = status_value(value)
            if value and value not in ("无", "暂无", "-", "—"):
                values[key] = value
        # 代替标准、替代情况中的“代替 XX”是前代，严禁反向认作新版本。
        if label in ("被代替标准", "被替代标准", "被代替", "被替代", "被以下标准替代"):
            replacements.extend(codes_in(value))
            relations.append(label + "：" + value)
        elif label in ("替代情况", "代替情况", "标准替代情况"):
            for match in re.finditer(r"被\s*(.*?)\s*(?:代替|替代)", value):
                replacements.extend(codes_in(match.group(1)))
                relations.append(match.group())
    # SAC 详情页的状态在 page-header 内；时间轴同时含“实施/废止”，不能读取时间轴。
    header = soup.select_one(".page-header")
    if header and "状态" not in values:
        for tag in header.select(".s-status"):
            status = status_value(tag.get_text(strip=True))
            if status:
                values["状态"] = status
    heading = soup.select_one(".page-header h4")
    if heading and "标准名称" not in values:
        values["标准名称"] = heading.get_text(strip=True)
    # 全国标准信息公共服务平台明确区分“代替以下标准”和“被以下标准替代”。
    for label in soup.select(".panel-heading"):
        if label.get_text(strip=True) not in ("被以下标准替代", "即将被以下标准替代"):
            continue
        body = label.find_next_sibling(class_="panel-body")
        if body:
            text = body.get_text(" ", strip=True)
            replacements.extend(codes_in(text))
            relations.append(label.get_text(strip=True) + "：" + text)
    return Observation(requested, values, list(dict.fromkeys(replacements)), relations, evidence)


def national_matches(html, requested, base):
    soup = BeautifulSoup(html, "html.parser")
    matches = []
    tables = [t for t in soup.find_all("table") if "标准号" in [x.get_text(strip=True) for x in t.find_all("th")]]
    if not tables:
        raise ValueError("国家标准检索页结构异常，可能是验证码或访问限制")
    for table in tables:
        headers = [x.get_text(strip=True) for x in table.find_all("th")]
        for tr in table.find_all("tr"):
            cells = tr.find_all("td", recursive=False)
            if len(cells) != len(headers):
                continue
            row = dict(zip(headers, [c.get_text(" ", strip=True) for c in cells]))
            if normalize(row.get("标准号")) != requested:
                continue
            if row.get("语种") not in (None, "", "CN", "中文"):
                continue
            hcno = re.search(r"showInfo\(['\"]([A-Fa-f0-9]+)['\"]\)", str(tr))
            if hcno:
                url = urljoin(base, "newGbInfo?" + urlencode({"hcno": hcno.group(1)}))
                matches.append((row, url))
    return matches


class LocalStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.directory / "核验数据.sqlite3")
        self.conn.execute("CREATE TABLE IF NOT EXISTS lookups (key TEXT PRIMARY KEY, updated TEXT, data TEXT)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS results (input_hash TEXT, sheet_row INTEGER, data TEXT, PRIMARY KEY(input_hash,sheet_row))")
        self.conn.commit()

    def cached(self, key, days, retry_failed=False):
        row = self.conn.execute("SELECT updated,data FROM lookups WHERE key=?", (key,)).fetchone()
        if not row or (dt.datetime.now(TZ) - dt.datetime.fromisoformat(row[0])).total_seconds() > days * 86400:
            return None
        data = json.loads(row[1])
        if retry_failed and (data["errors"] or data["conflicts"] or not data["values"].get("状态") or
                             (data["values"].get("状态") == "废止" and not data.get("replacements"))):
            return None
        return data

    def save_lookup(self, key, data):
        self.conn.execute("INSERT OR REPLACE INTO lookups VALUES(?,?,?)", (key, now(), json.dumps(data, ensure_ascii=False)))
        self.conn.commit()

    def save_row(self, input_hash, row, data):
        self.conn.execute("INSERT OR REPLACE INTO results VALUES(?,?,?)", (input_hash, row, json.dumps(data, ensure_ascii=False)))
        self.conn.commit()

    def snapshot(self, source, url, content, request):
        sha = hashlib.sha256(content).hexdigest()
        relative = Path("网页证据") / source / (sha + ".txt")
        path = self.directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(content)
        return Evidence(source, url, now(), str(relative), sha, request)


class RequestBudgetExceeded(RuntimeError):
    """请求预算到点，交由分片调用方保存已有成果。"""


class Fetcher:
    def __init__(self, store, delay=1, timeout=15, retries=2, budget_seconds=0, max_requests=0):
        self.store, self.delay, self.timeout, self.retries = store, delay, timeout, retries
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "Mozilla/5.0 (compatible; StandardAudit/1.0)"
        self.last_request = 0.0
        self.failures = {}
        self.blocked = set()
        self.started = time.monotonic()
        self.budget_seconds, self.max_requests, self.request_count = budget_seconds, max_requests, 0
        self.limits_path = self.store.directory / "站点暂停状态.json"
        self.limits = json.loads(self.limits_path.read_text()) if self.limits_path.exists() else {}
        # 兼容首版成果包：把已出现的 403/429 也带入后续分片的暂停状态。
        if not self.limits:
            for raw, in self.store.conn.execute("SELECT data FROM results"):
                record = json.loads(raw)
                errors = record.get("errors", []) + [e for c in record.get("replacement_chain", []) for e in c.get("采集错误", [])]
                for error in errors:
                    if re.search(r"HTTP (403|429)", error["message"]):
                        host = urlparse(SOURCES[error["source"]][1]).hostname
                        until = dt.datetime.fromisoformat(record["checked_at"]).timestamp() + 86400
                        self.limits[host] = max(self.limits.get(host, 0), until)
        self.blocked.update(h for h, until in self.limits.items() if until > time.time())
        self.save_limits()

    def save_limits(self):
        temporary = self.limits_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.limits, ensure_ascii=False, indent=2))
        temporary.replace(self.limits_path)

    def pause(self, host, seconds=86400):
        self.blocked.add(host)
        self.limits[host] = time.time() + seconds
        self.save_limits()

    def check_budget(self):
        if self.budget_seconds and time.monotonic() - self.started >= self.budget_seconds:
            raise RequestBudgetExceeded("本片时间预算已用完")
        if self.max_requests and self.request_count >= self.max_requests:
            raise RequestBudgetExceeded("本片请求数量预算已用完")

    def request(self, method, url, **kwargs):
        self.check_budget()
        time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
        self.check_budget()
        self.last_request = time.monotonic()
        self.request_count += 1
        return self.session.request(method, url, timeout=self.timeout, allow_redirects=False, **kwargs)

    def fetch(self, source, url, method="GET", **kwargs):
        host = urlparse(url).hostname
        if host not in ALLOWED_HOSTS:
            raise ValueError("来源地址不在允许的公开标准站点内")
        if host in self.blocked or self.failures.get(host, 0) >= 3:
            raise ValueError("该站点已暂停，等待冷却或使用其它来源")
        for attempt in range(self.retries):
            try:
                # 手动处理跳转，表格提供的 URL 不能把请求引向无关站点。
                resp = self.request(method, url, **kwargs)
                for _ in range(4):
                    if not resp.is_redirect:
                        break
                    target = urljoin(resp.url, resp.headers["Location"])
                    if urlparse(target).hostname not in ALLOWED_HOSTS:
                        raise ValueError("来源跳转到未允许的站点")
                    resp = self.request("GET", target)
                if resp.status_code in (403, 429):
                    self.pause(host)
                    self.store.snapshot(source, resp.url, resp.content, {"method": method, **kwargs})
                    raise ValueError(f"HTTP {resp.status_code}：该站点本次暂停请求，保留限制响应证据")
                resp.raise_for_status()
                if resp.is_redirect:
                    raise ValueError("来源重定向次数过多")
                ev = self.store.snapshot(source, resp.url, resp.content, {"method": method, **kwargs})
                encoding = resp.encoding
                if not encoding or encoding.lower() == "iso-8859-1":
                    encoding = resp.apparent_encoding or "utf-8"
                self.failures[host] = 0
                return resp.content.decode(encoding, errors="replace"), ev
            except requests.RequestException as exc:
                if attempt + 1 == self.retries:
                    self.failures[host] = self.failures.get(host, 0) + 1
                    if self.failures[host] >= 3:
                        self.pause(host, 3600)
                    raise ValueError("请求失败：" + type(exc).__name__) from exc
                time.sleep(2 ** attempt)
        raise AssertionError("请求重试次数必须大于零")


def merge_observations(observations, errors):
    values, provenance, conflicts = {}, {}, []
    observations = sorted(observations, key=lambda o: -SOURCES[o.evidence.source][2])
    for key in FIELDS:
        candidates = [o for o in observations if o.values.get(key)]
        if not candidates:
            continue
        chosen = candidates[0]
        values[key] = chosen.values[key]
        provenance[key] = [asdict(o.evidence) | {"value": o.values[key], "selected": comparable(o.values[key]) == comparable(chosen.values[key])} for o in candidates]
        if len({comparable(o.values[key]) for o in candidates}) > 1:
            conflicts.append(key + "：" + "；".join(SOURCES[o.evidence.source][0] + "=" + o.values[key] for o in candidates))
            top = [o for o in candidates if SOURCES[o.evidence.source][2] == SOURCES[chosen.evidence.source][2]]
            if len({comparable(o.values[key]) for o in top}) > 1:
                values.pop(key)  # 同等权威来源冲突时留空。
                for item in provenance[key]:
                    item["selected"] = False
    relations = [o for o in observations if o.replacements]
    replacements = relations[0].replacements if relations else []
    if relations:
        provenance["替代关系"] = [asdict(o.evidence) | {"value": o.replacements, "原文": o.relation_text} for o in relations]
        if len({tuple(sorted(o.replacements)) for o in relations}) > 1:
            conflicts.append("替代关系：多个来源给出的替代标准不同，需人工复核")
            replacements = []
    return {"values": values, "provenance": provenance, "replacements": replacements,
            "observations": [asdict(o) for o in observations], "conflicts": conflicts,
            "errors": errors, "checked_at": now()}


class Auditor:
    def __init__(self, store, fetcher, sources, refresh=False, cache_days=7, retry_failed=False, max_depth=10, source_policy="all"):
        self.store, self.fetcher, self.sources = store, fetcher, sources
        self.refresh, self.cache_days, self.retry_failed, self.max_depth = refresh, cache_days, retry_failed, max_depth
        self.memory = {}
        self.source_policy = source_policy

    def lookup(self, code, seed=""):
        code = normalize(code)
        key = json.dumps([VERSION, code, seed, self.sources, self.source_policy], ensure_ascii=False)
        if key in self.memory:
            return self.memory[key]
        cached = None if self.refresh else self.store.cached(key, self.cache_days, self.retry_failed)
        if cached:
            self.memory[key] = cached
            return cached
        observations, errors = [], []
        self.query_errors = []
        for source in self.sources:
            if self.source_policy == "fallback" and observations:
                merged = merge_observations(observations, [])
                enough = all(merged["values"].get(k) for k in ("标准名称", "状态", "发布日期", "实施日期"))
                relation_needed = merged["values"].get("状态") == "废止" and not merged["replacements"]
                if enough and not relation_needed and not merged["conflicts"]:
                    break
            if source == "national" and not re.match(r"^GB(?:/T|/Z)?\d", code):
                continue
            if source == "local" and not re.match(r"^DB\d", code):
                continue
            if source == "industry" and (re.match(r"^DB\d", code) or code.startswith(("GB", "Q/", "T/", "TSG", "JJ"))):
                continue
            try:
                observations.extend(self.query(source, code, seed))
            except (ValueError, KeyError, TypeError) as exc:
                if "验证码" in str(exc) or "响应结构异常" in str(exc):
                    self.fetcher.pause(urlparse(SOURCES[source][1]).hostname, 21600)
                errors.append({"source": source, "message": str(exc)})
        errors.extend(self.query_errors)
        result = merge_observations(observations, errors)
        self.store.save_lookup(key, result)
        self.memory[key] = result
        return result

    def query(self, source, code, seed):
        base = SOURCES[source][1]
        if source == "samr":
            if code.startswith(("Q/", "T/", "TSG", "JJ")):
                return []
            tid = 2 if re.match(r"^GB(?:/T|/Z)?\d", code) else (4 if re.match(r"^DB\d", code) else 3)
            html, ev = self.fetcher.fetch(source, base + "/gb/search/gbAdvancedSearchPage",
                                         params={"tid": tid, "typeRadio": {2: "std-param", 3: "hb-param", 4: "db-param"}[tid],
                                                 "std_p4": display_code(code), "pageNumber": 1, "pageSize": 100})
            try:
                payload = json.loads(html)
            except ValueError as exc:
                raise ValueError("检索响应结构异常，可能是验证码或访问限制") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
                raise ValueError("公共服务平台检索响应结构异常")
            items = [r for r in payload["rows"] if normalize(BeautifulSoup(r.get("C_STD_CODE", ""), "html.parser").get_text()) == code]
            if not items:
                raise ValueError("未找到精确匹配；不代表标准废止")
            observations = []
            for item in items:
                values = {"标准名称": item.get("C_C_NAME", ""), "状态": status_value(item.get("STATE")),
                          "发布日期": date_value(item.get("ISSUE_DATE")), "实施日期": date_value(item.get("ACT_DATE")),
                          "废止日期": date_value(item.get("ANNUL_DATE") or item.get("FZ_DATE"))}
                observations.append(Observation(code, {k: v for k, v in values.items() if v}, [], [], ev))
                route = {2: "/gb/search/gbDetailed", 3: "/hb/search/stdHBDetailed", 4: "/db/search/stdDBDetailed"}[tid]
                if item.get("id"):
                    try:
                        detail, detail_ev = self.fetcher.fetch(source, base + route, params={"id": item["id"]})
                        observations.insert(0, parse_detail(detail, code, detail_ev))
                    except ValueError as exc:
                        self.query_errors.append({"source": source, "message": "详情读取失败：" + str(exc)})
            return observations
        if source == "national":
            url = base + "/bzgk/std/std_list"
            html, ev = self.fetcher.fetch(source, url, params={"p.p2": display_code(code), "pageSize": 100, "page": 1})
            matches = national_matches(html, code, ev.url)
            if not matches:
                raise ValueError("未找到精确匹配；不代表标准废止")
            observations = []
            for row, detail_url in matches:
                values = {k: date_value(v) if k.endswith("日期") else v for k, v in row.items() if k in FIELDS and v}
                values["状态"] = status_value(row.get("状态"))
                values["标准类型"] = {"强标": "强制性国家标准", "推标": "推荐性国家标准", "指导": "指导性技术文件"}.get(row.get("类别"), row.get("类别", ""))
                observations.append(Observation(code, values, [], [], ev))
                try:
                    html, detail_ev = self.fetcher.fetch(source, detail_url)
                    observations.insert(0, parse_detail(html, code, detail_ev))
                except ValueError as exc:
                    # 已匹配的官方列表仍是有效证据，详情失败记录为字段空缺。
                    self.query_errors.append({"source": source, "message": "详情读取失败：" + str(exc)})
            return observations
        if source in ("industry", "local"):
            html, ev = self.fetcher.fetch(source, base + "/stdQueryList", method="POST",
                                         data={"current": 1, "size": 100, "key": display_code(code), "status": ""})
            try:
                payload = json.loads(html)
            except ValueError as exc:
                raise ValueError("官方检索响应结构异常，可能是验证码或访问限制") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
                raise ValueError("官方检索接口响应结构异常")
            records = [item for item in payload["records"] if normalize(item.get("code")) == code]
            if not records:
                raise ValueError("未找到精确匹配；不代表标准废止")
            result = []
            for item in records:
                values = {"标准名称": item.get("chName", ""), "状态": status_value(item.get("status")),
                          "发布日期": date_value(item.get("issueDate")), "实施日期": date_value(item.get("actDate")),
                          "行业" if source == "industry" else "地区": item.get("industry", ""), "主管部门": item.get("chargeDept", ""),
                          "标准类型": "行业标准" if source == "industry" else "地方标准"}
                result.append(Observation(code, {k: v for k, v in values.items() if v}, [], [], ev))
                if item.get("pk") and re.fullmatch(r"[a-zA-Z0-9]+", str(item["pk"])):
                    try:
                        detail, detail_ev = self.fetcher.fetch(source, base + "/stdDetail/" + item["pk"])
                        result.insert(0, parse_detail(detail, code, detail_ev))
                    except ValueError as exc:
                        self.query_errors.append({"source": source, "message": "详情读取失败：" + str(exc)})
            return result
        links = []
        if seed and urlparse(seed).hostname in {"www.csres.com", "csres.com"} and re.fullmatch(r"/detail/\d+\.html", urlparse(seed).path):
            links.append(seed)
        failures = []
        if links:
            try:
                html, ev = self.fetcher.fetch(source, links[0])
                return [parse_detail(html, code, ev)]
            except ValueError as exc:
                failures.append(str(exc))
        html, search_ev = self.fetcher.fetch(source, base + "/s.jsp", params={"keyword": code})
        soup = BeautifulSoup(html, "html.parser")
        # 限于精确标准号所在行，随后详情页再次核对标准号。
        for tr in soup.find_all("tr"):
            if not any(normalize(td.get_text(strip=True)) == code for td in tr.find_all("td")):
                continue
            for a in tr.find_all("a", href=True):
                target = urljoin(search_ev.url, a["href"])
                if urlparse(target).hostname in {"www.csres.com", "csres.com"} and re.fullmatch(r"/detail/\d+\.html", urlparse(target).path):
                    links.append(target)
        for link in dict.fromkeys(links):
            try:
                html, ev = self.fetcher.fetch(source, link)
                return [parse_detail(html, code, ev)]
            except ValueError as exc:
                failures.append(str(exc))
        raise ValueError("未找到可验证的工标网详情；" + "；".join(failures))

    def audit(self, code, seed=""):
        result = dict(self.lookup(code, seed))
        leaves, chain, issues = {}, [], []

        def visit(current, path):
            if current in path:
                issues.append("替代关系形成循环：" + " → ".join(path + [current]))
                return
            if len(path) >= self.max_depth:
                issues.append("替代链超过深度上限")
                return
            data = result if not path else self.lookup(current)
            chain.append({"标准号": current, "状态": data["values"].get("状态", ""),
                          "替代标准": data["replacements"], "字段来源": data["provenance"],
                          "冲突": data["conflicts"], "采集错误": data["errors"]})
            if data["conflicts"]:
                issues.append(current + " 的来源存在冲突")
            if data["values"].get("状态") == "现行":
                leaves[current] = data
            elif data["replacements"]:
                for replacement in data["replacements"]:
                    visit(replacement, path + [current])
            else:
                issues.append(current + " 未确认现行替代版本（可能无替代、即将实施或查询失败）")
            if any("部分" in t for obs in data["observations"] for t in obs["relation_text"]):
                issues.append(current + " 存在部分替代关系，需核对适用范围")

        if result["values"].get("状态") == "废止":
            visit(normalize(code), [])
        result["current_versions"] = [{"标准号": c, "发布日期": d["values"].get("发布日期", ""),
                                       "实施日期": d["values"].get("实施日期", ""), "字段来源": d["provenance"]}
                                      for c, d in leaves.items()]
        result["replacement_chain"] = chain
        result["review_notes"] = issues
        return result


def read_input(path, sheet=None):
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            headers, rows = reader.fieldnames, list(reader)
    elif path.suffix.lower() == ".xlsx":
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise ValueError("请先安装本地依赖：uv pip install -r requirements-local.txt") from exc
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            name = sheet or next((s.title for s in workbook if "标准号" in next(s.values, ())), None)
            if not name or name not in workbook.sheetnames:
                raise ValueError("未找到含标准号表头的工作表，请用 --sheet 指定")
            iterator = workbook[name].iter_rows(values_only=True)
            headers = [str(v or "").strip() for v in next(iterator)]
            rows = [dict(zip(headers, [v.isoformat() if isinstance(v, (dt.datetime, dt.date)) else v for v in row])) for row in iterator]
        finally:
            workbook.close()
    else:
        raise ValueError("输入仅支持 .xlsx 或 UTF-8 CSV")
    if not headers or "标准号" not in headers or len(set(headers)) != len(headers):
        raise ValueError("第一行必须包含标准号，且表头不可重复或为空")
    if any(not h for h in headers):
        raise ValueError("表头不可为空")
    return headers, [{k: "" if v is None else v for k, v in row.items()} for row in rows]


def export_csv(store, headers, rows, input_hash):
    records = {n: json.loads(data) for n, data in store.conn.execute("SELECT sheet_row,data FROM results WHERE input_hash=?", (input_hash,))}
    extra = ["原表行号"] + ["核验" + k for k in FIELDS] + ["核验现行版本", "现行版本发布日期", "现行版本实施日期",
             "核验数据来源（网页）", "字段来源", "现行版本字段来源", "替代关系链", "核验时间", "核验结论", "核验说明"]
    if set(headers) & set(extra):
        raise ValueError("输入表包含核验输出专用列，请使用原始统计表")
    target = store.directory / "标准信息核验结果.csv"
    tmp = target.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers + extra)
        writer.writeheader()
        for number, original in enumerate(rows, 2):
            data = records.get(number, {})
            values = data.get("values", {})
            output = original | {"原表行号": number} | {"核验" + k: values.get(k, "") for k in FIELDS}
            versions = data.get("current_versions", [])
            output["核验现行版本"] = "；".join(v["标准号"] for v in versions)
            for field_name in ("发布日期", "实施日期"):
                output["现行版本" + field_name] = "；".join(v["标准号"] + "：" + (v[field_name] or "未查到") for v in versions)
            evidence = [obs["evidence"] for obs in data.get("observations", [])]
            output["核验数据来源（网页）"] = "\n".join(dict.fromkeys(e["url"] for e in evidence))
            output["字段来源"] = json.dumps(data.get("provenance", {}), ensure_ascii=False) if data else ""
            output["现行版本字段来源"] = json.dumps(versions, ensure_ascii=False) if versions else ""
            output["替代关系链"] = json.dumps(data.get("replacement_chain", []), ensure_ascii=False) if data.get("replacement_chain") else ""
            output["核验时间"] = data.get("checked_at", "")
            if not data:
                conclusion = "未采集"
            elif data.get("skipped"):
                conclusion = "未覆盖"
            elif data.get("baseline_only"):
                conclusion = "本地历史数据匹配（未联网）" if values else "本地未匹配（待联网）"
            elif not values.get("状态"):
                conclusion = "待核查"
            elif data.get("conflicts") or data.get("review_notes"):
                conclusion = "需人工复核"
            else:
                status_sources = data.get("provenance", {}).get("状态", [])
                conclusion = "官方来源已核验" if any(SOURCES[e["source"]][2] == 100 and e["selected"] for e in status_sources) else "仅第三方来源"
            output["核验结论"] = conclusion
            notes = data.get("conflicts", []) + data.get("review_notes", []) + [SOURCES[e["source"]][0] + "：" + e["message"] for e in data.get("errors", [])]
            output["核验说明"] = "；".join(dict.fromkeys(notes))
            # CSV 用 Excel 打开时，外部文本不得被当作公式执行。
            writer.writerow({k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v for k, v in output.items()})
    tmp.replace(target)
    json_target = store.directory / "核验记录.jsonl"
    json_tmp = json_target.with_suffix(".jsonl.tmp")
    with json_tmp.open("w", encoding="utf-8") as f:
        for number, data in sorted(records.items()):
            f.write(json.dumps({"原表行号": number, **data}, ensure_ascii=False) + "\n")
    json_tmp.replace(json_target)
    return target, records


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="根据标准统计表核验状态、替代版本与日期，只保存本地数据")
    parser.add_argument("input", type=Path, help="原始 .xlsx 或 CSV")
    parser.add_argument("--sheet", help="Excel 工作表名（默认自动查找）")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "local_audit", help="本地结果目录")
    parser.add_argument("--sources", default="samr,national,industry,local,csres", help="逗号分隔的来源")
    parser.add_argument("--source-policy", choices=("all", "fallback"), default="all", help="全部来源交叉核对或官方优先按需补缺")
    parser.add_argument("--standard", action="append", default=[], help="只处理指定标准号，可重复")
    parser.add_argument("--limit", type=int, default=0, help="本次最多处理多少行，0 表示全部")
    parser.add_argument("--delay", type=float, default=1, help="请求间隔秒数")
    parser.add_argument("--timeout", type=float, default=15, help="单次请求超时秒数")
    parser.add_argument("--retries", type=int, default=2, help="请求尝试次数")
    parser.add_argument("--cache-days", type=float, default=7, help="查询缓存有效天数")
    parser.add_argument("--refresh", action="store_true", help="忽略历史查询缓存重新采集")
    parser.add_argument("--retry-failed", action="store_true", help="重新查询失败、冲突或未匹配记录")
    parser.add_argument("--dry-run", action="store_true", help="检查输入和数量，不发起请求")
    parser.add_argument("--export-only", action="store_true", help="只导出本地已有记录，不发起请求")
    args = parser.parse_args(argv)
    args.sources = list(dict.fromkeys(x.strip() for x in args.sources.split(",") if x.strip()))
    if not args.sources or any(x not in SOURCES or x == "baseline" for x in args.sources):
        parser.error("来源只能为 samr,national,industry,local,csres")
    if args.limit < 0 or args.delay < 0 or args.timeout <= 0 or args.retries < 1 or args.cache_days < 0:
        parser.error("次数/时长参数不合法")
    if args.output_dir.resolve() == ROOT or (ROOT / "standard_db") in args.output_dir.resolve().parents or args.output_dir.resolve() in (ROOT / "standard_db", ROOT / ".crawl_state") or (ROOT / ".crawl_state") in args.output_dir.resolve().parents:
        parser.error("本地结果目录不能使用仓库根目录、standard_db 或 .crawl_state")
    return args


def main(argv=None):
    args = parse_args(argv)
    store = None
    try:
        headers, rows = read_input(args.input, args.sheet)
        selected = {normalize(c) for c in args.standard}
        targets = [(i, r) for i, r in enumerate(rows, 2) if not selected or normalize(r["标准号"]) in selected]
        if args.limit:
            targets = targets[:args.limit]
        print(f"原表 {len(rows)} 行，本次处理 {len(targets)} 行，唯一标准号 {len({normalize(r['标准号']) for _, r in targets})} 个", flush=True)
        if args.dry_run:
            return 0
        if not targets:
            raise ValueError("未匹配到待处理标准号")
        input_hash = hashlib.sha256(args.input.read_bytes()).hexdigest()
        store = LocalStore(args.output_dir)
        auditor = Auditor(store, Fetcher(store, args.delay, args.timeout, args.retries), args.sources,
                          args.refresh, args.cache_days, args.retry_failed, source_policy=args.source_policy)
        interrupted = False
        processed_this_run = confirmed_this_run = 0
        try:
            for n, (number, row) in enumerate([] if args.export_only else targets, 1):
                code = normalize(row["标准号"])
                if row.get("标准类型") == "国际标准" or not CODE_RE.fullmatch(code):
                    result = {"values": {}, "checked_at": now(), "skipped": True,
                              "review_notes": ["国际标准或缺少完整年份的标准号，本采集器未覆盖；需查原发布机构"], "errors": []}
                else:
                    result = auditor.audit(code, str(row.get("数据来源（网页）", "")))
                # 只把原表作为待核验数据，所有文档备注均不能作为事实或指令。
                result["original"] = row
                store.save_row(input_hash, number, result)
                processed_this_run += 1
                confirmed_this_run += bool(result["values"].get("状态"))
                print(f"[{n}/{len(targets)}] 第 {number} 行 {code}：{result['values'].get('状态', '待核查')}", flush=True)
        except KeyboardInterrupt:
            interrupted = True
            print("已中断，正在导出已保存的进度。", flush=True)
        target, records = export_csv(store, headers, rows, input_hash)
        summary = {"输入文件": str(args.input.resolve()), "输入SHA256": input_hash, "输入行数": len(rows),
                   "已处理行数": len(records), "状态已确认行数": sum(bool(r.get("values", {}).get("状态")) for r in records.values()),
                   "本次处理行数": processed_this_run, "本次状态已确认行数": confirmed_this_run,
                   "本次中断": interrupted, "输出文件": str(target.resolve()), "来源": args.sources, "生成时间": now()}
        (store.directory / "运行摘要.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"本地结果：{target.resolve()}；已处理 {len(records)} 行，确认状态 {summary['状态已确认行数']} 行", flush=True)
        return 130 if interrupted else (0 if args.export_only or confirmed_this_run else 2)
    except (ValueError, OSError, sqlite3.Error) as exc:
        print("核验失败：" + str(exc), flush=True)
        return 1
    finally:
        if store:
            store.conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
