"""Excel 对话式数据分析智能体 · 启动入口。

用法：
    .venv/Scripts/python.exe app.py
然后浏览器打开 http://127.0.0.1:7860
首次运行请在项目根目录准备 .env（参考 .env.example 填入 DeepSeek Key）。
"""

from __future__ import annotations

import logging
import time

from src import config


def warm_up() -> None:
    """启动预热：Mem0 embedding 模型加载 + SQL 沙箱子进程（一次性，不计入单次查询）。"""
    from src.memory.longterm import get_longterm_memory
    from src.sandbox.runner import SqlRunner

    t0 = time.time()
    ltm = get_longterm_memory()
    if ltm.available:
        # 触发一次检索，把 sentence-transformers 前向计算也预热掉
        ltm.search("预热", limit=1)
        print(f"L3 长期记忆（Mem0）就绪，预热耗时 {time.time() - t0:.1f}s", flush=True)
    else:
        print(f"L3 长期记忆降级关闭：{ltm.disabled_reason}", flush=True)
    SqlRunner.instance().warmup()
    print("SQL 沙箱子进程就绪", flush=True)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)

    config.ensure_dirs()

    if not config.llm_configured():
        print("=" * 62)
        print("尚未配置 LLM 密钥：界面可以启动、可以导入数据，")
        print("    但对话功能需要 DeepSeek Key。")
        print("    请复制 .env.example 为 .env 并填入 OPENAI_API_KEY 后重启。")
        print("=" * 62)

    warm_up()

    import os

    from src.ui.gradio_app import launch

    port = int(os.getenv("EXCEL_AGENT_PORT", "7860"))
    launch(server_name="127.0.0.1", server_port=port)


if __name__ == "__main__":
    main()
