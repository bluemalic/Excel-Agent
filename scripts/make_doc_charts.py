"""生成 docs/assets 下的评测图表（开发自检用，随文档一起交付）。

数据来源：eval/results_m0.json / results_m1.json / results_m2.json（唯一口径）。
运行：.venv/Scripts/python.exe scripts/make_doc_charts.py
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "assets"

MODES = [("m0", "M0 无反思"), ("m1", "M1 仅R1"), ("m2", "M2 R1+R2")]
COLOR = "#2f6f4f"

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "sans-serif"]
plt.rcParams["axes.unicode_minus"] = False


def load_stats(mode: str) -> tuple[float, float]:
    """返回 (成功率%, 平均耗时秒)。数据结构与 eval/run.py 输出一致。"""
    data = json.loads((ROOT / "eval" / f"results_{mode}.json").read_text(encoding="utf-8"))
    total = len(data)
    passed = sum(1 for item in data.values() if item.get("pass"))
    elapsed = [float(item.get("elapsed_s") or 0.0) for item in data.values()]
    return passed / total * 100.0, sum(elapsed) / len(elapsed)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    labels = [label for _, label in MODES]
    rates = []
    latencies = []
    for mode, _ in MODES:
        rate, latency = load_stats(mode)
        rates.append(rate)
        latencies.append(latency)

    # 图 1：成功率
    fig, ax = plt.subplots(figsize=(6, 3.4), dpi=150)
    bars = ax.bar(labels, rates, color=COLOR, width=0.55)
    ax.set_ylim(0, 110)
    ax.set_ylabel("30 题成功率（%）")
    ax.set_title("消融实验：30 题端到端成功率")
    for bar, rate in zip(bars, rates):
        ax.text(bar.get_x() + bar.get_width() / 2, rate + 2, f"{rate:.0f}%",
                ha="center", va="bottom", fontsize=10)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(OUT / "eval_success.png")
    plt.close(fig)

    # 图 2：平均耗时
    fig, ax = plt.subplots(figsize=(6, 3.4), dpi=150)
    bars = ax.bar(labels, latencies, color="#40646e", width=0.55)
    ax.set_ylabel("平均耗时 / 题（秒）")
    ax.set_title("消融实验：平均耗时（含 DeepSeek 网络时延）")
    for bar, latency in zip(bars, latencies):
        ax.text(bar.get_x() + bar.get_width() / 2, latency + 0.3, f"{latency:.1f}s",
                ha="center", va="bottom", fontsize=10)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(OUT / "eval_latency.png")
    plt.close(fig)

    print("已生成：", *(str(OUT / name) for name in ("eval_success.png", "eval_latency.png")))


if __name__ == "__main__":
    main()
