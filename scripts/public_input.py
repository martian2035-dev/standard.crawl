#!/usr/bin/env python3
"""本地原表与公开查询清单之间的转换；原文件信息不进入公开清单。"""
import argparse
import csv
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from standard_audit import normalize, read_input

PUBLIC_INPUT = "公开标准查询清单.csv"
PUBLIC_HEADERS = ["标准类型", "标准号"]


def validate_public(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != PUBLIC_HEADERS:
            raise ValueError("公开查询清单只能包含标准类型、标准号两列")
        rows = list(reader)
    if not rows:
        raise ValueError("公开查询清单不能为空")
    for row in rows:
        code = row.get("标准号") or ""
        if (None in row or row.get("标准类型") not in ("国内标准", "国际标准")
                or not code or code[0] in "=+-@" or any(c in code for c in ("\n", "\r", "\\", ":"))):
            raise ValueError("公开查询清单存在非法类型或标准号，请在本地检查")
    return rows


def prepare_public(source, target):
    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve():
        raise ValueError("公开清单不能覆盖原始文件")
    _, rows = read_input(source)
    # 去重并排序，公开文件不携带原始行号、排列顺序或文件重复数量。
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
    """按标准号把公开核验结果映射回本地原表；不上传原始字段。"""
    from audit_excel import supplement_workbook

    source, output = Path(source), Path(output)
    if source.resolve() == output.resolve():
        raise ValueError("本地补充表不能覆盖原始文件")
    headers, rows = read_input(source)
    with Path(results).open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        extra = [h for h in reader.fieldnames or [] if h not in PUBLIC_HEADERS]
        if not {"标准号", "核验状态", "核验结论", "原表行号"}.issubset(reader.fieldnames or []):
            raise ValueError("请使用公开核验结果 CSV")
        records = {}
        for row in reader:
            code = normalize(row["标准号"])
            if code in records:
                raise ValueError("核验结果存在重复标准号，拒绝合并")
            records[code] = row
    if set(headers) & set(extra):
        raise ValueError("请使用尚未追加核验列的本地原始表")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "标准信息核验结果.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=headers + extra, lineterminator="\n")
            writer.writeheader()
            for number, original in enumerate(rows, 2):
                result = records.get(normalize(original["标准号"]), {})
                values = {k: result.get(k, "") for k in extra}
                values.update({"原表行号": number, "核验结论": result.get("核验结论", "未采集")})
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
    merge.add_argument("--out", type=Path, default=ROOT / "local_audit/标准信息统计表_已核验.xlsx")
    args = parser.parse_args()
    if args.command == "prepare":
        print(f"公开查询清单已就绪：{prepare_public(args.input, args.out)} 个去重查询项。原表保持不变。")
    else:
        print(f"本地补充表已保存：{merge_local(args.input, args.results, args.out)}")
