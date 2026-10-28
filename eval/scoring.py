"""评测判分工具：答案数字抽取、黄金答案比对、写后状态断言、陷阱题判定。

判分原则（规格 §11）：
- 查询类：答案中的数字必须覆盖黄金答案（由参考 SQL 在同一份真实数据上算出），容差 0.5%；
- 修改类：直接断言写后的工作簿状态（程序化，不经 LLM）；
- 陷阱类：断言触发澄清/诚实说明，且未见编造（flag + 关键词，另留人工核查文本）；
- 成文类：LLM 评审判分（见 judge.py）。
"""

from __future__ import annotations

import re

# 数字抽取：支持千分位/小数/中文"万"单位/百分比
_NUM_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
_WAN_RE = re.compile(r"([-+]?\d[\d,]*(?:\.\d+)?)\s*万")

CLARIFY_MARKERS = ("澄清", "确认", "不存在", "没有这", "找不到", "未找到", "没有「", "没有『",
                   "没有该列", "没有这个列", "无法", "没有数据", "无数据", "没有找到",
                   "无记录", "没有记录", "未覆盖", "不覆盖", "无销售", "没有可统计",
                   "先上传", "请上传", "先选择", "加载数据", "需要您", "需要你", "无法回答",
                   "没有可用的", "并非真实", "不是真实")
UNCERTAIN_MARKERS = ("估计", "区间", "大约", "约", "近似", "不确定", "无法精确", "轻量",
                     "统计模型", "仅供参考", "预测", "置信")


def extract_numbers(text: str) -> list[float]:
    """从回答文本抽取候选数字（含"万"换算、百分比原值）。"""
    out: list[float] = []
    for m in _NUM_RE.finditer(text or ""):
        try:
            out.append(float(m.group().replace(",", "")))
        except ValueError:
            continue
    for m in _WAN_RE.finditer(text or ""):
        try:
            out.append(float(m.group(1).replace(",", "")) * 10000)
        except ValueError:
            continue
    return out


def _close(a: float, b: float, rel: float = 0.005, abs_tol: float = 0.011) -> bool:
    if abs(a - b) <= abs_tol:
        return True
    denom = max(abs(b), 1e-9)
    return abs(a - b) / denom <= rel


def numbers_cover(response: str, golden: list[float]) -> tuple[bool, list[float]]:
    """答案数字是否覆盖全部黄金值（自动兼容百分比缩放 100x/÷100、负数写成"下降X%"）。"""
    nums = extract_numbers(response)
    missing: list[float] = []
    for g in golden:
        hit = any(_close(n, g) for n in nums) \
            or any(_close(n, g * 100) for n in nums) \
            or any(_close(n, g / 100) for n in nums)
        if not hit and g < 0:
            # 中文常写"下降 27.6%"（不带负号）
            hit = any(_close(n, abs(g)) for n in nums) \
                or any(_close(n, abs(g) * 100) for n in nums)
        if not hit:
            missing.append(g)
    return (not missing), missing


def normalize_kw(kw: str) -> list[str]:
    """关键词别名：'2024-05' 自动扩展 '5月'。"""
    m = re.fullmatch(r"(\d{4})-(\d{2})", kw.strip())
    if m:
        month = int(m.group(2))
        return [kw, f"{month}月", f"{int(m.group(1))}年{month}月", f"{m.group(1)}/{month:02d}"]
    return [kw]


def keywords_in(response: str, keywords: list[str]) -> tuple[bool, list[str]]:
    low = (response or "")
    missing = []
    for kw in keywords:
        if not any(alias in low for alias in normalize_kw(kw)):
            missing.append(kw)
    return (not missing), missing


# ---------------------------------------------------------------------------
# 写后状态断言
# ---------------------------------------------------------------------------
def run_post_check(manager, wb_id: str, check: dict, golden_value=None) -> tuple[bool, str]:
    ctype = check.get("type")
    try:
        if ctype == "no_missing":
            df = manager.get_df(wb_id, _only_sheet(manager, wb_id))
            n = int(df[check["column"]].isna().sum())
            return (n == 0), f"列「{check['column']}」剩余缺失 {n} 个"

        if ctype == "row_count":
            df = manager.get_df(wb_id, _only_sheet(manager, wb_id))
            ok = len(df) == int(golden_value)
            return ok, f"当前 {len(df)} 行，期望 {golden_value} 行"

        if ctype == "column_dtype":
            schema = manager.get_schema(wb_id, _only_sheet(manager, wb_id))
            col = next((c for c in schema.columns if c.name == check["column"]), None)
            if col is None:
                return False, f"列「{check['column']}」不存在"
            ok = col.dtype == check["dtype"] or (check["dtype"] == "float" and col.dtype == "int")
            return ok, f"列「{check['column']}」类型 {col.dtype}，期望 {check['dtype']}"

        if ctype == "derived_check":
            df = manager.get_df(wb_id, _only_sheet(manager, wb_id))
            if check["column"] not in df.columns:
                return False, f"新列「{check['column']}」不存在"
            cols = check["expr"].split()
            a, b, c = cols[0], cols[2], cols[4]   # 形如 "销售额 - 数量 * 单价"
            op1, op2 = cols[1], cols[3]
            if op1 == "-" and op2 == "*":
                expected = df[a] - df[b] * df[c]
            elif op1 == "+" and op2 == "*":
                expected = df[a] + df[b] * df[c]
            else:
                return False, f"不支持的表达式：{check['expr']}"
            got = df[check["column"]]
            mask = expected.notna() & got.notna()
            if mask.sum() == 0:
                return False, "新列全为空"
            import pandas as pd
            ok = bool(pd.concat([expected[mask], got[mask]], axis=1).apply(
                lambda r: _close(float(r.iloc[0]), float(r.iloc[1]), rel=0.01, abs_tol=0.5), axis=1).all())
            return ok, f"抽查 {int(mask.sum())} 行派生值，{'一致' if ok else '不一致'}"

        if ctype == "cell_value":
            df = manager.get_df(wb_id, _only_sheet(manager, wb_id))
            got = df.iloc[int(check["row"])][check["column"]]
            ok = str(got).strip() == str(check["value"]).strip()
            return ok, f"第 {check['row'] + 1} 行「{check['column']}」= {got!r}，期望 {check['value']!r}"

        if ctype == "sheet_exists":
            names = manager.list_sheet_names(wb_id)
            ok = check["sheet"] in names
            return ok, f"工作表列表 {names}，期望包含「{check['sheet']}」"

        if ctype == "style_applied":
            styles = manager.get_styles(wb_id, _only_sheet(manager, wb_id))
            ok = len(styles) > 0
            return ok, f"样式记录 {list(styles)[:5]}"
    except Exception as e:  # noqa: BLE001
        return False, f"断言执行异常：{type(e).__name__}: {e}"
    return False, f"未知断言类型 {ctype}"


def _only_sheet(manager, wb_id: str) -> str:
    names = manager.list_sheet_names(wb_id)
    return names[0]


# ---------------------------------------------------------------------------
# 陷阱题判定
# ---------------------------------------------------------------------------
def judge_trap(response: str, need_clarification: bool, trap_type: str) -> tuple[bool, str]:
    text = response or ""
    if trap_type == "clarify":
        hit = need_clarification or any(m in text for m in CLARIFY_MARKERS)
        reason = "明确澄清/说明无法回答" if hit else "未触发澄清"
        return hit, reason
    if trap_type == "honest_uncertainty":
        hit = need_clarification or any(m in text for m in UNCERTAIN_MARKERS) \
            or any(m in text for m in CLARIFY_MARKERS)
        reason = "诚实说明预测为估计值/区间" if hit else "未表达不确定性（疑似给出精确编造值）"
        return hit, reason
    return False, f"未知陷阱类型 {trap_type}"
