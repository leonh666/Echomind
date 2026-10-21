# EchoMind Frontend

独立 Vue 前端项目，默认 Docker 部署连接 EchoMind Python 版本。本地开发仍保留 Java 版本的接口适配。

项目目录：

```text
./EchoMindFrontend
```

## 功能

- Docker 模式直接连接 Python 后端；本地开发可切换 Java / Python 后端。
- 统一适配 `/chat` 响应字段：
  - Python：`conv_id`、`agent_type`、`latency_ms`
  - Java：`conversation_id`、`agent_type`、`latency_ms`
- 支持聊天调试、健康检查、监控摘要、知识库检索、知识库文档导入、文件上传。
- Python 聊天支持实时处理进度、逐段回复、实时工具调用记录和停止生成。
- 支持 Docker + Nginx 部署。

## 默认后端地址

| 后端 | 默认地址 |
|------|----------|
| Python | `http://localhost:8000` |
| Java | `http://localhost:8080` |

开发模式下，Vite 会代理：

| 前端路径 | 代理到 |
|----------|--------|
| `/api/python` | `http://localhost:8000` |
| `/api/java` | `http://localhost:8080` |

Docker 全栈模式下，前端使用 `/api/python`，由 Nginx 代理到同一 Docker 网络中的 Python 后端。统一入口和前端独立端口都支持这个路径。页面仅启用 Python，不显示 Java 切换按钮。

## 本地运行

安装依赖：

```bash
npm install
```

启动：

```bash
npm run dev
```

访问：

```text
http://localhost:5173
```

如果后端端口不是默认值，可以启动时覆盖：

```bash
VITE_PYTHON_API_URL=http://localhost:8000 \
VITE_JAVA_API_URL=http://localhost:8080 \
npm run dev
```

## Docker 部署

全栈包含前端、Python 后端、Redis、ChromaDB、Nginx 网关和 Prometheus。所有启动命令都在 `EchoMindFrontend` 目录执行。`EchoMindJava` 是独立的替代实现，不需要参与 Python 版的构建或运行。

### 环境配置

环境配置分别放在：

- Python：`../EchoMind/.env`，需要有效的 `ANTHROPIC_API_KEY`。
- 前端目录的 `.env`：仅保存 Redis 共享密码和宿主机端口，不需要复制 API 密钥。

首次配置时，如果前端目录还没有 `.env`，复制模板：

```powershell
Copy-Item .env.example .env
```

把其中 `REDIS_PASSWORD` 设置为现有 Python 后端使用的密码。Compose 会把这个共享密码应用到 Redis 和 Python 后端，容器间的数据库地址也由 Compose 自动设置。

### 首次启动或更新代码

```powershell
cd D:\EchoMind\EchoMindFrontend
docker compose --env-file ../EchoMind/.env --env-file .env up -d --build
docker compose ps
```

前提是 `EchoMindFrontend` 的父目录下存在 `EchoMind`；不需要 `EchoMindJava` 目录或 Java 环境。

构建时，前端使用 `npm ci` 和 `npm run build` 生成静态文件，再由 Nginx 提供页面。Python 后端按自己的 Dockerfile 构建；无需先在宿主机执行 `npm install`。

### 从已经运行的 Python 后端切换

全栈项目名固定为 `echomind`，Python 服务名称仍为 `echomind`，沿用 `echomind-app`、`echomind-redis`、`echomind-chromadb` 等现有容器名称。与从 `../EchoMind` 默认启动的部署属于同一个 Compose 项目。

直接执行上面的全栈启动命令即可，不需要先执行 `docker compose down`。Compose 会更新配置有变化的容器，加入前端，并将原来的 Nginx 入口改为前端页面。配置更新可能让相关服务短暂重启。

Redis、ChromaDB、Prometheus 和 Nginx 日志沿用原项目的数据卷；Python 的本地数据、Skills 和日志目录也沿用原来的路径。

此方式适用于原 Python 部署使用默认项目名 `echomind` 的情况；如果原来显式使用了 `-p` 或 `COMPOSE_PROJECT_NAME`，需要先对齐项目名。切换后统一从前端目录管理全栈，避免交替使用两份 Compose 配置更新同一套服务。

### 日常启动与停止

代码没有变化时：

```bash
docker compose --env-file ../EchoMind/.env --env-file .env up -d
```

停止全部服务并保留容器与数据：

```bash
docker compose stop
```

停止并移除容器、保留数据卷：

```bash
docker compose down
```

不要给 `down` 添加 `-v`，除非需要清空数据卷。

### 访问与排查

| 用途 | 地址 |
|------|------|
| 前端统一入口 | `http://localhost` |
| 前端独立端口 | `http://localhost:5174` |
| Python 接口文档 | `http://localhost:8000/docs` |
| Prometheus | `http://localhost:9090` |

可在前端 `.env` 中修改 `FRONTEND_PORT`（默认 `5174`）和 `GATEWAY_PORT`（默认 `80`）。页面顶部显示 Python，点击“检查连接”可确认后端是否正常。

查看日志：

```bash
docker compose logs --tail 100 echomind
docker compose logs --tail 100 frontend nginx
```

### 实时聊天

Python 前端通过 `POST /chat/stream` 接收 SSE 事件：会话标识、实际处理阶段、工具开始/结束、回复文字和最终结果。多个专业 Agent 协作时，页面展示汇总回复，避免把各个候选答案交错在一起。完成后仍保留工具追踪信息。

Docker 的两层 Nginx 已关闭 API 代理缓冲，确保收到文字后及时传到浏览器。修改后端或前端流式代码后，使用上文的双环境文件 `up -d --build` 命令应用更新；如果同时修改了网关配置，再执行 `docker compose restart nginx`，让运行中的网关重新读取配置。页面遇到尚未提供流式接口的旧后端时，会显示提示并使用原聊天接口。

## 后端启动参考

Python 版默认：

```text
http://localhost:8000
```

Java 版是可选的独立实现。只使用 Python 时无需配置、构建或启动它。

## Python 功能改进（2026-10-21）

本轮保留原生 Python 编排，新增登录、工单草稿核对与确认、工单状态/审计、知识文档更新与删除、实际检索来源、历史评测报告与显式回归基线。

运行前先按 [Python 实施说明](../EchoMind/wiki/Python纯链路实施与验收.md)开通账号。启动时加载 Python 与前端两个环境文件，使 Redis、认证配置和端口来源明确。默认使用 HttpOnly Cookie；生产 HTTPS 需配置服务端 Cookie 与允许的 Origin。Compose 已挂载业务 SQLite 数据目录。

验证命令：`node --test tests/*.test.js`、`npm run build`。本地页面测试使用独立临时业务库及模拟模型，不能作为真实模型效果或云部署证明。
