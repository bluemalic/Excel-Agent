# 贡献指南（CONTRIBUTING）

感谢参与本项目。请先阅读 [docs/README.md](docs/README.md) 了解文档地图，再动手。

## 1. 环境准备

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env        # 填入你自己的 DeepSeek Key（绝不提交）
.venv\Scripts\python.exe -m pytest -q
```

依赖版本在 `requirements.txt` 中**锁定**；升级任何依赖请单独一个 PR，并附上 `pytest` 与评测冒烟（`eval\run.py --mode m2 --filter s1`）的结果。

## 2. 分支与提交

- 分支命名：`feat/xxx`、`fix/xxx`、`docs/xxx`、`eval/xxx`。
- 提交信息使用 Conventional Commits：
  - `feat: 支持批量导入多个 csv`
  - `fix: 撤销后样式未重放`
  - `docs: 补充 MCP 调用示例`
  - `eval: 重跑 m2 消融`
- 一个 PR 只解决一件事；改动超过 3 个文件时在描述里列出验证方式（命令 + 退出码 + 结果摘要）。

## 3. 代码规范

- 中文注释与中文文案（界面/错误消息/文档），代码标识符英文。
- 沿用现有分层：UI 不直接碰 SQLite/Parquet，一律走 `AgentService`/`Toolkit`。
- 新增读工具放 `src/tools/`，新写操作必须：执行器 + 逆操作 + diff + 注册进白名单 + 单测。
- 不引入新的重依赖；确需引入时说明理由并锁定版本。

## 4. 红线（违反直接打回）

- **绝不提交密钥**：`.env` 永远不入库；`.env.example` 只允许占位符。
- **绝不绕过写确认流**：任何落盘修改必须走 `plan_write → 用户确认 → apply_write`。
- **绝不伪造结果**：评测数字以脚本输出为准；文档不得手抄第二套口径。
- 表格数据不得整块进入 LLM 上下文。

## 5. 文档更新要求

- 行为变化必须同步更新对应 `docs/` 文档与 `CHANGELOG.md`。
- 文档中的数字只能引用 `eval/report.md` / `eval/results_*.json` / `perf_result.json`。
- 与规格不一致的行为在 [docs/12](docs/12-project-management.md)「已知问题与规格偏差」登记。

## 6. 本地验证清单（提交前）

```powershell
.venv\Scripts\python.exe -m pytest -q
git grep -nI "sk-" --cached          # 应无真实密钥（占位符 sk-xxxx / sk-your-deepseek-key 除外）
git check-ignore .env data .tmp      # 应全部命中
```

## 7. 问题与讨论

- 行为问题：开 Issue，附「复现步骤 / 期望 / 实际 / 日志」。
- 安全问题：不要开公开 Issue，按 [SECURITY.md](SECURITY.md) 私密报告。
