"""Native Python business API. No agent framework and no application imports."""
import asyncio
from dataclasses import asdict, dataclass
import os
import time
from typing import Any, Callable, Dict, Literal, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field


COOKIE = "echomind_session"
router = APIRouter()
_runtime = None
_login_windows: Dict[str, tuple] = {}


@dataclass
class Runtime:
    store: Any
    auth: Any
    auth_required: bool = True
    secure_cookies: bool = False
    evaluator: Optional[Callable] = None


def configure(runtime):
    global _runtime
    _runtime = runtime


def runtime():
    if _runtime is None:
        raise HTTPException(503, {"code": "BUSINESS_UNAVAILABLE", "message": "业务服务尚未初始化"})
    return _runtime


def token_from(request):
    header = request.headers.get("authorization", "")
    if header:
        if not header.startswith("Bearer ") or not header[7:].strip():
            raise HTTPException(401, {"code": "INVALID_SESSION", "message": "登录凭证格式无效"})
        return header[7:].strip()
    return request.cookies.get(COOKIE)


def principal(request: Request):
    actor = getattr(request.state, "principal", None)
    if actor is None:
        raise HTTPException(401, {"code": "LOGIN_REQUIRED", "message": "请先登录"})
    return actor


def require_role(request, *roles):
    actor = principal(request)
    if actor.role not in roles:
        raise HTTPException(403, {"code": "ROLE_REQUIRED", "message": "当前账号没有此操作权限"})
    return actor


def knowledge_scope(request=None, actor=None):
    actor = actor or (getattr(request.state, "principal", None) if request else None)
    where = {"organization_id": "public"}
    if actor:
        where = {"$or": [{"organization_id": "public"}, {"organization_id": actor.organization_id}]}
    return {"where": where, "organization_id": actor.organization_id if actor else "public",
            "user_id": actor.user_id if actor else "anonymous"}


def install(app: FastAPI):
    """Protect existing and new routes when real lifespan configures Runtime."""
    from business.store import BusinessError

    @app.exception_handler(BusinessError)
    async def business_error(request, error):
        return JSONResponse(status_code=error.http_status,
                            content={"detail": {"code": error.code, "message": str(error)}})

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.principal = None
        rt = _runtime
        if rt is None:
            return await call_next(request)  # Isolated legacy tests, no real runtime.
        try:
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                origin = request.headers.get("origin")
                if origin:
                    allowed = {value.strip().rstrip("/") for value in
                               os.getenv("ECHOMIND_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if value.strip()}
                    own = f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
                    if origin.rstrip("/") not in allowed | {own.rstrip("/")}:
                        return JSONResponse(status_code=403, content={
                            "detail": {"code": "ORIGIN_DENIED", "message": "请求来源未获允许"}})
            public = request.url.path in {
                "/auth/login", "/auth/logout", "/health", "/health/live", "/health/ready",
                "/metrics", "/docs", "/openapi.json", "/redoc",
            } or request.method == "OPTIONS"
            token = token_from(request)
            if token and not public:
                request.state.principal = await asyncio.to_thread(rt.auth.authenticate, token)
            elif rt.auth_required and not public:
                return JSONResponse(status_code=401, content={
                    "detail": {"code": "LOGIN_REQUIRED", "message": "请先登录"}})
            if request.state.principal is not None:
                # Role changes are revalidated by authenticate/store, not copied
                # from an unsigned request body or from model tool parameters.
                path = request.url.path
                management = (
                    path == "/skills/reload" or path.startswith("/eval/") or path == "/eval/run"
                    or path == "/monitor" or path == "/trace/tools"
                    or (request.method in {"POST", "PUT", "PATCH", "DELETE"}
                        and path.startswith("/knowledge/"))
                )
                if management:
                    roles = {"admin", "kb_admin"} if path.startswith("/knowledge/") else {"admin"}
                    if request.state.principal.role not in roles:
                        return JSONResponse(status_code=403, content={
                            "detail": {"code": "ROLE_REQUIRED", "message": "当前账号没有此操作权限"}})
            return await call_next(request)
        except BusinessError as error:
            return JSONResponse(status_code=error.http_status,
                                content={"detail": {"code": error.code, "message": str(error)}})
        except HTTPException as error:
            return JSONResponse(status_code=error.status_code, content={"detail": error.detail})

    app.include_router(router)


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(StrictInput):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1024)
    organization_id: Optional[str] = None
    transport: Literal["cookie", "bearer"] = "cookie"


@router.post("/auth/login", tags=["身份"])
async def login(body: Login, request: Request, response: Response):
    rt = runtime()
    now = time.monotonic()
    address = request.client.host if request.client else "unknown"
    count, start = _login_windows.get(address, (0, now))
    if now - start >= 60:
        count, start = 0, now
    if count >= 10:
        raise HTTPException(429, {"code": "LOGIN_RATE_LIMIT", "message": "登录尝试过多，请稍后重试"})
    if len(_login_windows) > 2000:
        for key in list(_login_windows):
            if now - _login_windows[key][1] >= 60:
                _login_windows.pop(key, None)
    _login_windows[address] = (count + 1, start)
    result = await asyncio.to_thread(rt.auth.login, body.email, body.password, body.organization_id)
    token = result["token"]
    if body.transport == "cookie":
        response.set_cookie(COOKIE, token, httponly=True, secure=rt.secure_cookies,
                            samesite="lax", path="/",
                            max_age=int(getattr(rt.auth, "session_ttl_seconds", 86400)))
        result = {key: value for key, value in result.items() if key != "token"}
    return result


@router.get("/auth/me", tags=["身份"])
async def me(request: Request):
    actor = principal(request)
    return {"authenticated": True, **asdict(actor)}


@router.post("/auth/logout", tags=["身份"])
async def logout(request: Request, response: Response):
    token = token_from(request)
    if token:
        await asyncio.to_thread(runtime().auth.logout, token)
    response.delete_cookie(COOKIE, path="/")
    return {"success": True}


class TicketPayload(StrictInput):
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=10000)
    category: Literal["general", "technical", "billing"] = "technical"
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class DraftCreate(StrictInput):
    payload: TicketPayload
    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=128)


class DraftRevise(StrictInput):
    revision: int = Field(ge=1)
    payload: TicketPayload


class DraftConfirm(StrictInput):
    revision: int = Field(ge=1)
    payload_hash: str = Field(min_length=32, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)


class Cancel(StrictInput):
    revision: Optional[int] = Field(default=None, ge=1)


class TicketUpdate(StrictInput):
    status: str = Field(min_length=1, max_length=40)
    version: int = Field(ge=1)


@router.post("/tasks", status_code=201, tags=["工单任务"])
async def create_task(body: DraftCreate, request: Request):
    return await asyncio.to_thread(runtime().store.create_draft, principal(request),
                                   body.payload.model_dump(), body.idempotency_key)


@router.get("/tasks", tags=["工单任务"])
async def list_tasks(request: Request, limit: int = 50):
    return {"items": await asyncio.to_thread(runtime().store.list_tasks, principal(request), limit)}


@router.get("/tasks/{task_id}", tags=["工单任务"])
async def get_task(task_id: str, request: Request):
    return await asyncio.to_thread(runtime().store.get_task, principal(request), task_id)


@router.put("/tasks/{task_id}/draft", tags=["工单任务"])
async def revise_task(task_id: str, body: DraftRevise, request: Request):
    return await asyncio.to_thread(runtime().store.revise_draft, principal(request), task_id,
                                   body.payload.model_dump(), body.revision)


@router.post("/tasks/{task_id}/confirm", tags=["工单任务"])
async def confirm_task(task_id: str, body: DraftConfirm, request: Request):
    return await asyncio.to_thread(runtime().store.confirm_draft, principal(request), task_id,
                                   body.revision, body.payload_hash, body.idempotency_key)


@router.post("/tasks/{task_id}/cancel", tags=["工单任务"])
async def cancel_task(task_id: str, body: Cancel, request: Request):
    return await asyncio.to_thread(runtime().store.cancel_task, principal(request), task_id, body.revision)


@router.post("/tasks/{task_id}/reject", tags=["工单任务"])
async def reject_task(task_id: str, body: Cancel, request: Request):
    return await asyncio.to_thread(runtime().store.reject_task, principal(request), task_id, body.revision)


@router.get("/tickets", tags=["工单"])
async def list_tickets(request: Request, limit: int = 50):
    return {"items": await asyncio.to_thread(runtime().store.list_tickets, principal(request), limit)}


@router.get("/tasks/{task_id}/events", tags=["工单任务"])
async def task_events(task_id: str, request: Request):
    return {"items": await asyncio.to_thread(runtime().store.list_events,
                                             principal(request), task_id=task_id)}


@router.get("/tickets/{ticket_id}/events", tags=["工单"])
async def ticket_events(ticket_id: str, request: Request):
    return {"items": await asyncio.to_thread(runtime().store.list_events,
                                             principal(request), ticket_id=ticket_id)}


@router.get("/tickets/{ticket_id}", tags=["工单"])
async def get_ticket(ticket_id: str, request: Request):
    return await asyncio.to_thread(runtime().store.get_ticket, principal(request), ticket_id)


@router.patch("/tickets/{ticket_id}", tags=["工单"])
async def update_ticket(ticket_id: str, body: TicketUpdate, request: Request):
    return await asyncio.to_thread(runtime().store.update_ticket_status, principal(request),
                                   ticket_id, body.status, body.version)
