"""版本链与撤销单元测试（AC-5：写操作可撤销且逐字节一致）。

用临时数据根目录跑，不污染正式 data/。
"""

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest

from src.core import storage as storage_mod
from src.core.diff import DiffResult
from src.core.workbook import WorkbookManager, WriteOp


@pytest.fixture()
def manager(tmp_path, monkeypatch):
    """独立的 Storage + WorkbookManager（临时目录）。"""
    s = storage_mod.Storage(db_path=tmp_path / "meta.sqlite")
    import src.core.storage as st
    monkeypatch.setattr(st, "_storage", s)
    import src.config as cfg
    monkeypatch.setattr(cfg, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(cfg, "WORKSPACE_DIR", tmp_path / "workspaces")
    monkeypatch.setattr(cfg, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(cfg, "MEM0_PATH", tmp_path / "mem0")
    cfg.ensure_dirs()
    WorkbookManager._instance = None
    mgr = WorkbookManager.instance()
    mgr._df_cache.clear()
    yield mgr
    WorkbookManager._instance = None


def _make_source(tmp_path: Path) -> Path:
    df = pd.DataFrame({
        "名称": ["苹果", "香蕉", "橙子", "葡萄", "西瓜", "芒果"],
        "价格": [8.5, 3.2, 6.0, 12.0, 2.5, 9.9],
        "库存": [120, 300, None, 45, 26, None],
    })
    p = tmp_path / "水果.xlsx"
    df.to_excel(p, index=False, sheet_name="水果")
    return p


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def test_single_write_undo_byte_identical(manager, tmp_path):
    src = _make_source(tmp_path)
    wb_id, _ = manager.create_from_import(src)
    original = manager.export_xlsx_bytes(wb_id)
    h0 = _sha(original)
    assert h0 == _sha(src.read_bytes())          # v0 导出 = 原始文件字节

    plan = manager.plan_write(wb_id, WriteOp(
        "fill_missing", "水果", {"column": "库存", "strategy": "mean"}))
    assert plan.diff.n_cells == 2
    manager.apply_write(wb_id, plan)
    df = manager.get_df(wb_id, "水果")
    assert df["库存"].isna().sum() == 0

    msg = manager.undo(wb_id)
    assert "撤销" in msg
    assert _sha(manager.export_xlsx_bytes(wb_id)) == h0     # AC-5：逐字节一致
    assert manager.get_df(wb_id, "水果")["库存"].isna().sum() == 2

    manager.redo(wb_id)
    assert manager.get_df(wb_id, "水果")["库存"].isna().sum() == 0
    manager.goto(wb_id, 0)
    assert manager.get_df(wb_id, "水果")["库存"].isna().sum() == 2


def test_chained_ops_and_goto(manager, tmp_path):
    src = _make_source(tmp_path)
    wb_id, _ = manager.create_from_import(src)

    p1 = manager.plan_write(wb_id, WriteOp("rename_column", "水果",
                                           {"old": "名称", "new": "水果名称"}))
    manager.apply_write(wb_id, p1)
    assert "水果名称" in manager.get_df(wb_id, "水果").columns

    p2 = manager.plan_write(wb_id, WriteOp("fill_missing", "水果",
                                           {"column": "库存", "strategy": "value", "value": 0}))
    manager.apply_write(wb_id, p2)

    # 回到第 1 步之后：重命名生效、填充撤销
    manager.goto(wb_id, 1)
    df = manager.get_df(wb_id, "水果")
    assert "水果名称" in df.columns and df["库存"].isna().sum() == 2

    # 回到初始
    manager.goto(wb_id, 0)
    df = manager.get_df(wb_id, "水果")
    assert "名称" in df.columns


def test_diff_threshold_double_confirm(manager, tmp_path):
    src = _make_source(tmp_path)
    wb_id, _ = manager.create_from_import(src)
    plan = manager.plan_write(wb_id, WriteOp(
        "fill_missing", "水果", {"column": "库存", "strategy": "value", "value": 1}))
    d: DiffResult = plan.diff
    assert d.needs_double_confirm(1) is True      # 2 格 > 阈值 1
    assert d.needs_double_confirm(50) is False


def test_new_op_after_undo_truncates_redo(manager, tmp_path):
    src = _make_source(tmp_path)
    wb_id, _ = manager.create_from_import(src)
    p1 = manager.plan_write(wb_id, WriteOp("set_cell", "水果",
                                           {"row": 0, "column": "价格", "value": 99}))
    manager.apply_write(wb_id, p1)
    manager.undo(wb_id)
    p2 = manager.plan_write(wb_id, WriteOp("set_cell", "水果",
                                           {"row": 0, "column": "价格", "value": 77}))
    manager.apply_write(wb_id, p2)
    with pytest.raises(Exception):
        manager.redo(wb_id)     # 旧 redo 分支已截断
    assert manager.get_df(wb_id, "水果")["价格"].iloc[0] == 77


def test_op_params_invalid_column_fed_back(manager, tmp_path):
    src = _make_source(tmp_path)
    wb_id, _ = manager.create_from_import(src)
    from src.core.mutations import OpError
    with pytest.raises(OpError) as e:
        manager.plan_write(wb_id, WriteOp("fill_missing", "水果",
                                          {"column": "不存在的列", "strategy": "mean"}))
    assert "库存" in str(e.value)      # 中文错误里带可用列名，可回喂模型


def test_snapshot_restore(manager, tmp_path):
    src = _make_source(tmp_path)
    wb_id, _ = manager.create_from_import(src)
    manager.save_snapshot(wb_id, "干净初始")
    p1 = manager.plan_write(wb_id, WriteOp("fill_missing", "水果",
                                           {"column": "库存", "strategy": "mean"}))
    manager.apply_write(wb_id, p1)
    msg = manager.restore_snapshot(wb_id, "干净初始")
    assert "恢复" in msg
    assert manager.get_df(wb_id, "水果")["库存"].isna().sum() == 2


def test_style_undo(manager, tmp_path):
    src = _make_source(tmp_path)
    wb_id, _ = manager.create_from_import(src)
    plan = manager.plan_write(wb_id, WriteOp(
        "style_cells", "水果",
        {"targets": ["B2:B4"], "style": {"bold": True, "bg_color": "FFF2CC"}}))
    manager.apply_write(wb_id, plan)
    styles = manager.get_styles(wb_id, "水果")
    assert "B2:B4" in styles and styles["B2:B4"]["bold"] is True
    manager.undo(wb_id)
    assert "B2:B4" not in manager.get_styles(wb_id, "水果")
