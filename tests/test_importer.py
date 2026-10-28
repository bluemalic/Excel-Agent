"""导入器单元测试：编码识别 / 表头探测 / 合并单元格 / 脏数据标记。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src.core.importer import detect_header_row, import_file, load_with_header
from src.core.schema_extract import extract_schema

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def test_import_sales_orders():
    res = import_file(SAMPLES / "sales_orders.xlsx")
    assert set(res.sheets.keys()) == {"订单明细", "商品主表"}
    df = res.sheets["订单明细"]
    assert len(df) > 1000
    schema = extract_schema(df, "订单明细")
    names = schema.column_names()
    assert "销售额" in names and "大区" in names
    by_name = {c.name: c for c in schema.columns}
    assert by_name["销售额"].dtype == "float"
    assert 0.01 < by_name["销售额"].missing_rate < 0.1     # 埋了 3% 缺失
    assert by_name["折扣率"].dtype == "string"              # 文本型数值保持字符串
    assert any("文本型数值" in w for w in by_name["折扣率"].warnings)
    assert by_name["备注"].dtype == "empty"                 # 全空列标记
    assert by_name["日期"].dtype == "datetime"


def test_import_survey_gbk():
    res = import_file(SAMPLES / "survey.csv")
    df = res.sheets["survey"]
    assert "满意度" in df.columns
    assert len(df) == 300
    assert any("gb" in w.lower() for w in res.warnings)     # 编码提示


def test_dirty_merged_cells():
    res = import_file(SAMPLES / "dirty" / "merged_cells.xlsx")
    df = res.sheets["销售"]
    assert list(df.columns)[:3] == ["大区", "城市", "销售额"]
    # 合并单元格已按左上值填充：华东出现 8 次、华南 6 次
    assert (df["大区"] == "华东").sum() == 8
    assert (df["大区"] == "华南").sum() == 6
    assert any("合并单元格" in w for w in res.warnings)


def test_dirty_multi_header():
    res = import_file(SAMPLES / "dirty" / "multi_header.xlsx")
    df = res.sheets["指标"]
    # 第一行是大类（含空列名），第二行才是真正表头——自动探测应选中第二行
    assert any("2024Q1" in str(c) for c in df.columns)


def test_dirty_chinese_cols_gbk():
    res = import_file(SAMPLES / "dirty" / "chinese_cols.csv")
    df = res.sheets["chinese_cols"]
    assert "商品名称" in df.columns
    assert "单价（元）" in df.columns
    assert len(df) == 5


def test_header_detection_confidence():
    df = pd.DataFrame([["姓名", "年龄", "工资"],
                       ["张三", 28, 9000],
                       ["李四", 35, 12000]], dtype=object)
    row, conf, cands = detect_header_row(df)
    assert row == 0 and conf > 0.6
    assert 0 in cands


def test_duplicate_column_names_deduped():
    raw = pd.DataFrame([["a", "b", "b"], [1, 2, 3]], dtype=object)
    df = load_with_header(raw, 0)
    assert len(set(df.columns)) == 3
    assert "b_1" in df.columns
