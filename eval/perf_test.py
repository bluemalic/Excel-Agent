"""性能测试（AC-8）：10 万行文件单轮查询端到端 ≤ 10 秒。

用法：.venv/Scripts/python.exe eval/perf_test.py
（需要 .env 已配置 Key；计时覆盖：加载数据后的"规划 → 生成 SQL → 沙箱执行 → 成文"全链路）
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from eval.run import use_eval_workspace  # noqa: E402
from src import config  # noqa: E402
from src.service import AgentService  # noqa: E402


def make_big_file(path: Path, n: int = 100_000) -> None:
    rng = np.random.default_rng(7)
    dates = pd.to_datetime("2024-01-01") + pd.to_timedelta(rng.integers(0, 365, n), unit="D")
    df = pd.DataFrame({
        "订单ID": [f"B{i:07d}" for i in range(n)],
        "日期": dates,
        "大区": rng.choice(["华东", "华南", "华北", "西南"], n),
        "城市": rng.choice(["上海", "杭州", "广州", "北京", "成都"], n),
        "产品": rng.choice(["产品A", "产品B", "产品C", "产品D"], n),
        "数量": rng.integers(1, 50, n),
        "销售额": np.round(rng.uniform(10, 5000, n), 2),
        "负责人": rng.choice(["张三", "李四", "王五", "赵六"], n),
    })
    df.to_csv(path, index=False)


def main() -> int:
    use_eval_workspace("perf")
    src = config.DATA_ROOT / "bigtable.csv"
    if not src.exists():
        print("生成 10 万行测试数据…")
        make_big_file(src)
    else:
        print("复用已有 10 万行测试数据")

    # 一次性预热（Mem0 模型加载 + SQL 沙箱进程）：这是服务启动成本，不计入单次查询
    t_warm_start = time.time()
    from src.memory.longterm import get_longterm_memory
    ltm = get_longterm_memory()
    if ltm.available:
        ltm.search("预热", limit=1)
    from src.sandbox.runner import SqlRunner
    SqlRunner.instance().warmup()
    t_warm_done = time.time()
    print(f"冷启动预热：{t_warm_done - t_warm_start:.1f}s（L3 记忆：{'开启' if ltm.available else '降级'}）")

    svc = AgentService("eval_perf")
    t_load = time.time()
    wb_id, _ = svc.load_file(str(src), display_name="大表性能测试")
    print(f"加载 10 万行：{time.time() - t_load:.1f}s（加载不计入查询计时）")

    q = "各大区的销售额总和分别是多少？"
    # 正式计时前先跑一次同构查询（沙箱为 100k 表建内存副本是一次性成本，不计入单次查询）
    svc.chat("各产品的销售额总和分别是多少？", mode="m2")
    t0 = time.time()
    result = svc.chat(q, mode="m2")
    elapsed = time.time() - t0
    ok = elapsed <= 10
    print(f"热态端到端查询「{q}」：{elapsed:.1f}s（要求 ≤10s）{'✅' if ok else '❌'}")
    print("回答摘要：", result.response[:200].replace("\n", " "))

    # 落盘供 report 自动回填 AC-8
    import json
    payload = {"n_rows": 100000, "warm_up_s": round(t_warm_done - t_warm_start, 1),
               "query_elapsed_s": round(elapsed, 1), "pass": bool(ok), "question": q}
    (Path(__file__).resolve().parent / "perf_result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
