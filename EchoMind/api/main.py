"""
EchoMind 智能客服系统 — FastAPI 入口

启动时打印小熊饼干图案。
所有核心组件在 lifespan 中初始化，通过环境变量配置。
"""
import asyncio
import logging
import os
import pathlib
import sys
import uuid
import weakref
import json
import hashlib
import time
from dataclasses import asdict
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
from typing import Literal


_ROOT = str(pathlib.Path(__file__).parent.parent.resolve())
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response, UploadFile, File, Request as HTTPRequest, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from core.chat_events import emit
from core.chat_stream import chat_event_stream
from core.llm_limits import HISTORY_MESSAGES
from core.evidence import public_sources
from api import operations

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BANNER = r"""
    ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ
   ╔══════════════════════╗
   ║   EchoMind  v2.0     ║
   ║   智能客服 AI 系统    ║
   ╚══════════════════════╝
    ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ
"""

# ── 全局组件（lifespan 中初始化）─────────────────────────────────────────────
_orchestrator = None
_memory       = None
_tool_manager = None
_monitor      = None
_evaluator    = None
_skill_manager = None
_background_tasks = set()
_conversation_locks = weakref.WeakValueDictionary()
_chat_slots = asyncio.Semaphore(8)
_eval_lock = asyncio.Lock()

def _anthropic_cfg() -> Dict[str, Any]:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("未设置 ANTHROPIC_API_KEY")
    cfg: Dict[str, Any] = {
        "api_key":  key,
        "model":    os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022").strip(),
    }
    base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
    if base_url:
        cfg["base_url"] = base_url
    return cfg


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _orchestrator, _memory, _tool_manager, _monitor, _evaluator, _skill_manager, _chat_slots

    print(BANNER, flush=True)

    from agents.agent_orchestrator import AgentOrchestrator, Request, build_shared_rag_tools
    from core.intent_recognizer import IntentRecognizer
    from evaluation.evaluator import EndToEndEvaluator
    from mcp.knowledge_base import KnowledgeBase
    from mcp.tool_manager import MCPToolManager, Tool
    from memory.conversation_memory import MemoryManager
    from monitor.performance_monitor import PerformanceMonitor
    from core.skill_loader import SkillManager
    from business.store import BusinessStore
    from business.auth import AuthService
    from agents.tools import build_business_tools
    from integrations.feishu import FeishuConfig, FeishuService

    feishu_config = FeishuConfig.from_env()
    store = BusinessStore(
        os.getenv("ECHOMIND_BUSINESS_DB_PATH", str(pathlib.Path(_ROOT) / "data/business/echomind.sqlite3")),
        confirmation_ttl_seconds=int(os.getenv("ECHOMIND_CONFIRMATION_TTL", "900")),
        feishu_target=feishu_config.target,
    )
    feishu = FeishuService(store, feishu_config)
    operations.configure(operations.Runtime(
        store=store, auth=AuthService(store),
        auth_required=os.getenv("ECHOMIND_AUTH_REQUIRED", "true").lower() != "false",
        secure_cookies=os.getenv("ECHOMIND_SECURE_COOKIES", "false").lower() == "true",
        evaluator=lambda: _evaluator,
        feishu=feishu,
    ))
    _chat_slots = asyncio.Semaphore(max(1, int(os.getenv("ECHOMIND_MAX_CONCURRENT_CHATS", "8"))))

    cfg = _anthropic_cfg()
    logger.info(f"模型: {cfg['model']}  base_url: {cfg.get('base_url', '(官方)')}")

    # 意图识别器（Orchestrator 内部也会创建，这里单独暴露给 Evaluator）
    recognizer = IntentRecognizer(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
    )

    # Skills：启动时从目录加载业务能力说明，并在 Agent 调用 LLM 时动态注入。
    skills_dir = os.getenv("ECHOMIND_SKILLS_DIR", str(pathlib.Path(_ROOT) / "skills"))
    _skill_manager = SkillManager(
        root_dir=skills_dir,
    )
    _skill_manager.load()

    # Agent 编排器
    _orchestrator = AgentOrchestrator(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        skill_manager=_skill_manager,
    )

    # 记忆管理器（Redis 工作记忆 + ChromaDB 情景记忆/用户画像）
    _memory = MemoryManager(
        redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
        chroma_host=os.getenv("CHROMA_HOST", "chromadb"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
        chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", "/app/data/chroma"),
        allow_local_fallback=os.getenv("ECHOMIND_ALLOW_LOCAL_CHROMA_FALLBACK", "false").lower() == "true",
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
    )

    # MCP 工具管理器 + RAG 知识库（基于 ChromaDB 的真实检索）
    _tool_manager = MCPToolManager(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
    )
    kb = KnowledgeBase(
        chroma_host=os.getenv("CHROMA_HOST", "chromadb"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
        chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", "/app/data/chroma"),
        allow_local_fallback=os.getenv("ECHOMIND_ALLOW_LOCAL_CHROMA_FALLBACK", "false").lower() == "true",
    )
    logger.info(f"知识库已加载: {await kb.doc_count_async()} 个文档片段")

    def knowledge_fallback(params: Dict[str, Any], context: Optional[Dict[str, Any]], error: str):
        query = params.get("query", "")
        return [{
            "title": "知识库降级结果",
            "content": f"知识库暂时不可用，未能完成对“{query}”的语义检索。请稍后重试，或转人工客服确认。",
            "score": 0.0,
            "fallback": True,
            "error": error,
        }]

    _tool_manager.register(Tool(
        name="knowledge_search",
        description="搜索知识库（基于 ChromaDB 向量检索）",
        handler=kb.search_handler,
        schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
            },
            "required": ["query"],
        },
        cache_ttl=300.0,
        supports_rerank=True,
        fallback=knowledge_fallback,
    ))
    if _orchestrator is not None:
        _orchestrator.set_shared_tools({
            **build_shared_rag_tools(_tool_manager),
            **build_business_tools(store),
        })

    # 性能监控（可选启动 Prometheus）
    prom_port = int(os.getenv("PROMETHEUS_PORT", "0")) or None
    _monitor = PerformanceMonitor(
        orchestrator=_orchestrator,
        tool_manager=_tool_manager,
        interval_s=float(os.getenv("MONITOR_INTERVAL", "10")),
        webhook_url=os.getenv("ALERT_WEBHOOK_URL") or None,
        prometheus_port=prom_port,
    )
    await _monitor.start()

    # 评测器
    _evaluator = EndToEndEvaluator(
        orchestrator=_orchestrator,
        recognizer=recognizer,
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        baseline_path=os.getenv("EVAL_BASELINE_PATH", "/app/data/eval/baseline.json"),
    )

    logger.info("EchoMind 已就绪")
    feishu.start()
    try:
        yield
    finally:
        await feishu.close()

    await _monitor.stop()
    for task in list(_background_tasks):
        task.cancel()
    if _background_tasks:
        await asyncio.gather(*_background_tasks, return_exceptions=True)
    if _memory is not None:
        await _memory.close()
    operations.configure(None)
    logger.info("EchoMind 已关闭")


# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title="EchoMind 智能客服",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[value.strip() for value in os.getenv(
        "ECHOMIND_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if value.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
operations.install(app)


# ── 请求/响应模型 ─────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message:     str = Field(min_length=1, max_length=10000)
    user_id:     str = "anonymous"
    conv_id:     Optional[str] = None
    client_request_id: Optional[str] = Field(default=None, max_length=128)


class ChatResponse(BaseModel):
    conv_id:     str
    request_id:  str = ""
    response:    str
    intent:      str
    intent_group: str = "other"
    agent_type:  str
    agent_types: List[str] = Field(default_factory=list)
    primary_agent: str = ""
    supporting_agents: List[str] = Field(default_factory=list)
    tools_used: List[str] = Field(default_factory=list)
    routing_reason: str = ""
    routing_confidence: float = 0.0
    escalated:   bool
    escalation_reasons: List[str] = Field(default_factory=list)
    handoff_status: str = "not_requested"  # recommendation/draft, never a dispatched handoff
    latency_ms:  float
    knowledge_used: bool = False
    entities: Dict[str, List[str]] = Field(default_factory=dict)
    intent_confidence: float = 0.0
    intent_source_scores: Dict[str, float] = Field(default_factory=dict)
    sources: List[Dict[str, Any]] = Field(default_factory=list)
    tasks: List[Dict[str, Any]] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    duration_ms: float = 0.0
    feedback_available: bool = False


class ToolTraceResponse(BaseModel):
    request_id: str
    found: bool
    trace: Dict[str, Any] = Field(default_factory=dict)


class RecentToolTracesResponse(BaseModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)


# ── 路由 ──────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    rt = operations._runtime
    return {"status": "ok",
            "auth_required": bool(rt and rt.auth_required), "workflow": "native_python"}


@app.get("/health/live")
async def live():
    return {"status": "alive"}


@app.get("/health/ready")
async def ready():
    if _orchestrator is None or _memory is None or operations._runtime is None:
        raise HTTPException(503, "服务尚未完成初始化")
    try:
        store = operations.runtime().store
        def probe():
            connection = store._connect()
            try:
                connection.execute("SELECT 1").fetchone()
            finally:
                connection.close()
        await asyncio.wait_for(asyncio.to_thread(probe), timeout=3)
        redis = getattr(_memory, "_redis", None)
        if redis is not None:
            await asyncio.wait_for(redis.ping(), timeout=3)
        for client in [getattr(_memory, "_chroma", None),
                       getattr(_knowledge_base(), "_client", None)]:
            if client is not None:
                await asyncio.wait_for(asyncio.to_thread(client.heartbeat), timeout=3)
    except Exception:
        raise HTTPException(503, "依赖服务尚未就绪")
    return {"status": "ready", "workflow": "native_python"}


@app.get("/skills", tags=["Skills"])
async def skills_summary():
    """查看当前已加载的 Skills，便于确认热加载结果和排查解析错误。"""
    if _skill_manager is None:
        raise HTTPException(503, "Skills 未初始化")
    return _skill_manager.summary()


@app.post("/skills/reload", tags=["Skills"])
async def reload_skills():
    """运行时重新扫描 Skill 目录，不需要重启服务。"""
    if _skill_manager is None:
        raise HTTPException(503, "Skills 未初始化")
    _skill_manager.reload()
    if _orchestrator is not None:
        _orchestrator.set_skill_manager(_skill_manager)
    return _skill_manager.summary()


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: HTTPRequest):
    return await _run_chat(req, getattr(request.state, "principal", None))


@app.post("/chat/stream", tags=["聊天"])
async def chat_stream(req: ChatRequest, request: HTTPRequest):
    if _orchestrator is None or _memory is None:
        raise HTTPException(503, "服务未就绪")

    async def run():
        response = await _run_chat(req, getattr(request.state, "principal", None))
        return response.model_dump()

    return StreamingResponse(
        chat_event_stream(run),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _run_chat(req: ChatRequest, actor=None):
    req = req.model_copy(update={"conv_id": req.conv_id or str(uuid.uuid4()),
                                 "user_id": actor.user_id if actor else req.user_id})
    key = (actor.organization_id if actor else "legacy", req.user_id, req.conv_id)
    lock = _conversation_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _conversation_locks[key] = lock
    async with _chat_slots, lock:
        try:
            return await asyncio.wait_for(_run_chat_inner(req, actor),
                                          timeout=float(os.getenv("ECHOMIND_CHAT_TIMEOUT", "180")))
        except asyncio.TimeoutError:
            raise HTTPException(504, {"code": "CHAT_TIMEOUT", "message": "处理超时，请稍后重试"})


async def _run_chat_inner(req: ChatRequest, actor=None):
    """
    主对话接口。完整流程：
      记忆读取 → 意图识别 → Agent 路由 → 执行 → 记忆写入
    """
    if _orchestrator is None or _memory is None:
        raise HTTPException(503, "服务未就绪")
    started_at = time.perf_counter()

    from agents.agent_orchestrator import Request as OrcReq
    from memory.conversation_memory import MsgRole

    conv_id = req.conv_id or str(uuid.uuid4())
    if actor:
        await asyncio.to_thread(operations.runtime().store.claim_conversation, actor, conv_id)
    memory_user = f"{actor.organization_id}:{actor.user_id}" if actor else req.user_id
    emit("session", conv_id=conv_id)

    # 1. 读取记忆上下文
    emit("stage", message="正在读取对话上下文")
    mem_ctx = await _memory.get_context(memory_user, conv_id, query=req.message)

    # 2. 构建编排请求（含对话历史，用于意图识别上下文）
    history = [
        {"role": m.role.value, "content": m.content}
        for m in mem_ctx.recent_messages[-HISTORY_MESSAGES:]
    ] if mem_ctx.recent_messages else None

    emit("stage", message="正在识别问题类型")
    intent_result = await _orchestrator.recognize_intent(req.message, history=history)
    full_context = mem_ctx.to_prompt_text()

    orch_req = OrcReq(
        message=req.message,
        user_id=req.user_id,
        conv_id=conv_id,
        context=full_context,
        history=history,
        entities=intent_result.entities,
        intent=intent_result.intent,
        intent_group=intent_result.intent_group,
        urgency=intent_result.urgency,
        intent_confidence=intent_result.confidence,
        principal=actor,
        retrieval_context=operations.knowledge_scope(actor=actor),
    )
    if actor:
        await asyncio.to_thread(operations.runtime().store.bind_request, actor,
                                orch_req.request_id, conv_id)

    # 3. 执行
    result = await _orchestrator.run(orch_req)
    declined_ticket = any(word in req.message for word in ("不要创建", "不要提交", "无需工单", "不需要工单"))
    wants_ticket = not declined_ticket and (result.escalated or ("工单" in req.message and
                                       any(word in req.message for word in ("创建", "提交", "建立")))
    )
    if actor and wants_ticket and not orch_req.business_tasks:
        category = "billing" if intent_result.intent_group == "billing" else "technical"
        task = await asyncio.to_thread(operations.runtime().store.create_draft, actor, {
            "title": req.message[:150], "description": req.message, "category": category,
            "priority": "urgent" if intent_result.urgency.name == "CRITICAL" else "normal",
            "metadata": {"conversation_id": conv_id},
        }, "chat-draft:" + hashlib.sha256(
            f"{actor.user_id}:{conv_id}:{req.client_request_id or orch_req.request_id}".encode()).hexdigest())
        orch_req.business_tasks.append(task)
    if orch_req.business_tasks:
        result.response += "\n\n已保存待确认的工单草稿，请在“工单”页面核对后确认提交。"

    # 4. 写入记忆
    emit("stage", message="正在保存本轮对话")
    await _memory.add_message(memory_user, conv_id, MsgRole.USER, req.message)
    await _memory.add_message(memory_user, conv_id, MsgRole.ASSISTANT, result.response)

    # 5. 异步更新用户画像（不阻塞响应）
    async def update_profile():
        try:
            await _memory.update_profile(memory_user, conv_id)
        except Exception:
            logger.exception("用户偏好后台更新失败")
    background = asyncio.create_task(update_profile())
    _background_tasks.add(background)
    background.add_done_callback(_background_tasks.discard)

    response = ChatResponse(
        conv_id=conv_id,
        request_id=result.request_id,
        response=result.response,
        intent=result.intent.value if result.intent else "other",
        intent_group=intent_result.intent_group,
        agent_type=result.agent_type.value,
        agent_types=[agent_type.value for agent_type in result.agent_types],
        primary_agent=result.primary_agent.value if result.primary_agent else result.agent_type.value,
        supporting_agents=[agent_type.value for agent_type in result.supporting_agents],
        tools_used=result.tools_used,
        routing_reason=result.routing_reason,
        routing_confidence=result.routing_confidence,
        escalated=result.escalated,
        escalation_reasons=list(getattr(result, "escalation_reasons", [])),
        handoff_status=("pending_confirmation" if any(t.get("status") == "waiting_confirmation" for t in orch_req.business_tasks)
                        else "recommended" if result.escalated else "not_requested"),
        latency_ms=round(result.latency_ms, 1),
        knowledge_used=bool(orch_req.evidence),
        entities=intent_result.entities,
        intent_confidence=round(intent_result.confidence, 4),
        intent_source_scores=intent_result.source_scores,
        sources=public_sources(orch_req.evidence),
        tasks=orch_req.business_tasks,
        warnings=orch_req.warnings,
        duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
    )
    if actor:
        await asyncio.to_thread(operations.insight_service().record_answer, actor,
                                result.request_id, req.message, response.model_dump())
        response.feedback_available = True
    return response


@app.get("/monitor")
async def monitor_summary():
    """实时监控摘要：Agent 成功率、工具统计、告警、优化建议。"""
    if _monitor is None:
        raise HTTPException(503, "服务未就绪")
    return _monitor.summary()


@app.get("/trace/tool/{request_id}", response_model=ToolTraceResponse)
async def get_tool_trace(request_id: str, request: HTTPRequest):
    """查看某次请求的工具调用明细。"""
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    actor = getattr(request.state, "principal", None)
    if actor and not await asyncio.to_thread(operations.runtime().store.can_read_request, actor, request_id):
        raise HTTPException(404, "未找到该请求")
    trace = _orchestrator.get_tool_trace(request_id)
    return ToolTraceResponse(
        request_id=request_id,
        found=trace is not None,
        trace=trace or {},
    )


@app.get("/trace/tools", response_model=RecentToolTracesResponse)
async def list_recent_tool_traces(request: HTTPRequest, limit: int = Query(20, ge=1, le=100)):
    """查看最近 N 次请求的工具调用明细。"""
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    items = _orchestrator.get_recent_tool_traces(limit=limit)
    actor = getattr(request.state, "principal", None)
    if actor:
        items = [item for item in items if await asyncio.to_thread(
            operations.runtime().store.can_read_request, actor, item["request_id"])]
    return RecentToolTracesResponse(items=items)


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus 指标入口。"""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/search")
async def search(request: HTTPRequest, query: str = Query(min_length=1, max_length=4000),
                 top_k: int = Query(5, ge=1, le=100),
                 strategy: Literal["vector", "bm25", "hybrid"] = "vector", rewrite: bool = True,
                 product_version: Optional[str] = Query(None, min_length=1, max_length=100),
                 knowledge_base_id: Optional[str] = Query(None, min_length=1, max_length=100)):
    """
    在身份范围内检索，返回改写、重排及降级的实际执行状态。
    """
    if _tool_manager is None:
        raise HTTPException(503, "服务未就绪")
    filters = [{name: value} for name, value in (("product_version", product_version),
                                               ("knowledge_base_id", knowledge_base_id)) if value is not None]
    where = {"$and": filters} if len(filters) > 1 else filters[0] if filters else None
    result = await _tool_manager.search_with_rewrite(
        "knowledge_search", query, top_k=top_k, context=operations.knowledge_scope(request),
        strategy=strategy, rewrite=rewrite, where=where)
    payload = {"query": query, "results": result.data or [], "success": result.success,
               "error": result.error, "fallback": result.fallback, "cached": result.cached,
               "reranked": result.reranked, "rerank_status": result.rerank_status,
               "rerank_error": result.rerank_error, "partial_failure": result.partial_failure,
               "rewrite_status": result.rewrite_status, "strategy": strategy,
               "filters": {name: value for field in filters for name, value in field.items()}}
    return JSONResponse(payload, status_code=200 if result.success else 503)


class DocInput(BaseModel):
    """单篇文档输入。"""
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=1000000)
    document_id: Optional[str] = Field(default=None, max_length=200)
    source_url: str = Field(default="", max_length=2000)
    product_version: str = Field(default="", max_length=100)
    locator: str = Field(default="", max_length=500)
    knowledge_base_id: str = Field(default="default", min_length=1, max_length=100)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class BatchDocInput(BaseModel):
    """批量文档导入请求体。"""
    documents: List[DocInput] = Field(min_length=1, max_length=200)


class EvalIntentInput(BaseModel):
    """意图识别评测用例。"""
    message: str
    expected_intent: str
    context: Optional[Dict[str, Any]] = None


class EvalDialogInput(BaseModel):
    """对话质量评测用例。question 单轮，turns 多轮。"""
    question: Optional[str] = None
    turns: Optional[List[str]] = None
    user_id: Optional[str] = None
    conv_id: Optional[str] = None
    reference_facts: Optional[List[str]] = None
    actual_evidence: Optional[List[Dict[str, Any]]] = None


class EvalRunInput(BaseModel):
    """评测请求。为空时使用内置默认用例。"""
    intent_cases: Optional[List[EvalIntentInput]] = None
    dialog_cases: Optional[List[EvalDialogInput]] = None
    dataset_version: Optional[str] = Field(default=None, max_length=200)
    config_version: Optional[str] = Field(default=None, max_length=200)


@app.post("/knowledge/add", tags=["知识库"])
async def add_knowledge(body: BatchDocInput, request: HTTPRequest):
    """
    批量导入文档到知识库。

    文档按最多 500 字切片，由 Chroma 客户端配置的 Embedding 函数向量化。

    示例请求体：
    ```json
    {
      "documents": [
        {"title": "退款政策", "content": "用户在购买后 7 天内可以申请无理由退款..."},
        {"title": "配送说明", "content": "标准配送 3-5 个工作日..."}
      ]
    }
    ```
    """
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__
    documents = []
    for item in body.documents:
        documents.append(await _scoped_document(item.model_dump(exclude_none=True), request, kb))
    try:
        count = await kb.add_documents_async(documents)
    except ValueError as error:
        raise HTTPException(422, str(error))
    stats = await kb.stats_async(where=operations.knowledge_scope(request)["where"])
    return {"message": f"成功导入 {count} 个文档片段", "added_chunks": count, **stats}


@app.post("/knowledge/upload", tags=["知识库"])
async def upload_knowledge(request: HTTPRequest, file: UploadFile = File(...)):
    """
    上传文件导入知识库。

    支持格式：
    - `.txt` / `.md`：整个文件作为一篇文档，文件名作为标题
    - `.json`：JSON 数组格式 `[{"title": "...", "content": "..."}, ...]`

    文件大小限制：10MB
    """
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__

    filename = file.filename or "unknown"
    suffix = pathlib.Path(filename).suffix.lower()
    if suffix not in {".txt", ".md", ".json"}:
        raise HTTPException(415, "仅支持 TXT、Markdown 和 JSON 文档")
    chunks, size = [], 0
    while chunk := await file.read(65536):
        size += len(chunk)
        if size > 10 * 1024 * 1024:
            raise HTTPException(413, "文件大小超过 10MB 限制")
        chunks.append(chunk)
    try:
        text = b"".join(chunks).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(422, "文件须使用 UTF-8 编码")

    if suffix == ".json":
        import json as _json
        try:
            docs = _json.loads(text)
            if not isinstance(docs, list):
                raise HTTPException(400, "JSON 文件应为数组格式: [{title, content}, ...]")
        except _json.JSONDecodeError as e:
            raise HTTPException(400, f"JSON 解析失败: {e}")
    else:
        # txt / md：整个文件作为一篇文档
        title = filename.rsplit(".", 1)[0] if "." in filename else filename
        docs = [{"title": title, "content": text}]

    try:
        body = BatchDocInput(documents=docs)
    except ValueError:
        raise HTTPException(422, "文档结构无效，需提供非空 title、content 及有效元数据")
    return await add_knowledge(body, request)


@app.get("/knowledge/stats", tags=["知识库"])
async def knowledge_stats(request: HTTPRequest):
    """查看知识库统计信息（文档片段总数）。"""
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__
    return await kb.stats_async(where=operations.knowledge_scope(request)["where"])


def _knowledge_base():
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    return tool.handler.__self__


async def _scoped_document(document, request, kb):
    actor = getattr(request.state, "principal", None)
    organization_id = actor.organization_id if actor else "public"
    document = {**document, "organization_id": organization_id,
                "access_scope": "organization" if actor else "public"}
    if document.get("document_id"):
        existing = await kb.get_document_async(document["document_id"])
        if existing and existing.get("organization_id") != organization_id:
            raise HTTPException(404, "未找到可管理的文档")
    return document


@app.get("/knowledge/documents", tags=["知识库"])
async def list_knowledge(request: HTTPRequest):
    return {"items": await _knowledge_base().list_documents_async(
        where=operations.knowledge_scope(request)["where"])}


@app.get("/knowledge/documents/{document_id}", tags=["知识库"])
async def get_knowledge(document_id: str, request: HTTPRequest):
    item = await _knowledge_base().get_document_async(
        document_id, where=operations.knowledge_scope(request)["where"])
    if item is None:
        raise HTTPException(404, "未找到文档")
    return item


@app.put("/knowledge/documents/{document_id}", tags=["知识库"])
async def replace_knowledge(document_id: str, body: DocInput, request: HTTPRequest):
    kb = _knowledge_base()
    existing = await kb.get_document_async(document_id)
    if existing is None:
        raise HTTPException(404, "未找到文档")
    document = await _scoped_document({**body.model_dump(exclude_none=True, exclude_unset=True),
                                       "document_id": document_id}, request, kb)
    try:
        count = await kb.update_document_async(document_id, document)
    except (ValueError, KeyError) as error:
        raise HTTPException(422, str(error))
    return {"success": True, "document_id": document_id, "updated_chunks": count}


@app.delete("/knowledge/documents/{document_id}", tags=["知识库"])
async def delete_knowledge(document_id: str, request: HTTPRequest):
    kb = _knowledge_base()
    item = await kb.get_document_async(document_id)
    if item is None:
        raise HTTPException(404, "未找到文档")
    await _scoped_document({**item, "document_id": document_id}, request, kb)
    return {"success": True, "deleted_chunks": await kb.delete_document_async(document_id)}


@app.post("/eval/run")
async def run_eval(request: HTTPRequest, body: Optional[EvalRunInput] = None):
    """运行内置评测用例，返回评测报告。"""
    if _evaluator is None:
        raise HTTPException(503, "服务未就绪")
    from evaluation.evaluator import DEFAULT_DIALOG_CASES, DEFAULT_INTENT_CASES, IntentTestCase

    if body and body.intent_cases is not None:
        intent_cases = [
            IntentTestCase(
                message=c.message,
                expected_intent=c.expected_intent,
                context=c.context,
            )
            for c in body.intent_cases
        ]
    else:
        intent_cases = DEFAULT_INTENT_CASES

    if body and body.dialog_cases is not None:
        dialog_cases = [
            c.model_dump(exclude_none=True)
            for c in body.dialog_cases
        ]
    else:
        dialog_cases = DEFAULT_DIALOG_CASES

    actor = getattr(request.state, "principal", None)
    async with _eval_lock:
        report = await _evaluator.run(
            intent_cases=intent_cases, dialog_cases=dialog_cases,
            dataset_version=body.dataset_version if body else None,
            config_version=body.config_version if body else None,
            metadata={"organization_id": actor.organization_id if actor else "public"},
            retrieval_context=operations.knowledge_scope(request),
        )
    return asdict(report)


def _report_access(report, request):
    actor = getattr(request.state, "principal", None)
    if actor and report.metadata.get("custom", {}).get("organization_id") != actor.organization_id:
        raise HTTPException(404, "未找到评测报告")
    return report


@app.get("/eval/reports")
async def eval_reports(request: HTTPRequest, limit: int = Query(50, ge=1, le=200)):
    if _evaluator is None:
        raise HTTPException(503, "评测器未就绪")
    actor = getattr(request.state, "principal", None)
    reports = _evaluator.list_reports(limit, organization_id=actor.organization_id if actor else None)
    return {"items": [asdict(report) for report in reports]}


@app.get("/eval/reports/{run_id}")
async def eval_report(run_id: str, request: HTTPRequest):
    if _evaluator is None:
        raise HTTPException(503, "评测器未就绪")
    try:
        return asdict(_report_access(_evaluator.get_report(run_id), request))
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "未找到评测报告")


@app.post("/eval/reports/{run_id}/baseline")
async def promote_baseline(run_id: str, request: HTTPRequest):
    if _evaluator is None:
        raise HTTPException(503, "评测器未就绪")
    try:
        report = _report_access(_evaluator.get_report(run_id), request)
        async with _eval_lock:
            promoted = _evaluator.promote_baseline(report)
        return {"success": True, "run_id": promoted.run_id}
    except FileNotFoundError:
        raise HTTPException(404, "未找到可晋升的评测报告")
    except ValueError as error:
        raise HTTPException(422, str(error))


# ── 交互式 CLI ────────────────────────────────────────────────────────────────
async def _cli():
    print(BANNER)
    print("EchoMind CLI — 输入 quit 退出\n")

    from agents.agent_orchestrator import AgentOrchestrator, Request
    from memory.conversation_memory import MemoryManager, MsgRole
    from core.skill_loader import SkillManager

    cfg = _anthropic_cfg()
    skill_manager = SkillManager(
        root_dir=os.getenv("ECHOMIND_SKILLS_DIR", str(pathlib.Path(_ROOT) / "skills")),
    )
    skill_manager.load()
    orch = AgentOrchestrator(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        skill_manager=skill_manager,
    )
    mem  = MemoryManager(
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        chroma_host=os.getenv("CHROMA_HOST", "localhost"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
        chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", "/tmp/chroma"),
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
    )

    user_id, conv_id = "cli_user", str(uuid.uuid4())

    while True:
        try:
            msg = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见 ʕ•ᴥ•ʔ")
            break
        if not msg or msg.lower() in ("quit", "exit", "退出"):
            print("再见 ʕ•ᴥ•ʔ")
            break

        ctx = await mem.get_context(user_id, conv_id, query=msg)
        history = [
            {"role": m.role.value, "content": m.content}
            for m in ctx.recent_messages[-HISTORY_MESSAGES:]
        ] if ctx.recent_messages else None
        req = Request(message=msg, user_id=user_id, conv_id=conv_id, context=ctx.to_prompt_text(), history=history)
        result = await orch.run(req)

        await mem.add_message(user_id, conv_id, MsgRole.USER, msg)
        await mem.add_message(user_id, conv_id, MsgRole.ASSISTANT, result.response)

        print(f"\nEchoMind [{result.agent_type.value}]: {result.response}\n")

    await mem.close()


if __name__ == "__main__":
    if "--cli" in sys.argv:
        asyncio.run(_cli())
    else:
        uvicorn.run(
            "api.main:app",
            host=os.getenv("API_HOST", "0.0.0.0"),
            port=int(os.getenv("API_PORT", "8000")),
            reload=os.getenv("APP_ENV") == "development",
        )
