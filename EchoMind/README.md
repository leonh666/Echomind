# EchoMind

EchoMind 是一个面向客服/运营场景的多 Agent 智能系统。它不是单纯的聊天机器人，而是把以下能力串成闭环：

- 细粒度意图识别
- 路由驱动的多 Agent 编排
- 意图驱动 RAG 检索
- Redis + ChromaDB 分层记忆
- 动态 Skills 注入
- 在线监控与路由降权
- LLM-as-Judge 端到端评测

## 你可以先看什么

- [本轮 Python 纯链路实施、启动与验收](wiki/Python纯链路实施与验收.md)：登录、持久化工单确认、知识管理与评测基线；保留现有 Python 编排。
- [项目改进说明书](wiki/项目改进说明书.md)
- [自动验证记录](docs/native-python-verification.md)
- [技术亮点](wiki/技术亮点.md)
- [重点代码](wiki/重点代码.md)
- [业务流程说明](wiki/业务流程说明.md)
- [完整使用指南](wiki/完整使用指南.md)

## 快速开始

### 1. 准备环境

- Docker
- Docker Compose
- `ANTHROPIC_API_KEY`

如果使用兼容 Anthropic 协议的第三方模型服务，按服务商实际提供的地址与模型标识配置：

```env
ANTHROPIC_BASE_URL=https://your-provider.example/anthropic
ANTHROPIC_MODEL=your-model-id
ANTHROPIC_API_KEY=your_key
```

### 2. 配置环境变量

复制示例配置：

```bash
cp .env.example .env
```

最少确认这些变量可用：

```env
ANTHROPIC_API_KEY=your_api_key
REDIS_PASSWORD=replace-with-a-strong-password
```

新版本默认启用登录，请按[账号开通步骤](wiki/Python纯链路实施与验收.md#3-启动账号与权限)创建管理员后使用页面。业务数据库、原有 Redis/Chroma 数据与评测报告都需要保留。

### 3. 启动服务

在当前 `EchoMind` 目录启动 Python 后端及配套服务（Redis、ChromaDB、Prometheus、Nginx，不包含 Vue 前端和 Java 后端）：

```bash
docker compose up -d --build
```

查看状态：

```bash
docker compose ps
```

看日志：

```bash
docker compose logs -f echomind
```

如果要从前端目录统一启动完整系统（Vue 前端、Python 后端和配套服务），使用：

```powershell
cd D:\EchoMind\EchoMindFrontend
docker compose up -d --build
docker compose ps
```

全栈配置沿用默认的 `echomind` 项目名和已有 Python 数据。已经按上面的命令启动 Python 后端时，不需要先删除容器；切换后统一在前端目录管理服务。Python 后端读取本目录的 `.env`，前端目录的 `.env` 保存共享 Redis 密码和端口。配置更新可能让相关容器短暂重启。

`EchoMindJava` 是另一种语言的实现，不是 Python 版的依赖；这套全栈配置不构建或启动 Java 服务。

首次配置、后续启动和数据保留说明见 [前端全栈部署指南](../EchoMindFrontend/README.md#docker-部署)。

### 4. 访问入口

- API: `http://localhost:8000`
- Swagger: `http://localhost:8000/docs`
- Nginx: `http://localhost`
- Health: `http://localhost:8000/health`

前端全栈启动后，`http://localhost` 显示前端页面，也可以通过 `http://localhost:5174` 访问前端；Python Swagger 仍位于 `http://localhost:8000/docs`。

## 核心功能

### 对话主链路

`POST /chat`

网页聊天使用 `POST /chat/stream`，通过 SSE 实时返回处理进度、工具调用和回复文字。`POST /chat` 保留完整 JSON 响应，便于脚本和其他客户端使用。

流程是：

```text
读取记忆 -> 意图识别 -> 知识检索 -> Agent 路由 -> 回复生成 -> 写回记忆
```

### 知识库

- `POST /search`
- `POST /knowledge/add`
- `POST /knowledge/upload`
- `GET /knowledge/stats`

### Skills

- `GET /skills`
- `POST /skills/reload`

### 监控与评测

- `GET /monitor`
- `POST /eval/run`

## 项目结构

```text
api/main.py                  FastAPI 入口
agents/agent_orchestrator.py 多 Agent 编排
core/intent_recognizer.py    三路融合意图识别
core/skill_loader.py         动态 Skills 加载
memory/conversation_memory.py  Redis + ChromaDB 记忆
mcp/tool_manager.py          工具层、缓存、熔断、重排
mcp/knowledge_base.py        ChromaDB 知识库
monitor/performance_monitor.py 在线监控
evaluation/evaluator.py      端到端评测
wiki/                       详细文档
skills/                     动态业务规则
data/                       持久化数据
```

## 运行时架构

```text
用户请求
  -> /chat
  -> MemoryManager 读取工作记忆、情景记忆、用户画像
  -> IntentRecognizer 输出 intent / intent_group / urgency / entities
  -> 按意图决定是否检索知识库
  -> AgentOrchestrator 路由到 General / Technical / Billing / Escalation
  -> Skills 注入、工具调用、回复生成
  -> 写回 Redis 和 ChromaDB
  -> Monitor 采集在线指标
  -> Evaluator 做意图识别和回复质量评测
```

## 主要端口

| 服务 | 端口 |
|---|---:|
| EchoMind API | 8000 |
| ChromaDB | 8001 |
| Redis | 6379 |
| Prometheus | 9090 |
| Nginx | 80 |

## 开发和调试

常用顺序：

```text
1. /health
2. /chat
3. /skills
4. /monitor
5. /eval/run
```

如果你只想看项目怎么工作，直接读：

- [EchoMind定位与技术亮点](wiki/EchoMind定位与技术亮点.md)
- [技术亮点](wiki/技术亮点.md)
- [重点代码](wiki/重点代码.md)

## 一句话概括

EchoMind 是一个可观测、可评测、可降级的多 Agent 客服运行时。
