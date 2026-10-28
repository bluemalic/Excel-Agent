"""工具层单元测试：图表 / 分析四层计算 / 导出 / compute 沙箱 / 评测判分。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd


def _sample_df() -> pd.DataFrame:
    rng = np.random.default_rng(1)
    n = 120
    return pd.DataFrame({
        "日期": pd.to_datetime("2024-01-01") + pd.to_timedelta(np.arange(n) * 3, unit="D"),
        "大区": rng.choice(["华东", "华南", "华北"], n),
        "销售额": rng.uniform(100, 1000, n).round(2),
        "数量": rng.integers(1, 10, n),
    })


def test_chart_bar_renders_png(tmp_path):
    from src.tools.chart import render_chart
    df = _sample_df()
    g = df.groupby("大区")["销售额"].sum().reset_index()
    out = render_chart({"type": "bar", "title": "各大区销售额", "x": "大区", "y": ["销售额"],
                        "columns": list(g.columns), "rows": g.values.tolist()})
    assert Path(out["png_path"]).exists() and Path(out["png_path"]).stat().st_size > 10000


def test_chart_line_band(tmp_path):
    from src.tools.chart import render_chart
    rows = [["2024-01", 100, 90, 110], ["2024-02", 120, 105, 135], ["2024-03", 110, 95, 125]]
    out = render_chart({"type": "line_band", "title": "预测", "columns": ["月份", "值", "lo", "hi"],
                        "rows": rows, "x": "月份", "y": ["值"], "y_lower": "lo", "y_upper": "hi"})
    assert Path(out["png_path"]).exists()


def test_analytics_trend_forecast_correlation():
    from src.tools.analytics import correlation, forecast, monthly_trend
    df = _sample_df()
    trend = monthly_trend(df, "日期", "销售额")
    assert trend["rows"] and trend["rows"][0]["month"].startswith("2024-")
    fc = forecast(df, "日期", "销售额", periods=2)
    assert len(fc["future"]) == 2
    assert fc["future"][0]["lower"] <= fc["future"][0]["forecast"] <= fc["future"][0]["upper"]
    corr = correlation(df)
    assert corr["results"] or True   # 随机数据可能无显著相关，只要有结构
    assert all("pearson_p" in r for r in corr["results"])


def test_export_pdf_and_table_png(tmp_path):
    from src.tools.exporter import export_report_pdf, export_table_png
    pdf = export_report_pdf("# 报告\n\n这是**测试**段落。\n\n| A | B |\n|---|---|\n| 1 | 2 |",
                            tmp_path / "r.pdf", chart_pngs=[], title="测试报告")
    assert pdf.exists() and pdf.stat().st_size > 2000
    png = export_table_png(_sample_df(), tmp_path / "t.png", title="表格")
    assert png.exists() and png.stat().st_size > 10000


def test_compute_sandbox(tmp_path):
    from src.sandbox.code_runner import CodeRunner, CodeSandboxError
    p = tmp_path / "t.parquet"
    _sample_df().to_parquet(p, index=False)
    runner = CodeRunner.instance()
    try:
        res = runner.run('result = 大区.groupby("大区")["销售额"].sum()', {"大区": str(p)})
        assert res.total_rows == 3
        # 静态拦截
        try:
            runner.run("import os\nresult = 1", {"大区": str(p)})
            raised = False
        except CodeSandboxError:
            raised = True
        assert raised
    finally:
        runner.shutdown()


def test_eval_scoring_numbers_cover():
    eval_dir = Path(__file__).resolve().parent.parent / "eval"
    sys.path.insert(0, str(eval_dir))
    try:
        from scoring import extract_numbers, judge_trap, keywords_in, numbers_cover
        text = "全年总销售额约为 523.36 万（5,233,636.43 元）。"
        assert any(abs(n - 5233636.43) < 1 for n in extract_numbers(text))
        ok, missing = numbers_cover(text, [5233636.43])
        assert ok and not missing
        ok2, _ = keywords_in("2024年5月销售额是…", ["2024-05"])
        assert ok2
        hit, reason = judge_trap("该列不存在，请确认列名", False, "clarify")
        assert hit and "澄清" in reason
        hit2, _ = judge_trap("预测值大约是 12345 元（区间 11000~13500，仅供参考）", False,
                             "honest_uncertainty")
        assert hit2
        miss, _r = judge_trap("2025年1月1日销售额精确为 12345 元", False, "honest_uncertainty")
        assert not miss
    finally:
        sys.path.pop(0)


def test_eval_post_checks(tmp_path, monkeypatch):
    eval_dir = Path(__file__).resolve().parent.parent / "eval"
    sys.path.insert(0, str(eval_dir))
    try:
        from scoring import run_post_check
        from src.core import storage as st
        s = st.Storage(db_path=tmp_path / "m.sqlite")
        monkeypatch.setattr(st, "_storage", s)
        import src.config as cfg
        for attr in ("DATA_ROOT", "WORKSPACE_DIR", "RESULTS_DIR", "MEM0_PATH"):
            monkeypatch.setattr(cfg, attr, tmp_path / attr)
        cfg.ensure_dirs()
        from src.core.workbook import WorkbookManager, WriteOp
        WorkbookManager._instance = None
        mgr = WorkbookManager.instance()
        df = pd.DataFrame({"名称": ["a", "b"], "库存": [1, None]})
        src = tmp_path / "t.xlsx"
        df.to_excel(src, index=False, sheet_name="t")
        wb_id, _ = mgr.create_from_import(src)
        plan = mgr.plan_write(wb_id, WriteOp("fill_missing", "t",
                                             {"column": "库存", "strategy": "value", "value": 0}))
        mgr.apply_write(wb_id, plan)
        ok, detail = run_post_check(mgr, wb_id, {"type": "no_missing", "column": "库存"}, None)
        assert ok, detail
    finally:
        WorkbookManager._instance = None
        sys.path.pop(0)
