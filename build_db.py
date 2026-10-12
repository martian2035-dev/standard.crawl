#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_db.py —— 把抓取得到的 CSV 汇总构建为 SQLite 标准数据库 standard_db

用法:
    python build_db.py                          # 输出 ./standard_db/standards.db
    python build_db.py --out standard_db        # 指定输出目录
    python build_db.py --data-dir .             # 指定 CSV 所在目录
    python build_db.py --verify                 # 只校验已有数据库，不重建

输入（默认读取脚本同目录下的三个 CSV）:
    国家公开标准信息.csv
    行业公开标准信息.csv
    地方公开标准信息.csv

输出:
    standard_db/standards.db    SQLite 数据库（standards 主表 + 常规索引）
    standard_db/manifest.json   构建清单（构建时间 / 条数 / SHA256），供下游仓库校验

关于全文检索的说明
------------------
本库**不建 FTS5 虚拟表**。原因：
  * FTS5 默认的 unicode61 分词器会把整串中文当成一个 token，
    搜「移动电源」匹配不到「移动电源安全技术规范」，对中文几乎无效；
  * 换成 trigram 分词器虽然可以子串匹配，但 29.7 万行要多占约 69MB。

因此采用「常规索引 + LIKE」方案：全表 LIKE 扫描实测约 50ms，中文子串检索准确，
同时数据库体积减少约 18MB。推荐查询方式见 README.md，例如：

    SELECT * FROM standards WHERE name LIKE '%移动电源%';
    SELECT * FROM standards WHERE standard_no_compact LIKE '%' || 'GBT1234' || '%';
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    sys.stderr.write("缺少依赖 pandas，请先执行： pip install -r requirements.txt\n")
    raise

SCHEMA_VERSION = 3
DEFAULT_OUT_DIR = "standard_db"
DB_FILENAME = "standards.db"

# 国家公开标准信息.csv 中「标准类型」列 → 数据库 category 的映射
NATIONAL_CATEGORY_MAP = {
    "强制性国家标准": "国标强制",
    "推荐性国家标准": "国标推荐",
    "指导性技术文件": "指导性技术文件",
}


# --------------------------------------------------------------------------- #
# 规范化工具
# --------------------------------------------------------------------------- #
def normalize_standard_no(raw) -> str:
    """标准号规范化：全角转半角、破折号统一、去书名号、压缩空格、转大写。

    与 import_standards.py 保持一致，保证不同来源的数据可以互相匹配。
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none", "nat", "null"):
        return ""

    # 全角字母/数字 → 半角
    out = []
    for ch in s:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    s = "".join(out)

    # 各种破折号 → 半角连字符
    s = re.sub(r"[—–―‐‑‒⁃]", "-", s)
    # 去掉书名号
    s = s.replace("《", "").replace("》", "").replace("〈", "").replace("〉", "")
    # 空格归一化
    s = re.sub(r"\s+", " ", s).strip()
    return s.upper()


# 「代替标准」一格里多个标准号之间的分隔符；入库时统一成 REPLACES_SEPARATOR
REPLACES_SPLIT_PATTERN = re.compile(r"[;；,，、|｜\n]+")
REPLACES_SEPARATOR = "；"


def parse_replaced_codes(raw) -> list[str]:
    """把「代替标准」拆成规范化后的标准号列表（去空、去重、保持原顺序）。

    数据源用「-」「无」之类表示没有代替标准，标准号必含数字，不含数字的片段一律丢弃。"""
    codes = (normalize_standard_no(part) for part in REPLACES_SPLIT_PATTERN.split(str(raw or "")))
    return list(dict.fromkeys(code for code in codes if any(ch.isdigit() for ch in code)))


def compact_standard_no(normalized: str) -> str:
    """去掉标准号里的所有空白，便于用户「不带空格」地检索。

    GB/T 1234.5-2020  ->  GB/T1234.5-2020
    """
    return re.sub(r"\s+", "", normalized)


def normalize_date(raw) -> str:
    """把各种日期写法统一成 YYYY-MM-DD，无法识别时原样返回。"""
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none", "nat", "null"):
        return ""
    m = re.match(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        return "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if m:
        return "%s-%s-%s" % m.groups()
    return s


def clean(raw) -> str:
    """去掉 NaN / None 噪声，返回 strip 后的字符串。"""
    if raw is None:
        return ""
    s = str(raw).strip()
    if s.lower() in ("nan", "none", "nat", "null"):
        return ""
    return s


def _row_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- #
# 数据库结构
# --------------------------------------------------------------------------- #
DDL_MAIN = """
CREATE TABLE IF NOT EXISTS standards (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    standard_no            TEXT NOT NULL,
    standard_no_normalized TEXT NOT NULL,
    standard_no_compact    TEXT NOT NULL DEFAULT '',
    name                   TEXT NOT NULL DEFAULT '',
    category               TEXT NOT NULL,
    is_adopted             INTEGER NOT NULL DEFAULT 0,
    status                 TEXT NOT NULL DEFAULT '',
    publish_date           TEXT NOT NULL DEFAULT '',
    implement_date         TEXT NOT NULL DEFAULT '',
    industry               TEXT NOT NULL DEFAULT '',
    region                 TEXT NOT NULL DEFAULT '',
    source_file            TEXT NOT NULL DEFAULT '',
    content_hash           TEXT NOT NULL DEFAULT '',
    approval_dept          TEXT NOT NULL DEFAULT '',
    replaces               TEXT NOT NULL DEFAULT '',
    replaced_by            TEXT NOT NULL DEFAULT '',
    UNIQUE (category, standard_no_normalized)
);
CREATE INDEX IF NOT EXISTS idx_standard_no            ON standards(standard_no);
CREATE INDEX IF NOT EXISTS idx_standard_no_normalized ON standards(standard_no_normalized);
CREATE INDEX IF NOT EXISTS idx_standard_no_compact    ON standards(standard_no_compact);
CREATE INDEX IF NOT EXISTS idx_category               ON standards(category);
CREATE INDEX IF NOT EXISTS idx_status                 ON standards(status);
CREATE INDEX IF NOT EXISTS idx_publish_date           ON standards(publish_date);
CREATE INDEX IF NOT EXISTS idx_industry               ON standards(industry);
CREATE INDEX IF NOT EXISTS idx_region                 ON standards(region);
"""

# 数据源申报的全部代替关系（一行一对）。被代替的旧标准不一定在库内
# （例如工程建设标准的来源只收录现行版本），下游可据此回答“某旧版标准被谁代替”。
DDL_REPLACEMENTS = """
CREATE TABLE IF NOT EXISTS replacements (
    old_standard_no         TEXT NOT NULL,
    old_standard_no_compact TEXT NOT NULL,
    new_standard_id         INTEGER NOT NULL REFERENCES standards(id),
    new_standard_no         TEXT NOT NULL,
    PRIMARY KEY (old_standard_no_compact, new_standard_id)
);
"""

DDL_META = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

INSERT_SQL = """
INSERT OR IGNORE INTO standards
    (standard_no, standard_no_normalized, standard_no_compact, name, category,
     is_adopted, status, publish_date, implement_date, industry, region,
     source_file, content_hash, approval_dept, replaces)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# 下游常用的便捷视图：只保留业务字段
DDL_VIEW = """
CREATE VIEW IF NOT EXISTS standards_view AS
SELECT standard_no, name, category, status,
       publish_date, implement_date, industry, region,
       approval_dept, replaces, replaced_by
FROM standards;
"""


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL_MAIN)
    conn.executescript(DDL_REPLACEMENTS)
    conn.executescript(DDL_META)
    conn.executescript(DDL_VIEW)


# --------------------------------------------------------------------------- #
# CSV 读取 → 行生成
# --------------------------------------------------------------------------- #
def _read_csv(path: Path) -> pd.DataFrame:
    # 爬虫用 utf-8-sig 写出（带 BOM），这里必须用 utf-8-sig 读，否则首列名会带 \ufeff
    return pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False)


def _make_row(standard_no, name, category, status, publish_date, implement_date,
              industry, region, is_adopted, source_file, approval_dept="", replaces=""):
    normalized = normalize_standard_no(standard_no)
    replaces = REPLACES_SEPARATOR.join(parse_replaced_codes(replaces))
    return (
        standard_no,
        normalized,
        compact_standard_no(normalized),
        name,
        category,
        is_adopted,
        status,
        normalize_date(publish_date),
        normalize_date(implement_date),
        industry,
        region,
        source_file,
        _row_hash(standard_no, name, category, status, publish_date, implement_date,
                  approval_dept, replaces),
        approval_dept,
        replaces,
    )


def iter_national(df: pd.DataFrame, source_file: str):
    """国家公开标准信息.csv：标准类型,序号,标准号,是否采标,标准名称,状态,发布日期,实施日期"""
    for rec in df.to_dict("records"):
        raw_type = clean(rec.get("标准类型"))
        standard_no = clean(rec.get("标准号"))
        if not standard_no:
            continue
        yield _make_row(
            standard_no=standard_no,
            name=clean(rec.get("标准名称")),
            category=NATIONAL_CATEGORY_MAP.get(raw_type, raw_type or "国家标准"),
            status=clean(rec.get("状态")),
            publish_date=rec.get("发布日期"),
            implement_date=rec.get("实施日期"),
            industry="",
            region="",
            is_adopted=1 if clean(rec.get("是否采标")) == "采" else 0,
            source_file=source_file,
        )


def iter_industry(df: pd.DataFrame, source_file: str):
    """行业公开标准信息.csv：标准号,标准名称,行业领域,状态,批准日期,实施日期,代替标准,主管部门

    后两列是后加的，旧 CSV 没有时按空值处理。"""
    for rec in df.to_dict("records"):
        standard_no = clean(rec.get("标准号"))
        if not standard_no:
            continue
        yield _make_row(
            standard_no=standard_no,
            name=clean(rec.get("标准名称")),
            category="行业标准",
            status=clean(rec.get("状态")),
            publish_date=rec.get("批准日期"),
            implement_date=rec.get("实施日期"),
            industry=clean(rec.get("行业领域")),
            region="",
            is_adopted=0,
            source_file=source_file,
            approval_dept=clean(rec.get("主管部门")),
            replaces=clean(rec.get("代替标准")),
        )


def iter_local(df: pd.DataFrame, source_file: str):
    """地方公开标准信息.csv：标准号,标准名称,省市区,状态,批准日期,实施日期,代替标准,主管部门

    后两列是后加的，旧 CSV 没有时按空值处理。"""
    for rec in df.to_dict("records"):
        standard_no = clean(rec.get("标准号"))
        if not standard_no:
            continue
        yield _make_row(
            standard_no=standard_no,
            name=clean(rec.get("标准名称")),
            category="地方标准",
            status=clean(rec.get("状态")),
            publish_date=rec.get("批准日期"),
            implement_date=rec.get("实施日期"),
            industry="",
            region=clean(rec.get("省市区")),
            is_adopted=0,
            source_file=source_file,
            approval_dept=clean(rec.get("主管部门")),
            replaces=clean(rec.get("代替标准")),
        )


# 工程建设标准信息.csv 的「标准类型」→ 库内类别
ENGINEERING_CATEGORY_MAP = {
    "工程国标": "工程建设国标",
    "工程行标": "工程建设行标",
    "产品行标": "行业标准",
}
ENGINEERING_PREFIX_PATTERN = re.compile(r"^([A-Z]+(?:/[A-Z]+)?)\s*(?=\d)")


def spaced_standard_no(raw: str) -> str:
    """工程建设标准号在来源中不带空格（GB50235-2010），补成与其他来源一致的写法（GB 50235-2010）。"""
    return ENGINEERING_PREFIX_PATTERN.sub(lambda match: match.group(1) + " ", raw.strip(), count=1)


def engineering_category(raw_type: str, standard_no: str) -> str:
    """产品国标按代号归入国标强制 / 国标推荐，其余按 ENGINEERING_CATEGORY_MAP。"""
    if raw_type == "产品国标":
        return "国标推荐" if standard_no.upper().startswith("GB/T") else "国标强制"
    return ENGINEERING_CATEGORY_MAP.get(raw_type, raw_type or "工程建设标准")


def iter_engineering(df: pd.DataFrame, source_file: str):
    """工程建设标准信息.csv：标准号,标准名称,标准类型,专业领域,状态,发布日期,实施日期,代替标准,主管部门"""
    for rec in df.to_dict("records"):
        standard_no = spaced_standard_no(clean(rec.get("标准号")))
        if not standard_no:
            continue
        yield _make_row(
            standard_no=standard_no,
            name=clean(rec.get("标准名称")),
            category=engineering_category(clean(rec.get("标准类型")), standard_no),
            status=clean(rec.get("状态")),
            publish_date=rec.get("发布日期"),
            implement_date=rec.get("实施日期"),
            industry=clean(rec.get("专业领域")),
            region="",
            is_adopted=0,
            source_file=source_file,
            approval_dept=clean(rec.get("主管部门")),
            replaces=clean(rec.get("代替标准")),
        )


# (文件名, 解析函数, 中文说明)
SOURCES = [
    ("国家公开标准信息.csv", iter_national, "国家标准"),
    ("行业公开标准信息.csv", iter_industry, "行业标准"),
    ("地方公开标准信息.csv", iter_local, "地方标准"),
    ("工程建设标准信息.csv", iter_engineering, "工程建设标准"),
]
# 这些来源排在后面，其中标准号（去空格后）已由前面来源收录的条目不再入库：
# 住建部的产品国标、产品行标同时登记在国家标准和行业标准备案平台，以平台记录为准
SUPPLEMENTARY_SOURCES = {"工程建设标准信息.csv"}


# --------------------------------------------------------------------------- #
# 替代关系
# --------------------------------------------------------------------------- #
def link_replacements(conn: sqlite3.Connection) -> tuple[int, int]:
    """据各标准申报的「代替标准」登记代替关系，并反向标注库内被代替的旧标准。

    - replacements 表：每一对「旧标准号 → 申报代替它的标准」各一行，不论旧标准是否在库内；
    - standards.replaced_by：旧标准号（去空格后）与库内某条标准完全一致时，在该条上记下新标准号。

    只认数据源明确给出的代替关系，不按「同编号、年份更新」之类的规律推断。
    返回（申报了代替标准的条数, 被标注的旧标准条数）。
    """
    cur = conn.cursor()
    ids_by_compact: dict[str, list[int]] = {}
    for standard_id, compact in cur.execute("SELECT id, standard_no_compact FROM standards"):
        ids_by_compact.setdefault(compact, []).append(standard_id)

    declaring = 0
    successors: dict[int, list[str]] = {}
    pairs: list[tuple[str, str, int, str]] = []
    for standard_id, standard_no, replaces in cur.execute(
        "SELECT id, standard_no, replaces FROM standards WHERE replaces != ''"
    ).fetchall():
        declaring += 1
        for code in replaces.split(REPLACES_SEPARATOR):
            compact = compact_standard_no(code)
            pairs.append((code, compact, standard_id, standard_no))
            for old_id in ids_by_compact.get(compact, ()):
                if old_id != standard_id:
                    successors.setdefault(old_id, []).append(standard_no)

    cur.executemany("INSERT OR IGNORE INTO replacements VALUES (?, ?, ?, ?)", pairs)

    cur.executemany(
        "UPDATE standards SET replaced_by = ? WHERE id = ?",
        [(REPLACES_SEPARATOR.join(dict.fromkeys(nos)), old_id) for old_id, nos in successors.items()],
    )
    conn.commit()
    return declaring, len(successors)


# --------------------------------------------------------------------------- #
# 构建
# --------------------------------------------------------------------------- #
def build(data_dir: Path, out_dir: Path, quiet: bool = False) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / DB_FILENAME
    if db_path.exists():
        db_path.unlink()
    for suffix in ("-wal", "-shm", "-journal"):
        stale = Path(str(db_path) + suffix)
        if stale.exists():
            stale.unlink()

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA cache_size=-128000")  # 128MB 缓存
    conn.execute("PRAGMA temp_store=MEMORY")

    create_schema(conn)
    cur = conn.cursor()

    stats: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    seen_compact: set[str] = set()
    batch: list[tuple] = []
    total_rows = 0
    total_skipped = 0

    for filename, parser, label in SOURCES:
        path = data_dir / filename
        if not path.exists():
            sys.stderr.write("[警告] 找不到数据文件，已跳过：%s\n" % path)
            stats[label] = 0
            continue

        df = _read_csv(path)
        inserted = 0
        for row in parser(df, filename):
            key = (row[4], row[1])  # (category, normalized_no)，与 UNIQUE 约束一致
            already_collected = filename in SUPPLEMENTARY_SOURCES and row[2] in seen_compact
            if not row[1] or key in seen or already_collected:
                total_skipped += 1
                continue
            seen.add(key)
            seen_compact.add(row[2])
            batch.append(row)
            inserted += 1
            total_rows += 1
            if len(batch) >= 5000:
                cur.executemany(INSERT_SQL, batch)
                batch.clear()

        stats[label] = inserted
        if not quiet:
            print("  ✅ %-10s %-28s 读入 %7d 条" % (label, filename, inserted))

    if batch:
        cur.executemany(INSERT_SQL, batch)
        batch.clear()
    conn.commit()

    count_replaces, count_replaced_by = link_replacements(conn)
    if not quiet:
        print("  🔗 申报代替标准 %d 条，库内被标注为已被代替 %d 条" % (count_replaces, count_replaced_by))

    now = _dt.datetime.now(_dt.timezone.utc).astimezone()
    meta = {
        "schema_version": str(SCHEMA_VERSION),
        "built_at": now.strftime("%Y-%m-%d %H:%M:%S%z"),
        "built_at_utc": now.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": "standard_crawl/build_db.py",
        "search_method": "index+like",
    }
    for label, count in stats.items():
        meta["count_%s" % label] = str(count)
    meta["count_total"] = str(total_rows)
    meta["count_duplicates_skipped"] = str(total_skipped)
    meta["count_replaces"] = str(count_replaces)
    meta["count_replaced_by"] = str(count_replaced_by)

    cur.executemany(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", sorted(meta.items())
    )
    conn.commit()

    # 压缩 + 收尾，保证产出是一个干净的单文件
    cur.execute("VACUUM")
    cur.execute("ANALYZE")
    conn.commit()
    conn.close()

    # 内容校验和（下载方可用它比对）
    digest = hashlib.sha256()
    with open(db_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    meta["sha256"] = digest.hexdigest()
    meta["size_bytes"] = str(db_path.stat().st_size)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "built_at": meta["built_at"],
        "built_at_utc": meta["built_at_utc"],
        "counts": stats,
        "count_total": total_rows,
        "count_duplicates_skipped": total_skipped,
        "count_replaces": count_replaces,
        "count_replaced_by": count_replaced_by,
        "database": DB_FILENAME,
        "sha256": meta["sha256"],
        "size_bytes": int(meta["size_bytes"]),
        "sources": [f for f, _, _ in SOURCES],
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    if not quiet:
        print("\n📊 standard_db 构建完成")
        print("   路径     : %s" % db_path)
        print("   总条数   : %d（去重跳过 %d）" % (total_rows, total_skipped))
        print("   文件大小 : %.1f MB" % (int(meta["size_bytes"]) / 1024 / 1024))
        print("   SHA256   : %s" % meta["sha256"])
    return manifest


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def verify(out_dir: Path) -> int:
    db_path = out_dir / DB_FILENAME
    if not db_path.exists():
        sys.stderr.write("[错误] 找不到数据库：%s\n" % db_path)
        return 1

    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    cur = conn.cursor()
    ok = True

    check = cur.execute("PRAGMA integrity_check").fetchone()[0]
    print("integrity_check : %s" % check)
    if check != "ok":
        ok = False

    try:
        total = cur.execute("SELECT COUNT(*) FROM standards").fetchone()[0]
        print("standards 行数  : %d" % total)
        for cat, cnt in cur.execute(
            "SELECT category, COUNT(*) FROM standards GROUP BY category ORDER BY 2 DESC"
        ):
            print("  %-16s %7d" % (cat, cnt))
    except sqlite3.Error as exc:
        print("[错误] 读取 standards 失败：%s" % exc)
        conn.close()
        return 1

    try:
        hit = cur.execute(
            "SELECT standard_no, name FROM standards WHERE name LIKE ? LIMIT 1",
            ("%标准%",),
        ).fetchone()
        print("中文 LIKE 查询  : %s" % ("正常 -> %s" % (hit[0] if hit else "无结果"),))
    except sqlite3.Error as exc:
        print("中文 LIKE 查询  : 失败（%s）" % exc)
        ok = False

    if not total:
        print("[错误] 数据库为空")
        ok = False

    try:
        with_replaces, with_replaced_by, with_dept = cur.execute(
            "SELECT SUM(replaces != ''), SUM(replaced_by != ''), SUM(approval_dept != '') FROM standards"
        ).fetchone()
        print("替代关系        : 申报代替标准 %d 条，已被代替 %d 条，有主管部门 %d 条"
              % (with_replaces or 0, with_replaced_by or 0, with_dept or 0))
    except sqlite3.Error as exc:
        print("[错误] 缺少替代关系字段（schema v3）：%s" % exc)
        ok = False

    manifest_path = out_dir / "manifest.json"
    if manifest_path.exists():
        mf = json.loads(manifest_path.read_text(encoding="utf-8"))
        print("manifest 构建于 : %s" % mf.get("built_at"))
        print("manifest SHA256 : %s" % mf.get("sha256"))
        digest = hashlib.sha256()
        with open(db_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        match = actual == mf.get("sha256")
        print("实际 SHA256     : %s %s" % (actual, "✅ 一致" if match else "❌ 不一致"))
        ok = ok and match
        if mf.get("count_total") is not None and int(mf["count_total"]) != total:
            print("❌ manifest 条数(%s) 与库内条数(%d) 不一致" % (mf["count_total"], total))
            ok = False

    conn.close()
    print("\n%s" % ("✅ 校验通过" if ok else "❌ 校验失败"))
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="构建 standard_db SQLite 标准数据库")
    ap.add_argument("--data-dir", default=".", help="CSV 所在目录（默认当前目录）")
    ap.add_argument("--out", default=DEFAULT_OUT_DIR, help="输出目录（默认 ./standard_db）")
    ap.add_argument("--verify", action="store_true", help="只校验已有数据库，不重建")
    ap.add_argument("--quiet", action="store_true", help="减少输出")
    args = ap.parse_args(argv)

    base = Path(__file__).resolve().parent
    data_dir = Path(args.data_dir) if os.path.isabs(args.data_dir) else base / args.data_dir
    out_dir = Path(args.out) if os.path.isabs(args.out) else base / args.out

    if args.verify:
        print("🔍 校验 standard_db ...")
        return verify(out_dir)

    print("🚀 开始构建 standard_db ...")
    print("   数据目录 : %s" % data_dir)
    print("   输出目录 : %s" % out_dir)
    build(data_dir, out_dir, quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
