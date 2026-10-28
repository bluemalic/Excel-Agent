"""全局配置：路径、环境变量、模型参数。

密钥只从 .env / 环境变量读取，绝不硬编码。所有路径统一由本模块解析，
保证 app.py、评测脚本、MCP Server、pytest 用到同一套数据根目录。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录 = src/ 的上一级
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 加载 .env（存在才加载，评测/CI 环境允许没有）
load_dotenv(PROJECT_ROOT / ".env")


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


# ---------------------------------------------------------------------------
# LLM（OpenAI 兼容端点，指向 DeepSeek）
# ---------------------------------------------------------------------------
OPENAI_API_KEY = _env("OPENAI_API_KEY")
OPENAI_BASE_URL = _env("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
OPENAI_MODEL = _env("OPENAI_MODEL", "deepseek-chat")
LLM_TEMPERATURE = float(_env("LLM_TEMPERATURE", "0.0") or 0.0)
LLM_MAX_TOKENS = int(_env("LLM_MAX_TOKENS", "2048") or 2048)

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
DATA_ROOT = (PROJECT_ROOT / _env("EXCEL_AGENT_DATA_ROOT", "data")).resolve()
SAMPLES_DIR = PROJECT_ROOT / "samples"
TEMPLATES_DIR = PROJECT_ROOT / "templates"

DB_PATH = DATA_ROOT / "meta.sqlite"          # 元数据/会话/日志/版本/样式
WORKSPACE_DIR = DATA_ROOT / "workspaces"     # 每 workbook 一个目录（parquet/原始文件）
RESULTS_DIR = DATA_ROOT / "results"          # 查询结果引用（res_id）
MEM0_PATH = DATA_ROOT / "mem0"               # Mem0 本地存储（qdrant + history db）

# HuggingFace 镜像：国内网络首次下载 embedding 模型用；须在 import HF 相关库之前设置
if _env("HF_ENDPOINT"):
    os.environ.setdefault("HF_ENDPOINT", _env("HF_ENDPOINT"))


def ensure_dirs() -> None:
    """启动时确保数据目录存在。"""
    for p in (DATA_ROOT, WORKSPACE_DIR, RESULTS_DIR, MEM0_PATH):
        p.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 沙箱与安全参数（规格 §4）
# ---------------------------------------------------------------------------
SQL_TIMEOUT_SECONDS = 5          # 单条 SQL 执行超时
SQL_MAX_ROWS = 5000              # 结果行数上限
SQL_MEMORY_LIMIT = "512MB"       # DuckDB 内存上限
WRITE_CONFIRM_THRESHOLD = 50     # 影响单元格 > 该值时强制二次确认
UPLOAD_MAX_BYTES = 20 * 1024 * 1024  # 上传文件 ≤ 20MB

# ---------------------------------------------------------------------------
# 反思闭环（规格 §6）
# ---------------------------------------------------------------------------
MAX_REFLECTION = 2               # R1/R2 重试预算：第 3 次失败转 R3 澄清

# ---------------------------------------------------------------------------
# 记忆（规格 §5）
# ---------------------------------------------------------------------------
L1_COMPRESS_THRESHOLD = 24       # 对话历史超过该条数则滚动摘要压缩
L2_RESULT_PREVIEW_ROWS = 8       # last_result_ref 注入上下文时的样例行数


def llm_configured() -> bool:
    """是否已配置可用的 LLM Key（决定 UI 提示与评测前置检查）。"""
    return bool(OPENAI_API_KEY) and not OPENAI_API_KEY.startswith("sk-xxxx")
