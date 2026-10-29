# 项目文档导航

> 本目录是 **as-built（按实际实现撰写）** 的项目文档。需求基线是根目录的《[Excel智能体-规格说明书.md](../Excel智能体-规格说明书.md)》；
> 当实现与规格不一致时，以代码为准，并在 [12-project-management.md](12-project-management.md) 的「已知问题与规格偏差」中如实登记。
>
> 所有数字口径（评测结果、性能、耗时）以 `eval/report.md` 与 `eval/results_m*.json`、`eval/perf_result.json` 为唯一来源，文档不另抄第二份口径。
> 最后核对日期：2026-10-28（对应 v0.1.0）。

## 文档地图

| 编号 | 文档 | 读者 | 回答的问题 |
|---|---|---|---|
| 01 | [需求与验收](01-requirements.md) | 评审 / 老师 | 项目要做什么、不做什么、怎么算验收通过 |
| 02 | [总体架构](02-architecture.md) | 开发者 | 系统由哪些部分组成、一次请求如何流动、为什么这样设计 |
| 03 | [Agent 设计](03-agent-design.md) | 开发者 / 答辩 | LangGraph 主图、反思闭环、多 Agent 编排、写确认流 |
| 04 | [数据与存储](04-data-storage.md) | 开发者 | SQLite 表结构、Parquet 版本链、逆操作日志、撤销语义 |
| 05 | [工具与 MCP](05-tools-mcp.md) | 开发者 / 集成方 | 读/写工具清单、diff 与确认、FastMCP 接口与示例 |
| 06 | [沙箱与安全](06-sandbox-security.md) | 开发者 / 安全评审 | 模型生成代码如何被约束、密钥如何管理 |
| 07 | [记忆层](07-memory.md) | 开发者 / 答辩 | L1/L2/L3 三层记忆各自管什么、指代消解如何工作 |
| 08 | [用户手册](08-user-guide.md) | 使用者 | 界面怎么用、每个功能怎么操作、常见问题 |
| 09 | [部署与运维](09-deployment-ops.md) | 运维 / 组员 | 环境准备、数据目录、备份、故障排查 |
| 10 | [测试与评测](10-testing-eval.md) | 评审 / 开发者 | 52 个单测、30 题评测、消融实验、性能测试怎么跑、结果如何 |
| 11 | [演示视频脚本](11-demo-script.md) | 剪辑 / 配音 | 2 分 25 秒成片的分镜、台词、字幕、TTS 指南 |
| 12 | [项目管理](12-project-management.md) | 项目组 | 里程碑、风险、已知问题与规格偏差、路线图 |
| 13 | [队友上手指南](13-team-onboarding.md) | 组员 | 部署、功能彩排、答辩话术、PPT 素材、视频分工 |

## 按角色的阅读路径

- **第一次接触项目**：根 [README](../README.md) → [08 用户手册](08-user-guide.md) → 看 [演示视频](../recordings/excel_agent_demo.mp4)
- **接手开发**：[02 架构](02-architecture.md) → [03 Agent 设计](03-agent-design.md) → [04 数据与存储](04-data-storage.md) → [10 测试与评测](10-testing-eval.md)
- **准备答辩**：[01 需求与验收](01-requirements.md) → [03 Agent 设计](03-agent-design.md) → [07 记忆层](07-memory.md) → [10 测试与评测](10-testing-eval.md)（重点看消融与 AC-7 归因）
- **部署演示环境**：[09 部署与运维](09-deployment-ops.md) → [13 队友上手指南](13-team-onboarding.md) → [11 演示脚本](11-demo-script.md)
- **通过 MCP 接入**：[05 工具与 MCP](05-tools-mcp.md)

## 文档约定

1. 中文撰写；代码标识符、SQL、配置键保持英文原样。
2. 引用代码使用 `路径/函数名` 形式（如 `src/agents/nodes.py#act`），尽量不写会漂移的行号。
3. 所有 Mermaid 图直接内嵌在 Markdown 中，GitHub 原生渲染。
4. 评测数字只从 `eval/report.md` / `eval/results_m*.json` 引用；修改评测后需同步更新引用它的文档。
5. 任何与规格不一致的行为，必须在 [12-project-management.md](12-project-management.md) 的「已知问题与规格偏差」表中登记，并注明原因。
