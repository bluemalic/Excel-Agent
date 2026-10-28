"""评测主脚本（规格 §7/§11）：真实调用 LLM 跑完 30 题，结果落盘 JSON。

用法（在项目根目录，.env 已配置 DeepSeek Key）：
    .venv/Scripts/python.exe eval/run.py --mode m2            # 跑一遍完整 30 题
    .venv/Scripts/python.exe eval/run.py --mode m0            # 无反思消融
    .venv/Scripts/python.exe eval/run.py --mode m1 --filter w # 只跑修改类
    .venv/Scripts/python.exe eval/run.py --mode m2 --fresh    # 忽略断点重跑

模式：
    m0 = 无反思（生成即执行）   m1 = 仅 R1 规则校验 + 重试   m2 = R1 + R2 Reflector（完整）

说明：
- 每题独立 session 与全新 workbook，黄金答案由参考 SQL 在同一份真实数据上先算出；
- 写操作自动走 diff → 确认（>50 单元格自动二次确认），并记录「已出 diff 预览」（AC-6）；
- 每题结果断点落盘，中断后重跑自动跳过已完成题（--fresh 覆盖）。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402
from src.core.workbook import WorkbookManager  # noqa: E402
from src.memory.working import WorkingMemory  # noqa: E402
from src.service import AgentService  # noqa: E402
from src.tools.toolkit import Toolkit  # noqa: E402

from scoring import (extract_numbers, judge_trap, keywords_in,  # noqa: E402
                     numbers_cover, run_post_check)

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.ERROR)

EVAL_DIR = ROOT / "eval"


def use_eval_workspace(mode: str) -> Path:
    """把全局数据根指到 eval 专属目录（与正式 data/ 完全隔离）。"""
    root = EVAL_DIR / "checkpoints" / f"workspace_{mode}"
    root.mkdir(parents=True, exist_ok=True)
    config.DATA_ROOT = root
    config.WORKSPACE_DIR = root / "workspaces"
    config.RESULTS_DIR = root / "results"
    config.MEM0_PATH = root / "mem0"
    config.ensure_dirs()
    import src.core.storage as st
    st._storage = None            # 重置单例 → 下次 get_storage() 用新路径
    WorkbookManager._instance = None
    WorkingMemory._instance = None
    return root


def load_evalset() -> dict:
    return json.loads((EVAL_DIR / "evalset.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 黄金答案
# ---------------------------------------------------------------------------
def compute_golden(tk: Toolkit, expect: dict) -> dict:
    """执行参考 SQL 得到黄金数字/关键词。golden_kind: numbers(默认)/keyword/keywords_all。"""
    out = {"numbers": [], "keywords": []}
    sql = expect.get("ref_sql")
    if not sql:
        return out
    res = tk.query(sql, purpose="评测黄金答案")
    kind = expect.get("golden_kind", "numbers")
    if kind == "keyword":
        if res["rows"]:
            out["keywords"] = [str(res["rows"][0][0])]
    elif kind == "keywords_all":
        out["keywords"] = [str(r[0]) for r in res["rows"] if r[0] is not None]
    else:
        out["numbers"] = [float(r[-1]) for r in res["rows"]
                          if isinstance(r[-1], (int, float))]
    # 附加显式关键词
    out["keywords"] += expect.get("keywords", [])
    return out


# ---------------------------------------------------------------------------
# 单题执行
# ---------------------------------------------------------------------------
def run_item(item: dict, mode: str, datasets: dict) -> dict:
    t0 = time.time()
    record: dict = {"id": item["id"], "category": item["category"], "mode": mode,
                    "pass": False, "detail": "", "turns": [], "retries": 0,
                    "reflections": 0, "diff_shown": False, "elapsed_s": 0.0,
                    "responses": []}

    session_id = f"eval_{mode}_{item['id']}"
    svc = AgentService(session_id)

    dataset = item.get("dataset", "sales")
    wb_id = None
    if dataset and dataset != "none":
        wb_id, _warns = svc.load_file(str(ROOT / datasets[dataset]))
    tk = Toolkit(workbook_id=wb_id or "", session_id=session_id)

    # 黄金答案（在智能体动数据之前算好）
    golden = {}
    if wb_id and item["expect"].get("ref_sql"):
        golden = compute_golden(tk, item["expect"])

    # 写后黄金值（如去重后行数）
    golden_row_count = None
    if item["expect"].get("post_check", {}).get("golden") == "dedup_full_row":
        df = WorkbookManager.instance().get_df(wb_id, tk.sheet_names()[0])
        golden_row_count = int(len(df.drop_duplicates()))

    # ── 逐轮对话 + 自动确认 ──
    turns = item.get("turns") or [item.get("question")]
    last_result = None
    for user in turns:
        result = svc.chat(user, mode=mode)
        record["retries"] += result.retries
        record["reflections"] += len(result.reflection_log)
        record["responses"].append(result.response[:600])
        if result.confirm_required and result.pending_write:
            record["diff_shown"] = True
        # 自动确认链（>50 单元格二次确认）
        for _ in range(3):
            if not result.confirm_required:
                break
            result = svc.confirm_write()
            record["responses"].append(result.response[:300])
            if result.confirm_required and result.pending_write:
                record["diff_shown"] = True   # 二次确认卡也是 diff
        last_result = result

    record["turns"] = len(turns)
    record["reflection_log"] = (last_result.reflection_log if last_result else [])
    record["trace_brief"] = [
        {k: v for k, v in t.items() if k in ("tool", "res_id", "n_cells")}
        for t in ((last_result.trace if last_result else []) or [])
    ][:12]
    final_text = last_result.response if last_result else ""

    # ── 判分 ──
    category = item["category"]
    expect = item["expect"]
    try:
        if category in ("single", "multi"):
            ok, detail = score_query(final_text, last_result, golden, expect)
        elif category == "write":
            ok, detail = score_write(wb_id, expect, golden_row_count, record["diff_shown"])
        elif category == "summary":
            from judge import llm_judge
            hint = ""
            if golden.get("numbers"):
                hint = "关键数字：" + "、".join(str(n) for n in golden["numbers"][:6])
            elif golden.get("keywords"):
                hint = "关键词：" + "、".join(golden["keywords"][:8])
            if wb_id:
                try:
                    mat = tk.summarize(None)
                    hint += (f"\n【全表口径参考，仅用于核对数量级；若问题限定了范围（如某大区），"
                             f"不要用它判定范围内指标为错误】工作表 {mat['sheet']} 共 "
                             f"{mat['n_rows']} 行；{mat['numeric_summary'][:260]}")
                except Exception:  # noqa: BLE001
                    pass
            score, reason = llm_judge(final_text, expect.get("judge", ""), hint)
            ok, detail = bool(score), f"judge: {reason}"
        elif category == "trap":
            ok, reason = judge_trap(final_text, last_result.need_clarification
                                    if last_result else False, expect.get("type", "clarify"))
            detail = reason
        else:
            ok, detail = False, f"未知类别 {category}"
    except Exception as e:  # noqa: BLE001
        ok, detail = False, f"判分异常：{traceback.format_exc()[-300:]}"

    record["pass"] = bool(ok)
    record["detail"] = detail
    record["elapsed_s"] = round(time.time() - t0, 1)
    return record


def score_query(text: str, last_result, golden: dict, expect: dict) -> tuple[bool, str]:
    problems: list[str] = []
    if golden.get("numbers"):
        ok, missing = numbers_cover(text, golden["numbers"])
        if not ok:
            problems.append(f"缺少黄金数字 {missing}")
    if golden.get("keywords"):
        ok, missing = keywords_in(text, golden["keywords"])
        if not ok:
            problems.append(f"缺少关键词 {missing}")
    if expect.get("require_chart"):
        charts = (last_result.artifacts or {}).get("charts") if last_result else None
        if not charts:
            problems.append("未生成图表")
    if problems:
        return False, "；".join(problems)
    return True, "黄金数字/关键词全部命中" + ("（含图表）" if expect.get("require_chart") else "")


def score_write(wb_id, expect: dict, golden_row_count, diff_shown: bool) -> tuple[bool, str]:
    if not expect.get("post_check"):
        return False, "缺少 post_check"
    if not diff_shown:
        return False, "未观察到 diff 预览（AC-6）"
    check = expect["post_check"]
    ok, detail = run_post_check(WorkbookManager.instance(), wb_id, check, golden_row_count)
    return ok, f"diff已预览；{detail}"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="30 题评测")
    parser.add_argument("--mode", choices=["m0", "m1", "m2"], default="m2")
    parser.add_argument("--filter", default="", help="只跑 id/类别包含该子串的题")
    parser.add_argument("--fresh", action="store_true", help="忽略断点重跑")
    parser.add_argument("--out", default="", help="结果文件路径（默认 eval/results_<mode>.json）")
    args = parser.parse_args()

    if not config.llm_configured():
        print("❌ 未配置 OPENAI_API_KEY：评测必须真实调用 LLM。请先填写 .env（参考 .env.example）。")
        return 2

    use_eval_workspace(args.mode)

    # 一次性预热（Mem0 embedding 模型 + SQL 沙箱子进程），不计入单题耗时
    t_warm = time.time()
    from src.memory.longterm import get_longterm_memory
    ltm = get_longterm_memory()
    if ltm.available:
        ltm.search("预热", limit=1)
    from src.sandbox.runner import SqlRunner
    SqlRunner.instance().warmup()
    print(f"预热完成（{time.time() - t_warm:.1f}s）；L3 记忆：{'开启' if ltm.available else '降级'}")

    evalset = load_evalset()
    items = [it for it in evalset["items"]
             if not args.filter or args.filter in it["id"] or args.filter in it["category"]]

    out_path = Path(args.out) if args.out else EVAL_DIR / f"results_{args.mode}.json"
    results: dict = {}
    if out_path.exists() and not args.fresh:
        results = json.loads(out_path.read_text(encoding="utf-8"))
    elif out_path.exists() and args.fresh and args.filter:
        # --fresh + --filter：只清掉被筛中的题，保留其他已有结果
        results = json.loads(out_path.read_text(encoding="utf-8"))
        for it in items:
            results.pop(it["id"], None)

    print(f"== 评测 mode={args.mode}，共 {len(items)} 题（断点文件 {out_path.name}）==")
    for it in items:
        if it["id"] in results and not args.fresh:
            print(f"  ⏭ {it['id']} 已有结果，跳过（--fresh 重跑）")
            continue
        t0 = time.time()
        try:
            rec = run_item(it, args.mode, evalset["datasets"])
        except Exception as e:  # noqa: BLE001 —— 单题故障不中断整体（记录后继续）
            logging.error("题目 %s 执行异常：%s\n%s", it["id"], e, traceback.format_exc())
            rec = {"id": it["id"], "category": it["category"], "mode": args.mode,
                   "pass": False, "detail": f"执行异常：{e}", "error": True,
                   "turns": 0, "retries": 0, "reflections": 0, "diff_shown": False,
                   "elapsed_s": round(time.time() - t0, 1), "responses": []}
        results[it["id"]] = rec
        mark = "✅" if rec["pass"] else "❌"
        print(f"  {mark} {it['id']}（{it['category']}）{rec['detail'][:80]}"
              f"  [{rec['elapsed_s']}s, 重试{rec['retries']}]")
        out_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    # 汇总
    n_pass = sum(1 for r in results.values() if r.get("pass"))
    n_all = len(results)
    print(f"\n== 结果：{n_pass}/{n_all} = {n_pass / n_all:.0%}（结果已写入 {out_path}）==")
    by_cat: dict[str, list] = {}
    for r in results.values():
        by_cat.setdefault(r["category"], []).append(r["pass"])
    for cat, flags in sorted(by_cat.items()):
        print(f"   {cat}: {sum(flags)}/{len(flags)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
