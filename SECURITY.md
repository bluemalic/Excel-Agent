# 安全策略（SECURITY）

## 支持版本

| 版本 | 状态 |
|---|---|
| v0.1.x（main 分支） | ✅ 支持 |
| 更早提交 | ❌ 不支持 |

## 报告漏洞

**不要**用公开 Issue 报告安全问题。请通过 GitHub 私密渠道联系仓库所有者
（仓库页 → Security → Report a vulnerability，或 privately security advisory）。

请在报告中包含：影响范围、复现步骤、可能的修复方向。我们会在 7 天内首次回应。

## 密钥处理（对贡献者）

- DeepSeek/OpenAI 兼容密钥只放 `.env`（已被 `.gitignore` 忽略），仓库内只允许占位符。
- 若你发现任何提交/文档/截图/视频中包含真实密钥：
  1. **立即在服务商控制台轮换该密钥**（这是第一优先级，比清理历史更重要）；
  2. 通知仓库所有者移除内容并评估历史改写（`git filter-repo` / force push）；
  3. 在 SECURITY advisory 中记录。

## 内置安全机制（供评估参考）

- 模型生成的 SQL/代码在子进程沙箱执行：语句/表/列/函数白名单、超时击杀、内存与行数上限（[docs/06](docs/06-sandbox-security.md)）。
- 写操作必须经 diff 预览与人工确认，带逆操作日志（[docs/04](docs/04-data-storage.md)）。
- 密钥不进上下文、不写日志；表格数据不整块进入 LLM（[docs/02](docs/02-architecture.md)）。

## 已知边界

- 本项目定位为**单机单用户**工具：无认证、无多租户隔离，不要把 7860 端口直接暴露到公网。
- MCP Server 使用固定会话（`mcp`），多客户端共享状态（[docs/05](docs/05-tools-mcp.md#6-fastmcp-server)）。
- 依赖以 `requirements.txt` 锁定版本；升级前请复核供应链（锁文件变更逐条审查）。
