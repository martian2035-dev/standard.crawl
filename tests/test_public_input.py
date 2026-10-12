"""Offline regression tests for public input."""
import csv

import pytest
from openpyxl import Workbook, load_workbook

from scripts.public_input import merge_local, prepare_public, validate_public


def original(path):
    book = Workbook()
    book.properties.creator = "不应公开的作者"
    sheet = book.active
    sheet.title = "原表"
    sheet.append(["标准类型", "标准号", "文件名", "备注"])
    sheet.append(["国家标准", "GB 2-2020", "私有甲.pdf", "内部备注"])
    sheet.append(["国家标准", "GB 1-2020", "私有乙.pdf", "=1+1"])
    sheet.append(["国家标准", "GB 2-2020", "私有丙.pdf", "内部备注"])
    book.save(path)
    book.close()


def test_public_input_drops_metadata_private_columns_and_row_order(tmp_path):
    source, target = tmp_path / "original.xlsx", tmp_path / "public.csv"
    original(source)
    before = source.read_bytes()
    assert prepare_public(source, target) == 2
    assert validate_public(target) == [
        {"标准类型": "国内标准", "标准号": "GB1-2020"},
        {"标准类型": "国内标准", "标准号": "GB2-2020"},
    ]
    assert source.read_bytes() == before
    for text in ("作者", "文件名", "备注", "行号", ".pdf"):
        assert text not in target.read_text(encoding="utf-8-sig")


def test_rejects_private_columns_and_formula_input(tmp_path):
    path = tmp_path / "public.csv"
    path.write_text("标准类型,标准号,备注\n国内标准,GB1-2020,隐私\n")
    with pytest.raises(ValueError, match="must contain only"):
        validate_public(path)
    path.write_text("标准类型,标准号\n国内标准,=1+1\n")
    with pytest.raises(ValueError, match="invalid"):
        validate_public(path)


def test_local_merge_preserves_private_fields_duplicates_and_missing_results(tmp_path):
    source = tmp_path / "original.xlsx"
    original(source)
    results = tmp_path / "results.csv"
    with results.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["标准类型", "标准号", "Input row", "Verified status", "Conclusion"])
        writer.writeheader()
        writer.writerow({"标准类型": "国内标准", "标准号": "GB2-2020", "Input row": 2, "Verified status": "现行", "Conclusion": "Verified by official sources"})
    output = merge_local(source, results, tmp_path / "local.xlsx")
    book = load_workbook(output)
    sheet = book["原表"]
    headers = {c.value: c.column for c in sheet[1]}
    assert sheet["C2"].value == "私有甲.pdf" and sheet["C4"].value == "私有丙.pdf"
    assert sheet["D3"].data_type == "f"
    assert sheet.cell(2, headers["Verified status"]).value == sheet.cell(4, headers["Verified status"]).value == "现行"
    assert sheet.cell(3, headers["Conclusion"]).value == "Not collected"
    book.close()
    with pytest.raises(ValueError, match="must not overwrite"):
        merge_local(source, results, source)
