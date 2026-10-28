"""成文类 LLM 评审判分（规格 §11：总结成文 4 题用 LLM judge，标准落盘于 evalset）。"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

JUDGE_SYSTEM = """你是评测裁判。根据评分标准，判断"AI 分析回答"是否合格（0 或 1 分）。

评分关注：
- 是否引用了与【真实数据】一致的具体数字（与给定数据矛盾 = 0 分）；
- 是否覆盖评分标准要求的结构/内容要点；
- 长度要求；
- 不要求文采，要求准确与完整。

注意：
- 允许 ±5% 以内的合理四舍五入/口语化近似（如 24390 写成"约2.4万"）；
- 判定"数字与数据矛盾"时只对照与问题范围一致的参考指标；回答里出现的**范围派生指标**
  （如某大区平均单价）只要由该范围真实数据可解释，不算矛盾；
- 长度要求按评分标准判定，"100-300字左右"允许约 400 字以内的合理浮动。

输出 JSON：{"score": 0 或 1, "reason": "一两句中文理由"}
"""

# 无 LLM 时的兜底：只做结构与长度检查（评测正常路径不会用到）
def _fallback_judge(response: str, criteria: str) -> tuple[int, str]:
    length = len(re.sub(r"\s", "", response))
    has_num = bool(re.search(r"\d", response))
    if length >= 80 and has_num:
        return 1, f"（兜底规则）长度 {length} 字且含数字"
    return 0, f"（兜底规则）长度 {length} 字或缺少数字"


def llm_judge(response: str, criteria: str, real_data_hint: str = "") -> tuple[int, str]:
    """返回 (0/1, 理由)。LLM 不可用时降级为结构检查并在理由中注明。"""
    from src.llm import get_chat_model, LLMNotConfiguredError, parse_json_block
    from langchain_core.messages import HumanMessage, SystemMessage

    prompt = (
        f"## 评分标准\n{criteria}\n\n"
        + (f"## 真实数据参考（用于核对数字，以此为准）\n{real_data_hint}\n" if real_data_hint else "")
        + f"\n## AI 分析回答\n{response[:3000]}\n\n请输出 JSON 评分。"
    )
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        model = get_chat_model(temperature=0.0)
        resp = model.invoke([SystemMessage(content=JUDGE_SYSTEM), HumanMessage(content=prompt)])
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        verdict = parse_json_block(text)
    except LLMNotConfiguredError:
        return _fallback_judge(response, criteria)
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM judge 失败，降级结构检查：%s", e)
        return _fallback_judge(response, criteria)
    score = int(verdict.get("score", 0))
    return (1 if score >= 1 else 0), str(verdict.get("reason", ""))
