# 13 · 队友上手指南（部署 / 熟悉功能 / 答辩·PPT·视频分工）

> 给项目组成员的 30 分钟上手路径。按顺序做完 §1–§3，就能跑起来并演示；
> 答辩、PPT、视频制作者直接跳 §4–§6。

## 1. 五分钟部署

```powershell
git clone https://github.com/bluemalic/Excel-Agent.git
cd Excel-Agent

py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
# 用记事本打开 .env，把 OPENAI_API_KEY 换成自己的 DeepSeek Key
# DeepSeek 申请：https://platform.deepseek.com（个人充值 ¥10 足够日常+答辩演示）

.venv\Scripts\python.exe app.py
```

浏览器打开 `http://127.0.0.1:7860`。**首次启动要等 30–40 秒**（下载 embedding 模型 + 预热沙箱，只有第一次）。

验证装好了：

```powershell
.venv\Scripts\python.exe -m pytest -q      # 应输出 52 passed
```

常见问题：没配 Key 界面能开但不能对话；7860 被占用就换端口
（PowerShell：`$env:EXCEL_AGENT_PORT="7861"` 再启动）；详细排障见 [09 部署与运维](09-deployment-ops.md)。

> 注意：`.env` 绝不提交（已被忽略）；每个人用各自的数据目录互不干扰时，可设置
> `$env:EXCEL_AGENT_DATA_ROOT="data_我的名字"` 后再启动。

## 2. 十分钟熟悉功能（照着点一遍）

先看一遍成片视频（2 分 25 秒）：仓库首页 → `recordings/excel_agent_demo.mp4`。
然后自己照着演一遍（也是答辩演示的彩排）：

1. **接入**：点「加载示例数据（销售台账）」；看数据卡片（列/缺失率/样本值）和推荐问题。
2. **描述层**：问「各大区销售额总和是多少？」——注意回答末尾的「执行的查询」（每个数字可复现）。
3. **指代**：接着问「把上面的结果画成柱状图」→「那华南呢？也按月份看趋势」——体会 L2 工作记忆。
4. **诊断层**：问「分析订单明细有没有异常，并给出相关性结论」；右侧切「反思」看 R1/R2 轨迹。
5. **写操作**：问「用均值填充『销售额』列的缺失值」→ 看 diff 红绿预览 →「确认执行」→「撤销」；
   再试「把第1行负责人改成测试员」→ 确认 → 撤销。
6. **处方层 + 交付**：问「帮我出一份月度经营小结，包含趋势和异常月份」→ 右侧「报告」页 → 导出 xlsx / PDF。
7. **会话**：点「新建会话」→ 会话历史里「打开」旧会话（聊天/图表恢复）→「记忆」面板看 L1/L2/L3 → 快照恢复。

每一步的原理一句话解释在 [08 用户手册](08-user-guide.md)；想讲深了再看 [03 Agent 设计](03-agent-design.md)。

## 3. 演示口径（答辩现场照这个说）

| 观众可能问 | 一句话回答 | 深挖出处 |
|---|---|---|
| 数据会不会被模型拿走？ | 表格数据永不整块进上下文，模型只看 schema 摘要 + 统计 + 结果前 20 行 | [02 架构](02-architecture.md) |
| 改数据怎么保证安全？ | 模型只能选 12 种结构化写操作；先 diff 红绿预览、确认后落盘，>50 格二次确认 | [05 工具与 MCP](05-tools-mcp.md) |
| 撤销为什么可靠？ | 每次写操作记录逆操作 + Parquet 版本快照；撤销回初始态与原文件逐字节一致（sha256 单测） | [04 存储与版本](04-data-storage.md) |
| 「那华南呢」怎么听懂的？ | L2 工作记忆记住上轮表/查询/结果引用，指代词触发一次轻量改写成完整问题 | [07 记忆层](07-memory.md) |
| 反思失败会死循环吗？ | 预算 2 次：R1 规则毫秒级先查，R2 独立上下文复核，超限转 R3 向用户澄清 | [03 Agent 设计](03-agent-design.md#3-反思闭环) |
| 多 Agent 图什么？ | 三条铁律：写只走 Writer 且串行、读可并行、简单请求零开销不经 supervisor | [03 Agent 设计](03-agent-design.md#4-多agent编排) |
| 模型生成 SQL 会不会乱来？ | 语句/表/列/函数四层白名单 + 子进程 5s 超时 + 512MB + 5000 行上限 | [06 沙箱与安全](06-sandbox-security.md) |
| **AC-7 为什么没达标？** | **如实答**：M0 无反思就已 30/30，没有提升空间（0pp）；M2 实测有 1 次 R1 重试修复成功，说明机制在真实失败时有效；评测未做修饰 | [10 测试与评测](10-testing-eval.md#4-实测结果) |
| 结果数字可信吗？ | 30 题黄金 SQL 客观比对 + 写后状态断言 + 陷阱题规则 + LLM judge，全 100%；报告在 `eval/report.md` | [10 测试与评测](10-testing-eval.md) |

**答辩必背的 5 个数字**：30/30（100%）· 10 万行热态 5.2s（≤10s 达标）· 52 个单测 · 撤销逐字节一致（AC-5）· M2 平均 20.8s/题。

## 4. PPT 素材清单（都在仓库里，直接取用）

| 素材 | 位置 |
|---|---|
| 架构图（Mermaid，可截图） | `README.md` 或 [docs/02](02-architecture.md) |
| LangGraph 主循环 / Supervisor 拓扑 / 写确认时序图 | [docs/03](03-agent-design.md) |
| SQLite ER 图 / 版本链图 | [docs/04](04-data-storage.md) |
| 记忆三层图 / 沙箱防线图 | [docs/07](07-memory.md) / [docs/06](06-sandbox-security.md) |
| 评测成功率 / 耗时柱状图（PNG） | `docs/assets/eval_success.png`、`eval_latency.png` |
| 界面截图 16 张 | `eval/ui_shots/`（首页/对话图表/diff 确认/反思/流式/回放/记忆） |
| 评测表 / AC 核对表 | `eval/report.md`、[docs/01](01-requirements.md#5-验收标准ac与实测状态) |
| 演示视频（可截段） | `recordings/excel_agent_demo.mp4`（2:25，章节见 [docs/11](11-demo-script.md#2-分镜与台词)） |

**建议的 7 页结构**：定位与四大能力 → 架构图 + 「数据不进上下文」铁律 → 安全写操作（diff/撤销时序图）→ 记忆与反思（三层图 + R1/R2/R3）→ 评测结果（柱状图 + AC 表，如实说明 AC-7）→ 视频演示（播 1:14–2:03 写操作+报告段最有说服力）→ 已知限制与路线图（[docs/12](12-project-management.md)）。

## 5. 视频制作（配音/字幕/剪辑）

全部材料在 `docs/11-demo-script.md`：

- 画面底稿（无音轨）：`recordings/excel_agent_demo.mp4`
- 台词：`docs/assets/demo_narration.txt`（560 字，9 章）
- 字幕草稿：`docs/assets/demo.srt`
- 配音：`pip install edge-tts` 后按 docs/11 §4 逐章生成（`zh-CN-YunxiNeural`，`--rate=+5%`），
  再按 §5 与画面混流、§6 烧硬字幕（ffmpeg 命令都已写好，复制即用）。
- 时长基准：2:25；台词已按每章画面时间预排，**逐章配音后拼接**，不要整篇变速。

## 6. 组内分工建议

| 角色 | 用这份文档的哪部分 |
|---|---|
| 部署/环境 | §1 + [09 部署与运维](09-deployment-ops.md) |
| 产品演示（答辩主讲） | §2 + §3 + [08 用户手册](08-user-guide.md) |
| PPT 制作 | §4（素材全部现成） |
| 视频剪辑/配音 | §5 + [11 演示脚本](11-demo-script.md) |
| 技术答疑（架构/评测） | [02](02-architecture.md) [03](03-agent-design.md) [10](10-testing-eval.md) + `eval/report.md` |
