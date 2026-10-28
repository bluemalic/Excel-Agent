"""模板加载与 MCP Server 冒烟测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import yaml

from src.config import PROJECT_ROOT, TEMPLATES_DIR

TEMPLATES = sorted(TEMPLATES_DIR.glob("*.yaml"))


def test_templates_present_and_valid():
    assert len(TEMPLATES) >= 4   # 规格要求 4-5 个
    for yml in TEMPLATES:
        tpl = yaml.safe_load(yml.read_text(encoding="utf-8"))
        assert tpl.get("template_id"), yml
        assert tpl.get("name"), yml
        assert tpl.get("sheets"), yml
        for sheet in tpl["sheets"]:
            assert sheet.get("name")
            assert sheet.get("columns"), yml
            for col in sheet["columns"]:
                assert col.get("name"), yml
                assert col.get("type"), yml


def test_template_create_empty_table(tmp_path, monkeypatch):
    from src.core import storage as storage_mod
    s = storage_mod.Storage(db_path=tmp_path / "m.sqlite")
    import src.core.storage as st
    monkeypatch.setattr(st, "_storage", s)
    import src.config as cfg
    monkeypatch.setattr(cfg, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(cfg, "WORKSPACE_DIR", tmp_path / "ws")
    cfg.ensure_dirs()
    from src.core.workbook import WorkbookManager
    WorkbookManager._instance = None
    mgr = WorkbookManager.instance()

    tpl = yaml.safe_load((TEMPLATES_DIR / "sales_monthly.yaml").read_text(encoding="utf-8"))
    sheet = tpl["sheets"][0]
    wb_id = mgr.create_from_template("测试台账", sheet["name"], sheet["columns"])
    df = mgr.get_df(wb_id, sheet["name"])
    assert len(df) == 0
    assert list(df.columns) == [c["name"] for c in sheet["columns"]]
    # 对话式填数（add_rows 走写工具链）
    plan = mgr.plan_write(wb_id, __import__("src.core.workbook", fromlist=["WriteOp"]).WriteOp(
        "add_rows", sheet["name"],
        {"rows": [{"日期": "2024-07-01", "大区": "华东", "产品": "智能手表Pro",
                   "销售额": 120000, "数量": 45, "负责人": "张三"}]}))
    assert plan.diff.rows_added == 1
    mgr.apply_write(wb_id, plan)
    assert len(mgr.get_df(wb_id, sheet["name"])) == 1
    mgr.undo(wb_id)
    assert len(mgr.get_df(wb_id, sheet["name"])) == 0
    WorkbookManager._instance = None


@pytest.mark.asyncio
async def test_mcp_server_tools():
    fastmcp = pytest.importorskip("fastmcp")
    from fastmcp import Client
    from src.mcp_server.server import mcp as server

    async with Client(server) as client:
        tools = {t.name for t in await client.list_tools()}
        assert {"excel_query", "excel_schema", "excel_chart", "excel_join",
                "excel_propose_write", "excel_confirm_write", "excel_undo"} <= tools
        r = await client.call_tool("list_workbooks", {})
        assert "workbook_id" in r.content[0].text


@pytest.mark.asyncio
async def test_mcp_write_confirm_flow(tmp_path, monkeypatch):
    """MCP 写路径：propose（不落盘）→ confirm（落盘）→ undo（恢复）。"""
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from src.core import storage as storage_mod
    s = storage_mod.Storage(db_path=tmp_path / "m.sqlite")
    import src.core.storage as st
    monkeypatch.setattr(st, "_storage", s)
    import src.config as cfg
    for attr in ("DATA_ROOT", "WORKSPACE_DIR", "RESULTS_DIR", "MEM0_PATH"):
        monkeypatch.setattr(cfg, attr, tmp_path / attr)
    cfg.ensure_dirs()

    import pandas as pd
    df = pd.DataFrame({"名称": ["a", "b", "c"], "库存": [1, None, 3]})
    src = tmp_path / "t.xlsx"
    df.to_excel(src, index=False, sheet_name="t")
    from src.core.workbook import WorkbookManager
    WorkbookManager._instance = None
    mgr = WorkbookManager.instance()
    wb_id, _ = mgr.create_from_import(src)

    from src.mcp_server.server import mcp as server
    async with Client(server) as client:
        r = await client.call_tool("excel_propose_write", {
            "workbook_id": wb_id, "op_type": "fill_missing", "sheet": "t",
            "params_json": '{"column": "库存", "strategy": "value", "value": 0}'})
        assert "待确认" in r.content[0].text or "n_cells" in r.content[0].text
        assert mgr.get_df(wb_id, "t")["库存"].isna().sum() == 1   # 未落盘
        r2 = await client.call_tool("excel_confirm_write", {"workbook_id": wb_id})
        assert '"ok": true' in r2.content[0].text or '"ok":true' in r2.content[0].text.replace(" ", "")
        assert mgr.get_df(wb_id, "t")["库存"].isna().sum() == 0
        await client.call_tool("excel_undo", {"workbook_id": wb_id})
        assert mgr.get_df(wb_id, "t")["库存"].isna().sum() == 1
    WorkbookManager._instance = None
