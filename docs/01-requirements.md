# 01 · 需求与验收

> 需求基线：根目录《[Excel智能体-规格说明书.md](../Excel智能体-规格说明书.md)》（§0 定位、§9 FR/AC）。
> 本文档是面向验收的摘要 + 实测状态；逐条 FR 的实现位置见「需求追踪矩阵」。

## 1. 项目定位

**一句话**：上传或选择一份表格，用中文对话完成「看清数据 → 清洗整理 → 分析出图 → 得出结论 → 改数交付」，全过程可追溯、可撤销、会反省。

四个动词对应四大能力：

| 动词 | 能力 | 主要模块 |
|---|---|---|
| 看懂 | schema 抽取 + 语义理解 | `src/core/schema_extract.py`、`src/tools/toolkit.py` |
| 改得动 | 结构化写操作 | `src/core/mutations.py`、`src/core/workbook.py` |
| 记得住 | 多轮上下文与指代 | `src/memory/`、`src/agents/nodes.py#resolve_l2` |
| 会反省 | 执行失败自纠 | `src/agents/reflector.py`、`src/agents/graph.py` |

范围档位：**C 档**（在 B 档分析 + 清洗/派生 + 撤销之上，增加模板创建、任意单元格编辑/格式化、格式化导出）。

## 2. 范围与非目标

**做**：单机 Web 应用（Gradio），本地 SQLite + Parquet 存储，DeepSeek（OpenAI 兼容）作为 LLM，Mem0 自托管做长期记忆，FastMCP 对外暴露工具。

**明确不做**（规格 §0）：多人协同 / 账号体系 / 宏·VBA·复杂公式求值引擎 / 对接外部数据库 / 训练或微调模型 / 移动端适配。

**边界说明**：`.xls` 老格式可读但不做合并单元格填充（仅 `.xlsx` 支持）；见 [已知问题](12-project-management.md#4-已知问题与规格偏差)。

## 3. 可演示的 8 类能力（对应规格 §9 FR 清单）

| # | 能力 | 演示问句/操作 |
|---|---|---|
| 1 | 数据接入 | 上传 Excel/CSV；选内置示例（销售台账/考勤表/问卷）；模板建空表后对话填数；多 sheet 切换 |
| 2 | 分析四层 | 描述 →「各大区销售额总和」；诊断 →「分析有没有异常/相关性」；预测 →「预测下季度销售额」；处方 →「出一份月度经营小结」 |
| 3 | 多表关联 | 「把订单明细和商品主表按产品关联一下」（预览确认后另存新表） |
| 4 | 写操作 | 「用均值填充销售额缺失值」「删重复行」「把折扣率转成数值」「新增利润列」「把第1行负责人改成张三」「给表头加粗」——全部先出 diff 预览，>50 单元格二次确认 |
| 5 | 撤销与版本 | 撤销/重做/回到第 N 步/命名快照；撤销回 v0 与原始文件逐字节一致 |
| 6 | 记忆与反思 | 「那华南呢」「按刚才的再拆一层」；反思模式 m0/m1/m2 现场对比；右侧「反思过程」页 |
| 7 | 格式化导出 | 一键导出带样式 xlsx / PDF 报告 / 表格 PNG |
| 8 | 对外 MCP | `python -m src.mcp_server.server`（stdio），见 [05 工具与 MCP](05-tools-mcp.md) |

## 4. 功能需求（FR）与追踪矩阵

> FR 原文见规格 §9.1。「实现位置」指向主要代码；「测试」指向单测或评测题类别。

| FR | 需求 | 实现位置 | 验证 |
|---|---|---|---|
| FR-1.1 | 上传 xlsx/xls/csv ≤20MB | `src/core/importer.py` `src/ui/gradio_app.py#on_upload` | tests/test_importer.py |
| FR-1.2 | 自动识别表头，低置信度允许手动指定 | `importer.py#_detect_header`（阈值 0.62）+ UI「手动指定表头行」 | tests/test_importer.py |
| FR-1.3 | 多工作表列出并切换 | `workbook.py#list_sheet_names` + UI sheet 下拉 | 手动 |
| FR-1.4 | 内置示例 ≥3 份 | `samples/`（销售台账/考勤/问卷 + 3 张脏表） | `scripts/make_samples.py` 固定种子 |
| FR-1.5 | UTF-8/GBK 编码识别 | `importer.py#_read_csv`（utf-8-sig→gb18030→utf-16→latin-1） | tests/test_importer.py |
| FR-2.1 | 预览前 100 行 | `src/ui/gradio_app.py` 预览视图（自绘数据网格） | UI |
| FR-2.2 | 数据卡片（行列数/类型/缺失率/极值均值） | `src/core/schema_extract.py` + 数据卡片 | UI |
| FR-2.3 | 依据 schema 生成 3–5 个推荐问题 | `toolkit.py#recommend_questions`（规则生成，不调 LLM） | UI |
| FR-2.4 | 标记全空列/重复列名/文本型数值/日期文本 | `schema_extract.py` 异常标记 | tests/test_importer.py |
| FR-3.1 | 中文问答返回表格+文字 | 主图 `respond` + Markdown 表 | 评测 single 类 |
| FR-3.2 | 每条回答附「执行的查询」 | `nodes.py#respond` + trace + 查询记录页 | 评测 trace_brief |
| FR-3.3 | 多轮 ≥10 轮上下文 | L1 滚动摘要（阈值 24 条）+ `recent_turns` | 评测 multi 类 |
| FR-3.4 | 指代消解 | `nodes.py#_resolve_reference` + L2 | 评测 multi 6/6 |
| FR-3.5 | 对结果排序/取前 N/换图 | L2 `last_result_ref` + 追问改写 | 评测 m6 |
| FR-3.6 | 复合请求拆步骤、显示进度 | `plan` + 流式 stage 事件 | UI 流式 |
| FR-3.7 | 无法回答说明原因、不编造 | R3 澄清 + 陷阱题判定 | 评测 trap 4/4 |
| FR-4.1 | 描述/聚合/趋势/同比环比 | `src/tools/analytics.py` + SQL | 评测 single |
| FR-4.2 | 自动出图可切换 | `src/tools/chart.py`（bar/line/pie/line_band） | 评测 m6 |
| FR-4.3 | 主动提示数据问题 | `analytics.py#diagnose` 空值/离群/骤降 | 评测 single |
| FR-4.4 | 100–300 字分析小结 | `respond` 篇幅约束 + judge | 评测 summary |
| FR-4.5 | 一页式报告导出 | `src/tools/exporter.py`（PDF）+ 报告页 | 评测 r4 |
| FR-5.1–5.4 | 填缺失/去重/类型转换/派生列 | `mutations.py` 执行器 | 评测 write 类 |
| FR-5.5 | 所有修改先 diff 预览 | `plan_write`（不落盘）+ 确认卡 | 评测 write diff_shown |
| FR-5.6 | >50 单元格二次确认 | `diff.py#needs_double_confirm` | tests/test_versions.py |
| FR-6.1–6.4 | 撤销/重做/跳转/快照/前后对比 | `workbook.py#undo/redo/goto/snapshot` | tests/test_versions.py |
| FR-7.1–7.4 | 三层记忆 | [07 记忆层](07-memory.md) | 评测 multi |
| FR-8.1–8.4 | 三层反思、界面可见 | [03 Agent 设计](03-agent-design.md#3-反思闭环) | 评测 m2 |
| FR-9.1–9.3 | diff 确认/取消/反馈重试 | 确认卡 + 取消 + R1/R2 重试 | UI + 评测 |
| FR-10.1–10.3 | 会话新建/清空/切换/回放 | `service.py#clear_conversation/delete_session` + UI | tests/test_sessions.py |
| C 档增量 | 模板/任意编辑/join/导出/报告 | `templates/*.yaml`、`style_cells`、`join_preview/apply`、`exporter.py` | tests/test_templates_mcp.py |

## 5. 验收标准（AC）与实测状态

> 判定数据来源：`eval/report.md`（2026-10-28 生成）、`eval/perf_result.json`、`tests/`。逐条说明见 [10 测试与评测](10-testing-eval.md)。

| 编号 | 验收条件 | 实测结果 | 状态 |
|---|---|---|---|
| AC-1 | 3 张真实脏表正确识别列名并预览 | 合并单元格 / 两行表头 / GB2312 中文列名，单测 + 手动指定表头行覆盖 | ✅ |
| AC-2 | 30 题端到端成功率 ≥85% | M2 = 30/30（100%） | ✅ |
| AC-3 | 6 道指代题 ≥80% | 6/6 | ✅ |
| AC-4 | 4 道陷阱题 100% 澄清且无编造 | 4/4 触发澄清/诚实说明（判分为规则 + 关键词，回答全文留档供人工复核） | ✅ |
| AC-5 | 写操作可撤销且逐字节一致 | `tests/test_versions.py` sha256 断言（v0 与原始文件字节一致） | ✅ |
| AC-6 | 写前必有 diff 预览 | 修改类 6 题 diff_shown 全部为 true | ✅ |
| AC-7 | 开反思后成功率提升 ≥10pp | M0 100% → M2 100%，提升 0pp | ❌（归因见下） |
| AC-8 | 10 万行单轮查询 ≤10s | 热态 5.2s；冷启动预热 29.1s（一次性） | ✅ |
| AC-9 | 工具失败会话不中断 | 沙箱超时击杀重建单测 + 节点级 try/except | ✅ |
| AC-10 | 每个数字可用「执行的查询」复现 | trace 含 res_id/sql，「查询记录」页可复现；**未自动化判定，人工抽查** | — |

### AC-7 归因（如实说明）

M0（无反思）在本评测集上首轮生成已达 30/30，可提升空间为零，因此「反思提升 ≥10pp」无法满足，实测 0pp。
M2 运行中观测到 1 次 R1 重试并成功修复（r1 题：子查询嵌套过深 → 简化后通过），说明反思机制在真实失败时有效，
但它不是提高本评测集成功率的杠杆。**未做任何数据修饰**。若要验证反思的收益，可用同一脚本更换更弱的生成模型或扩充更难的口径题后重跑（命令见 [10 测试与评测](10-testing-eval.md)）。
