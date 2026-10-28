"""Gradio 三区界面（VSCode 套壳风格 · 浅色工作台）。

布局：标题栏 / 活动栏 / 资源管理器（左）/ 对话编辑器（中）/ 检查器面板（右）/ 状态栏。
图标：Lucide 图标集（ISC License），以内联 SVG mask 形式使用，不使用 emoji。

业务事件与输出元数保持不变；新增仅：面板视图切换、侧栏开关、清空对话、状态栏模式联动。
"""

from __future__ import annotations

import html
import logging
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import gradio as gr
import pandas as pd

from src import config
from src.agents.events import AgentEvents
from src.core.workbook import WorkbookManager
from src.memory.longterm import get_longterm_memory
from src.service import AgentService, TurnResult
from src.tools.exporter import export_report_pdf, export_table_png

logger = logging.getLogger(__name__)

SAMPLE_CHOICES = {
    "销售台账（含缺失/重复/多表）": "samples/sales_orders.xlsx",
    "员工考勤表（文本日期/工时）": "samples/employee_attendance.xlsx",
    "问卷结果（GBK 编码 CSV）": "samples/survey.csv",
}
DIRTY_CHOICES = {
    "脏表 1：合并单元格": "samples/dirty/merged_cells.xlsx",
    "脏表 2：两行表头": "samples/dirty/multi_header.xlsx",
    "脏表 3：中文列名（GB2312）": "samples/dirty/chinese_cols.csv",
}
TEMPLATE_CHOICES: dict[str, str] = {}   # 启动时填充 {显示名: template_id}

MODE_CHOICES = {
    "M2 · 完整反思": "m2",
    "M1 · 仅规则校验": "m1",
    "M0 · 无反思": "m0",
}

VIEW_CHOICES = ["预览", "图表", "反思", "记录", "报告", "记忆"]


def _new_session_id() -> str:
    return f"s_{uuid.uuid4().hex[:10]}"


def _svc(sid: str) -> AgentService:
    return AgentService(sid)


# ---------------------------------------------------------------------------
# 图标（Lucide, ISC License）：内联 SVG → CSS mask，颜色随 currentColor
# ---------------------------------------------------------------------------
_LUCIDE: dict[str, str] = {
    "database": "<ellipse cx='12' cy='5' rx='9' ry='3'/><path d='M3 5v14a9 3 0 0 0 18 0V5'/>"
                "<path d='M3 12a9 3 0 0 0 18 0'/>",
    "undo": "<path d='M9 14 4 9l5-5'/><path d='M4 9h10.5a5.5 5.5 0 0 1 5.5 5.5v0a5.5 5.5 0 0 1-5.5 5.5H11'/>",
    "redo": "<path d='m15 14 5-5-5-5'/><path d='M20 9H9.5A5.5 5.5 0 0 0 4 14.5v0A5.5 5.5 0 0 0 9.5 20H13'/>",
    "download": "<path d='M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4'/>"
                "<polyline points='7 10 12 15 17 10'/><line x1='12' x2='12' y1='15' y2='3'/>",
    "upload": "<path d='M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4'/>"
              "<polyline points='17 8 12 3 7 8'/><line x1='12' x2='12' y1='3' y2='15'/>",
    "table": "<rect width='18' height='18' x='3' y='3' rx='2'/><path d='M3 9h18'/><path d='M3 15h18'/>"
             "<path d='M9 3v18'/>",
    "chart": "<path d='M3 3v18h18'/><path d='M18 17V9'/><path d='M13 17V5'/><path d='M8 17v-3'/>",
    "activity": "<path d='M22 12h-4l-3 9L9 3l-3 9H2'/>",
    "terminal": "<polyline points='4 17 10 11 4 5'/><line x1='12' x2='20' y1='19' y2='19'/>",
    "file-text": "<path d='M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z'/>"
                 "<path d='M14 2v4a2 2 0 0 0 2 2h4'/><path d='M10 9H8'/><path d='M16 13H8'/>"
                 "<path d='M16 17H8'/>",
    "file-sheet": "<path d='M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z'/>"
                  "<path d='M14 2v4a2 2 0 0 0 2 2h4'/><path d='M8 13h2'/><path d='M14 13h2'/>"
                  "<path d='M8 17h2'/><path d='M14 17h2'/>",
    "image": "<rect width='18' height='18' x='3' y='3' rx='2' ry='2'/><circle cx='9' cy='9' r='2'/>"
             "<path d='m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21'/>",
    "check": "<path d='M20 6 9 17l-5-5'/>",
    "x": "<path d='M18 6 6 18'/><path d='m6 6 12 12'/>",
    "send": "<path d='m5 12 7-7 7 7'/><path d='M12 19V5'/>",
    "save": "<line x1='22' x2='2' y1='12' y2='12'/>"
            "<path d='M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z'/>"
            "<line x1='6' x2='6.01' y1='16' y2='16'/><line x1='10' x2='10.01' y1='16' y2='16'/>",
    "plus": "<path d='M5 12h14'/><path d='M12 5v14'/>",
    "trash": "<path d='M3 6h18'/><path d='M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6'/>"
             "<path d='M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2'/>",
    "panel": "<rect width='18' height='18' x='3' y='3' rx='2'/><path d='M15 3v18'/>",
    "folder": "<path d='M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z'/>",
}


def _mask_url(name: str) -> str:
    svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' "
           "stroke='black' stroke-width='1.7' stroke-linecap='round' stroke-linejoin='round'>"
           f"{_LUCIDE[name]}</svg>")
    return f'url("data:image/svg+xml;utf8,{quote(svg, safe="")}")'


_ICON_CLASSES = {
    "database": "ic-database", "undo": "ic-undo", "redo": "ic-redo", "download": "ic-download",
    "upload": "ic-upload", "table": "ic-table", "chart": "ic-chart", "activity": "ic-activity",
    "terminal": "ic-terminal", "file-text": "ic-file-text", "file-sheet": "ic-file-sheet",
    "image": "ic-image", "check": "ic-check", "x": "ic-x", "send": "ic-send",
    "save": "ic-save", "plus": "ic-plus", "trash": "ic-trash", "panel": "ic-panel",
    "folder": "ic-folder",
}


def _icon_css() -> str:
    return "\n".join(
        f".{cls}::before {{ --m: {_mask_url(name)}; }}"
        for name, cls in _ICON_CLASSES.items()
    )


def _logo_svg(size: int = 18) -> str:
    return (
        f"<svg width='{size}' height='{size}' viewBox='0 0 24 24' fill='none' "
        "stroke='currentColor' stroke-width='1.7' stroke-linecap='round' stroke-linejoin='round'>"
        "<rect x='3' y='3' width='18' height='18' rx='4'/>"
        "<path d='M3 9h18'/><path d='M9 9v12'/>"
        "<circle cx='16.5' cy='15.5' r='1.6' fill='currentColor' stroke='none'/>"
        "</svg>"
    )


# ---------------------------------------------------------------------------
# 面板刷新
# ---------------------------------------------------------------------------
def refresh_workbook_panel(sid: str):
    """返回 (sheet 下拉, 数据卡片 HTML, 预览 DF, 推荐问题 samples, 历史 MD, 图表占位)。"""
    empty = (gr.update(), "尚未加载数据", _df_table_html(None), gr.update(choices=[]),
             "（暂无写操作）", [])
    if not sid:
        return empty
    svc = _svc(sid)
    wb_id = svc.workbook_id
    if not wb_id:
        return empty
    manager = WorkbookManager.instance()
    names = manager.list_sheet_names(wb_id)
    if not names:
        return empty
    sheet = svc.wm.get(sid, "active_sheet") or names[0]
    if sheet not in names:
        sheet = names[0]
    from src.tools.toolkit import Toolkit
    tk = Toolkit(wb_id, sid)
    try:
        info = tk.inspect(sheet)
        df = manager.get_df(wb_id, sheet)
    except Exception as e:  # noqa: BLE001
        return (gr.update(choices=names, value=sheet), f"读取失败：{e}", pd.DataFrame(),
                [], "（暂无写操作）", [])
    card = _data_card(info, df, names, sheet)
    preview = df.head(100)
    recs = tk.recommend_questions(sheet)
    history = manager.history(wb_id)
    hist_md = "\n".join(
        f"`{h['seq']:>2}` {h['description']} · {h['state']}"
        for h in reversed(history[:12])) or "（暂无写操作）"
    return (gr.update(choices=names, value=sheet), card, _df_table_html(df),
            gr.update(choices=recs), hist_md, [])


def _fmt_cell(v) -> str:
    """单元格展示格式：日期去时间、浮点去尾零。"""
    if v is None:
        return ""
    if isinstance(v, float):
        if v != v:
            return ""
        return str(int(v)) if v.is_integer() else f"{v:.4f}".rstrip("0").rstrip(".")
    try:
        import pandas as pd
        if isinstance(v, pd.Timestamp):
            if v.hour == 0 and v.minute == 0 and v.second == 0:
                return v.strftime("%Y-%m-%d")
            return v.strftime("%Y-%m-%d %H:%M")
    except ImportError:
        pass
    return str(v)


def _is_number_text(text: str) -> bool:
    if not text:
        return False
    try:
        float(text.replace(",", ""))
        return True
    except ValueError:
        return False


def _df_table_html(df: pd.DataFrame | None, max_rows: int = 100) -> str:
    """自绘数据网格（VSCode 风格，避免 Gradio Dataframe 的默认外观与空表占位）。"""
    if df is None or len(df) == 0:
        return ("<div class='empty-state'><div class='empty-title'>暂无数据</div>"
                "<div class='empty-sub'>在左侧载入 Excel / CSV 或选择示例数据集后，这里显示前 "
                f"{max_rows} 行预览。</div></div>")
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    rows = []
    for i, row in enumerate(df.head(max_rows).itertuples(index=False), 1):
        cells = []
        for v in row:
            text = _fmt_cell(v)
            cls = "num" if _is_number_text(text) else ""
            cells.append(f"<td class='{cls}' title='{html.escape(text[:80])}'>{html.escape(text[:40])}</td>")
        rows.append(f"<tr><td class='rn'>{i}</td>{''.join(cells)}</tr>")
    return (f"<div class='grid-wrap'><table class='data-grid'><thead><tr>"
            f"<th class='rn'>#</th>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
            f"<div class='grid-foot'>显示前 {min(len(df), max_rows)} 行 / 共 {len(df)} 行 · "
            f"{len(df.columns)} 列</div>")


def _data_card(info: dict, df: pd.DataFrame, names: list[str], sheet: str) -> str:
    rows_html = ""
    for line in info["schema_prompt"].splitlines()[1:]:
        warn = "提示" in line
        rows_html += f"<div class='schema-row{' warn' if warn else ''}'>{html.escape(line)}</div>"
    others = "、".join(n for n in names if n != sheet) or "—"
    return (
        "<div class='data-card'>"
        f"<div class='card-head'><span class='card-title'>{html.escape(sheet)}</span>"
        f"<span class='card-meta'>{len(df)} 行 × {len(df.columns)} 列</span></div>"
        f"<div class='card-sub'>其他工作表：{html.escape(others)}</div>"
        f"<div class='schema-list'>{rows_html}</div>"
        f"<div class='card-note'>{html.escape(info['numeric_summary'][:150])}</div>"
        "</div>"
    )


# ---------------------------------------------------------------------------
# 事件处理：数据接入
# ---------------------------------------------------------------------------
def on_upload(sid, file, header_rows_text):
    if not file:
        return no_data_panel_update() + (sid, "")
    if not sid:
        sid = _new_session_id()
    svc = _svc(sid)
    try:
        header_map = None
        if header_rows_text and header_rows_text.strip():
            header_map = {}
            for part in header_rows_text.split(","):
                if ":" in part:
                    s, r = part.split(":", 1)
                    header_map[s.strip()] = int(r.strip())
        path = file if isinstance(file, str) else file.name
        wb_id, warnings = svc.load_file(path, header_rows=header_map)
        svc.wm.set(sid, "session_charts", [])
        note = f"导入成功：{Path(path).name}"
        if warnings:
            note += "\n\n" + "\n".join(f"- {w}" for w in warnings)
        panel = refresh_workbook_panel(sid)
        return panel + (sid, note)
    except Exception as e:  # noqa: BLE001
        return no_data_panel_update() + (sid, f"导入失败：{e}")


def on_load_demo(sid):
    """一键加载内置销售示例（演示与录屏用，无需切换 Tab）。"""
    return on_load_sample(sid, "销售台账（含缺失/重复/多表）")


def on_load_sample(sid, sample_name):
    if not sample_name:
        return no_data_panel_update() + (sid, "请先选择一个数据集")
    if not sid:
        sid = _new_session_id()
    svc = _svc(sid)
    path = SAMPLE_CHOICES.get(sample_name) or DIRTY_CHOICES.get(sample_name)
    wb_id, warnings = svc.load_file(str(config.PROJECT_ROOT / path))
    svc.wm.set(sid, "session_charts", [])
    note = f"已加载：{sample_name}"
    if warnings:
        note += "\n\n" + "\n".join(f"- {w}" for w in warnings)
    panel = refresh_workbook_panel(sid)
    return panel + (sid, note)


def on_create_template(sid, tpl_display, wb_name):
    if not tpl_display:
        return no_data_panel_update() + (sid, "请先选择模板")
    if not sid:
        sid = _new_session_id()
    import yaml
    tpl_id = TEMPLATE_CHOICES[tpl_display]
    tpl = yaml.safe_load((config.TEMPLATES_DIR / f"{tpl_id}.yaml").read_text(encoding="utf-8"))
    sheet = tpl["sheets"][0]
    svc = _svc(sid)
    wb_id = WorkbookManager.instance().create_from_template(
        name=wb_name or tpl["name"], sheet_name=sheet["name"], columns=sheet["columns"])
    svc.set_workbook(wb_id)
    svc.wm.set(sid, "session_charts", [])
    note = (f"已按模板「{tpl['name']}」创建空表「{sheet['name']}」。"
            f"可直接对话填数，例如：加一行 2024-07-01，华东，智能手表Pro，120000，45，张三")
    panel = refresh_workbook_panel(sid)
    return panel + (sid, note)


def no_data_panel_update():
    return (gr.update(choices=[], value=None), "尚未加载数据", _df_table_html(None),
            gr.update(choices=[]), "（暂无写操作）", [])


def on_sheet_change(sid, sheet_name):
    if not sid or not sheet_name:
        return no_data_panel_update()
    _svc(sid).wm.set(sid, "active_sheet", sheet_name)
    return refresh_workbook_panel(sid)


# ---------------------------------------------------------------------------
# 事件处理：对话
# ---------------------------------------------------------------------------
_ACTIVE_TURNS: dict[str, dict] = {}


def _stop_turn(sid: str):
    """点击“停止”：标记取消，当前 LLM 调用结束后立即停止后续步骤。"""
    entry = _ACTIVE_TURNS.get(sid)
    if entry:
        entry["events"].cancel()
        return gr.update(value="停止中…", interactive=False)
    return gr.update(value="停止", interactive=True)


def on_send(sid, message, chat_history, mode):
    """主对话入口（流式）。

    输出（16）：sid / chatbot / diff 卡 / gallery / 反思 / 记录 / 报告 /
    sheet / 数据卡片 / 预览 / 推荐问题 / 历史 / 面板视图 / 确认按钮行 / 输入框 / 停止按钮。
    """
    idle = (sid, chat_history, gr.update(visible=False, value=""), gr.update(), "", "", "",
            gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
            gr.update(visible=False), "", gr.update(visible=False, value="停止", interactive=True))
    if not message or not message.strip():
        yield idle
        return
    if not sid:
        sid = _new_session_id()

    entry = _ACTIVE_TURNS.get(sid)
    if entry and entry["thread"].is_alive():
        chat_history = list(chat_history or []) + [
            {"role": "assistant", "content": "上一条任务仍在执行，请先点击“停止”或稍候再发送。"}]
        yield (sid, chat_history, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
               gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
               gr.update(visible=False), "", gr.update(visible=True, value="停止", interactive=True))
        return

    svc = _svc(sid)
    chat_history = list(chat_history or []) + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": "正在规划与执行…"},
    ]
    events = AgentEvents()
    box: dict = {}

    def _worker() -> None:
        try:
            box["result"] = svc.chat(message.strip(), mode=mode, events=events)
        except Exception as e:  # noqa: BLE001
            logger.exception("对话异常")
            box["error"] = e
        finally:
            events.emit("done", "")

    thread = threading.Thread(target=_worker, daemon=True)
    _ACTIVE_TURNS[sid] = {"events": events, "thread": thread}
    thread.start()

    def _live(content: str, stop_visible: bool):
        snapshot = [dict(m) for m in chat_history]
        snapshot[-1] = {"role": "assistant", "content": content}
        return (sid, snapshot, gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
                gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
                gr.update(visible=False), "",
                gr.update(visible=stop_visible,
                           value=("停止中…" if events.cancelled() else "停止"),
                           interactive=not events.cancelled()))

    live_text = ""
    tokens: list[str] = []
    last_content = None
    try:
        while True:
            for item in events.drain():
                if item["kind"] == "stage":
                    live_text = item["text"]
                    tokens = []
                elif item["kind"] == "token":
                    tokens.append(item["text"])
            if tokens:
                content = "".join(tokens)
            elif live_text:
                content = f"正在执行：{live_text}…"
            else:
                content = "正在规划与执行…"
            if content != last_content:
                last_content = content
                yield _live(content, stop_visible=True)
            if not thread.is_alive() and events.empty():
                break
            time.sleep(0.12)
    finally:
        _ACTIVE_TURNS.pop(sid, None)

    if "error" in box:
        chat_history[-1] = {"role": "assistant", "content": f"处理出错：{box['error']}"}
        yield (sid, [dict(m) for m in chat_history], gr.update(), gr.update(), gr.update(),
               gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
               gr.update(), gr.update(), gr.update(visible=False), "", gr.update(visible=False, value="停止", interactive=True))
        return

    result = box.get("result")
    if result is None:
        chat_history[-1] = {"role": "assistant", "content": "处理结束，但没有返回结果。"}
        yield (sid, [dict(m) for m in chat_history], gr.update(), gr.update(), gr.update(),
               gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
               gr.update(), gr.update(), gr.update(visible=False), "", gr.update(visible=False, value="停止", interactive=True))
        return

    answer = result.response
    if result.reflection_log:
        answer += "\n\n<details><summary>反思过程</summary>\n\n" + \
                  "\n".join(f"- {line}" for line in result.reflection_log) + "\n</details>"
    chat_history[-1] = {"role": "assistant", "content": answer}

    current_charts = result.artifacts.get("charts") or []
    charts = result.artifacts.get("all_charts") or current_charts
    gallery = [(p, Path(p).stem) for p in charts]
    report_md = _last_report_markdown(result)
    reflection_md = _reflection_markdown(result)
    trace_md = _trace_markdown(result)
    confirm_html = result.pending_write.get("diff_html") if (result.confirm_required and result.pending_write) else ""
    panel = refresh_workbook_panel(sid)

    if current_charts:
        view_update = gr.update(value="图表")
    elif result.intent in ("report", "complex"):
        view_update = gr.update(value="报告")
    else:
        view_update = gr.update()

    yield (sid, [dict(m) for m in chat_history],
           gr.update(visible=bool(confirm_html), value=confirm_html),
           gallery, reflection_md, trace_md, report_md,
           panel[0], panel[1], panel[2], panel[3], panel[4], view_update,
           gr.update(visible=bool(confirm_html)), "", gr.update(visible=False))


def _last_report_markdown(result) -> str:
    if result.intent in ("report", "complex") and result.response:
        from datetime import datetime
        return f"# 分析报告\n\n*生成时间：{datetime.now():%Y-%m-%d %H:%M}*\n\n{result.response}"
    return ""


def _reflection_markdown(result) -> str:
    lines = [f"意图 `{result.intent or '—'}` · 重试 `{result.retries}`"]
    lines += [f"- {line}" for line in result.reflection_log]
    if result.plan:
        lines.append(f"- 规划：{result.plan.get('reasoning', '')}")
    return "\n".join(lines) or "本回合无反思事件"


def _trace_markdown(result) -> str:
    lines = []
    for t in result.trace:
        tool = t.get("tool", "?")
        args = {k: v for k, v in (t.get("args") or {}).items() if k != "sql"}
        lines.append(f"**{tool}** {args}")
        if "sql" in (t.get("args") or {}):
            lines.append(f"```sql\n{t['args']['sql']}\n```")
        if t.get("res_id"):
            lines.append(f"结果编号 `{t['res_id']}` · {t.get('total_rows', '?')} 行")
    return "\n\n".join(lines) or "本回合没有工具调用"


def on_confirm(sid, chat_history):
    if not sid:
        return (sid, chat_history, gr.update(visible=False, value=""), _df_table_html(None),
                "（暂无写操作）", gr.update(visible=False))
    svc = _svc(sid)
    result = svc.confirm_write()
    chat_history = list(chat_history or []) + [{"role": "assistant", "content": result.response}]
    panel = refresh_workbook_panel(sid)
    keep = result.pending_write.get("diff_html") if (result.confirm_required and result.pending_write) else ""
    return (sid, chat_history, gr.update(visible=bool(keep), value=keep), panel[2], panel[4],
            gr.update(visible=bool(keep)))


def on_cancel(sid, chat_history):
    if not sid:
        return (sid, chat_history, gr.update(visible=False, value=""), gr.update(visible=False),
            gr.update(visible=False))
    result = _svc(sid).cancel_write()
    if result is None:
        return sid, chat_history, "", gr.update(visible=False)
    chat_history = list(chat_history or []) + [{"role": "assistant", "content": result.response}]
    return sid, chat_history, "", gr.update(visible=False)


def on_undo(sid, chat_history):
    return _version_action(sid, chat_history, lambda s: s.undo())


def on_redo(sid, chat_history):
    return _version_action(sid, chat_history, lambda s: s.redo())


def on_goto(sid, chat_history, n):
    return _version_action(sid, chat_history, lambda s: s.goto(int(n or 0)))


def on_snapshot_save(sid, name, chat_history):
    return _version_action(sid, chat_history,
                           lambda s: s.save_snapshot(name or "未命名快照"))


def _version_action(sid, chat_history, action):
    if not sid:
        return (sid, chat_history, "尚未加载数据", _df_table_html(None), "（暂无写操作）")
    try:
        msg = action(_svc(sid))
    except Exception as e:  # noqa: BLE001
        msg = str(e)
    chat_history = list(chat_history or []) + [{"role": "assistant", "content": msg}]
    panel = refresh_workbook_panel(sid)
    return (sid, chat_history, panel[1], panel[2], panel[4])


def on_export(sid, fmt, report_md, gallery):
    if not sid:
        return None, "请先加载数据"
    svc = _svc(sid)
    from datetime import datetime
    out_dir = config.WORKSPACE_DIR / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        if fmt == "xlsx":
            path = WorkbookManager.instance().export_xlsx(svc.workbook_id, out_dir / f"导出_{stamp}.xlsx")
        elif fmt == "pdf":
            charts = [g[0] if isinstance(g, (list, tuple)) else g for g in (gallery or [])]
            path = export_report_pdf(report_md or "# 空报告", out_dir / f"报告_{stamp}.pdf",
                                     chart_pngs=charts)
        else:
            df = WorkbookManager.instance().get_df(svc.workbook_id, svc.wm.get(sid, "active_sheet"))
            path = export_table_png(df, out_dir / f"表格_{stamp}.png")
        return str(path), f"已导出：{path.name}"
    except Exception as e:  # noqa: BLE001
        return None, f"导出失败：{e}"


# ---------------------------------------------------------------------------
# 事件处理：IDE 交互（视图切换 / 侧栏开关 / 清空对话 / 状态栏）
# ---------------------------------------------------------------------------
def on_switch_view(view: str, sid: str):
    updates = [gr.update(visible=(v == view)) for v in VIEW_CHOICES]
    memory = on_memory_refresh(sid) if view == "记忆" else gr.update()
    return (*updates, memory)


def _session_choices(limit: int = 30) -> list[tuple[str, str]]:
    """历史会话下拉项：标题 · 更新时间 · 工作簿。"""
    from src.core.storage import get_storage
    out: list[tuple[str, str]] = []
    for r in get_storage().list_sessions(limit):
        title = (r["title"] or "未命名会话").strip()
        when = (r["updated_at"] or r["created_at"] or "")[:16].replace("T", " ")
        wb = r["workbook_name"] or "无数据"
        out.append((f"{title} · {when} · {wb}", r["session_id"]))
    return out


def _snap_choices(sid: str) -> list[str]:
    if not sid:
        return []
    svc = _svc(sid)
    if not svc.workbook_id:
        return []
    return [r["name"] for r in svc.storage.list_snapshots(svc.workbook_id)]


def on_sessions_refresh(sid):
    return gr.update(choices=_session_choices(), value=None)


def on_delete_session(sid, selected, confirm):
    if not selected:
        return gr.update(), "请先选择要删除的会话。", gr.update(value=False)
    if not confirm:
        return gr.update(), "请先勾选“确认删除选中会话”。", gr.update(value=False)
    if selected == sid:
        return gr.update(), "当前打开的会话不能删除；请先点「新建会话」。", gr.update(value=False)
    try:
        _svc(selected).delete_session(selected)
    except Exception as e:  # noqa: BLE001
        return gr.update(), f"删除失败：{e}", gr.update(value=False)
    return (gr.update(choices=_session_choices(), value=None),
            "已删除该会话（聊天记录 / 记忆 / 结果索引）。", gr.update(value=False))


def _session_noop(sid, chat_history, note):
    return (sid, chat_history) + tuple(gr.update() for _ in range(20)) + (
        note, gr.update(visible=False), gr.update())


def _render_session(new_sid: str, note: str = ""):
    """把历史会话恢复成界面状态：聊天记录、工作簿、图表、反思/记录摘要。"""
    svc = AgentService(new_sid)
    msgs = svc.session_messages(new_sid)
    chat = [{"role": m["role"], "content": m["content"]}
            for m in msgs if m["role"] in ("user", "assistant")]
    last_meta = next((m["meta"] for m in reversed(msgs)
                      if m["role"] == "assistant" and m.get("meta")), None) or {}
    probe = TurnResult(response="",
                       intent=last_meta.get("intent", ""),
                       retries=int(last_meta.get("retries") or 0),
                       reflection_log=last_meta.get("reflection_log") or [],
                       trace=last_meta.get("trace") or [])
    reflection_md = _reflection_markdown(probe) if last_meta else "（该会话没有反思记录）"
    trace_md = _trace_markdown(probe) if last_meta else "（该会话没有查询记录）"
    charts = svc.wm.get(new_sid, "session_charts", []) or []
    gallery = [(c, Path(c).stem) for c in charts]
    panel = refresh_workbook_panel(new_sid)
    row = svc.storage.get_session(new_sid)
    title = (row["title"] if row else None) or "未命名会话"
    note = note or f"已打开会话：{title}（{len(chat)} 条消息）"
    return (new_sid, chat,
            panel[0], panel[1], panel[2], panel[3], panel[4], gallery,
            gr.update(visible=False, value=""), gr.update(visible=False),
            reflection_md, trace_md, "",
            "（切换到「记忆」页查看）",
            gr.update(value="预览"),
            gr.update(visible=True), gr.update(visible=False), gr.update(visible=False),
            gr.update(visible=False), gr.update(visible=False), gr.update(visible=False),
            None, note,
            gr.update(visible=False, value="停止", interactive=True),
            gr.update(choices=_session_choices(), value=new_sid))


def on_open_session(sid, chat_history, selected):
    if not selected:
        return _session_noop(sid, chat_history, "请先在列表中选择一个历史会话。")
    if sid and sid != selected:
        _stop_turn(sid)
        try:
            _svc(sid).cancel_write()
        except Exception:  # noqa: BLE001
            pass
    try:
        return _render_session(selected)
    except Exception as e:  # noqa: BLE001
        return _session_noop(sid, chat_history, f"打开会话失败：{e}")


def on_snap_refresh(sid):
    return gr.update(choices=_snap_choices(sid), value=None)


def on_snapshot_save2(sid, name, chat_history):
    sid2, chat2, card, preview, hist = on_snapshot_save(sid, name, chat_history)
    return sid2, chat2, card, preview, hist, gr.update(
        choices=_snap_choices(sid2), value=(name or None))


def on_snap_restore(sid, name, chat_history):
    if not sid or not name:
        return sid, chat_history, gr.update(), gr.update(), gr.update(), "请先选择要恢复的快照。"
    try:
        msg = _svc(sid).restore_snapshot(name)
    except Exception as e:  # noqa: BLE001
        msg = f"恢复失败：{e}"
    chat_history = list(chat_history or []) + [{"role": "assistant", "content": msg}]
    panel = refresh_workbook_panel(sid)
    return sid, chat_history, panel[1], panel[2], panel[4], msg


def on_memory_refresh(sid):
    """记忆面板：L3 长期偏好 + L2 工作记忆 + L1 会话摘要。"""
    ltm = get_longterm_memory()
    lines = ["### L3 长期记忆（Mem0：用户偏好 + 会话事实）", ""]
    if ltm.available:
        mems = ltm.list_all(limit=20)
        lines.append("状态：已启用（本地向量库）")
        if mems:
            lines.append("")
            for m in mems:
                lines.append(f"- {m.get('memory', '')}")
        else:
            lines.append("")
            lines.append("暂无长期记忆。说“以后都用万元 / 默认用柱状图”这类偏好时会写入。")
    else:
        lines.append(f"状态：降级关闭（{ltm.disabled_reason[:100]}）")
    if sid:
        svc = _svc(sid)
        snap = svc.wm.snapshot(sid)
        lines += ["", "### L2 工作记忆（会话）", ""]
        lines.append(f"- 当前工作簿：{snap.get('active_workbook') or '—'}")
        lines.append(f"- 当前工作表：{snap.get('active_sheet') or '—'}")
        last_q = snap.get("last_query") or ""
        shown = last_q[:120] + "…" if len(last_q) > 120 else last_q
        lines.append(f"- 上一条查询：`{shown or '—'}`")
        lines.append(f"- 上次结果：{snap.get('last_result_ref') or '—'}")
        l1 = svc.storage.kv_get(f"session:{sid}", "l1_summary", "") or ""
        lines += ["", "### L1 会话摘要", "", l1[:600] if l1 else "（尚未触发滚动压缩）"]
    return "\n".join(lines)


def on_toggle_sidebar(visible: bool):
    new_visible = not bool(visible)
    return gr.update(visible=new_visible), new_visible


def on_new_session(sid, chat_history):
    """新建会话：停止当前任务、丢弃当前会话的 workbook/记忆/图表绑定。

    新会话使用全新的 session_id；下一次导入/示例/提问会自动创建新会话，
    与旧会话的 workbook、L2/L3 记忆、图表完全隔离。
    """
    if sid:
        _stop_turn(sid)
        try:
            _svc(sid).cancel_write()
        except Exception:  # noqa: BLE001
            pass
    panel = no_data_panel_update()
    return ("", [], panel[0], panel[1], panel[2], panel[3], panel[4], [],
            gr.update(visible=False, value=""), gr.update(visible=False),
            "本回合无反思事件", "本回合没有工具调用", "",
            "（切换到「记忆」页查看）",
            gr.update(value="预览"),
            gr.update(visible=True), gr.update(visible=False), gr.update(visible=False),
            gr.update(visible=False), gr.update(visible=False), gr.update(visible=False),
            None, "已新建会话。请加载 Excel/CSV 或选择示例后开始分析。",
            gr.update(visible=False, value="停止", interactive=True),
            gr.update(choices=_session_choices(), value=None))


def on_clear_chat(sid, chat_history):
    if sid:
        _stop_turn(sid)
        try:
            svc = _svc(sid)
            svc.cancel_write()
            svc.clear_conversation()
        except Exception:  # noqa: BLE001
            pass
    return ([], gr.update(visible=False, value=""), "本回合无反思事件",
            "本回合没有工具调用", "", gr.update(visible=False), [],
            "对话已清空（同时清除该会话的记录、摘要与图表）。")


def _status_html(mode_label: str) -> str:
    ltm = get_longterm_memory()
    mem = "L3 记忆 已启用" if ltm.available else "L3 记忆 已降级"
    mem_cls = "ok" if ltm.available else "warn"
    return (
        "<div class='statusbar'>"
        "<span class='sb-item'>本地会话</span>"
        "<span class='sb-sep'></span>"
        f"<span class='sb-item'>{html.escape(mode_label)}</span>"
        "<span class='sb-sep'></span>"
        f"<span class='sb-item {mem_cls}'>{mem}</span>"
        "<span class='sb-spacer'></span>"
        f"<span class='sb-item mono'>{html.escape(config.OPENAI_MODEL)}</span>"
        "<span class='sb-sep'></span>"
        "<span class='sb-item'>127.0.0.1:7860</span>"
        "</div>"
    )


def _titlebar_html() -> str:
    return (
        "<div class='tb-left'>"
        f"<span class='tb-logo'>{_logo_svg(18)}</span>"
        "<span class='tb-title'>表格管家</span>"
        "<span class='tb-sub'>Excel Agent</span>"
        "<span class='tb-chip'>本地工作区</span>"
        "</div>"
    )


def _panel_head(title: str, hint: str = "") -> str:
    hint_html = f"<span class='panel-hint'>{html.escape(hint)}</span>" if hint else ""
    return f"<div class='panel-head'><span>{html.escape(title)}</span>{hint_html}</div>"


def _side_head(title: str, hint: str = "") -> str:
    hint_html = f"<span class='side-hint'>{html.escape(hint)}</span>" if hint else ""
    return f"<div class='side-head'><span>{html.escape(title)}</span>{hint_html}</div>"


# ---------------------------------------------------------------------------
# 样式
# ---------------------------------------------------------------------------
def _build_css() -> str:
    return """
:root {
  --chrome:#f3f3f3; --sidebar:#f8f8f8; --editor:#ffffff; --panel:#ffffff;
  --border:#e4e4e4; --border-strong:#d2d2d2;
  --text:#1f2328; --text-2:#5c6268; --muted:#8b9096;
  --accent:#0f6b4f; --accent-soft:#e8f2ee;
  --hover:#ececec; --active:#e3e3e3;
  --btn:#14181d; --btn-hover:#2a3037;
  --warn:#8a5a00; --warn-bg:#fff6e0;
  --mono:"Cascadia Code","Consolas","SFMono-Regular","JetBrains Mono",monospace;
  --ui:"Segoe UI Variable Text","Segoe UI","Microsoft YaHei UI","PingFang SC",system-ui,sans-serif;
  --radius:5px;
}

html, body { background: var(--sidebar) !important; height: 100%; overflow: hidden !important; }
.gradio-container {
  max-width: 100% !important; height: 100vh; overflow: hidden; padding: 0 !important;
  background: var(--sidebar) !important; font-family: var(--ui); color: var(--text);
  /* Gradio 6 主题变量覆盖：统一成 VSCode 式紧凑控件 */
  --background-fill-primary: #ffffff;
  --background-fill-secondary: #f8f8f8;
  --block-background-fill: transparent;
  --block-border-color: transparent;
  --block-border-width: 0;
  --block-shadow: none;
  --block-radius: 0;
  --input-background-fill: #ffffff;
  --input-border-color: #d2d2d2;
  --input-radius: 4px;
  --button-primary-background-fill: #14181d;
  --button-primary-text-color: #ffffff;
  --button-secondary-background-fill: #f3f3f3;
  --button-secondary-text-color: #1f2328;
  --button-secondary-border-color: #d2d2d2;
  --border-color-primary: #e4e4e4;
  --border-color-accent: #0f6b4f;
  --color-accent: #0f6b4f;
  --color-accent-soft: #e8f2ee;
  --body-text-color: #1f2328;
  --body-text-color-subdued: #5c6268;
  --block-label-text-color: #5c6268;
  --block-info-text-color: #5c6268;
  --block-title-text-color: #1f2328;
  --input-placeholder-color: #9aa0a6;
  --text-md: 13px;
  --text-sm: 12px;
  --text-lg: 14px;
}
.gradio-container .prose { color: var(--text); }
footer, .built-with { display: none !important; }
.gradio-container .main.fillable, .gradio-container .main.app {
  padding: 0 !important; height: 100vh !important; overflow: hidden !important;
  max-width: none !important; width: 100% !important; }
.gradio-container main.contain { padding: 0 !important; display: flex; flex-direction: column;
  gap: 0 !important; height: 100vh; overflow: hidden; }
.gradio-container main.contain > .column { gap: 0 !important; }
.gradio-container .wrap { padding: 0; }
.ide-titlebar, .ide-shell, .ide-statusbar-wrap { margin: 0 !important; }

/* ---------- 图标 ---------- */
.ic-database::before, .ic-undo::before, .ic-redo::before, .ic-download::before,
.ic-upload::before, .ic-table::before, .ic-chart::before, .ic-activity::before,
.ic-terminal::before, .ic-file-text::before, .ic-file-sheet::before, .ic-image::before,
.ic-check::before, .ic-x::before, .ic-send::before, .ic-save::before, .ic-plus::before,
.ic-trash::before, .ic-panel::before, .ic-folder::before {
  content: ""; display: inline-block; width: 14px; height: 14px; flex: none;
  background-color: currentColor;
  -webkit-mask: var(--m) center / contain no-repeat;
  mask: var(--m) center / contain no-repeat;
}
__ICON_RULES__

/* ---------- 标题栏 ---------- */
.ide-titlebar { display: flex !important; flex-wrap: nowrap; align-items: center; gap: 8px;
  height: 44px; padding: 0 12px; background: var(--chrome); border-bottom: 1px solid var(--border);
  position: relative; z-index: 30; }
.ide-titlebar .block { border: none !important; background: transparent !important; padding: 0 !important; box-shadow: none !important; }
.tb-left { display: flex; align-items: center; gap: 10px; flex: 1; min-width: 0; }
.tb-logo { display: inline-flex; width: 22px; height: 22px; align-items: center; justify-content: center;
  color: var(--accent); background: var(--accent-soft); border-radius: 5px; }
.tb-title { font-size: 13px; font-weight: 600; letter-spacing: .01em; }
.tb-sub { font-size: 11.5px; color: var(--muted); padding-left: 2px; }
.tb-chip { font-size: 11px; color: var(--text-2); background: #fff; border: 1px solid var(--border-strong);
  padding: 1px 7px; border-radius: 10px; white-space: nowrap; }
.ide-titlebar .tb-mode { width: 168px; min-width: 168px; }
.ide-titlebar .tb-mode input, .ide-titlebar .tb-mode .wrap { font-size: 12px !important; height: 26px; min-height: 26px; }
.ide-titlebar .tb-icon-btn { font-size: 0 !important; width: 28px !important;
  min-width: 28px !important; height: 26px !important; padding: 0 !important;
  flex: 0 0 auto !important; display: inline-flex !important; align-items: center !important;
  justify-content: center !important; }
.ide-titlebar .tb-icon-btn::before { width: 14px !important; height: 14px !important; }
.ide-titlebar .tb-btn { min-width: auto !important; width: auto !important; flex: 0 0 auto !important;
  height: 26px !important; padding: 0 10px !important; font-size: 12px !important;
  white-space: nowrap !important; line-height: 1 !important; }

/* ---------- 工作台骨架 ---------- */
.ide-shell { display: flex !important; align-items: stretch !important; gap: 0 !important;
  height: calc(100vh - 70px); min-height: 520px; overflow: hidden; }
.ide-shell > .column { min-height: 0 !important; height: 100%;
  flex-wrap: nowrap !important; }
.ide-shell > .column { padding: 0 !important; }
.ide-shell .block { border: none !important; box-shadow: none !important; background: transparent !important; }
.ide-shell .form { gap: 6px !important; }
.ide-shell label { font-size: 11px !important; color: var(--text-2) !important; font-weight: 500; }
.ide-shell .block > .label-wrap, .ide-shell .block > label { margin-bottom: 2px; }

/* ---------- 活动栏 ---------- */
.ide-rail { flex: 0 0 56px !important; min-width: 56px !important; width: 56px !important;
  background: var(--chrome); border-right: 1px solid var(--border);
  display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 8px 0; }
.ide-rail .block { width: 40px; min-width: 40px; }
.rail-logo { display: flex; justify-content: center; padding: 4px 0 8px; color: var(--accent); }
.rail-sep { width: 24px; height: 1px; background: var(--border-strong); margin: 4px 0; }
.ide-shell .ide-rail .rail-btn { width: 40px !important; min-width: 40px !important;
  height: 40px !important; padding: 0 !important; font-size: 0 !important;
  border-radius: 6px !important; background: transparent !important;
  border: 1px solid transparent !important; box-shadow: none !important;
  color: var(--text-2) !important; display: flex; align-items: center; justify-content: center; }
.ide-shell .ide-rail .rail-btn::before { width: 19px !important; height: 19px !important; }
.ide-shell .ide-rail .rail-btn:hover { background: var(--hover) !important; color: var(--text) !important; }
.ide-shell .ide-rail .rail-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }

/* ---------- 资源管理器 ---------- */
.ide-sidebar > * { flex: 0 0 auto !important; }
.ide-sidebar { flex: 0 0 var(--sidebar-w, 320px) !important;
  width: var(--sidebar-w, 320px) !important; min-width: 260px !important;
  max-width: 520px !important; background: var(--sidebar);
  border-right: 1px solid var(--border); padding: 8px 10px 14px !important;
  overflow-y: auto !important; overflow-x: hidden; height: 100%;
  display: block !important; }
.side-head { display: flex; align-items: baseline; justify-content: space-between;
  font-size: 11px; font-weight: 600; letter-spacing: .07em; color: var(--text-2);
  margin: 12px 2px 5px; }
.side-head:first-of-type { margin-top: 2px; }
.side-hint { font-size: 10.5px; color: var(--muted); font-weight: 400; letter-spacing: 0; }
.ide-sidebar .block { margin: 0 0 4px !important; padding: 0 !important;
  background: transparent !important; border: none !important; box-shadow: none !important; }
.ide-sidebar .form { background: transparent !important; border: none !important;
  padding: 0 !important; gap: 4px !important; }
.ide-sidebar .wrap.default, .ide-sidebar .wrap-inner { background: #fff !important;
  border: 1px solid var(--border-strong) !important; border-radius: 4px !important;
  padding: 1px 7px !important; min-height: 28px !important; }
.ide-sidebar .row { flex-wrap: nowrap !important; gap: 6px !important; }
.ide-sidebar .row > *, .ide-sidebar .row .block { min-width: 0 !important; }
.ide-sidebar .row button { padding-left: 8px !important; padding-right: 8px !important; }
.ide-sidebar input[type="text"], .ide-sidebar input[type="number"] {
  background: #fff !important; border: 1px solid var(--border-strong) !important;
  border-radius: 4px !important; outline: none !important; min-height: 30px !important;
  padding: 4px 8px !important; font-size: 12px !important; }
.ide-sidebar input[type="text"]:focus, .ide-sidebar input[type="number"]:focus {
  border-color: var(--accent) !important; box-shadow: 0 0 0 2px var(--accent-soft) !important; }
.ide-sidebar textarea { background: #fff !important; border: 1px solid var(--border-strong) !important;
  border-radius: 4px !important; padding: 4px 8px !important; min-height: 30px !important;
  font-size: 12px !important; }
.ide-sidebar textarea:focus { border-color: var(--accent) !important;
  box-shadow: 0 0 0 2px var(--accent-soft) !important; }
.ide-sidebar .file-preview, .ide-sidebar .file { background: #fff !important; }
#uploader { padding: 0 !important; margin: 0 0 4px !important; background: transparent !important;
  border: none !important; border-style: none !important; border-radius: 0 !important; }
#uploader > div, #uploader .file-preview, #uploader .block {
  background: transparent !important; border: none !important; border-radius: 0 !important;
  padding: 0 !important; margin: 0 !important; }
#uploader .upload-container { min-height: 78px !important; height: 78px !important;
  background: #fff !important; border: 1px dashed var(--border-strong) !important;
  border-radius: 4px !important; }
#uploader .upload-container svg { width: 22px !important; height: 22px !important;
  color: var(--muted) !important; }
#uploader .upload-container, #uploader .upload-container span { font-size: 11.5px !important;
  line-height: 1.5 !important; }
#uploader .label, #uploader label { background: transparent !important;
  border: none !important; padding: 0 0 4px !important; font-size: 11px !important;
  color: var(--text-2) !important; position: static !important;
  display: flex !important; align-items: center; gap: 5px !important; height: auto !important; }
#uploader label svg { width: 14px !important; height: 14px !important; flex: none; }
#uploader label span { display: inline-flex !important; align-items: center; gap: 4px; }
#uploader .upload-container { display: flex !important; align-items: center !important;
  justify-content: center !important; text-align: center; }
#uploader [data-testid="upload-icon"] { display: none !important; }
#uploader .empty { display: none !important; }
#uploader [data-testid="upload-text"] { display: flex !important; flex-direction: column !important;
  align-items: center !important; justify-content: center !important; gap: 1px !important;
  font-size: 12px !important; color: var(--text-2) !important; }
#uploader [data-testid="upload-text"] .or { font-size: 10.5px !important;
  color: var(--muted) !important; }

#download-box { border: 1px solid var(--border) !important; border-radius: 4px !important;
  background: #fff !important; padding: 0 !important; }
#download-box label, #download-box [data-testid="block-label"] {
  padding: 4px 8px !important; color: var(--text-2) !important; font-size: 11px !important;
  background: transparent !important; border: none !important; }
#download-box .empty { min-height: 42px !important; padding: 4px !important;
  border: none !important; background: transparent !important; }
#download-box .empty .icon { display: none !important; }
#download-box .empty { font-size: 11px !important; color: var(--muted) !important; }
.ide-sidebar input[type="text"], .ide-sidebar textarea, .ide-sidebar .wrap-inner {
  font-size: 12px !important; }
.side-tabs .tab-container { border-bottom: 1px solid var(--border) !important; gap: 0 !important;
  background: transparent !important; margin-bottom: 6px; display: flex; }
.side-tabs .tab-container button { font-size: 12px !important; color: var(--text-2) !important;
  padding: 5px 8px !important; border-radius: 0 !important; background: transparent !important;
  border: none !important; }
.side-tabs .tab-container button.selected { color: var(--text) !important;
  border-bottom: 2px solid var(--accent) !important; font-weight: 500; }
.side-note { font-size: 11.5px; color: var(--text-2); line-height: 1.55; margin: 4px 0 6px; }
.side-note strong { color: var(--text); font-weight: 600; }

.data-card { border: 1px solid var(--border); background: #fff; border-radius: var(--radius);
  padding: 8px 10px; margin: 4px 0 6px; }
.card-head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.card-title { font-size: 12.5px; font-weight: 600; }
.card-meta { font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
.card-sub { font-size: 11px; color: var(--muted); margin-top: 2px; }
.schema-list { max-height: 150px; overflow: auto; margin-top: 6px; border-top: 1px solid var(--border); }
.schema-row { font-size: 11.5px; color: var(--text-2); padding: 3px 0; border-bottom: 1px dashed #f0f0f0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.schema-row.warn { color: var(--warn); background: var(--warn-bg); border-radius: 3px; padding: 3px 5px; }
.card-note { font-size: 11px; color: var(--muted); margin-top: 6px; }

/* 推荐问题（Radio 列表） */
.rec-list { border: 1px solid var(--border) !important; border-radius: var(--radius);
  background: #fff; margin-top: 2px; padding: 0 !important;
  max-height: 150px; overflow-y: auto; }
.rec-list .wrap { display: flex !important; flex-direction: column; gap: 0 !important; }
.rec-list label { display: flex; align-items: center; width: 100%; margin: 0 !important;
  padding: 6px 9px !important; font-size: 12px !important; color: var(--text-2) !important;
  border-bottom: 1px solid #f1f1f1; border-radius: 0 !important; background: transparent !important;
  cursor: pointer; }
.rec-list label:last-child { border-bottom: none; }
.rec-list label.selected { background: var(--accent-soft) !important; color: var(--text) !important; }
.rec-list label:hover { background: var(--hover) !important; color: var(--text) !important; }
.rec-list input[type="radio"] { display: none; }

/* 版本历史 */
.ide-sidebar .prose { font-size: 11.5px; color: var(--text-2); line-height: 1.6; }
.ide-sidebar .prose code { font-family: var(--mono); font-size: 10.5px; color: var(--muted);
  background: transparent; padding: 0; }
.ide-sidebar .prose p { margin: 0; }

/* ---------- 编辑器（对话） ---------- */
.ide-editor { flex: 1 1 0 !important; min-width: 380px !important; background: var(--editor);
  border-right: 1px solid var(--border); display: flex !important; flex-direction: column;
  padding: 0 !important; overflow: hidden; height: 100%; min-height: 0 !important; }
.ide-editor > .block { flex: 0 0 auto; padding: 0 !important; margin: 0 !important; }
.editor-tabbar { display: flex; align-items: center; gap: 8px; height: 36px; padding: 0 12px;
  background: var(--chrome); border-bottom: 1px solid var(--border); flex: none; }
.editor-tab { font-size: 12px; color: var(--text); background: var(--editor);
  border: 1px solid var(--border); border-bottom-color: var(--editor);
  padding: 5px 12px; border-radius: 4px 4px 0 0; margin-bottom: -1px; display: inline-flex;
  align-items: center; gap: 6px; }
.editor-tab .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--accent); }
.editor-tabbar .spacer { flex: 1; }
.editor-tabbar .hint { font-size: 11px; color: var(--muted); }

#chat { flex: 1 1 0 !important; min-height: 0 !important; height: auto !important;
  background: var(--editor); border: none !important; border-radius: 0 !important;
  overflow-y: auto; }

#chat .message-row { border: none !important; padding: 10px 16px !important;
  background: transparent !important; }
#chat .message-row.user-row { background: var(--sidebar) !important;
  border-top: 1px solid #f4f4f4 !important; border-bottom: 1px solid #f4f4f4 !important; }
#chat .message-row.bot-row { background: var(--editor) !important; }
#chat .message { font-size: 13px !important; line-height: 1.68 !important;
  color: var(--text) !important; background: transparent !important;
  border: none !important; box-shadow: none !important; max-width: 100% !important; }
#chat button { display: none !important; }
#chat .message p { margin: 0 0 6px; }
#chat .message table { font-size: 12px; border-collapse: collapse; margin: 6px 0; }
#chat .message th { background: var(--chrome); font-weight: 600; }
#chat .message th, #chat .message td { border: 1px solid var(--border); padding: 3px 8px; }
#chat .message code { font-family: var(--mono); font-size: 11.5px; background: var(--chrome);
  padding: 1px 4px; border-radius: 3px; }
#chat .message pre { background: var(--sidebar); border: 1px solid var(--border); border-radius: 4px;
  padding: 8px 10px; overflow: auto; }
#chat .message pre code { background: transparent; padding: 0; }
#chat .message details { font-size: 12px; color: var(--text-2); margin-top: 4px; }
#chat .message summary { cursor: pointer; color: var(--text-2); }

.confirm-card { border: 1px solid var(--border-strong); border-left: 3px solid var(--accent);
  background: #fff; border-radius: 4px; padding: 8px 10px; font-size: 12px;
  margin: 6px 10px; max-height: 220px; overflow: auto; }
.confirm-card table { font-size: 11.5px; border-collapse: collapse; }
.confirm-card th, .confirm-card td { border: 1px solid var(--border); padding: 2px 7px; }

.ide-editor .confirm-row { padding: 0 10px 8px; gap: 8px !important;
  justify-content: flex-end; flex-wrap: nowrap !important; }
.ide-editor .confirm-row button { height: 28px !important; font-size: 12px !important;
  width: auto !important; min-width: 0 !important; flex: 0 0 auto !important;
  padding: 0 14px !important; }

.composer { padding: 8px 10px 12px; border-top: 1px solid var(--border); background: var(--editor);
  gap: 8px !important; align-items: flex-end; }
.composer .block { margin: 0 !important; }
.composer .wrap, .composer .wrap-inner, .composer .form {
  background: transparent !important; border: none !important; padding: 0 !important;
  box-shadow: none !important; min-height: 0 !important; }
.composer textarea { min-height: 44px !important; max-height: 140px; font-size: 13px !important;
  border: 1px solid var(--border-strong) !important; border-radius: var(--radius) !important;
  background: #fff !important; padding: 8px 10px !important; resize: none; }
.composer textarea:focus { border-color: var(--accent) !important; outline: none !important;
  box-shadow: 0 0 0 2px var(--accent-soft) !important; }
.composer .stop-btn { width: 72px !important; min-width: 72px !important;
  max-width: 72px !important; flex: 0 0 72px !important; height: 36px !important;
  font-size: 12.5px !important; color: var(--text-2) !important; }
.composer .send-btn { width: 92px !important; min-width: 92px !important; max-width: 92px !important;
  flex: 0 0 92px !important; height: 36px !important; font-size: 12.5px !important;
  display: inline-flex !important; flex-direction: row !important; align-items: center !important;
  justify-content: center !important; gap: 6px !important; padding: 0 10px !important; }

/* ---------- 右侧检查器面板 ---------- */
.ide-panel { flex: 0 0 var(--panel-w, 400px) !important;
  width: var(--panel-w, 400px) !important; min-width: 300px !important;
  max-width: 70% !important; background: var(--panel); padding: 0 !important;
  display: flex !important; flex-direction: column; flex-wrap: nowrap !important;
  gap: 0 !important; overflow: hidden; height: 100%; }
.panel-tabs { flex: none; border-bottom: 1px solid var(--border); background: var(--chrome);
  border-radius: 0 !important; padding: 0 6px !important; }
.panel-tabs .wrap { display: flex !important; flex-wrap: wrap; gap: 0 !important; }
.panel-tabs label { font-size: 12px !important; color: var(--text-2) !important; padding: 8px 10px !important;
  border-bottom: 2px solid transparent; border-radius: 0 !important; cursor: pointer;
  background: transparent !important; }
.panel-tabs label.selected, .panel-tabs label:has(input:checked) { color: var(--text) !important;
  background: var(--panel) !important; border-bottom-color: var(--accent) !important; }
.panel-tabs input { display: none; }
.panel-view { flex: 1 1 auto; min-height: 0; overflow: auto; padding: 0 !important; }
.panel-head { position: sticky; top: 0; z-index: 2; display: flex; justify-content: space-between;
  align-items: baseline; padding: 8px 12px 6px; font-size: 11px; font-weight: 600;
  letter-spacing: .06em; color: var(--text-2); background: var(--panel);
  border-bottom: 1px solid var(--border); }
.panel-hint { font-size: 10.5px; color: var(--muted); font-weight: 400; letter-spacing: 0; }
.panel-body { padding: 8px 12px 14px; }
.panel-body .prose { font-size: 12.5px; line-height: 1.65; }
.panel-body .prose table { font-size: 12px; border-collapse: collapse; }
.panel-body .prose th, .panel-body .prose td { border: 1px solid var(--border); padding: 3px 8px; }
.panel-body .prose th { background: var(--chrome); }
.panel-body .prose pre { background: var(--sidebar); border: 1px solid var(--border);
  border-radius: 4px; padding: 8px 10px; overflow: auto; font-size: 11.5px; }
.panel-body .prose code { font-family: var(--mono); font-size: 11px; }
#preview { padding: 0 !important; }
.grid-wrap { overflow: auto; max-height: calc(100vh - 210px); }
table.data-grid { border-collapse: separate; border-spacing: 0; font-size: 11.5px;
  width: max-content; min-width: 100%; }
.data-grid th { position: sticky; top: 0; z-index: 1; background: var(--chrome);
  text-align: left; font-weight: 600; color: var(--text-2); padding: 5px 10px;
  border-bottom: 1px solid var(--border); border-right: 1px solid #f0f0f0;
  white-space: nowrap; }
.data-grid td { padding: 3px 10px; border-bottom: 1px solid #f2f2f2;
  border-right: 1px solid #f7f7f7; white-space: nowrap; color: var(--text); }
.data-grid td.num { font-family: var(--mono); font-variant-numeric: tabular-nums;
  text-align: right; }
.data-grid tr:hover td { background: #f6faf8; }
.data-grid th.rn, .data-grid td.rn { position: sticky; left: 0; z-index: 1;
  background: var(--sidebar); color: var(--muted); font-family: var(--mono);
  font-size: 10.5px; text-align: right; padding: 3px 8px; }
.data-grid th.rn { z-index: 2; }
.grid-foot { font-size: 11px; color: var(--muted); padding: 6px 12px;
  border-top: 1px solid var(--border); background: var(--panel); }
.empty-state { padding: 40px 22px; text-align: center; }
.empty-title { font-size: 13px; font-weight: 600; color: var(--text-2); }
.empty-sub { font-size: 11.5px; color: var(--muted); margin-top: 6px; line-height: 1.7; }
#gallery { background: var(--panel); padding: 0 !important; }
#gallery [data-testid="block-label"], #gallery .label, #gallery .caption,
#gallery [class*="caption"], #gallery [class*="file-name"], #gallery [class*="filename"],
#gallery button[aria-label*="Download"], #gallery button[aria-label*="Fullscreen"] {
  display: none !important; }
#gallery .gallery { gap: 10px; padding: 0 !important; }
#gallery .gallery-item, #gallery .thumbnail-item { background: #fff !important;
  border: 1px solid var(--border) !important; border-radius: 4px !important; }
#gallery img { object-fit: contain !important; }

/* Gradio label / block-info：统一字号与颜色（默认过浅、过大） */
.ide-shell [data-testid="block-info"], .ide-shell [data-testid="block-label"],
.ide-shell [data-testid="block-label"] span, .ide-shell label > span,
.ide-shell .svelte-jdcl7l {
  color: var(--text-2) !important; font-size: 11px !important; font-weight: 500 !important;
  line-height: 1.4 !important;
}
.ide-shell label { color: var(--text-2) !important; }

/* ---------- 通用控件 ---------- */
.gradio-container button { border-radius: 4px !important; box-shadow: none !important;
  font-family: var(--ui); font-size: 12px !important; min-height: 28px; }
.gradio-container button.primary { background: var(--btn) !important;
  border: 1px solid var(--btn) !important; color: #fff !important; }
.gradio-container button.primary:hover { background: var(--btn-hover) !important; }
.gradio-container button.secondary { background: #fff !important; color: var(--text) !important;
  border: 1px solid var(--border-strong) !important; }
.gradio-container button.secondary:hover { background: var(--hover) !important; }
.ide-sidebar button.secondary { background: var(--chrome) !important; }
.ide-sidebar button.secondary:hover { background: var(--active) !important; }
.ide-shell .primary, .ide-titlebar .primary { background: var(--btn) !important; color: #fff !important;
  border: 1px solid var(--btn) !important; box-shadow: none !important; }
.ide-shell .primary:hover, .ide-titlebar .primary:hover { background: var(--btn-hover) !important; }
.ide-shell .secondary, .ide-titlebar .secondary { background: #fff !important; color: var(--text) !important;
  border: 1px solid var(--border-strong) !important; box-shadow: none !important; }
.ide-shell .secondary:hover, .ide-titlebar .secondary:hover { background: var(--hover) !important; }
.ide-shell button:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.ide-shell .icon-btn { font-size: 0 !important; width: 30px !important; min-width: 30px !important;
  height: 28px !important; padding: 0 !important; }
.ide-shell .icon-btn::before { width: 14px !important; height: 14px !important; }
.ide-shell input[type="text"], .ide-shell input[type="number"], .ide-shell textarea,
.ide-shell select { font-family: var(--ui); }
.ide-shell .gr-box, .ide-shell .block { border-radius: 0; }
.ide-shell .file-preview, .ide-shell .file { font-size: 11.5px; }

/* 状态栏 */
.ide-statusbar-wrap { margin: 0 !important; }
.ide-statusbar-wrap .html-container, .ide-statusbar-wrap .block,
.ide-statusbar-wrap .prose { padding: 0 !important; margin: 0 !important; }
.statusbar { display: flex; align-items: center; height: 26px; padding: 0 12px;
  background: var(--chrome); border-top: 1px solid var(--border);
  font-size: 11px; color: var(--text-2); gap: 8px; }
.sb-item.ok { color: var(--accent); }
.sb-item.warn { color: var(--warn); }
.sb-item.mono { font-family: var(--mono); font-size: 10.5px; }
.sb-sep { width: 1px; height: 12px; background: var(--border-strong); }
.sb-spacer { flex: 1; }

/* 可拖拽分隔条 */
.ide-splitter { flex: 0 0 5px !important; width: 5px !important;
  min-width: 5px !important; max-width: 5px !important; align-self: stretch;
  cursor: col-resize; background: transparent; position: relative; z-index: 5; }
.ide-splitter::after { content: ""; position: absolute; left: 2px; top: 0; bottom: 0;
  width: 1px; background: var(--border); }
.ide-splitter:hover::after, .ide-splitter.dragging::after { background: var(--accent); width: 2px; }
.ide-sidebar[style*="display: none"] + .ide-splitter { display: none !important; }

/* 代码 chip（反思/历史中的 `xxx`） */
.panel-body .prose code, .ide-sidebar .prose code, .panel-view .prose code {
  background: #ececec !important; color: #1f2328 !important; border: none !important;
  padding: 1px 5px !important; border-radius: 3px !important; font-size: 11px !important; }

/* 滚动条 */
.ide-sidebar::-webkit-scrollbar, .panel-view::-webkit-scrollbar, #chat::-webkit-scrollbar { width: 10px; }
.ide-sidebar::-webkit-scrollbar-thumb, .panel-view::-webkit-scrollbar-thumb,
#chat::-webkit-scrollbar-thumb { background: #d8d8d8; border-radius: 6px;
  border: 2px solid transparent; background-clip: content-box; }

@media (max-width: 1180px) {
  .ide-shell { height: auto; flex-wrap: wrap; }
  .ide-rail { flex-direction: row !important; width: 100% !important; min-width: 0 !important;
    height: 46px; padding: 0 8px; border-right: none; border-bottom: 1px solid var(--border); }
  .rail-sep { width: 1px; height: 22px; margin: 0 4px; }
  .ide-sidebar { flex: 1 1 100% !important; max-width: none !important; max-height: 42vh; }
  .ide-editor { flex: 1 1 100% !important; min-width: 0 !important; border-right: none; }
  #chat { height: 62vh; }
  .ide-panel { flex: 1 1 100% !important; max-width: none !important; }
}
""".replace("__ICON_RULES__", _icon_css())


CSS = _build_css()


# ---------------------------------------------------------------------------
# 组装界面
# ---------------------------------------------------------------------------
def build_demo() -> gr.Blocks:
    import yaml
    for yml in sorted(config.TEMPLATES_DIR.glob("*.yaml")):
        try:
            tpl = yaml.safe_load(yml.read_text(encoding="utf-8"))
            TEMPLATE_CHOICES[f"{tpl['name']}（{tpl['template_id']}）"] = tpl["template_id"]
        except Exception:  # noqa: BLE001
            continue

    with gr.Blocks(title="表格管家 · Excel Agent") as demo:
        sid = gr.State("")
        sidebar_visible = gr.State(True)

        # ── 标题栏
        with gr.Row(elem_classes="ide-titlebar"):
            gr.HTML(_titlebar_html(), scale=1)
            btn_sidebar = gr.Button("侧栏", size="sm",
                                    elem_classes=["tb-icon-btn", "secondary", "ic-panel"], scale=0,
                                    elem_id="btn-sidebar-toggle")
            mode_dd = gr.Dropdown(choices=list(MODE_CHOICES), value="M2 · 完整反思",
                                  show_label=False, elem_classes="tb-mode", scale=0,
                                  elem_id="mode-select")
            btn_new = gr.Button("新建会话", size="sm",
                                elem_classes=["tb-btn", "secondary", "ic-plus"], scale=0,
                                elem_id="btn-new-session")
            btn_clear = gr.Button("清空对话", size="sm", elem_classes=["tb-btn", "secondary"],
                                  scale=0, elem_id="btn-clear-chat")

        # ── 工作台
        with gr.Row(elem_classes="ide-shell"):
            # 活动栏
            with gr.Column(elem_classes="ide-rail", scale=0, min_width=56):
                gr.HTML(f"<div class='rail-logo'>{_logo_svg(20)}</div>")
                btn_rail_data = gr.Button("数据源", elem_classes=["rail-btn", "ic-database"])
                btn_rail_undo = gr.Button("撤销", elem_classes=["rail-btn", "ic-undo"])
                btn_rail_redo = gr.Button("重做", elem_classes=["rail-btn", "ic-redo"])
                gr.HTML("<div class='rail-sep'></div>")
                btn_rail_export = gr.Button("导出", elem_classes=["rail-btn", "ic-download"])

            # 资源管理器
            with gr.Column(elem_classes="ide-sidebar", scale=1, min_width=320) as sidebar_col:
                gr.HTML(_side_head("数据源", "RESOURCES"))
                btn_demo = gr.Button("加载示例数据（销售台账）", variant="primary", size="sm",
                                     elem_id="btn-load-demo")
                with gr.Tabs(elem_classes="side-tabs"):
                    with gr.Tab("上传", elem_id="tab-upload"):
                        upload = gr.File(label="选择 .xlsx / .xls / .csv（≤20MB）",
                                         file_types=[".xlsx", ".xls", ".csv"], elem_id="uploader")
                        header_rows = gr.Textbox(
                            label="手动指定表头行（可选，格式：表名:行号）",
                            placeholder="留空=自动识别", lines=1)
                        btn_upload = gr.Button("导入", variant="primary", size="sm",
                                               elem_classes=["ic-upload"], elem_id="btn-upload")
                    with gr.Tab("示例", elem_id="tab-samples"):
                        sample_dd = gr.Dropdown(choices=list(SAMPLE_CHOICES) + list(DIRTY_CHOICES),
                                                label="内置数据集", elem_id="sample-select")
                        btn_sample = gr.Button("加载", variant="primary", size="sm",
                                               elem_classes=["ic-folder"], elem_id="btn-load-sample")
                    with gr.Tab("模板", elem_id="tab-template"):
                        tpl_dd = gr.Dropdown(choices=list(TEMPLATE_CHOICES), label="分析模板", elem_id="template-select")
                        tpl_name = gr.Textbox(label="工作簿名称（可选）", lines=1)
                        btn_tpl = gr.Button("创建空表", variant="primary", size="sm",
                                           elem_classes=["ic-plus"], elem_id="btn-create-template")
                load_note = gr.Markdown("", elem_classes="side-note")
                sheet_dd = gr.Dropdown(label="当前工作表", choices=[], value=None)
                data_card = gr.HTML("尚未加载数据")

                gr.HTML(_side_head("推荐问题", "SUGGESTED"))
                rec_questions = gr.Radio(choices=[], value=None, show_label=False,
                                         elem_classes="rec-list")

                gr.HTML(_side_head("会话历史", "SESSIONS"))
                session_dd = gr.Dropdown(label="历史会话", choices=[], value=None, elem_id="session-select")
                with gr.Row():
                    btn_session_open = gr.Button("打开", size="sm", elem_classes=["secondary"], elem_id="btn-session-open")
                    btn_session_refresh = gr.Button("刷新", size="sm", elem_classes=["secondary"], elem_id="btn-session-refresh")
                chk_session_delete = gr.Checkbox(label="确认删除选中会话", value=False, elem_id="chk-delete-session")
                btn_session_delete = gr.Button("删除选中会话", size="sm",
                                               elem_classes=["secondary"], elem_id="btn-session-delete")

                gr.HTML(_side_head("版本历史", "VERSIONS"))
                with gr.Row():
                    btn_undo = gr.Button("撤销", size="sm", elem_classes=["ic-undo", "secondary"], elem_id="btn-undo")
                    btn_redo = gr.Button("重做", size="sm", elem_classes=["ic-redo", "secondary"], elem_id="btn-redo")
                with gr.Row():
                    goto_n = gr.Number(label="回到第 N 步", value=0, precision=0, minimum=0)
                    btn_goto = gr.Button("跳转", size="sm", elem_classes=["secondary"])
                with gr.Row():
                    snap_name = gr.Textbox(label="快照名", scale=3, lines=1, elem_id="snap-name")
                    btn_snap = gr.Button("快照", size="sm", elem_classes=["ic-save", "secondary"], elem_id="btn-snapshot")
                with gr.Row():
                    snap_dd = gr.Dropdown(label="已保存快照", choices=[], scale=2, elem_id="snap-select")
                    btn_snap_restore = gr.Button("恢复", size="sm", elem_classes=["secondary"], elem_id="btn-snap-restore")
                    btn_snap_refresh = gr.Button("刷新", size="sm", elem_classes=["secondary"], elem_id="btn-snap-refresh")
                history_md = gr.Markdown("（暂无写操作）", elem_classes="side-note")

                gr.HTML(_side_head("导出", "EXPORT"))
                with gr.Row():
                    btn_xlsx = gr.Button("xlsx", size="sm", elem_classes=["ic-file-sheet", "secondary"], elem_id="btn-export-xlsx")
                    btn_pdf = gr.Button("PDF", size="sm", elem_classes=["ic-file-text", "secondary"], elem_id="btn-export-pdf")
                    btn_png = gr.Button("PNG", size="sm", elem_classes=["ic-image", "secondary"], elem_id="btn-export-png")
                export_file = gr.File(label="下载", interactive=False, elem_id="download-box")
                export_note = gr.Markdown("", elem_classes="side-note")

            # 对话编辑器
            with gr.Column(elem_classes="ide-editor", scale=3, min_width=380):
                gr.HTML(
                    "<div class='editor-tabbar'><span class='editor-tab'>"
                    "<span class='dot'></span>对话</span>"
                    "<span class='spacer'></span>"
                    "<span class='hint'>Enter 发送</span></div>")
                chatbot = gr.Chatbot(height=520, elem_id="chat", show_label=False,
                                     placeholder="在左侧载入数据后，用中文开始分析")
                confirm_box = gr.HTML("", elem_classes="confirm-card", visible=False)
                with gr.Row(elem_classes="confirm-row", visible=False) as confirm_row:
                    btn_confirm = gr.Button("确认执行", variant="primary", size="sm",
                                            elem_classes=["ic-check"], elem_id="btn-confirm")
                    btn_cancel = gr.Button("取消", size="sm", elem_classes=["ic-x", "secondary"], elem_id="btn-cancel")
                with gr.Row(elem_classes="composer"):
                    msg = gr.Textbox(placeholder="描述你要做的分析或修改，例如：各大区销售额总和是多少",
                                     scale=7, autofocus=True, show_label=False, lines=1,
                                     elem_id="composer-input")
                    btn_stop = gr.Button("停止", size="sm", variant="secondary",
                                         elem_classes=["stop-btn"], scale=0, visible=False,
                                         elem_id="btn-stop")
                    btn_send = gr.Button("发送", variant="primary", elem_classes=["send-btn", "ic-send"],
                                         scale=0, elem_id="btn-send")

            # 检查器面板
            with gr.Column(elem_classes="ide-panel", scale=2, min_width=360):
                view_radio = gr.Radio(choices=VIEW_CHOICES, value="预览", show_label=False,
                                      elem_classes="panel-tabs", elem_id="view-tabs")
                with gr.Column(visible=True, elem_classes="panel-view") as view_preview:
                    gr.HTML(_panel_head("数据预览", "前 100 行"))
                    with gr.Column(elem_classes="panel-body"):
                        preview_tbl = gr.HTML(_df_table_html(None), elem_id="preview")
                with gr.Column(visible=False, elem_classes="panel-view") as view_charts:
                    gr.HTML(_panel_head("图表", "本会话生成"))
                    with gr.Column(elem_classes="panel-body"):
                        gallery = gr.Gallery(label="", columns=1, height=420, elem_id="gallery")
                with gr.Column(visible=False, elem_classes="panel-view") as view_reflection:
                    gr.HTML(_panel_head("反思过程", "R1 / R2"))
                    with gr.Column(elem_classes="panel-body"):
                        reflection_md = gr.Markdown("发起一次对话后，这里展示规划、校验与反思轨迹")
                with gr.Column(visible=False, elem_classes="panel-view") as view_trace:
                    gr.HTML(_panel_head("查询记录", "SQL 可复现"))
                    with gr.Column(elem_classes="panel-body"):
                        trace_md = gr.Markdown("每条回答执行的查询会记录在这里")
                with gr.Column(visible=False, elem_classes="panel-view") as view_report:
                    gr.HTML(_panel_head("报告", "可导出 PDF"))
                    with gr.Column(elem_classes="panel-body"):
                        report_md = gr.Markdown("「帮我出一份分析报告」的成文结果会显示在这里")
                with gr.Column(visible=False, elem_classes="panel-view") as view_memory:
                    gr.HTML(_panel_head("记忆", "L1 / L2 / L3"))
                    with gr.Column(elem_classes="panel-body"):
                        btn_mem_refresh = gr.Button("刷新记忆", size="sm",
                                                    elem_classes=["secondary"], elem_id="btn-mem-refresh")
                        memory_md = gr.Markdown(
                            "切换到「记忆」页可查看 L1 会话摘要、L2 工作记忆与 L3 长期偏好")

        # ── 状态栏
        status_bar = gr.HTML(_status_html("M2 · 完整反思"), elem_classes="ide-statusbar-wrap")

        # ---------------- 事件绑定 ----------------
        common_out = [sheet_dd, data_card, preview_tbl, rec_questions, history_md, gallery]

        btn_upload.click(on_upload, [sid, upload, header_rows],
                         common_out + [sid, load_note]).then(
            on_snap_refresh, [sid], [snap_dd], queue=False)
        btn_demo.click(on_load_demo, [sid], common_out + [sid, load_note]).then(
            on_snap_refresh, [sid], [snap_dd], queue=False)
        btn_sample.click(on_load_sample, [sid, sample_dd],
                         common_out + [sid, load_note]).then(
            on_snap_refresh, [sid], [snap_dd], queue=False)
        btn_tpl.click(on_create_template, [sid, tpl_dd, tpl_name],
                      common_out + [sid, load_note]).then(
            on_snap_refresh, [sid], [snap_dd], queue=False)
        sheet_dd.change(on_sheet_change, [sid, sheet_dd], common_out)

        def _rec_click(value):
            return value or ""
        rec_questions.change(_rec_click, [rec_questions], [msg], queue=False)

        session_out = [sid, chatbot, sheet_dd, data_card, preview_tbl, rec_questions,
                        history_md, gallery, confirm_box, confirm_row, reflection_md,
                        trace_md, report_md, memory_md, view_radio, view_preview,
                        view_charts, view_reflection, view_trace, view_report,
                        view_memory, export_file, load_note, btn_stop, session_dd]

        mode_map = {k: v for k, v in MODE_CHOICES.items()}
        chat_out = [sid, chatbot, confirm_box, gallery, reflection_md, trace_md, report_md,
                    sheet_dd, data_card, preview_tbl, rec_questions, history_md, view_radio,
                    confirm_row, msg, btn_stop]

        def _chat(sid_, m, hist, mode_label):
            yield from on_send(sid_, m, hist, mode_map.get(mode_label, "m2"))

        btn_send.click(_chat, [sid, msg, chatbot, mode_dd], chat_out,
                       show_progress="minimal")
        msg.submit(_chat, [sid, msg, chatbot, mode_dd], chat_out,
                   show_progress="minimal")
        btn_stop.click(_stop_turn, [sid], [btn_stop], queue=False, show_progress="hidden")

        # 前 5 个输出：sid、chatbot、diff 卡、预览、历史；后 2 个：确认按钮行与卡片的可见性
        btn_confirm.click(on_confirm, [sid, chatbot],
                          [sid, chatbot, confirm_box, preview_tbl, history_md, confirm_row])
        btn_cancel.click(on_cancel, [sid, chatbot],
                         [sid, chatbot, confirm_box, confirm_row])

        btn_undo.click(on_undo, [sid, chatbot], [sid, chatbot, data_card, preview_tbl, history_md])
        btn_redo.click(on_redo, [sid, chatbot], [sid, chatbot, data_card, preview_tbl, history_md])
        btn_goto.click(on_goto, [sid, chatbot, goto_n],
                       [sid, chatbot, data_card, preview_tbl, history_md])
        btn_snap.click(on_snapshot_save2, [sid, snap_name, chatbot],
                       [sid, chatbot, data_card, preview_tbl, history_md, snap_dd])
        btn_snap_refresh.click(on_snap_refresh, [sid], [snap_dd], queue=False)
        btn_snap_restore.click(on_snap_restore, [sid, snap_dd, chatbot],
                               [sid, chatbot, data_card, preview_tbl, history_md, load_note])
        btn_session_refresh.click(on_sessions_refresh, [sid], [session_dd], queue=False)
        btn_session_open.click(on_open_session, [sid, chatbot, session_dd], session_out,
                               queue=False).then(
            on_snap_refresh, [sid], [snap_dd], queue=False)
        btn_mem_refresh.click(on_memory_refresh, [sid], [memory_md], queue=False)

        btn_rail_data.click(on_toggle_sidebar, [sidebar_visible], [sidebar_col, sidebar_visible])
        btn_sidebar.click(on_toggle_sidebar, [sidebar_visible], [sidebar_col, sidebar_visible])
        btn_rail_undo.click(on_undo, [sid, chatbot], [sid, chatbot, data_card, preview_tbl, history_md])
        btn_rail_redo.click(on_redo, [sid, chatbot], [sid, chatbot, data_card, preview_tbl, history_md])
        btn_rail_export.click(lambda s, r, g: on_export(s, "xlsx", r, g),
                              [sid, report_md, gallery], [export_file, export_note])

        view_radio.change(on_switch_view, [view_radio, sid],
                          [view_preview, view_charts, view_reflection, view_trace,
                           view_report, view_memory, memory_md], queue=False)
        mode_dd.change(_status_html, [mode_dd], [status_bar])
        btn_new.click(on_new_session, [sid, chatbot], session_out, queue=False).then(
            on_snap_refresh, [sid], [snap_dd], queue=False)
        demo.load(on_sessions_refresh, [sid], [session_dd], queue=False)
        btn_clear.click(on_clear_chat, [sid, chatbot],
                        [chatbot, confirm_box, reflection_md, trace_md, report_md,
                         confirm_row, gallery, load_note])
        btn_session_delete.click(on_delete_session, [sid, session_dd, chk_session_delete],
                                 [session_dd, load_note, chk_session_delete], queue=False)

        def _export_xlsx(sid_, rpt, gal):
            return on_export(sid_, "xlsx", rpt, gal)
        btn_xlsx.click(_export_xlsx, [sid, report_md, gallery], [export_file, export_note])
        btn_pdf.click(lambda s, r, g: on_export(s, "pdf", r, g),
                      [sid, report_md, gallery], [export_file, export_note])
        btn_png.click(lambda s, r, g: on_export(s, "png", r, g),
                      [sid, report_md, gallery], [export_file, export_note])

    return demo


_SPLITTER_JS = """
<script>
(function () {
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const EDITOR_MIN = 380, PANEL_MIN = 360, SIDEBAR_MIN = 260, SIDEBAR_MAX = 520, GAPS = 24;
  let varsLoaded = false;

  function applySavedVars(shell) {
    if (varsLoaded) return;
    varsLoaded = true;
    const s = localStorage.getItem('excel-agent-sidebar-w');
    if (s) shell.style.setProperty('--sidebar-w', s);
    const p = localStorage.getItem('excel-agent-panel-w');
    if (p) shell.style.setProperty('--panel-w', p);
  }

  function bindSplitter(sp, shell, mode) {
    if (sp.dataset.bound) return;
    sp.dataset.bound = '1';
    sp.setAttribute('role', 'separator');
    sp.setAttribute('aria-orientation', 'vertical');
    let startX = 0, startW = 0, dragging = false;
    const sidebar = () => shell.querySelector('.ide-sidebar');
    const editor = () => shell.querySelector('.ide-editor');
    const panel = () => shell.querySelector('.ide-panel');
    const railW = () => Math.max(56, shell.querySelector('.ide-rail')?.getBoundingClientRect().width || 56);

    const maxSidebar = () => {
      const pw = panel()?.getBoundingClientRect().width || 400;
      return Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX,
        shell.clientWidth - railW() - EDITOR_MIN - pw - GAPS));
    };
    const maxPanel = () => {
      const sw = (sidebar()?.offsetWidth || 0) > 0
        ? sidebar().getBoundingClientRect().width : 0;
      return Math.max(PANEL_MIN, shell.clientWidth - railW() - sw - EDITOR_MIN - GAPS);
    };

    sp.addEventListener('pointerdown', (e) => {
      const target = mode === 'sidebar' ? sidebar() : panel();
      if (!target) return;
      dragging = true;
      startX = e.clientX;
      startW = target.getBoundingClientRect().width;
      sp.setPointerCapture(e.pointerId);
      sp.classList.add('dragging');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
      e.preventDefault();
    });
    sp.addEventListener('pointermove', (e) => {
      if (!dragging) return;
      const dx = e.clientX - startX;
      if (mode === 'sidebar') {
        const w = clamp(startW + dx, SIDEBAR_MIN, maxSidebar());
        shell.style.setProperty('--sidebar-w', w + 'px');
      } else {
        const w = clamp(startW - dx, PANEL_MIN, maxPanel());
        shell.style.setProperty('--panel-w', w + 'px');
      }
    });
    const end = () => {
      if (!dragging) return;
      dragging = false;
      sp.classList.remove('dragging');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      const target = mode === 'sidebar' ? sidebar() : panel();
      if (!target) return;
      const w = Math.round(target.getBoundingClientRect().width);
      localStorage.setItem(mode === 'sidebar' ? 'excel-agent-sidebar-w'
                                             : 'excel-agent-panel-w', w + 'px');
    };
    sp.addEventListener('pointerup', end);
    sp.addEventListener('pointercancel', end);
    sp.addEventListener('dblclick', () => {
      if (mode === 'sidebar') {
        shell.style.removeProperty('--sidebar-w');
        localStorage.removeItem('excel-agent-sidebar-w');
      } else {
        shell.style.removeProperty('--panel-w');
        localStorage.removeItem('excel-agent-panel-w');
      }
    });
  }

  function ensure() {
    const shell = document.querySelector('.ide-shell');
    if (!shell) return;
    const sidebar = shell.querySelector('.ide-sidebar');
    const editor = shell.querySelector('.ide-editor');
    const panel = shell.querySelector('.ide-panel');
    if (!editor || !panel) return;
    applySavedVars(shell);

    let spSide = shell.querySelector('.ide-splitter[data-kind="side"]');
    if (!spSide) {
      spSide = document.createElement('div');
      spSide.className = 'ide-splitter';
      spSide.dataset.kind = 'side';
      bindSplitter(spSide, shell, 'sidebar');
    }
    let spPanel = shell.querySelector('.ide-splitter[data-kind="panel"]');
    if (!spPanel) {
      spPanel = document.createElement('div');
      spPanel.className = 'ide-splitter';
      spPanel.dataset.kind = 'panel';
      bindSplitter(spPanel, shell, 'panel');
    }
    if (spSide.nextSibling !== editor) shell.insertBefore(spSide, editor);
    if (spPanel.nextSibling !== panel) shell.insertBefore(spPanel, panel);
    spSide.style.display = (sidebar && sidebar.offsetWidth > 0) ? '' : 'none';

    // 窗口变窄时把已保存的宽度收敛回可行范围
    const sideW = (sidebar && sidebar.offsetWidth > 0)
      ? sidebar.getBoundingClientRect().width : 0;
    const railW = Math.max(56, shell.querySelector('.ide-rail')?.getBoundingClientRect().width || 56);
    const sideCap = Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX,
      shell.clientWidth - railW - EDITOR_MIN - PANEL_MIN - GAPS));
    if (sideW > sideCap) shell.style.setProperty('--sidebar-w', Math.floor(sideCap) + 'px');
    const panelW = panel.getBoundingClientRect().width;
    const maxP = Math.max(PANEL_MIN, shell.clientWidth - railW - sideW - EDITOR_MIN - GAPS);
    if (panelW > maxP) shell.style.setProperty('--panel-w', Math.floor(maxP) + 'px');

    const sideBtn = document.querySelector('.ide-titlebar .tb-icon-btn');
    if (sideBtn && !sideBtn.title) sideBtn.title = '收起 / 展开数据源面板';

    // 聊天区自动跟随：pinned 表示用户是否停留在底部。
    // Gradio 6 的实际滚动容器是 #chat 内的 .bubble-wrap，需直接操作它。
    const chat = document.getElementById('chat');
    if (chat) {
      if (!chat.dataset.pinned) chat.dataset.pinned = '1';
      const scroller = chat.querySelector('.bubble-wrap') || chat;

      const nearBottom = () =>
        scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 160;
      const follow = () => {
        if (chat.dataset.pinned !== '1') return;
        if (scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight > 2) {
          scroller.scrollTop = scroller.scrollHeight;
        }
      };
      const repin = () => {
        chat.dataset.pinned = '1';
        [30, 250, 900, 1800, 3000].forEach(ms => setTimeout(follow, ms));
      };

      if (!scroller.dataset.autoScroll) {
        scroller.dataset.autoScroll = '1';
        scroller.addEventListener('scroll', () => {
          chat.dataset.pinned = nearBottom() ? '1' : '0';
        }, { passive: true });
        new MutationObserver(follow).observe(
          scroller, { childList: true, subtree: true, characterData: true });
        // 初次加载（含历史回放）先落到最新一条
        [0, 250, 900, 1800].forEach(ms => setTimeout(follow, ms));
      }

      const REPIN_IDS = ['btn-send', 'btn-confirm', 'btn-cancel', 'btn-undo',
        'btn-redo', 'btn-session-open', 'btn-new-session', 'btn-clear-chat',
        'btn-snap-restore', 'btn-load-demo'];
      if (!chat.dataset.repinWired) {
        chat.dataset.repinWired = '1';
        document.addEventListener('click', (ev) => {
          const node = ev.target && ev.target.closest ? ev.target.closest('[id]') : null;
          if (node && REPIN_IDS.includes(node.id)) repin();
        }, true);
        document.addEventListener('keydown', (ev) => {
          if (ev.key !== 'Enter' || !ev.target || !ev.target.closest) return;
          if (ev.target.closest('#composer-input')) repin();
        }, true);
      }
    }
  }

  document.addEventListener('DOMContentLoaded', ensure);
  setInterval(ensure, 500);
  new MutationObserver(ensure).observe(document.documentElement,
    { childList: true, subtree: true });
  ensure();
})();
</script>
"""


def launch(server_name: str = "127.0.0.1", server_port: int = 7860, **kwargs):
    demo = build_demo()
    demo.launch(server_name=server_name, server_port=server_port,
                theme=gr.themes.Base(), css=CSS, head=_SPLITTER_JS, **kwargs)
    return demo
