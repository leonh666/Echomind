"""
亮点：MCP 工具调用框架

核心问题：工具调用出错（检索不全、召回不好）怎么优化？

本模块的答案：
  1. 查询改写（Query Rewriting）—— 用 LLM 把用户原始问题扩写成多个角度的子查询，
     再合并去重，解决"召回不全"问题。
  2. 结果重排（Reranking）—— 对召回结果用 LLM 打分，按相关性重新排序，
     解决"召回不好/排序差"问题。
  3. 熔断器（Circuit Breaker）—— 连续失败超阈值时自动断开，防止雪崩。
  4. 结果缓存（TTL Cache）—— 相同参数直接返回缓存，减少重复调用。
  5. 降级策略（Fallback）—— 工具不可用时返回有意义的降级结果。
"""
import asyncio
import copy
import hashlib
import inspect
import json
import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from anthropic import AsyncAnthropic

from core.llm_utils import extract_response_text
from core.llm_limits import RERANK_ITEM_MAX_CHARS, max_tokens_for

logger = logging.getLogger(__name__)


# ── 数据结构 ──────────────────────────────────────────────────────────────────

class CircuitState(Enum):
    CLOSED    = "closed"     # 正常
    OPEN      = "open"       # 熔断，拒绝请求
    HALF_OPEN = "half_open"  # 探测恢复


@dataclass
class ToolResult:
    success:        bool
    data:           Any
    tool_name:      str
    error:          Optional[str] = None
    cached:         bool = False
    latency_ms:     float = 0.0
    reranked:       bool = False   # 是否经过重排
    rerank_status:  str = "skipped"
    rerank_error:   Optional[str] = None
    fallback:       bool = False
    partial_failure: bool = False
    rewrite_status: str = "skipped"
    rewrite_error:  Optional[str] = None


@dataclass
class ToolStats:
    """工具运行时统计，供 Monitor 读取。"""
    total:              int = 0
    success:            int = 0
    failed:             int = 0
    total_latency_ms:   float = 0.0
    consecutive_fails:  int = 0

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total else 1.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.total if self.total else 0.0


# ── 熔断器 ────────────────────────────────────────────────────────────────────

class CircuitBreaker:
    """
    三态熔断器：CLOSED → OPEN → HALF_OPEN → CLOSED

    连续失败 failure_threshold 次后打开；
    打开 recovery_s 秒后进入 HALF_OPEN 探测；
    探测成功则关闭，失败则重新打开。
    """

    def __init__(self, failure_threshold: int = 5, recovery_s: float = 60.0):
        self.threshold   = failure_threshold
        self.recovery_s  = recovery_s
        self.state       = CircuitState.CLOSED
        self.fail_count  = 0
        self.opened_at:  Optional[float] = None
        self._probe_in_flight = False

    def allow(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if time.monotonic() - self.opened_at >= self.recovery_s:  # type: ignore
                self.state = CircuitState.HALF_OPEN
                self._probe_in_flight = True
                return True
            return False
        if self._probe_in_flight:
            return False
        self._probe_in_flight = True
        return True

    def record_success(self) -> None:
        self.fail_count = 0
        self.state = CircuitState.CLOSED
        self._probe_in_flight = False

    def record_failure(self) -> None:
        self.fail_count += 1
        self._probe_in_flight = False
        if self.fail_count >= self.threshold:
            self.state     = CircuitState.OPEN
            self.opened_at = time.monotonic()
            logger.warning(f"熔断器打开（连续失败 {self.fail_count} 次）")


# ── 工具定义 ──────────────────────────────────────────────────────────────────

@dataclass
class Tool:
    name:        str
    description: str
    handler:     Callable                    # async (params, context) -> Any
    schema:      Dict[str, Any]              # JSON Schema
    cache_ttl:   float = 0.0                 # 0 = 不缓存
    timeout_s:   float = 30.0
    supports_rerank: bool = False            # 是否支持结果重排
    fallback:    Optional[Callable] = None    # sync/async (params, context, error) -> Any

    # 运行时状态（不参与构造）
    stats:   ToolStats    = field(default_factory=ToolStats, init=False)
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker, init=False)


# ── MCP 工具管理器 ────────────────────────────────────────────────────────────

class MCPToolManager:
    """
    MCP 工具调用框架。

    核心优化链路（针对检索类工具）：
      用户查询 → 查询改写（多角度子查询）→ 并行召回 → 结果重排 → 返回 Top-K
    """

    def __init__(self, api_key: str, base_url: Optional[str] = None, model: str = "claude-3-5-sonnet-20241022"):
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncAnthropic(**kwargs)
        self._model  = model
        self._tools: Dict[str, Tool] = {}
        self._cache: Dict[str, tuple] = {}   # key → (result, expire_at, reranked)

    # ── 注册 / 注销 ───────────────────────────────────────────────────────────

    def register(self, tool: Tool) -> None:
        self._invalidate_tool_cache(tool.name)
        self._tools[tool.name] = tool
        logger.info(f"注册工具: {tool.name}")

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        self._invalidate_tool_cache(name)

    def _invalidate_tool_cache(self, name):
        for key in list(self._cache):
            if key.startswith(name + ":"):
                del self._cache[key]

    def _source_version(self, name):
        handler = self._tools[name].handler
        owner = getattr(handler, "__self__", None)
        return getattr(owner, "cache_version", None)

    # ── 核心调用 ──────────────────────────────────────────────────────────────

    async def call(
        self,
        name: str,
        params: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
        *,
        use_cache: bool = True,
        rerank_top_k: int = 0,          # >0 时对结果重排，取 Top-K
    ) -> ToolResult:
        """
        调用工具，完整执行链：
          缓存检查 → 熔断检查 → 参数校验 → 执行（含超时）→ 可选重排 → 缓存写入
        """
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(success=False, data=None, tool_name=name, error=f"工具不存在: {name}")

        t0 = time.monotonic()
        tool.stats.total += 1
        try:
            self._validate_params(tool, params)
            if isinstance(rerank_top_k, bool) or not isinstance(rerank_top_k, int) or not 0 <= rerank_top_k <= 100:
                raise ValueError("rerank_top_k must be an integer between 0 and 100")
        except (TypeError, ValueError) as error:
            tool.stats.failed += 1
            return ToolResult(False, None, name, error=str(error))
        cache_rerank_top_k = rerank_top_k if tool.supports_rerank else 0
        source_version = self._source_version(name)
        if use_cache and tool.cache_ttl > 0:
            cached = self._get_cache(name, params, cache_rerank_top_k, context=context,
                                     source_version=source_version)
            if cached is not None:
                cached_data, cached_status = cached
                tool.stats.success += 1
                return ToolResult(True, cached_data, name, cached=True,
                                  reranked=cached_status == "succeeded", rerank_status=cached_status)
        if not tool.breaker.allow():
            tool.stats.failed += 1
            return await self._fallback_result(tool, params, context, f"工具熔断中: {name}，请稍后重试")
        try:
            data = await asyncio.wait_for(self._run_handler(tool, params, context), timeout=tool.timeout_s)
            if isinstance(data, ToolResult):
                if not data.success or data.fallback:
                    raise RuntimeError(data.error or "handler reported failure")
                data = data.data
            if isinstance(data, dict) and (data.get("success") is False or data.get("fallback") is True):
                raise RuntimeError(str(data.get("error") or "handler returned fallback"))
            if tool.supports_rerank:
                data = self._dedupe_candidates(data)
            rerank_status, rerank_error = "skipped", None
            if rerank_top_k and tool.supports_rerank:
                data, rerank_status, rerank_error = await self._rerank_result(
                    params.get("query", ""), data, rerank_top_k, timeout=tool.timeout_s)
            latency = (time.monotonic() - t0) * 1000
            tool.stats.success += 1
            tool.stats.consecutive_fails = 0
            tool.stats.total_latency_ms += latency
            tool.breaker.record_success()
            if use_cache and tool.cache_ttl > 0 and rerank_status != "failed":
                self._set_cache(name, params, data, tool.cache_ttl, cache_rerank_top_k,
                                rerank_status == "succeeded", context=context, source_version=source_version)
            return ToolResult(True, data, name, latency_ms=latency,
                              reranked=rerank_status == "succeeded", rerank_status=rerank_status,
                              rerank_error=rerank_error)
        except asyncio.CancelledError:
            # Release a half-open probe if its caller disconnects/cancels.
            tool.breaker.record_failure()
            raise
        except asyncio.TimeoutError:
            tool.stats.failed += 1
            tool.stats.consecutive_fails += 1
            tool.breaker.record_failure()
            logger.error(f"工具超时: {name} ({tool.timeout_s}s)")
            result = await self._fallback_result(tool, params, context, "执行超时")
            result.latency_ms = (time.monotonic() - t0) * 1000
            return result

        except Exception as ex:
            tool.stats.failed += 1
            tool.stats.consecutive_fails += 1
            tool.breaker.record_failure()
            logger.error(f"工具异常: {name} — {ex}")
            result = await self._fallback_result(tool, params, context, str(ex))
            result.latency_ms = (time.monotonic() - t0) * 1000
            return result

    async def _fallback_result(
        self,
        tool: Tool,
        params: Dict[str, Any],
        context: Optional[Dict[str, Any]],
        error: str,
    ) -> ToolResult:
        """工具不可用时返回降级结果，而不是把空错误直接暴露给调用方。"""
        if tool.fallback is None:
            return ToolResult(success=False, data=None, tool_name=tool.name, error=error)
        try:
            data = tool.fallback(params, context, error)
            if asyncio.iscoroutine(data):
                data = await data
            return ToolResult(
                success=False,
                data=data,
                tool_name=tool.name,
                error=error,
                fallback=True,
            )
        except Exception as ex:
            logger.error(f"工具降级失败: {tool.name} — {ex}")
            return ToolResult(success=False, data=None, tool_name=tool.name, error=f"{error}; fallback失败: {ex}")

    async def _run_handler(
        self,
        tool: Tool,
        params: Dict[str, Any],
        context: Optional[Dict[str, Any]],
    ) -> Any:
        """
        执行工具 handler。

        优先支持 async handler；如果历史工具仍是同步函数，则放入线程池执行，
        避免阻塞事件循环。
        """
        if inspect.iscoroutinefunction(tool.handler):
            return await tool.handler(params, context)
        result = await asyncio.to_thread(tool.handler, params, context)
        if inspect.isawaitable(result):
            return await result
        return result

    # ── 查询改写（解决召回不全）────────────────────────────────────────────────

    async def rewrite_query(self, query: str, n: int = 3) -> List[str]:
        queries, _, _ = await self._rewrite_query_result(query, n)
        return queries

    async def _rewrite_query_result(self, query: str, n: int = 3):
        """
        用 LLM 将原始查询改写为 n 个不同角度的子查询。

        目的：单一查询往往只能召回某一角度的文档，
        多角度子查询并行检索后合并，显著提升召回率。

        示例：
          原始: "退款流程"
          改写: ["如何申请退款", "退款需要多少天", "退款政策是什么"]
        """
        query = self._validate_query(query)
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 5:
            raise ValueError("rewrite count must be between 1 and 5")
        prompt = f"""将以下用户查询改写为 {n} 个不同角度的搜索子查询，用于检索知识库。
要求：每个子查询角度不同，覆盖原始问题的不同方面。
原始查询: "{query}"
返回 JSON 数组，例如: ["子查询1", "子查询2", "子查询3"]"""
        prompt = self._clean_text(prompt)
        try:
            resp = await asyncio.wait_for(self._client.messages.create(
                model=self._model, max_tokens=max_tokens_for("query_rewrite"), temperature=0.3,
                messages=[{"role": "user", "content": prompt}],
            ), timeout=30)
            raw = extract_response_text(resp)
            s, e = raw.find("["), raw.rfind("]") + 1
            queries = json.loads(raw[s:e])
            if not isinstance(queries, list) or not all(isinstance(q, str) and q.strip() and len(q) <= 4000 for q in queries):
                raise ValueError("rewrite must be an array of nonempty query strings")
            return list(dict.fromkeys([query] + [q.strip() for q in queries]))[:n + 1], "succeeded", None
        except Exception as ex:
            logger.warning(f"查询改写失败，使用原始查询: {ex}")
            return [query], "failed", str(ex)

    async def search_with_rewrite(
        self,
        tool_name: str,
        query: str,
        top_k: int = 5,
        context: Optional[Dict[str, Any]] = None,
        *,
        strategy: str = "vector",
        where: Optional[Dict[str, Any]] = None,
        rewrite: bool = True,
    ) -> ToolResult:
        """
        完整的检索优化链路：查询改写 → 并行召回 → 去重 → 重排 → Top-K

        这是解决"检索不全、召回不好"的完整方案。
        """
        t0 = time.monotonic()
        try:
            query = self._validate_query(query)
            if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 100:
                raise ValueError("top_k must be an integer between 1 and 100")
            if strategy not in {"vector", "bm25", "hybrid"}:
                raise ValueError("invalid retrieval strategy")
            if where is not None and not isinstance(where, dict):
                raise ValueError("where must be an object")
        except ValueError as error:
            return ToolResult(False, [], tool_name, error=str(error))
        if rewrite:
            sub_queries, rewrite_status, rewrite_error = await self._rewrite_query_result(query, n=3)
        else:
            sub_queries, rewrite_status, rewrite_error = [query], "skipped", None
        recall_k = max(top_k, 5)
        extra = {}
        if strategy != "vector":
            extra["strategy"] = strategy
        if where:
            extra["where"] = where
        tasks = [
            self.call(tool_name, {"query": q, "top_k": recall_k, **extra}, context, use_cache=True)
            for q in sub_queries
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        merged, errors, healthy = [], [], 0
        for r in results:
            if isinstance(r, ToolResult) and r.success and isinstance(r.data, list):
                try:
                    merged.extend(self._dedupe_candidates(r.data))
                    healthy += 1
                except ValueError as error:
                    errors.append(str(error))
            else:
                errors.append(str(r.error if isinstance(r, ToolResult) else r))
        if not healthy:
            return ToolResult(False, [], tool_name, error="; ".join(errors) or "all searches failed",
                              fallback=any(isinstance(r, ToolResult) and r.fallback for r in results),
                              partial_failure=bool(errors), rewrite_status=rewrite_status, rewrite_error=rewrite_error,
                              latency_ms=(time.monotonic() - t0) * 1000)
        merged = self._dedupe_candidates(merged)[:100]
        ranked, status, rerank_error = await self._rerank_result(query, merged, top_k)
        return ToolResult(True, ranked, tool_name, error="; ".join(errors) or None,
                          reranked=status == "succeeded", rerank_status=status, rerank_error=rerank_error,
                          partial_failure=bool(errors), rewrite_status=rewrite_status, rewrite_error=rewrite_error,
                          latency_ms=(time.monotonic() - t0) * 1000)

    # ── 结果重排（解决召回不好）──────────────────────────────────────────────

    async def _rerank(self, query: str, items: List[Any], top_k: int) -> List[Any]:
        ranked, _, _ = await self._rerank_result(query, items, top_k)
        return ranked

    async def _rerank_result(self, query, items, top_k, timeout=30):
        """
        用 LLM 对召回结果重新打分排序。

        解决问题：向量检索的相似度分数不等于"对用户有用"，
        LLM 能理解语义相关性，重排后 Top-K 质量显著提升。
        """
        items = self._dedupe_candidates(items)
        if len(items) <= top_k:
            return items, "skipped", None

        # 将结果序列化为文本供 LLM 评分
        items_text = "\n".join(f"{i}. {json.dumps(item, ensure_ascii=False)[:RERANK_ITEM_MAX_CHARS]}"
                               for i, item in enumerate(items))
        prompt = f"""根据用户查询，对以下检索结果按相关性打分（0-10），返回 JSON 数组。
用户查询: "{query}"
检索结果:
{items_text}

返回格式（按相关性降序排列的索引列表）: [最相关的索引, ..., 最不相关的索引]
只返回 JSON 数组，不要其他文字。"""
        prompt = self._clean_text(prompt)

        try:
            resp = await asyncio.wait_for(self._client.messages.create(
                model=self._model, max_tokens=max_tokens_for("rerank"), temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            ), timeout=timeout)
            raw = extract_response_text(resp)
            s, e = raw.find("["), raw.rfind("]") + 1
            order: List[int] = json.loads(raw[s:e])
            if not isinstance(order, list) or any(isinstance(i, bool) or not isinstance(i, int) for i in order):
                raise ValueError("rerank indices must be integers")
            if len(order) != len(items) or set(order) != set(range(len(items))):
                raise ValueError("rerank must return each candidate index exactly once")
            reranked = [items[i] for i in order if 0 <= i < len(items)]
            return reranked[:top_k], "succeeded", None
        except Exception as ex:
            logger.warning(f"重排失败，返回原始顺序: {ex}")
            return items[:top_k], "failed", str(ex)

    @staticmethod
    def _validate_query(query):
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise ValueError("query must contain 1 to 4000 characters")
        return query.strip()

    @staticmethod
    def _candidate_key(item):
        if item.get("chunk_id"):
            return "chunk:" + str(item["chunk_id"])
        if item.get("document_id") and ("chunk" in item or "chunk_index" in item):
            return json.dumps([item["document_id"], item.get("document_version"), item.get("chunk_index", item.get("chunk"))])
        stable = {k: item.get(k) for k in ("title", "content", "source_url", "product_version")}
        return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _dedupe_candidates(self, items):
        if not isinstance(items, list):
            raise ValueError("retrieval candidates must be a list")
        seen, result = set(), []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("content"), str) or not item["content"].strip():
                raise ValueError("retrieval candidate must have nonempty content")
            if item.get("fallback") or item.get("error"):
                raise ValueError("retrieval returned a fallback/error candidate")
            key = self._candidate_key(item)
            if key not in seen:
                seen.add(key)
                result.append(item)
        return result

    # ── 缓存 ──────────────────────────────────────────────────────────────────

    def _cache_key(self, name: str, params: Dict, rerank_top_k: int = 0, *, context=None, source_version=None) -> str:
        payload = {"params": params, "rerank_top_k": rerank_top_k, "context": context,
                   "source_version": source_version, "model": self._model}
        return f"{name}:{hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()}"

    def _get_cache(self, name: str, params: Dict, rerank_top_k: int = 0, *, context=None, source_version=None):
        key = self._cache_key(name, params, rerank_top_k, context=context, source_version=source_version)
        if key in self._cache:
            data, expire_at, reranked = self._cache[key]
            if time.monotonic() < expire_at:
                return copy.deepcopy(data), reranked
            del self._cache[key]
        return None

    def _set_cache(
        self,
        name: str,
        params: Dict,
        data: Any,
        ttl: float,
        rerank_top_k: int = 0,
        reranked: bool = False,
        *,
        context=None,
        source_version=None,
    ) -> None:
        if len(self._cache) >= 5000:
            # 清掉最旧的 1/4
            for k in list(self._cache)[:1250]:
                del self._cache[k]
        self._cache[self._cache_key(name, params, rerank_top_k, context=context, source_version=source_version)] = (
            copy.deepcopy(data), time.monotonic() + ttl, "succeeded" if reranked else "skipped")

    # ── 参数校验 ──────────────────────────────────────────────────────────────

    _TYPE_MAP = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "array": list, "object": dict}

    def _validate_params(self, tool: Tool, params: Dict[str, Any]) -> None:
        """根据工具的 JSON Schema 校验参数，不合法时抛出 ValueError。"""
        schema = tool.schema
        if not isinstance(params, dict):
            raise ValueError("tool params must be an object")
        required = schema.get("required", [])
        properties = schema.get("properties", {})

        for field in required:
            if field not in params:
                raise ValueError(f"工具 {tool.name} 缺少必需参数: {field}")

        for key, value in params.items():
            if key in properties:
                expected_type = properties[key].get("type")
                if expected_type and expected_type in self._TYPE_MAP:
                    if not isinstance(value, self._TYPE_MAP[expected_type]) or (expected_type in {"number", "integer"} and isinstance(value, bool)):
                        raise ValueError(
                            f"工具 {tool.name} 参数 {key} 类型错误: 期望 {expected_type}，实际 {type(value).__name__}"
                        )
                specification = properties[key]
                if "enum" in specification and value not in specification["enum"]:
                    raise ValueError(f"invalid value for {key}")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if not math.isfinite(value) or value < specification.get("minimum", -math.inf) or value > specification.get("maximum", math.inf):
                        raise ValueError(f"{key} is outside its allowed range")

    @staticmethod
    def _clean_text(value: Any) -> str:
        """移除 Unicode 代理字符，避免 LLM 请求编码失败。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")

    # ── 统计 ──────────────────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        return {
            name: {
                "total": t.stats.total,
                "success_rate": round(t.stats.success_rate, 3),
                "avg_latency_ms": round(t.stats.avg_latency_ms, 1),
                "consecutive_fails": t.stats.consecutive_fails,
                "circuit_state": t.breaker.state.value,
            }
            for name, t in self._tools.items()
        }
