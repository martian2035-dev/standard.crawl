"""Offline regression tests for crawl dataset."""

import pandas as pd

import standard_crawl
from standard_crawl import (CCSN_COLUMNS, SACINFO_COLUMNS, TASK_BY_KEY, Dataset,
                            get_page_data_ccsn, get_page_data_sacinfo)


def industry_row(code="NB/T 47014-2023", status="现行", date="2023-12-28", replaces="NB/T 47014-2011"):
    return {
        "标准号": code,
        "标准名称": "承压设备焊接工艺评定",
        "行业领域": "能源",
        "状态": status,
        "批准日期": date,
        "实施日期": "2024-06-28",
        "代替标准": replaces,
        "主管部门": "国家能源局",
    }


def test_new_code_is_added(tmp_path):
    dataset = Dataset(tmp_path / "industry.csv", SACINFO_COLUMNS)

    assert dataset.add(industry_row()) is True
    assert (len(dataset), dataset.added, dataset.updated) == (1, 1, 0)


def test_identical_row_is_not_rewritten(tmp_path):
    dataset = Dataset(tmp_path / "industry.csv", SACINFO_COLUMNS)
    dataset.add(industry_row())
    dataset.dirty = False

    assert dataset.add(industry_row()) is False
    assert dataset.updated == 0 and dataset.dirty is False


def test_status_change_updates_existing_row(tmp_path):
    dataset = Dataset(tmp_path / "industry.csv", SACINFO_COLUMNS)
    dataset.add(industry_row(status="现行"))

    assert dataset.add(industry_row(status="废止")) is False
    assert dataset.rows[0]["状态"] == "废止"
    assert (len(dataset), dataset.added, dataset.updated) == (1, 1, 1)


def test_older_record_with_same_code_does_not_override(tmp_path):
    dataset = Dataset(tmp_path / "industry.csv", SACINFO_COLUMNS)
    dataset.add(industry_row(status="现行", date="2023-12-28"))

    dataset.add(industry_row(status="废止", date="2011-07-01"))

    assert dataset.rows[0]["状态"] == "现行" and dataset.updated == 0


def test_list_position_change_alone_is_not_an_update(tmp_path):
    columns = ["标准类型", "序号", "标准号", "是否采标", "标准名称", "状态", "发布日期", "实施日期"]
    row = {"标准类型": "推荐性国家标准", "序号": "1", "标准号": "GB/T 150.1-2024", "是否采标": "",
           "标准名称": "压力容器", "状态": "现行", "发布日期": "2024-07-24", "实施日期": "2025-02-01"}
    dataset = Dataset(tmp_path / "national.csv", columns)
    dataset.add(row)

    dataset.add({**row, "序号": "37"})

    assert dataset.updated == 0 and dataset.rows[0]["序号"] == "1"


def test_old_csv_without_new_columns_is_backfilled(tmp_path):
    path = tmp_path / "industry.csv"
    old_columns = ["标准号", "标准名称", "行业领域", "状态", "批准日期", "实施日期"]
    pd.DataFrame(
        [["NB/T 47014-2023", "承压设备焊接工艺评定", "能源", "现行", "2023-12-28", "2024-06-28"],
         ["SY/T 4109-2020", "石油天然气钢质管道无损检测", "石油天然气", "现行", "2020-10-23", "2021-02-01"]],
        columns=old_columns,
    ).to_csv(path, index=False, encoding="utf-8-sig")
    dataset = Dataset(path, SACINFO_COLUMNS)

    dataset.add(industry_row())
    dataset.save()

    saved = pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    assert list(saved.columns) == SACINFO_COLUMNS
    assert saved.loc[0, "代替标准"] == "NB/T 47014-2011" and saved.loc[0, "主管部门"] == "国家能源局"
    assert saved.loc[1, "代替标准"] == "" and saved.loc[1, "标准号"] == "SY/T 4109-2020"
    assert dataset.updated == 1 and len(dataset) == 2


def test_sacinfo_page_keeps_replaced_standards_and_department(monkeypatch):
    class FakeResponse:
        @staticmethod
        def json():
            return {
                "pages": 7,
                "records": [
                    {"code": "NB/T 47014—2023", "chName": "承压设备焊接工艺评定", "industry": "能源", "status": "现行",
                     "issueDate": 1703692800000, "actDate": 1719504000000,
                     "reviseStdCodes": " NB/T 47014—2011 ", "chargeDept": "国家能源局"},
                    {"code": "SY/T 4109-2020", "chName": "无损检测", "industry": "石油天然气", "status": "现行"},
                ],
            }

    monkeypatch.setattr(standard_crawl, "fetch_with_retry", lambda *args, **kwargs: FakeResponse())

    rows, total_pages = get_page_data_sacinfo(TASK_BY_KEY["industry"], 1, 50)

    assert total_pages == 7
    assert rows[0]["代替标准"] == "NB/T 47014—2011" and rows[0]["主管部门"] == "国家能源局"
    assert rows[1]["代替标准"] == "" and rows[1]["主管部门"] == ""
    assert set(rows[0]) == set(SACINFO_COLUMNS)


def test_ccsn_page_maps_engineering_standard_fields(monkeypatch):
    calls = {}

    class FakeResponse:
        @staticmethod
        def json():
            return {
                "Pages": 64,
                "List": [
                    {"StandardCode": "GB50235-2010", "StandardCNName": "工业金属管道工程施工规范", "StandardType": "工程国标",
                     "AreaOrTrade": "建筑工程", "StandardState": "现行", "PublishDate": "2010-08-18T00:00:00",
                     "PerformDate": "2011-06-01T00:00:00", "ReplaceStandardCode": "GB50235-1997",
                     "ApprovalDep": "中华人民共和国住房和城乡建设部", "Preface": "<p>不入库的长文本</p>"},
                    {"StandardCode": "GB55023-2022", "StandardCNName": "施工脚手架通用规范", "StandardState": "现行",
                     "PublishDate": None, "ReplaceStandardCode": None, "AreaOrTrade": None},
                ],
            }

    def fake_fetch(method, url, **kwargs):
        calls.update(method=method, url=url, data=kwargs["data"])
        return FakeResponse()

    monkeypatch.setattr(standard_crawl, "fetch_with_retry", fake_fetch)

    rows, total_pages = get_page_data_ccsn(TASK_BY_KEY["engineering"], 2, 50)

    assert total_pages == 64
    assert calls["data"] == {"gjz": "", "pageIndex": 2, "pageSize": 50}
    assert rows[0] == {
        "标准号": "GB50235-2010", "标准名称": "工业金属管道工程施工规范", "标准类型": "工程国标", "专业领域": "建筑工程",
        "状态": "现行", "发布日期": "2010-08-18", "实施日期": "2011-06-01", "代替标准": "GB50235-1997",
        "主管部门": "中华人民共和国住房和城乡建设部",
    }
    assert rows[1]["发布日期"] == "" and rows[1]["代替标准"] == "" and rows[1]["专业领域"] == ""
    assert set(rows[0]) == set(CCSN_COLUMNS)


def test_ccsn_page_failure_is_reported_as_unavailable(monkeypatch):
    def failing(*args, **kwargs):
        raise OSError("connection reset")

    monkeypatch.setattr(standard_crawl, "fetch_with_retry", failing)

    assert get_page_data_ccsn(TASK_BY_KEY["engineering"], 1, 50) == (None, 0)
