"""把核验 CSV 和逐字段证据导出为可筛选的 Excel；长记录保留在 JSONL。"""
import csv
import datetime as dt
import json
import math
import re
from copy import copy
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from standard_audit import SOURCES


def supplement_workbook(input_path, result_dir, output_path):
    """在原工作簿副本中补充核验列；保留工作表、原值、公式和样式。"""
    from standard_audit import normalize

    result_dir, output_path = Path(result_dir), Path(output_path)
    with (result_dir / "标准信息核验结果.csv").open(encoding="utf-8-sig", newline="") as f:
        results = list(csv.DictReader(f))
    book = load_workbook(input_path)
    try:
        sheet = next((s for s in book if "标准号" in [c.value for c in s[1]]), None)
        if sheet is None or sheet.max_row != len(results) + 1:
            raise ValueError("核验结果与原工作表行数不一致，拒绝补充")
        headers = {c.value: c.column for c in sheet[1] if c.value}
        fields = ["核验标准名称", "核验状态", "核验发布日期", "核验实施日期", "核验废止日期",
                  "核验现行版本", "现行版本发布日期", "现行版本实施日期",
                  "核验数据来源（网页）", "核验时间", "核验结论", "核验说明"]
        for name in fields:
            if name in headers:
                raise ValueError("请使用原始统计表，不要使用已经补充核验列的结果表")
            column = sheet.max_column + 1
            cell = sheet.cell(1, column, name)
            cell._style = copy(sheet.cell(1, headers["标准号"])._style)
            sheet.column_dimensions[get_column_letter(column)].width = 42 if name in fields[-4:] else 24
            headers[name] = column
        for number, result in enumerate(results, 2):
            if int(result["原表行号"]) != number or normalize(result["标准号"]) != normalize(sheet.cell(number, headers["标准号"]).value):
                raise ValueError("核验结果与原表行号或标准号不一致，拒绝补充")
            for name in fields:
                cell = sheet.cell(number, headers[name])
                text_cell(cell, result.get(name, ""))
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                value = result.get(name, "")
                if name.endswith("日期") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    cell.value = dt.date.fromisoformat(value)
                    cell.number_format = "yyyy-mm-dd"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp = output_path.with_suffix(".tmp.xlsx")
        book.save(temp)
        temp.replace(output_path)
        return output_path
    finally:
        book.close()

DETAIL_COLUMNS = {"字段来源": "字段证据", "现行版本字段来源": "字段证据", "替代关系链": "替代关系"}


def text_cell(cell, value):
    text = str(value or "")
    if len(text) > 32767:
        text = "内容超过 Excel 单元格长度上限，完整内容见核验记录.jsonl。"
    cell.value = text
    cell.data_type = "s"


def style_table(sheet, header_row=1, freeze="A2"):
    sheet.freeze_panes = freeze
    sheet.auto_filter.ref = f"A{header_row}:{get_column_letter(sheet.max_column)}{sheet.max_row}"
    for row in sheet.iter_rows(min_row=header_row):
        height = 1
        for cell in row:
            cell.font = Font(name="Arial", size=10, color="23313D")
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if row[0].row == header_row:
                cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="24465B")
            elif row[0].row % 2:
                cell.fill = PatternFill("solid", fgColor="F2F6F8")
            width = sheet.column_dimensions[cell.column_letter].width
            height = max(height, sum(max(1, math.ceil(len(s) / max(8, width * .65))) for s in str(cell.value or "").split("\n")))
        sheet.row_dimensions[row[0].row].height = min(300, max(30, height * 14 + 6))
    sheet.sheet_view.showGridLines = False


def export_excel(directory):
    directory = Path(directory)
    with (directory / "标准信息核验结果.csv").open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers, rows = reader.fieldnames, list(reader)
    records = [json.loads(s) for s in (directory / "核验记录.jsonl").read_text().splitlines()]
    summary_path = directory / "运行摘要.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    workbook = Workbook()
    result = workbook.active
    result.title = "标准核验结果"
    result.merge_cells("A1:H1")
    result["A1"] = "标准信息核验结果"
    result["A1"].font = Font(name="Arial", size=18, bold=True, color="24465B")
    result.row_dimensions[1].height = 32
    result.merge_cells("A2:H2")
    result["A2"] = summary.get("任务状态", "历史筛查 / 部分联网核验") + "；未查到或未覆盖的记录仍保留，请查看核验结论。"
    result["A2"].font = Font(name="Arial", size=11, color="475A69")
    result["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    result.row_dimensions[2].height = 36
    for column, name in enumerate(headers, 1):
        result.cell(4, column, name)
        width = 18
        if "标准名称" in name or name == "文件名":
            width = 44
        elif "来源" in name or "说明" in name or name == "备注":
            width = 62
        elif "现行版本" in name:
            width = 36
        elif name in DETAIL_COLUMNS:
            width = 24
        result.column_dimensions[get_column_letter(column)].width = width
    for number, row in enumerate(rows, 5):
        for column, name in enumerate(headers, 1):
            value = row[name]
            cell = result.cell(number, column)
            if name in DETAIL_COLUMNS and value:
                value = "详见“" + DETAIL_COLUMNS[name] + "”工作表及核验记录.jsonl"
            if name.endswith("日期") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""):
                try:
                    cell.value = dt.date.fromisoformat(value)
                    cell.number_format = "yyyy-mm-dd"
                except ValueError:
                    text_cell(cell, value)
            else:
                text_cell(cell, value)
    style_table(result, 4, "D5")
    conclusion_column = headers.index("核验结论") + 1
    for number in range(5, result.max_row + 1):
        cell = result.cell(number, conclusion_column)
        color = {"官方来源已核验": "157A64", "需人工复核": "9D5D06", "待核查": "A03838"}.get(cell.value)
        if color:
            cell.font = Font(name="Arial", size=10, bold=True, color=color)
    # 证据展平为每个原表行、字段、来源一行，避免将庞大的 JSON 塞进单元格。
    evidence = workbook.create_sheet("字段证据")
    evidence.append(["原表行号", "原标准号", "所核验标准号", "字段", "值", "来源", "采用", "采集时间", "网页 URL", "快照路径", "SHA256", "请求参数"])
    relations = workbook.create_sheet("替代关系")
    relations.append(["原表行号", "原标准号", "链中标准号", "状态", "被替代为", "冲突 / 查询说明", "关系原文及来源"])
    evidence_row = relation_row = 1
    for record in records:
        original = record["original"]
        row_number = original.get("原统计表行号", record["原表行号"])
        raw_code = original["标准号"]
        items = [(raw_code, record.get("provenance", {}))]
        items.extend((v["标准号"], v["字段来源"]) for v in record.get("current_versions", []))
        for code, fields in items:
            for field, sources in fields.items():
                for source in sources:
                    values = [row_number, raw_code, code, field, source.get("value", ""), SOURCES[source["source"]][0],
                              "是" if source.get("selected") else "否 / 关系证据", source["fetched_at"], source["url"],
                              source["snapshot"], source["sha256"], source.get("request", {})]
                    evidence.append([json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for v in values])
                    evidence_row += 1
                    for cell in (evidence.cell(evidence_row, c) for c in range(1, 13)):
                        text_cell(cell, cell.value)
                    if source["url"].startswith(("https://", "http://")):
                        evidence.cell(evidence_row, 9).hyperlink = source["url"]
        for link in record.get("replacement_chain", []):
            rel_sources = link.get("字段来源", {}).get("替代关系", [])
            explanation = link.get("冲突", []) + [e["message"] for e in link.get("采集错误", [])]
            relations.append([row_number, raw_code, link["标准号"], link["状态"], "；".join(link["替代标准"]),
                              "；".join(explanation), json.dumps(rel_sources, ensure_ascii=False)])
            relation_row += 1
            for cell in (relations.cell(relation_row, c) for c in range(1, 8)):
                text_cell(cell, cell.value)
    for sheet in (evidence, relations):
        for column in range(1, sheet.max_column + 1):
            sheet.column_dimensions[get_column_letter(column)].width = 22 if column < 5 else 58
        style_table(sheet)
    target = directory / "标准信息核验结果.xlsx"
    temporary = target.with_suffix(".tmp.xlsx")
    workbook.save(temporary)
    workbook.close()
    # 检查已保存工作簿的记录数、公式注入与原始标准号映射。
    saved = load_workbook(temporary, read_only=True, data_only=False)
    try:
        sheet = saved["标准核验结果"]
        if sheet.max_row != len(rows) + 4:
            raise ValueError("Excel 导出记录数量不一致")
        for values in saved:
            for row in values:
                if any(cell.data_type == "f" for cell in row):
                    raise ValueError("核验 Excel 出现非预期公式")
    finally:
        saved.close()
    temporary.replace(target)
    return target
