"""LLM response helpers shared by Anthropic-compatible providers."""
from typing import Any, Iterable, List

from core.chat_events import emit, streaming_enabled


async def create_message(client: Any, *, source: str = "assistant", **kwargs: Any) -> Any:
    """Use real provider text deltas for streaming chat; other callers stay unchanged."""
    if not streaming_enabled():
        return await client.messages.create(**kwargs)
    emit("stage", message="正在整理最终回复" if source == "composer" else "正在生成回复")
    emit("answer_start", source=source)
    async with client.messages.stream(**kwargs) as stream:
        async for text in stream.text_stream:
            emit("text_delta", source=source, text=text)
        return await stream.get_final_message()


def extract_text_content(content: Iterable[Any]) -> str:
    """Return text blocks from Anthropic-style response content."""
    texts: List[str] = []
    for block in content or []:
        if isinstance(block, str):
            texts.append(block)
            continue

        block_type = getattr(block, "type", None)
        text = getattr(block, "text", None)
        if isinstance(block, dict):
            block_type = block.get("type", block_type)
            text = block.get("text", text)

        if isinstance(text, str) and (block_type in (None, "text")):
            texts.append(text)

    return "\n".join(t for t in texts if t)


def ensure_not_truncated(response: Any) -> None:
    """拒绝预算耗尽的响应，即使已有文本或可解析的 JSON。"""
    reason = response.get("stop_reason") if isinstance(response, dict) else getattr(response, "stop_reason", None)
    if reason == "max_tokens":
        raise ValueError("模型输出被截断（stop_reason=max_tokens）")


def extract_response_text(response: Any) -> str:
    """只返回未被截断且非空的正文；调用方沿用各自的失败兜底。"""
    ensure_not_truncated(response)
    content = response.get("content") if isinstance(response, dict) else getattr(response, "content", None)
    text = extract_text_content(content).strip()
    if not text:
        raise ValueError("模型输出没有文本内容")
    return text
