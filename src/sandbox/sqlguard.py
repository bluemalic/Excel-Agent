"""SQL 防护与列名白名单校验（规格 §4：最容易翻车也最容易出成绩的一处）。

三层防线：
1. 语句类型：只允许单条 SELECT/WITH 查询；任何 INSERT/UPDATE/DELETE/DDL/COPY/
   PRAGMA/ATTACH/CALL/SET 等命令节点直接拒绝。
2. 表白名单：FROM/JOIN 的表只能是当前 workbook 注册的 DuckDB 视图（库级隔离）。
3. 列白名单：sqlglot 解析出所有列引用，必须存在于 schema 列集或查询自身别名；
   非法列名拒绝，并把**正确列名列表**回喂给模型重试。

校验失败抛 SqlGuardError，消息为中文、可直接放进重试上下文。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

# 允许的 SQL 函数白名单（覆盖聚合/字符串/日期/数学/窗口常用函数）
_ALLOWED_FUNCTIONS: set[str] = {
    # 聚合
    "count", "sum", "avg", "min", "max", "median", "mode", "stddev", "stddev_pop",
    "variance", "approx_count_distinct", "string_agg", "group_concat", "first", "last",
    "count_if", "bool_and", "bool_or", "listagg",
    # 字符串
    "upper", "lower", "length", "len", "substr", "substring", "trim", "ltrim", "rtrim",
    "replace", "concat", "concat_ws", "contains", "left", "right", "lpad", "rpad",
    "reverse", "instr", "position", "str_split", "string_split", "regexp_matches",
    "regexp_extract", "regexp_replace", "format", "starts_with", "ends_with", "strip_accents",
    # 条件与空值
    "coalesce", "nullif", "ifnull", "nvl", "if", "iif", "case", "when", "then", "else", "end",
    # 数学
    "round", "abs", "ceil", "ceiling", "floor", "mod", "power", "pow", "sqrt", "exp",
    "ln", "log", "log10", "greatest", "least", "sign", "random",
    # 日期时间
    "date_trunc", "timestamp_trunc", "date_part", "datepart", "date_diff", "datediff",
    "date_add", "date_sub", "strftime", "time_to_str", "str_to_time", "str_to_date",
    "date_str_to_date", "ts_or_ds", "ds_or_ts", "time_to_time_str", "unix_to_time",
    "time_to_unix", "date_to_di", "di_to_date", "extract",
    "strptime", "year", "month", "day", "hour",
    "minute", "week", "quarter", "weekday", "dayofweek", "dayname", "monthname",
    "last_day", "make_date", "to_days", "age", "current_date", "now", "today", "epoch",
    # 类型与判断
    "try_cast", "typeof", "isnan", "isinf",
    # 窗口
    "row_number", "rank", "dense_rank", "percent_rank", "cume_dist", "ntile", "lag",
    "lead", "first_value", "last_value", "nth_value",
}

# 完全禁用的危险关键字（作为解析层之外的第二道正则防线；read_* 由函数白名单再拦一层）
_FORBIDDEN_KEYWORDS = (
    "insert into", "update set", "delete from", "drop table", "drop view", "drop database",
    "alter table", "create table", "create view", "create schema", "attach", "detach",
    "copy ", "export database", "import database", "install ", "load ", "call ",
    "pragma ", "set ", "reset ", "checkpoint", "vacuum", "prepare ", "execute ",
    "grant ", "revoke ", "read_csv", "read_json", "read_parquet", "read_iceberg",
    "glob(", "parquet_scan", "csv_scan", "json_scan",
    "write_file", "httpfs", "hf://", "s3://", "gs://", "azure://", "file://",
)


@dataclass
class GuardResult:
    ok: bool
    message: str = ""
    available_columns: list[str] = field(default_factory=list)
    available_tables: list[str] = field(default_factory=list)


class SqlGuardError(ValueError):
    """SQL 校验失败（消息可直接回喂模型）。"""


def _func_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return (node.name or "").lower()
    try:
        return node.sql_name().lower()
    except Exception:
        return node.key.lower()


def _extract_tables_and_ctes(tree: exp.Expression) -> tuple[set[str], dict[str, list[str]]]:
    """收集 CTE 名（可当表用）与 CTE 内部 SELECT 的输出别名（可当列用）。"""
    cte_tables: set[str] = set()
    cte_cols: dict[str, list[str]] = {}
    with_ = tree.args.get("with_") or tree.args.get("with")
    if with_:
        for cte in with_.expressions:
            alias = cte.alias_or_name
            cte_tables.add(alias.lower())
            cols = []
            try:
                for proj in cte.this.expressions:
                    cols.append(proj.alias_or_name.lower())
            except Exception:
                pass
            cte_cols[alias.lower()] = [c for c in cols if c]
    return cte_tables, cte_cols


def _output_aliases(tree: exp.Expression) -> set[str]:
    """查询输出列别名（ORDER BY/HAVING 可引用）。"""
    aliases: set[str] = set()
    for sel in tree.find_all(exp.Select):
        for proj in sel.expressions:
            if isinstance(proj, exp.Alias):
                aliases.add(proj.alias_or_name.lower())
    return aliases


def validate_sql(sql: str, tables: dict[str, list[str]]) -> GuardResult:
    """校验模型生成的 SQL。tables: {视图名: [列名]}。"""
    sql = (sql or "").strip().rstrip(";").strip()
    available_tables = sorted(tables.keys())
    all_columns = sorted({c for cols in tables.values() for c in cols})

    if not sql:
        return GuardResult(False, "SQL 为空", all_columns, available_tables)

    lowered = " " + sql.lower().replace("\n", " ") + " "
    lowered = _strip_strings(lowered)
    for kw in _FORBIDDEN_KEYWORDS:
        if kw in lowered:
            return GuardResult(
                False, f"SQL 含被禁止的关键字「{kw.strip()}」——只允许 SELECT 查询",
                all_columns, available_tables)

    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except sqlglot.errors.ParseError as e:
        return GuardResult(False, f"SQL 解析失败：{e}", all_columns, available_tables)

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        return GuardResult(False, "只允许一条查询语句", all_columns, available_tables)
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Subquery, exp.With)):
        return GuardResult(False, f"只允许 SELECT 查询，收到 {tree.key.upper()}",
                           all_columns, available_tables)

    cte_tables, cte_cols = _extract_tables_and_ctes(tree)
    output_aliases = {a.lower() for a in _output_aliases(tree)}
    # 白名单统一用小写比较（「订单ID」等含 ASCII 大写的列名不能被误杀）
    tables_lower = {name.lower(): name for name in tables}
    columns_lower = {c.lower(): c for cols in tables.values() for c in cols}
    known_columns = set(columns_lower) | output_aliases \
        | {c for cols in cte_cols.values() for c in cols} \
        | {c.lower() for c in output_aliases} | {"*"}

    # 命令/写语句节点与危险函数扫描
    for node in tree.walk():
        if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop,
                             exp.Alter, exp.Command, exp.Transaction, exp.Commit,
                             exp.Rollback, exp.Grant, exp.Merge, exp.TruncateTable,
                             exp.LoadData)):
            return GuardResult(False, f"SQL 含被禁止的语句类型 {node.key.upper()}",
                               all_columns, available_tables)
        # 运算符节点（AND/OR/算术等，sqlglot 里部分继承自 Func）不是函数调用，跳过
        if isinstance(node, (exp.Binary, exp.Unary)):
            continue
        if isinstance(node, exp.Anonymous) and _func_name(node) not in _ALLOWED_FUNCTIONS:
            return GuardResult(
                False, f"函数「{_func_name(node)}」不在允许清单（防止读取文件/生成数据）",
                all_columns, available_tables)
        if isinstance(node, exp.Func) and not isinstance(node, exp.Cast):
            name = _func_name(node)
            if name and name not in _ALLOWED_FUNCTIONS:
                return GuardResult(
                    False, f"函数「{name}」不在允许清单（防止读取文件/生成数据）",
                    all_columns, available_tables)
        # FROM/JOIN 表引用必须是注册视图或 CTE
        if isinstance(node, exp.Table):
            tname = (node.name or "").lower()
            if tname and tname not in tables_lower and tname not in cte_tables:
                return GuardResult(
                    False, f"表「{node.name}」不存在。可用表：{available_tables}",
                    all_columns, available_tables)
        # 列引用白名单（小写比较，报错时回显 schema 原始列名）
        if isinstance(node, exp.Column):
            cname = node.name.lower()
            if cname not in known_columns:
                real_cols = sorted(columns_lower.values())
                hint = _closest(cname, real_cols)
                msg = f"列「{node.name}」不存在。可用列：{real_cols}"
                if hint:
                    msg = f"列「{node.name}」不存在，你是不是想要「{hint}」？可用列：{real_cols}"
                return GuardResult(False, msg, all_columns, available_tables)

    # 嵌套深度限制
    depth = max((len(list(node.find_all(exp.Select))) for node in [tree]), default=1)
    if depth > 8:
        return GuardResult(False, "子查询嵌套过深（>8 层），请简化", all_columns, available_tables)

    return GuardResult(True, "校验通过", all_columns, available_tables)


def validate_expression(expr_sql: str, tables: dict[str, list[str]]) -> GuardResult:
    """校验派生列表达式（op_add_column 用）：单表达式，禁止子查询/窗口/聚合歧义。

    允许聚合函数（用户可能要"总销售额-总成本"这种整列聚合值），其余规则同 SELECT。
    """
    wrapped = f"SELECT ({expr_sql}) AS _derived_col_ FROM {_any_table(tables)}"
    return validate_sql(wrapped, tables)


def _any_table(tables: dict[str, list[str]]) -> str:
    return next(iter(tables.keys()), "_t")


def _closest(target: str, candidates: list[str]) -> str | None:
    """编辑距离最近的可列名提示。"""
    best, best_d = None, 10**9

    def _dist(a: str, b: str) -> int:
        import difflib
        return int(difflib.SequenceMatcher(None, a, b).ratio() * 100)

    for c in candidates:
        d = 100 - _dist(target.lower(), c.lower())
        if d < best_d:
            best, best_d = c, d
    return best if best and best_d < 60 else None


def _strip_strings(s: str) -> str:
    """去掉引号内内容，避免关键字误伤列名（第二道防线的粗筛足够）。"""
    import re
    return re.sub(r"'[^']*'|\"[^\"]*\"", "''", s)


def ensure_limit(sql: str, max_rows: int) -> str:
    """没有 LIMIT 的查询包一层强制 LIMIT（防全表拉爆）。"""
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except sqlglot.errors.ParseError:
        return f"SELECT * FROM ({sql}) _limited LIMIT {max_rows}"
    if isinstance(tree, (exp.Select, exp.Union)) and tree.args.get("limit") is None:
        return f"SELECT * FROM ({sql}) _limited LIMIT {max_rows}"
    return sql
