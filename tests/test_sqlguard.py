"""sqlguard 单元测试：语句类型 / 表白名单 / 列白名单 / 注入拦截。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.sandbox.sqlguard import SqlGuardError, ensure_limit, validate_sql, validate_expression

TABLES = {"订单明细": ["订单ID", "日期", "大区", "城市", "产品", "数量", "单价", "销售额", "负责人"],
          "商品主表": ["产品", "品类", "成本", "供应商"]}


def test_select_aggregation_ok():
    r = validate_sql('SELECT "大区", SUM("销售额") AS 总额 FROM "订单明细" GROUP BY "大区"', TABLES)
    assert r.ok, r.message


def test_cte_and_join_ok():
    sql = ('WITH t AS (SELECT "大区", SUM("销售额") AS s FROM "订单明细" GROUP BY "大区") '
           'SELECT t."大区", t.s FROM t ORDER BY t.s DESC')
    r = validate_sql(sql, TABLES)
    assert r.ok, r.message
    r2 = validate_sql('SELECT a."产品", b."品类" FROM "订单明细" a JOIN "商品主表" b ON a."产品"=b."产品"',
                      TABLES)
    assert r2.ok, r2.message


def test_unknown_column_rejected_with_suggestion():
    r = validate_sql('SELECT 销售 FROM "订单明细"', TABLES)
    assert not r.ok
    assert "销售额" in r.message  # 回喂正确列名


def test_unknown_table_rejected():
    r = validate_sql('SELECT * FROM 其他表', TABLES)
    assert not r.ok
    assert "其他表" in r.message


def test_dml_rejected():
    for bad in ['DELETE FROM "订单明细"', 'INSERT INTO "订单明细" VALUES (1)',
                'UPDATE "订单明细" SET 销售额=0']:
        assert not validate_sql(bad, TABLES).ok


def test_ddl_and_commands_rejected():
    for bad in ['DROP TABLE "订单明细"', 'CREATE TABLE t(a int)', 'ATTACH \'x.db\' AS x',
                'PRAGMA database_list', "COPY (SELECT 1) TO 'out.csv'", 'CALL something()']:
        assert not validate_sql(bad, TABLES).ok, bad


def test_file_read_functions_rejected():
    for bad in ["SELECT * FROM read_parquet('x.parquet')", "SELECT * FROM read_csv('x.csv')"]:
        assert not validate_sql(bad, TABLES).ok, bad


def test_multi_statement_rejected():
    assert not validate_sql('SELECT 1; SELECT 2', TABLES).ok


def test_ensure_limit_wraps():
    out = ensure_limit('SELECT "大区" FROM "订单明细"', 100)
    assert "LIMIT 100" in out.upper()
    out2 = ensure_limit('SELECT "大区" FROM "订单明细" LIMIT 5', 100)
    assert out2.upper().count("LIMIT") == 1


def test_expression_valid():
    r = validate_expression('"销售额" - "成本"', {"订单明细": ["销售额", "成本"]})
    assert r.ok, r.message


def test_expression_scalar_subquery_allowed_but_deep_nested_rejected():
    # 规格只禁"子查询嵌套过深"：单层标量子查询（只读）允许
    r = validate_expression('(SELECT MAX("销售额") FROM "订单明细")',
                            {"订单明细": ["销售额"]})
    assert r.ok, r.message


def test_case_insensitive_column_ok():
    r = validate_sql('select "大区" from "订单明细" where "销售额" > 1', TABLES)
    assert r.ok, r.message


def test_ascii_uppercase_column_ok():
    # 「订单ID」含 ASCII 大写，大小写归一化比较不能误杀
    r = validate_sql('SELECT "订单ID", "销售额" FROM "订单明细"', TABLES)
    assert r.ok, r.message


def test_logical_operators_allowed():
    # AND/OR/IN/BETWEEN 是运算符不是函数，不能进函数白名单拦截
    sqls = [
        'SELECT * FROM "订单明细" WHERE "大区" = \'华东\' AND "销售额" > 100',
        'SELECT * FROM "订单明细" WHERE "大区" IN (\'华东\', \'华南\') OR "数量" > 3',
        'SELECT * FROM "订单明细" WHERE "销售额" BETWEEN 100 AND 1000',
        'SELECT "销售额" * 1.1 - "数量" / 2 AS x FROM "订单明细"',
    ]
    for sql in sqls:
        r = validate_sql(sql, TABLES)
        assert r.ok, f"{sql} -> {r.message}"


def test_strftime_allowed():
    r = validate_sql("SELECT strftime(\"日期\", '%Y-%m') AS m, SUM(\"销售额\") "
                     "FROM \"订单明细\" GROUP BY m", TABLES)
    assert r.ok, r.message


def test_report_sql_with_and_filters():
    sql = ("WITH t AS (SELECT strftime(\"日期\", '%Y-%m') AS m, SUM(\"销售额\") AS v "
           "FROM \"订单明细\" WHERE \"大区\" = '华东' AND \"日期\" >= '2024-04-01' GROUP BY m) "
           "SELECT m, v FROM t ORDER BY m")
    r = validate_sql(sql, TABLES)
    assert r.ok, r.message
