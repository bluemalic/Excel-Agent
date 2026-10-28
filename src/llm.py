"""LLM 工厂：统一的 OpenAI 兼容 client（LangChain ChatOpenAI）。

全部 Agent（主图节点 / supervisor worker / 反思器 / 评审判分 / Mem0 内部 LLM）
都从这里拿模型实例，保证 base_url/model/密钥只有一处配置。
"""

from __future__ import annotations

import functools
import logging

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from src import config

logger = logging.getLogger(__name__)


class LLMNotConfiguredError(RuntimeError):
    """未配置 OPENAI_API_KEY 时抛出，上层转成中文提示。"""


@functools.lru_cache(maxsize=8)
def get_chat_model(
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> BaseChatModel:
    """返回对话模型实例（带缓存，同参数复用同一 client）。

    温度默认取全局配置（规划/SQL 生成类节点建议 0，叙述类可调高）。
    """
    if not config.llm_configured():
        raise LLMNotConfiguredError(
            "未检测到可用的 OPENAI_API_KEY：请复制 .env.example 为 .env 并填入 DeepSeek Key"
        )
    return ChatOpenAI(
        model=config.OPENAI_MODEL,
        api_key=config.OPENAI_API_KEY,
        base_url=config.OPENAI_BASE_URL,
        temperature=config.LLM_TEMPERATURE if temperature is None else temperature,
        max_tokens=max_tokens or config.LLM_MAX_TOKENS,
        timeout=120,
        max_retries=2,
    )


@functools.lru_cache(maxsize=1)
def get_raw_openai_client():
    """返回 openai SDK 原始 client（供 Mem0 等需要原生 client 的场景复用配置）。"""
    from openai import OpenAI

    if not config.llm_configured():
        raise LLMNotConfiguredError("未配置 OPENAI_API_KEY")
    return OpenAI(
        api_key=config.OPENAI_API_KEY,
        base_url=config.OPENAI_BASE_URL,
    )


def invoke_json(model: BaseChatModel, prompt: str, system: str | None = None) -> dict:
    """调用 LLM 并解析 JSON 输出；自动剥离 ```json 代码栅栏，解析失败抛 ValueError。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    messages = []
    if system:
        messages.append(SystemMessage(content=system))
    messages.append(HumanMessage(content=prompt))
    resp = model.invoke(messages)
    text = resp.content if isinstance(resp.content, str) else "".join(
        part.get("text", "") for part in resp.content if isinstance(part, dict)
    )
    return parse_json_block(text)


def parse_json_block(text: str) -> dict:
    """从模型输出中提取 JSON 对象：优先解析 ```json 栅栏，否则截取首个 {...}。"""
    import json
    import re

    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError(f"模型输出不是合法 JSON：{text[:200]}")
