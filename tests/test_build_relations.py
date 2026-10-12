"""构建器 schema v3（主管部门、代替标准、被代替标注）测试"""

import sqlite3

import pandas as pd
import pytest

import build_db
from build_db import parse_replaced_codes


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", []),
        (None, []),
        ("-", []),
        ("无", []),
        ("/；NB/T 47014—2011", ["NB/T 47014-2011"]),
        ("NB/T 47014—2011", ["NB/T 47014-2011"]),
        ("JB/T 7630－1994;JB 3385－1991", ["JB/T 7630-1994", "JB 3385-1991"]),
        ("GBJ2-86|GB/T50100-2001", ["GBJ2-86", "GB/T50100-2001"]),
        ("gb/t 150.1-2011；GB/T 150.1-2011、 GB 151-1999 ", ["GB/T 150.1-2011", "GB 151-1999"]),
    ],
)
def test_parse_replaced_codes(raw, expected):
    assert parse_replaced_codes(raw) == expected


def write_csv(path, columns, rows):
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False, encoding="utf-8-sig")


@pytest.fixture
def built(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    write_csv(
        data_dir / "国家公开标准信息.csv",
        ["标准类型", "序号", "标准号", "是否采标", "标准名称", "状态", "发布日期", "实施日期"],
        [["推荐性国家标准", "1", "GB/T 150.1-2011", "", "压力容器 第1部分", "废止", "2011-11-21", "2012-03-01"]],
    )
    write_csv(
        data_dir / "行业公开标准信息.csv",
        ["标准号", "标准名称", "行业领域", "状态", "批准日期", "实施日期", "代替标准", "主管部门"],
        [
            ["NB/T 47014—2023", "承压设备焊接工艺评定", "能源", "现行", "2023-12-28", "2024-06-28", "NB/T 47014—2011", "国家能源局"],
            ["NB/T 47014-2011", "承压设备焊接工艺评定", "能源", "废止", "2011-07-01", "2011-10-01", "JB 4708-2000", "国家能源局"],
            ["NB/T 10558-2021", "压力容器涂敷与运输包装", "能源", "现行", "2021-01-07", "2021-07-01",
             "JB/T 4711－2003;GB/T 150.1-2011", "国家能源局"],
        ],
    )
    # 旧格式：没有「代替标准」「主管部门」两列
    write_csv(
        data_dir / "地方公开标准信息.csv",
        ["标准号", "标准名称", "省市区", "状态", "批准日期", "实施日期"],
        [["DB11/T 1322.82-2019", "安全生产等级评定技术规范", "北京市", "有更新版", "2019-06-18", "2019-10-01"]],
    )
    write_csv(
        data_dir / "工程建设标准信息.csv",
        ["标准号", "标准名称", "标准类型", "专业领域", "状态", "发布日期", "实施日期", "代替标准", "主管部门"],
        [
            ["GB50235-2010", "工业金属管道工程施工规范", "工程国标", "建筑工程", "现行", "2010-08-18", "2011-06-01",
             "GB50235-1997", "中华人民共和国住房和城乡建设部"],
            ["JGJ/T46-2024", "建筑与市政工程施工现场临时用电安全技术标准", "工程行标", "建筑工程", "现行",
             "2024-05-01", "2024-10-01", "JGJ46-2005", "中华人民共和国住房和城乡建设部"],
            ["GB50016-2014（2018年版）", "建筑设计防火规范", "工程国标", "建筑工程", "现行", "2014-08-27", "2015-05-01",
             "", "中华人民共和国住房和城乡建设部"],
            # 行业标准备案平台已收录的产品行标：不重复入库
            ["NB/T47014-2011", "承压设备焊接工艺评定", "产品行标", "", "现行", "2011-07-01", "2011-10-01", "", ""],
            ["JG/T163-2013", "钢筋机械连接用套筒", "产品行标", "建筑工程", "现行", "2013-05-01", "2013-10-01",
             "JG163-2004、JG171-2005", ""],
            ["GB/T50001-2017", "房屋建筑制图统一标准", "产品国标", "", "现行", "2017-09-27", "2018-05-01", "", ""],
        ],
    )
    out_dir = tmp_path / "out"
    manifest = build_db.build(data_dir, out_dir, quiet=True)
    connection = sqlite3.connect(out_dir / build_db.DB_FILENAME)
    connection.row_factory = sqlite3.Row
    yield manifest, connection, out_dir
    connection.close()


def fetch(connection, standard_no):
    return connection.execute("SELECT * FROM standards WHERE standard_no = ?", (standard_no,)).fetchone()


def test_replaces_and_department_are_stored_normalized(built):
    _, connection, _ = built

    row = fetch(connection, "NB/T 10558-2021")

    assert row["replaces"] == "JB/T 4711-2003；GB/T 150.1-2011"
    assert row["approval_dept"] == "国家能源局"


def test_replaced_standard_points_to_its_successor(built):
    _, connection, _ = built

    assert fetch(connection, "NB/T 47014-2011")["replaced_by"] == "NB/T 47014—2023"


def test_replacement_links_across_categories(built):
    _, connection, _ = built

    assert fetch(connection, "GB/T 150.1-2011")["replaced_by"] == "NB/T 10558-2021"


def test_standards_without_declared_replacement_stay_blank(built):
    _, connection, _ = built

    current = fetch(connection, "NB/T 47014—2023")
    local = fetch(connection, "DB11/T 1322.82-2019")

    assert current["replaced_by"] == ""
    assert (local["replaces"], local["replaced_by"], local["approval_dept"]) == ("", "", "")


def test_manifest_and_meta_report_schema_v3_and_relation_counts(built):
    manifest, connection, _ = built
    meta = dict(connection.execute("SELECT key, value FROM meta").fetchall())

    assert manifest["schema_version"] == 3 and meta["schema_version"] == "3"
    assert manifest["count_replaces"] == 6 and meta["count_replaces"] == "6"
    assert manifest["count_replaced_by"] == 2


def test_view_exposes_relation_columns(built):
    _, connection, _ = built

    row = connection.execute("SELECT * FROM standards_view WHERE standard_no = 'NB/T 47014-2011'").fetchone()

    assert row["replaced_by"] == "NB/T 47014—2023" and row["replaces"] == "JB 4708-2000"


def test_verify_passes_on_built_database(built, capsys):
    _, _, out_dir = built

    assert build_db.verify(out_dir) == 0
    assert "申报代替标准 6 条，已被代替 2 条" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("GB50235-2010", "GB 50235-2010"),
        ("JGJ/T46-2024", "JGJ/T 46-2024"),
        ("GB50016-2014（2018年版）", "GB 50016-2014（2018年版）"),
        ("GB/T 50001-2017", "GB/T 50001-2017"),
    ],
)
def test_engineering_standard_numbers_get_a_space(raw, expected):
    assert build_db.spaced_standard_no(raw) == expected


def test_engineering_standards_are_categorized(built):
    _, connection, _ = built

    national = fetch(connection, "GB 50235-2010")
    industry = fetch(connection, "JGJ/T 46-2024")

    assert (national["category"], national["industry"], national["status"]) == ("工程建设国标", "建筑工程", "现行")
    assert national["approval_dept"] == "中华人民共和国住房和城乡建设部"
    assert national["standard_no_compact"] == "GB50235-2010"
    assert industry["category"] == "工程建设行标"
    assert fetch(connection, "JG/T 163-2013")["category"] == "行业标准"
    assert fetch(connection, "GB/T 50001-2017")["category"] == "国标推荐"


def test_engineering_row_already_collected_elsewhere_is_skipped(built):
    manifest, connection, _ = built

    rows = connection.execute("SELECT category FROM standards WHERE standard_no_compact = 'NB/T47014-2011'").fetchall()

    assert [row["category"] for row in rows] == ["行业标准"]
    assert manifest["counts"]["工程建设标准"] == 5


def test_replacements_table_covers_old_standards_not_in_database(built):
    _, connection, _ = built

    rows = connection.execute(
        "SELECT old_standard_no, new_standard_no FROM replacements "
        "WHERE old_standard_no_compact IN ('GB50235-1997', 'JGJ46-2005', 'JG171-2005') ORDER BY 1"
    ).fetchall()

    assert [tuple(row) for row in rows] == [
        ("GB50235-1997", "GB 50235-2010"),
        ("JG171-2005", "JG/T 163-2013"),
        ("JGJ46-2005", "JGJ/T 46-2024"),
    ]
    assert fetch(connection, "GB 50235-2010")["replaces"] == "GB50235-1997"


def test_build_without_engineering_csv_still_works(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    write_csv(
        data_dir / "行业公开标准信息.csv",
        ["标准号", "标准名称", "行业领域", "状态", "批准日期", "实施日期"],
        [["SY/T 4109-2020", "石油天然气钢质管道无损检测", "石油天然气", "现行", "2020-10-23", "2021-02-01"]],
    )

    manifest = build_db.build(data_dir, tmp_path / "out", quiet=True)

    assert manifest["count_total"] == 1 and manifest["counts"]["工程建设标准"] == 0
