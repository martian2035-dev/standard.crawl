#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build an indexed SQLite standards database from collected CSV files.

Usage:
    python build_db.py --data-dir . --out standard_db
    python build_db.py --verify

Outputs are standards.db and manifest.json (build time, record count and SHA256).
National, industry, local and optional engineering CSV data retain Chinese fields.
Use ordinary indexes and LIKE for Chinese substring searches; no FTS table is built.
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
    sys.stderr.write("Missing pandas; run: pip install -r requirements.txt\n")
    raise

SCHEMA_VERSION = 3
DEFAULT_OUT_DIR = "standard_db"
DB_FILENAME = "standards.db"

# Map source national standard types to database categories.
NATIONAL_CATEGORY_MAP = {
    "强制性国家标准": "国标强制",
    "推荐性国家标准": "国标推荐",
    "指导性技术文件": "指导性技术文件",
}


# --------------------------------------------------------------------------- #
# Normalization helpers.
# --------------------------------------------------------------------------- #
def normalize_standard_no(raw) -> str:
    """Normalize width, dashes, brackets, spacing and case for matching across sources."""
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none", "nat", "null"):
        return ""

    # Convert full-width letters and digits to ASCII.
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

    # Normalize dashes to the ASCII hyphen.
    s = re.sub(r"[—–―‐‑‒⁃]", "-", s)
    # Remove title brackets.
    s = s.replace("《", "").replace("》", "").replace("〈", "").replace("〉", "")
    # Normalize spaces.
    s = re.sub(r"\s+", " ", s).strip()
    return s.upper()


# Split multiple replacement identifiers and join with REPLACES_SEPARATOR.
REPLACES_SPLIT_PATTERN = re.compile(r"[;；,，、|｜\n]+")
REPLACES_SEPARATOR = "；"


def parse_replaced_codes(raw) -> list[str]:
    """Split replacement identifiers, normalize and deduplicate in order; discard entries without digits."""
    codes = (normalize_standard_no(part) for part in REPLACES_SPLIT_PATTERN.split(str(raw or "")))
    return list(dict.fromkeys(code for code in codes if any(ch.isdigit() for ch in code)))


def compact_standard_no(normalized: str) -> str:
    """Remove all whitespace for compact standard-number searches."""
    return re.sub(r"\s+", "", normalized)


def normalize_date(raw) -> str:
    """Normalize recognized dates to YYYY-MM-DD; preserve unrecognized values."""
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
    """Return stripped text without NaN or None values."""
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
# Database schema.
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

# Store every declared predecessor-successor pair, even if the predecessor is absent.
# Engineering sources may contain only current versions; explicit links still identify older replacements.
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

# Convenience view containing domain fields.
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
# Convert CSV records to database rows.
# --------------------------------------------------------------------------- #
def _read_csv(path: Path) -> pd.DataFrame:
    # Read UTF-8 with BOM support to keep the first header clean.
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
    """Read national standards from the original Chinese CSV schema."""
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
    """Read industry standards; missing replacement or authority columns in older CSVs are empty."""
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
    """Read local standards; missing replacement or authority columns in older CSVs are empty."""
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


# Map engineering source types to database categories.
ENGINEERING_CATEGORY_MAP = {
    "工程国标": "工程建设国标",
    "工程行标": "工程建设行标",
    "产品行标": "行业标准",
}
ENGINEERING_PREFIX_PATTERN = re.compile(r"^([A-Z]+(?:/[A-Z]+)?)\s*(?=\d)")


def spaced_standard_no(raw: str) -> str:
    """Insert a space after the engineering standard prefix, for example GB 50235-2010."""
    return ENGINEERING_PREFIX_PATTERN.sub(lambda match: match.group(1) + " ", raw.strip(), count=1)


def engineering_category(raw_type: str, standard_no: str) -> str:
    """Classify product national standards by prefix; map other engineering categories explicitly."""
    if raw_type == "产品国标":
        return "国标推荐" if standard_no.upper().startswith("GB/T") else "国标强制"
    return ENGINEERING_CATEGORY_MAP.get(raw_type, raw_type or "工程建设标准")


def iter_engineering(df: pd.DataFrame, source_file: str):
    """Read engineering standards from the original Chinese CSV schema."""
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


# (Filename, reader function, display label).
SOURCES = [
    ("国家公开标准信息.csv", iter_national, "National standards"),
    ("行业公开标准信息.csv", iter_industry, "Industry standards"),
    ("地方公开标准信息.csv", iter_local, "Local standards"),
    ("工程建设标准信息.csv", iter_engineering, "Engineering standards"),
]
# Later sources skip compact standard numbers already present in earlier sources.
# Prefer registry records for product standards also listed by the engineering source.
SUPPLEMENTARY_SOURCES = {"工程建设标准信息.csv"}


# --------------------------------------------------------------------------- #
# Replacement links.
# --------------------------------------------------------------------------- #
def link_replacements(conn: sqlite3.Connection) -> tuple[int, int]:
    """Record only explicit replacement links and annotate matching older standards. Return (declared links, annotated records). Never infer replacements from years."""
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
# Build database.
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
    conn.execute("PRAGMA cache_size=-128000")  # 128 MB cache.
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
            sys.stderr.write("[WARNING] Missing data file; skipped: %s\n" % path)
            stats[label] = 0
            continue

        df = _read_csv(path)
        inserted = 0
        for row in parser(df, filename):
            key = (row[4], row[1])  # (Category, normalized number), matching the UNIQUE constraint.
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
            print("  %-10s %-28s loaded %7d records" % (label, filename, inserted))

    if batch:
        cur.executemany(INSERT_SQL, batch)
        batch.clear()
    conn.commit()

    count_replaces, count_replaced_by = link_replacements(conn)
    if not quiet:
        print("  %d declared replacement links; %d stored records marked as replaced" % (count_replaces, count_replaced_by))

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

    # Compact and close to produce a standalone database file.
    cur.execute("VACUUM")
    cur.execute("ANALYZE")
    conn.commit()
    conn.close()

    # Checksum for consumers to verify downloads.
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
        print("\nstandard_db build complete")
        print("   Path: %s" % db_path)
        print("   Total records: %d (%d duplicates skipped)" % (total_rows, total_skipped))
        print("   File size: %.1f MB" % (int(meta["size_bytes"]) / 1024 / 1024))
        print("   SHA256   : %s" % meta["sha256"])
    return manifest


# --------------------------------------------------------------------------- #
# Verify database.
# --------------------------------------------------------------------------- #
def verify(out_dir: Path) -> int:
    db_path = out_dir / DB_FILENAME
    if not db_path.exists():
        sys.stderr.write("[ERROR] Database not found: %s\n" % db_path)
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
        print("Standards rows: %d" % total)
        for cat, cnt in cur.execute(
            "SELECT category, COUNT(*) FROM standards GROUP BY category ORDER BY 2 DESC"
        ):
            print("  %-16s %7d" % (cat, cnt))
    except sqlite3.Error as exc:
        print("[ERROR] Failed to read standards: %s" % exc)
        conn.close()
        return 1

    try:
        hit = cur.execute(
            "SELECT standard_no, name FROM standards WHERE name LIKE ? LIMIT 1",
            ("%标准%",),
        ).fetchone()
        print("Chinese LIKE query: %s" % ("OK -> %s" % (hit[0] if hit else "no results"),))
    except sqlite3.Error as exc:
        print("Chinese LIKE query failed: %s" % exc)
        ok = False

    if not total:
        print("[ERROR] Database is empty")
        ok = False

    try:
        with_replaces, with_replaced_by, with_dept = cur.execute(
            "SELECT SUM(replaces != ''), SUM(replaced_by != ''), SUM(approval_dept != '') FROM standards"
        ).fetchone()
        print("Replacement links: %d declared, %d replaced, %d with authorities"
              % (with_replaces or 0, with_replaced_by or 0, with_dept or 0))
    except sqlite3.Error as exc:
        print("[ERROR] Missing replacement fields (schema v3): %s" % exc)
        ok = False

    manifest_path = out_dir / "manifest.json"
    if manifest_path.exists():
        mf = json.loads(manifest_path.read_text(encoding="utf-8"))
        print("Manifest built at: %s" % mf.get("built_at"))
        print("manifest SHA256 : %s" % mf.get("sha256"))
        digest = hashlib.sha256()
        with open(db_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        match = actual == mf.get("sha256")
        print("Actual SHA256: %s %s" % (actual, "match" if match else "mismatch"))
        ok = ok and match
        if mf.get("count_total") is not None and int(mf["count_total"]) != total:
            print("Manifest count (%s) differs from database count (%d)" % (mf["count_total"], total))
            ok = False

    conn.close()
    print("\n%s" % ("Verification passed" if ok else "Verification failed"))
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the standard_db SQLite standards database")
    ap.add_argument("--data-dir", default=".", help="CSV directory (default: current directory)")
    ap.add_argument("--out", default=DEFAULT_OUT_DIR, help="Output directory (default: ./standard_db)")
    ap.add_argument("--verify", action="store_true", help="Verify the existing database without rebuilding")
    ap.add_argument("--quiet", action="store_true", help="Reduce output")
    args = ap.parse_args(argv)

    base = Path(__file__).resolve().parent
    data_dir = Path(args.data_dir) if os.path.isabs(args.data_dir) else base / args.data_dir
    out_dir = Path(args.out) if os.path.isabs(args.out) else base / args.out

    if args.verify:
        print("Verifying standard_db...")
        return verify(out_dir)

    print("Building standard_db...")
    print("   Data directory: %s" % data_dir)
    print("   Output directory: %s" % out_dir)
    build(data_dir, out_dir, quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
