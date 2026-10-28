# 05 · 工具与 MCP

> 覆盖：读工具、结构化写工具（diff/确认/逆操作）、图表/分析/导出、FastMCP Server。
> 代码：`src/tools/toolkit.py`、`src/core/mutations.py`、`src/mcp_server/server.py`。

## 1. 设计原则

1. **读/写分离**：读工具走只读 SQL 沙箱；写操作是结构化工具（模型只能选操作 + 填参数，不能生成改表代码）。
2. **写必先预览**：任何写操作先 `plan_write` 生成 diff（旧值红/新值绿），确认后才 `apply_write` 落盘；>50 单元格强制二次确认。
3. **可撤销**：每个写操作附带逆操作，落盘即记录；见 [04 数据与存储](04-data-storage.md)。
4. **每个数字可溯源**：查询结果存 `result_refs`（res_id + SQL + 数据），回答附「执行的查询」。

## 2. 读工具（`Toolkit`，绑定 workbook_id + session_id）

| 工具 | 作用 | 备注 |
|---|---|---|
| `inspect(sheet)` | schema prompt、数值摘要、列名、sheet 列表 | 新会话自动调用 |
| `query(sql)` | 唯一 SQL 执行入口；经 sqlguard 校验后在 DuckDB 沙箱执行 | 结果写 `result_refs`，回写 L2 `last_query/last_result_ref` |
| `get_result(res_id)` / `latest_result()` | 取历史结果（图表、追问基于它） | — |
| `chart(spec)` | bar/line/pie/line_band → PNG + 底层数据 | 图随数据返回，数字可溯源 |
| `join_preview(left, right, on)` | 多表关联预览（不落盘） | — |
| `join_apply(...)` | 关联结果物化为 `create_sheet` 写操作 | 走确认流 |
| `summarize(sheet)` | 关键指标摘要（供总结类请求） | — |
| `diagnose(sheet)` | 异常洞察：空值聚集、常数列、z>3 离群、环比骤降 | — |
| `correlate(sheet)` | Pearson/Spearman + p 值 | scipy |
| `forecast(...)` | 线性回归预测 + 95% 置信带 | 诚实标注轻量模型 |
| `compute(code)` | pandas 降级沙箱（复杂变换） | import 白名单，见 [06 沙箱与安全](06-sandbox-security.md) |
| `eval_expression(expr)` | 派生列表达式在沙箱预计算 | 供 `add_column` |
| `recommend_questions()` | 依据 schema 生成 3–5 个推荐问题 | 规则生成，不调 LLM |

## 3. 结构化写工具

LLM 可见的写操作白名单（`prompts.py`，12 个）：

`set_cell` `set_range` `add_rows` `delete_rows` `fill_missing` `drop_duplicates` `cast_column` `add_column` `rename_column` `drop_column` `style_cells` `create_sheet`

执行器注册表（`mutations.py`，实际 16 个 + 内部 `noop`）：

| 执行器 | 逆操作 | 高风险 |
|---|---|---|
| `set_cell` / `set_cells` / `set_range` | `set_cells`（记录旧值） | — |
| `add_rows` | `delete_rows` | — |
| `delete_rows` | `add_rows`（记录行内容与位置） | ✅ |
| `add_column` | `drop_column` | — |
| `restore_column`（内部，用于逆 cast/drop） | — | — |
| `rename_column` | 反向 rename | — |
| `drop_column` | `restore_column`（保留列数据） | ✅ |
| `fill_missing` | `set_cells`（记录被填单元格旧值） | — |
| `drop_duplicates` | `add_rows` | ✅ |
| `cast_column` | `restore_column` | — |
| `create_sheet` | `drop_sheet` | ✅ |
| `drop_sheet`（内部） | — | — |
| `style_cells` | `style_restore`（记录旧样式） | — |
| `style_restore`（内部） | — | — |

- 参数校验失败抛 `OpError`，错误消息为中文（例如「列『销售额』不存在，可用列：…」），可直接回喂模型重试。
- 高风险操作（删行/删列/去重/建表）在 UI 与 MCP 中都会进入确认流；>50 单元格再叠加二次确认。
- 派生列：`add_column` 的表达式先经 `eval_expression` 在沙箱计算，再写入，避免模型手算。

## 4. diff 与确认

- `DiffResult`（`diff.py`）：受影响单元格计数、逐格旧值/新值、`to_html()` 渲染红绿 diff、`needs_double_confirm(50)`。
- UI 确认卡：`gradio_app.py` 的 `confirm_box/confirm_row`；「确认执行 / 取消」。
- MCP 与 UI 语义一致：`excel_propose_write` 只登记 `pending_write`，`excel_confirm_write` 落盘（>50 格要求 `double_confirm=true`），`excel_cancel_write` 取消。

## 5. 图表 / 分析 / 导出

| 能力 | 实现 | 说明 |
|---|---|---|
| 图表 | `tools/chart.py` | matplotlib Agg → PNG；类型 bar/line/pie/line_band；返回 `{png_path, columns, rows, title}`，图与数据一起返回 |
| 趋势/环比 | `tools/analytics.py#monthly_trend` | 按月聚合 + 环比 |
| 异常诊断 | `analytics.py#diagnose` | 空值聚集、常数列、z>3 离群、环比骤降 |
| 相关性 | `analytics.py#correlate` | Pearson/Spearman + p 值（scipy） |
| 预测 | `analytics.py#forecast` | 线性回归 + 残差标准差置信带，诚实标注「轻量模型」 |
| 报告素材 | `analytics.py#report_materials` | 聚合/异常/相关性汇总，供处方层 |
| xlsx 导出 | `workbook.py#export_xlsx` | v0 直接复制原始字节（AC-5）；否则 openpyxl 重建 + 样式 |
| PDF 报告 | `tools/exporter.py` | reportlab + 中文字体，一页式 |
| PNG 导出 | `exporter.py` | 图表复制 / 表格渲染为 PNG |

## 6. FastMCP Server

**启动**（stdio 传输）：

```powershell
.venv\Scripts\python.exe -m src.mcp_server.server
```

> 注意：启动入口是 `src.mcp_server.server` 模块（`server.py` 末尾 `mcp.run()`）。
> `python -m src.mcp_server` 不可用——该包没有 `__main__.py`（已知问题，见 [12 项目管理](12-project-management.md#4-已知问题与规格偏差)）。

**会话模型**：MCP 侧使用固定会话 `"mcp"`（`_MCP_SESSION`），所有客户端共享同一个活动工作簿与 `pending_write`；
与 Gradio 的 per-session 隔离不同，适合单用户本地工具化场景。

### Tools

| 工具 | 参数 | 返回 | 说明 |
|---|---|---|---|
| `list_workbooks()` | — | 工作簿列表 | 选择活动工作簿用 |
| `excel_schema(workbook_id)` | workbook_id | 各 sheet 的 schema 摘要 | 只读元数据，不塞整表 |
| `excel_query(workbook_id, sql)` | 同上 + SQL | columns/rows/total_rows/res_id | 走 sqlguard + 沙箱 |
| `excel_chart(workbook_id, spec)` | 同上 + 图表 spec | PNG 路径 + 数据 | bar/line/pie/line_band |
| `excel_join(workbook_id, left, right, on)` | 同上 | 预览/另存说明 | 走确认流 |
| `excel_propose_write(workbook_id, op)` | 同上 + 写操作 | diff + pending 标识 | **不落盘** |
| `excel_confirm_write(workbook_id, double_confirm)` | 同上 | 落盘结果（版本号/影响单元格数） | >50 格需 `double_confirm=true` |
| `excel_cancel_write(workbook_id)` | — | 取消确认 | 清除待定项 |
| `excel_undo(workbook_id)` | — | 撤销结果 | 逆操作 + 版本回退 |

### Resource

- `excel://{workbook_id}/schema`：只读 schema 摘要（列名/类型/缺失率/样本值），客户端可据此决定查询。

### 调用示例

```json
// 1) 提议写操作（不落盘）
{"tool": "excel_propose_write",
 "arguments": {"workbook_id": "wb_xxx",
               "op": {"op_type": "fill_missing",
                      "sheet": "订单明细",
                      "params": {"column": "销售额", "strategy": "mean"}}}}
// → 返回 diff（旧值红/新值绿）与 n_cells；>50 单元格时提示需要 double_confirm

// 2) 确认落盘
{"tool": "excel_confirm_write",
 "arguments": {"workbook_id": "wb_xxx", "double_confirm": true}}
// → {"ok": true, "version": 3, "n_cells": 37, ...}

// 3) 撤销
{"tool": "excel_undo", "arguments": {"workbook_id": "wb_xxx"}}
```

## 7. 已知限制

- MCP 会话为全局单例（`_MCP_SESSION="mcp"`），不支持多客户端并发隔离（登记于 [12 项目管理](12-project-management.md#4-已知问题与规格偏差)）。
- `python -m src.mcp_server` 不可用，需用 `python -m src.mcp_server.server`。
- MCP 侧没有暴露 chart 之外的分析工具（diagnose/forecast 未封装为 MCP tool），如需可按 `server.py` 现有模式扩展。
