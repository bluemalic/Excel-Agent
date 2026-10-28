# 09 · 部署与运维

> 覆盖：环境准备、启动、数据目录、备份、日志、故障排查、发布前检查。
> 目标环境：单机、单实例、本地存储（无外部依赖服务）。

## 1. 环境要求

| 项 | 要求 |
|---|---|
| Python | 3.11（按 3.11 开发与锁定依赖版本） |
| LLM | DeepSeek API Key（OpenAI 兼容端点），评测约 ¥1–3/遍 |
| 网络 | 首次运行通过 HF 镜像下载 embedding 模型（约 90MB），之后可离线 |
| 操作系统 | Windows / Linux / macOS 均可（路径分隔符注意）；开发与实测在 Windows |
| 浏览器 | 任意现代浏览器（截图自检用系统 Edge） |

## 2. 安装与启动

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env        # 编辑 .env，填入 OPENAI_API_KEY
.venv\Scripts\python.exe app.py
```

- 默认地址 `http://127.0.0.1:7860`；端口用环境变量 `EXCEL_AGENT_PORT` 覆盖。
- 数据根用 `EXCEL_AGENT_DATA_ROOT` 覆盖（演示/评测隔离，例如 `data_demo`）。
- 首次启动预热：Mem0 加载本地 embedding + 一次检索、SQL 沙箱子进程导入；合计约 30–40 秒（一次性）。

## 3. 数据目录布局

| 路径 | 内容 | 说明 |
|---|---|---|
| `data/meta.sqlite` | 工作簿/表/版本/ops/样式/快照/会话/聊天/结果引用 | 核心元数据 |
| `data/workspaces/<wb_id>/v*.parquet` | 各 sheet 版本快照 | 数据本体 |
| `data/workspaces/charts/*.png` | 对话生成的图表 | 可删除 |
| `data/results/` | 导出文件 | 可删除 |
| `data/checkpoints.sqlite` | LangGraph 执行轨迹 | 可删除 |
| `data/mem0/` | qdrant 本地库 + history.db | L3 长期记忆 |
| `.tmp/` | DuckDB 临时文件 | 已加入 `.gitignore`；可清理（应用停止后） |

## 4. 备份 / 恢复 / 清理

- **备份**：停止应用后整目录拷贝 `data/`（SQLite + Parquet + mem0）。
- **恢复**：放回原相对路径结构；`meta.sqlite` 与 Parquet 必须配套。
- **重置**：删除 `data/` 即回到出厂状态（含记忆）；只清聊天/记忆见 [07 记忆层](07-memory.md#6-降级与隐私)。
- **清理 `.tmp/`**：DuckDB 临时文件，应用停止后可删除。

## 5. 日志

- 应用日志走标准 logging，输出到 stdout；评测/运行日志文件匹配 `*.log`（已被 git 忽略）。
- 关键日志：导入结果、SQL 执行失败与重试、Mem0 初始化结果、沙箱超时重建。
- UI 上的「反思过程」「查询记录」页是排查回答质量的第一个人工入口。

## 6. 故障排查表

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动即提示未配置密钥 | `.env` 缺失或 Key 为空 | 复制 `.env.example` 并填写；重启 |
| `L3 记忆 已降级` | 离线且无本地模型 / qdrant 锁被占 / 初始化异常 | 看启动日志的 mem0 原因；确保单实例；联网跑一次让模型落盘 |
| `Address already in use` / 端口占用 | 7860 被占 | `EXCEL_AGENT_PORT=7861` 启动 |
| 查询报「列不存在」 | 模型幻觉列名 | 系统会回喂真实列名自动重试；若仍失败，换问法或检查表头行 |
| 导入中文乱码 | CSV 编码冷门 | 导入器按 utf-8-sig→gb18030→utf-16→latin-1 探测；仍失败可转存 UTF-8 |
| 合并单元格错位 | `.xls` 不支持合并单元格填充 | 转存 `.xlsx` |
| 撤销后导出与预期不符 | 撤销到中间版本是 Parquet 重建 + 样式重放 | 对照 [04 数据与存储](04-data-storage.md#4-撤销逐字节一致的口径ac-5)；v0 才是字节一致 |
| 评测中途失败 | 网络/Key 额度 | 直接重跑原命令（断点续跑）；`--fresh` 全量重跑 |
| Mem0 报迁移目录锁 | 多进程并发 | 串行执行；后启动者自动降级 L3 |

## 7. 发布前安全检查

```powershell
# 1) 确认 .env 被忽略（应输出 .env）
git check-ignore .env
# 2) 暂存区无真实密钥（应无输出）
git grep -nI "sk-" --cached
# 3) 确认大目录被忽略
git check-ignore .tmp recordings/raw data .venv
# 4) 单文件体积检查（应无 >20MB 的待提交文件）
git diff --cached --stat | Sort-Object -Descending
```

## 8. 性能参考（实测）

| 指标 | 数值 | 说明 |
|---|---|---|
| 10 万行单轮查询（热态） | 5.2s | 端到端，含 LLM 生成与回答 |
| 冷启动预热 | 29.1s | 模型加载 + 沙箱导入，一次性 |
| 30 题评测 | M0 11.2s/题、M1 11.0s/题、M2 20.8s/题 | 含网络时延 |
| 应用启动 | 预热后 <5s 出 UI | 首次含模型下载更久 |

## 9. 升级与回滚

- 依赖版本在 `requirements.txt` 中锁定；升级前跑 `pytest -q` 与评测冒烟（`--filter s1 --mode m2`）。
- 回滚 = checkout 上一提交 + 重建 `.venv`；数据目录与代码解耦，回滚不影响 `data/`。
