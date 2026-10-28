"""反思闭环 R1 / R2（规格 §6）。

R1 规则校验（确定性，毫秒级）：执行报错 / 0 行 / 过滤未生效（行数==全表）/
要 TopN 却多行 / 要数值却返回文本 / 结果空值率>80%。
R2 Reflector（独立上下文 LLM）：只看 问题+schema+SQL+结果前5行，输出可执行修正建议。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.agents.prompts import REFLECTOR_SYSTEM
from src.llm import get_chat_model, invoke_json

# 出现这些词的问题期望数值答案（用于"要数值却返回文本"检查）
_NUMERIC_HINTS = ("多少", "总和", "合计", "平均", "均值", "最大", "最小", "几", "占比", "排名", "top", "前")


@dataclass
class R1Verdict:
    ok: bool               # True=通过
    level: str             # hard=确定性失败（直接重试）；soft=可疑（交 R2 或如实提示）
    reason: str


def r1_check(step: dict, result: dict | None, full_rows: int | None) -> R1Verdict:
    """对一步执行结果做规则校验。step: {action, question?...}；result: toolkit 返回。"""
    if result is None:
        return R1Verdict(False, "hard", "该步骤没有产出任何结果")
    if not result.get("ok", True):
        return R1Verdict(False, "hard", f"执行失败：{result.get('error', '未知错误')}")

    action = step.get("action", "")
    if action == "sql_query":
        question = str(step.get("question", "") or "")
        total = int(result.get("total_rows", 0))
        rows = result.get("rows") or []

        # 1) 返回 0 行
        if total == 0:
            return R1Verdict(False, "soft",
                             "查询返回 0 行——可能过滤条件过严，也可能该条件下确实无数据，需要复核")

        # 2) 过滤没生效：SQL 带 WHERE 但行数==全表行数
        sql = str(result.get("sql", ""))
        if full_rows is not None and total == full_rows and re.search(r"\bWHERE\b", sql, re.I):
            return R1Verdict(False, "soft",
                             f"SQL 含 WHERE 条件却返回了全部 {full_rows} 行，过滤可能没生效")

        # 3) 问 TopN 却返回过多行
        m = re.search(r"(?:前|top\s*|头)\s*(\d+)", question, re.I)
        if m and total > int(m.group(1)) > 0:
            return R1Verdict(False, "soft",
                             f"问题要前 {m.group(1)} 名，结果却有 {total} 行")

        # 4) 要数值却返回字符串列
        if any(h in question.lower() for h in _NUMERIC_HINTS) and rows:
            first_data_col = rows[0][-1] if rows[0] else None
            if isinstance(first_data_col, str) and not _str_is_number(first_data_col):
                return R1Verdict(False, "soft",
                                 f"问题期望数值，结果末列却是文本（如 {first_data_col!r}），"
                                 f"可能聚合了文本列或选错列")

        # 5) 结果空值率 > 80%
        if rows and rows[0]:
            n_cells = sum(len(r) for r in rows)
            n_null = sum(1 for r in rows for v in r if v is None)
            if n_cells and n_null / n_cells > 0.8:
                return R1Verdict(False, "soft",
                                 f"结果空值率 {n_null / n_cells:.0%} 过高，可能列名或条件有误")

    return R1Verdict(True, "none", "规则校验通过")


def _str_is_number(v: str) -> bool:
    try:
        float(str(v).replace(",", "").replace("%", ""))
        return True
    except ValueError:
        return False


def r2_reflect(user_question: str, schema_prompt: str, sql: str,
               result_rows: list, columns: list[str],
               execution_error: str = "") -> dict:
    """R2：独立上下文复核（剥掉执行历史，避免模型为自身决策辩护）。"""
    import json

    preview = [dict(zip(columns, r)) for r in (result_rows or [])[:5]]
    prompt = (
        f"## 用户问题\n{user_question}\n\n"
        f"## 表结构\n{schema_prompt}\n\n"
        f"## 生成的 SQL\n```sql\n{sql or '（无）'}\n```\n\n"
        + (f"## 执行错误\n{execution_error}\n\n" if execution_error else "")
        + f"## 返回结果（前 {len(preview)} 行）\n"
        f"{json.dumps(preview, ensure_ascii=False, indent=1) if preview else '（无结果）'}\n\n"
        "请输出 JSON 复核结论。"
    )
    model = get_chat_model(temperature=0.0)
    try:
        verdict = invoke_json(model, prompt, system=REFLECTOR_SYSTEM)
    except Exception as e:  # noqa: BLE001 —— 反思器自身故障不得中断会话（AC-9）
        return {"answered": True, "reason": f"复核器暂时不可用（{e}），按原结果继续",
                "fix_suggestion": "", "need_clarify": False, "clarify_question": ""}
    return verdict
