"""SQL 沙箱子进程 worker（规格 §4：子进程隔离 + 超时 5s + 结果 ≤5000 行）。

以独立进程运行：`python -m src.sandbox.sql_worker`
协议（stdin/stdout 各一行 JSON）：
  请求: {"id": 1, "sql": "...", "tables": {"明细": "path.parquet"}, "max_rows": 5000}
  响应: {"id": 1, "ok": true, "columns": [...], "rows": [...], "total_rows": n, "elapsed_ms": 12}
        {"id": 1, "ok": false, "error": "中文错误信息"}

安全措施：
- DuckDB 内存库 + memory_limit=512MB + threads=2；
- 每张注册表是只读 VIEW（CREATE VIEW ... AS SELECT * FROM read_parquet(path)）；
- 外层还有 sqlguard 白名单与 runner 的超时击杀兜底。
"""

from __future__ import annotations

import json
import math
import os
import sys
import time

os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _clean_value(v):
    """把 DuckDB 返回值收敛成 JSON 安全类型（Decimal→float、日期转 ISO）。"""
    from decimal import Decimal

    if v is None:
        return None
    if isinstance(v, Decimal):
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, (int,)):
        return v
    if isinstance(v, float):
        if math.isinf(v):
            return None
        return round(v, 6)
    if hasattr(v, "isoformat"):
        return v.isoformat(sep=" ", timespec="seconds") if hasattr(v, "hour") else v.isoformat()
    if hasattr(v, "item"):  # numpy 标量
        return _clean_value(v.item())
    return str(v)


def main() -> None:
    import duckdb

    con = duckdb.connect(":memory:")
    con.execute("SET memory_limit='512MB'")
    con.execute("SET threads=2")
    con.execute("SET autoinstall_known_extensions=false")
    con.execute("SET autoload_known_extensions=false")
    # 说明：外部访问需保持开启（注册表时要读 parquet 文件）；
    # 用户 SQL 侧由 sqlguard 的函数白名单 + read_*/COPY/ATTACH 关键字黑名单拦截文件访问。
    _emit({"event": "ready"})

    registered: dict[str, str] = {}   # 视图名 → parquet 路径（路径变化说明版本已更新，需重建）
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _emit({"id": None, "ok": False, "error": "请求不是合法 JSON"})
            continue
        rid = req.get("id")
        try:
            views: dict[str, str] = req.get("tables") or {}
            for name, path in views.items():
                if registered.get(name) != path:
                    # CREATE TABLE 不能走参数绑定（duckdb 不 prepare DDL），改为转义字面量；
                    # 用 TABLE 而非 VIEW：数据一次载入内存，后续查询不再触碰文件系统
                    qn = str(name).replace('"', '""')
                    lit = "'" + str(path).replace("'", "''") + "'"
                    con.execute(f'DROP TABLE IF EXISTS "{qn}"')
                    con.execute(f'CREATE TABLE "{qn}" AS SELECT * FROM read_parquet({lit})')
                    registered[name] = path
            # 清掉本轮没出现的历史表（sheet 被撤销停用等）
            for stale in [n for n in registered if n not in views]:
                con.execute(f'DROP TABLE IF EXISTS "{str(stale).replace(chr(34), chr(34) * 2)}"')
                del registered[stale]
            sql = req["sql"]
            max_rows = int(req.get("max_rows", 5000))
            t0 = time.perf_counter()
            cur = con.execute(sql)
            rows_raw = cur.fetchall()
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            columns = [d[0] for d in cur.description] if cur.description else []
            total = len(rows_raw)
            rows = [[_clean_value(v) for v in r] for r in rows_raw[:max_rows]]
            _emit({"id": rid, "ok": True, "columns": columns, "rows": rows,
                   "total_rows": total, "elapsed_ms": elapsed_ms,
                   "truncated": total > max_rows})
        except Exception as e:  # noqa: BLE001 —— 错误信息原样回传主进程
            _emit({"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    main()
