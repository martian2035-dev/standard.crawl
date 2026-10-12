#!/usr/bin/env python3
"""Prepare a public query list and merge results locally without publishing private fields."""
import argparse
import csv
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from standard_audit import normalize, read_input

PUBLIC_INPUT = "public_standards.csv"
PUBLIC_HEADERS = ["标准类型", "标准号"]


def validate_public(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != PUBLIC_HEADERS:
            raise ValueError("Public query list must contain only 标准类型 and 标准号")
        rows = list(reader)
    if not rows:
        raise ValueError("Public query list must not be empty")
    for row in rows:
        code = row.get("标准号") or ""
        if (None in row or row.get("标准类型") not in ("国内标准", "国际标准")
                or not code or code[0] in "=+-@" or any(c in code for c in ("\n", "\r", "\\", ":"))):
            raise ValueError("Public query list contains an invalid type or standard number; check it locally")
    return rows


def prepare_public(source, target):
    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve():
        raise ValueError("Public query list must not overwrite the original file")
    _, rows = read_input(source)
    # Deduplicate and sort; never expose original row numbers, ordering or duplicate counts.
    records = sorted({("国际标准" if r.get("标准类型") == "国际标准" else "国内标准", normalize(r["标准号"]))
                      for r in rows if str(r.get("标准号") or "").strip()})
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(PUBLIC_HEADERS)
            writer.writerows(records)
        validate_public(temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return len(records)


def merge_local(source, results, output):
    """Map public results to the original local workbook by standard number without uploading private fields."""
    from audit_excel import supplement_workbook

    source, output = Path(source), Path(output)
    if source.resolve() == output.resolve():
        raise ValueError("Audited workbook must not overwrite the original file")
    headers, rows = read_input(source)
    with Path(results).open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        extra = [h for h in reader.fieldnames or [] if h not in PUBLIC_HEADERS]
        if not {"标准号", "Verified status", "Conclusion", "Input row"}.issubset(reader.fieldnames or []):
            raise ValueError("Use the public audit results CSV")
        records = {}
        for row in reader:
            code = normalize(row["标准号"])
            if code in records:
                raise ValueError("Audit results contain duplicate standard numbers; refusing to merge")
            records[code] = row
    if set(headers) & set(extra):
        raise ValueError("Use the original local workbook without audit columns")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "audit-results.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=headers + extra, lineterminator="\n")
            writer.writeheader()
            for number, original in enumerate(rows, 2):
                result = records.get(normalize(original["标准号"]), {})
                values = {k: result.get(k, "") for k in extra}
                values.update({"Input row": number, "Conclusion": result.get("Conclusion", "Not collected")})
                writer.writerow({**original, **values})
        return supplement_workbook(source, Path(directory), output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("input", type=Path)
    prepare.add_argument("--out", type=Path, default=ROOT / PUBLIC_INPUT)
    merge = sub.add_parser("merge")
    merge.add_argument("input", type=Path)
    merge.add_argument("results", type=Path)
    merge.add_argument("--out", type=Path, default=ROOT / "local_audit/standards-audited.xlsx")
    args = parser.parse_args()
    if args.command == "prepare":
        print(f"Public query list ready: {prepare_public(args.input, args.out)} unique queries. Original workbook unchanged.")
    else:
        print(f"Audited local workbook saved: {merge_local(args.input, args.results, args.out)}")
