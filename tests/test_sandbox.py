"""沙箱与工作记忆单元测试（子进程执行、结果上限、L2 指代锚点）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest

from src.sandbox.runner import SqlGuardError, SqlRunner, SandboxTimeoutError


@pytest.fixture(scope="module")
def runner_tables(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sb")
    df = pd.DataFrame({"大区": ["华东", "华南"] * 50, "销售额": range(100)})
    p = tmp / "t.parquet"
    df.to_parquet(p, index=False)
    return SqlRunner.instance(), {"测试表": str(p)}, {"测试表": ["大区", "销售额"]}


def test_query_basic(runner_tables):
    runner, tables, cols = runner_tables
    res = runner.query('SELECT "大区", COUNT(*) AS n FROM "测试表" GROUP BY "大区"', tables, cols)
    assert res.total_rows == 2
    assert res.rows[0][1] == 50


def test_query_guard_rejects_before_execution(runner_tables):
    runner, tables, cols = runner_tables
    with pytest.raises(SqlGuardError):
        runner.query('SELECT 不存在列 FROM "测试表"', tables, cols)


def test_query_timeout_kills_worker(runner_tables):
    runner, tables, cols = runner_tables
    # DuckDB 没有内置 sleep 函数且 generate_series 被函数白名单限制；
    # 用大笛卡尔积制造超时（100 行 × 重复自联 ~ 上亿次）——5s 内必超时
    heavy = 'SELECT COUNT(*) FROM "测试表" a, "测试表" b, "测试表" c, "测试表" d, "测试表" e, "测试表" f, "测试表" g, "测试表" h'
    with pytest.raises((SandboxTimeoutError, SqlGuardError)):
        runner.query(heavy, tables, cols)
    # 超时击杀后 worker 自动重建，后续查询正常（AC-9）
    res = runner.query('SELECT COUNT(*) AS n FROM "测试表"', tables, cols)
    assert res.rows[0][0] == 100


def test_result_row_cap(runner_tables):
    runner, tables, cols = runner_tables
    res = runner.query('SELECT * FROM "测试表" LIMIT 10', tables, cols, max_rows=5)
    assert len(res.rows) == 5 and res.truncated


def test_working_memory_roundtrip(tmp_path, monkeypatch):
    from src.core import storage as storage_mod
    s = storage_mod.Storage(db_path=tmp_path / "m.sqlite")
    import src.core.storage as st
    monkeypatch.setattr(st, "_storage", s)

    from src.memory.working import WorkingMemory
    WorkingMemory._instance = None
    wm = WorkingMemory.instance()
    wm.set("s1", "active_sheet", "订单明细")
    wm.set("s1", "last_result_ref", "res_abc")
    snap = wm.snapshot("s1")
    assert snap["active_sheet"] == "订单明细"
    assert "res_abc" in wm.to_prompt("s1")
    with pytest.raises(ValueError):
        wm.set("s1", "hack_key", 1)
    WorkingMemory._instance = None
