"""模型输出预算与关键输入范围；环境变量在调用时读取。"""
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

STRUCTURED_MAX_TOKENS = 4096
RESPONSE_MAX_TOKENS = 8192
SUMMARY_MAX_TOKENS = 8192
SKILLS_MAX_PROMPT_CHARS = 16000
SKILL_MAX_CHARS = 8000
SUMMARY_MAX_CHARS = 4000
RERANK_ITEM_MAX_CHARS = 4000
EPISODIC_FULL_TEXT_MAX_CHARS = 8000
HISTORY_MESSAGES = 10
CONTEXT_MESSAGES = 20

_OUTPUT_BUDGETS = {
    "intent": STRUCTURED_MAX_TOKENS,
    "query_rewrite": STRUCTURED_MAX_TOKENS,
    "rerank": STRUCTURED_MAX_TOKENS,
    "judge": STRUCTURED_MAX_TOKENS,
    "profile": STRUCTURED_MAX_TOKENS,
    "summary": SUMMARY_MAX_TOKENS,
    "summary_merge": SUMMARY_MAX_TOKENS,
    "general": RESPONSE_MAX_TOKENS,
    "technical": RESPONSE_MAX_TOKENS,
    "billing": RESPONSE_MAX_TOKENS,
    "escalation": STRUCTURED_MAX_TOKENS,
    "composer": RESPONSE_MAX_TOKENS,
}


def positive_int_env(name: str, default: int) -> int:
    """非法或非正数配置回退默认值，避免零预算或启动失败。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    logger.warning("忽略非法正整数配置 %s", name)
    return default


def max_tokens_for(stage: str, default: Optional[int] = None) -> int:
    """如 intent 对应 ECHOMIND_INTENT_MAX_TOKENS。"""
    budget = _OUTPUT_BUDGETS[stage] if default is None else default
    return positive_int_env(f"ECHOMIND_{stage.upper()}_MAX_TOKENS", budget)
