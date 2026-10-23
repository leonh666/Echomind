# EchoMind

基于原生 Python 链路的客服 Agent 项目，包含 FastAPI 后端、Vue 工作台、知识检索、分层记忆、持久化工单和评测集。

本版本保持 Python Orchestrator + asyncio + 工具函数，不依赖 LangGraph、Dify 或 Coze。适合学习、二次开发和展示可验证的 Agent 业务流程。

## 功能

- 意图识别、多 Agent 路由、动态 Skills 与流式回复。
- 向量、BM25、RRF 混合检索，支持产品版本与知识库筛选，返回实际来源片段。
- 知识文档导入、更新替换、删除、缓存失效与发布失败隔离。
- SQLite 工单草稿、版本绑定确认、幂等提交、处理状态和审计。
- 管理员后台与个人工作区分离：管理员查看本组织用户、审计、会话索引、任务和工单，普通用户只访问自己的资料、对话与工单；后台接口独立校验角色。
- 回答来源快照、采纳/修改/弃用反馈、独立解决结果与反馈版本检查；普通用户回看本人咨询，管理员生成本组织诊断报告并导出 Markdown。
- 登录、角色权限和组织范围校验；模型只能生成草稿，提交由用户独立确认。
- 独立评测报告、失败覆盖率和显式回归基线；附带可复现的中文合成评测包。

## 目录

```text
EchoMind/             Python 后端、业务模块、测试、评测包与文档
EchoMindFrontend/     Vue 前端、Nginx 与整套 Docker Compose
THIRD_PARTY_NOTICES.md 原始仓库来源说明
```

## 启动

需要 Docker / Compose，以及兼容 Anthropic 协议的模型服务。先分别复制配置模板：

```bash
cp EchoMind/.env.example EchoMind/.env
cp EchoMindFrontend/.env.example EchoMindFrontend/.env
```

填写 Python 配置中的模型地址、模型标识和密钥；将两个配置文件的 Redis 密码设为相同的自定义值。真实 `.env`、业务数据库与运行日志不在此公开仓库内。

首次启动前，在仓库根目录生成仅用于监控抓取的本地随机凭据文件。命令不输出凭据，文件已存在时会拒绝覆盖；`.secrets/` 已被 Git 忽略：

```bash
python -c "from pathlib import Path; import secrets; p=Path('EchoMind/.secrets/metrics_token'); p.parent.mkdir(exist_ok=True); f=p.open('x', encoding='utf-8'); f.write(secrets.token_urlsafe(48)+'\n'); f.close()"
```

Redis、Chroma 和 Prometheus 的宿主机端口默认只绑定 `127.0.0.1`，供服务器本机维护；容器之间仍通过内部网络连接。远程管理使用 SSH 隧道，普通用户通过应用登录访问数据。

```bash
cd EchoMindFrontend
docker compose --env-file ../EchoMind/.env --env-file .env up -d --build
docker compose exec echomind python -m business.auth --db /app/data/business/echomind.sqlite3 create-user --email admin@example.invalid --organization-name Demo
```

创建管理员时在终端输入密码，然后访问 `http://localhost:5174` 登录。普通用户可点击“立即注册”，使用用户名、密码、确认密码创建账号。旧邮箱账号自动补充唯一用户名，原密码不变。注册组织设置及兼容方式见 [用户名登录与注册](EchoMind/wiki/用户名登录与注册.md)。管理员与普通用户的权限、同组织账号创建及监控配置见 [管理员与用户权限说明](EchoMind/wiki/管理员与用户权限说明.md)；其他启动和备份步骤见 [Python 实施与验收](EchoMind/wiki/Python纯链路实施与验收.md)。

## 验证与数据边界

2026-10-23 最新本地回归：Python 应用和评测包 **440 项测试通过**（含人工升级规则、用户名注册、旧账号迁移、权限隔离、数据 v1.1 修订与反馈/诊断），前端 **47 项测试通过**，Vite 生产构建通过。使用合成账号、假模型与独立 SQLite 库，完成连接检查、服务数据刷新、人工建议与待确认草稿、注册、用户名登录及管理员分页、普通用户隔离、来源与反馈、退出清理和诊断报告的页面检查。权限首轮条件见 [验收记录](EchoMind/docs/admin-permissions-verification.md)，新增功能见 [证据反馈与诊断报告](EchoMind/wiki/证据反馈与诊断报告.md) 和 [咨询状态与服务检查](EchoMind/wiki/咨询状态与服务检查说明.md)。

```bash
cd EchoMind
python -m pip install -r requirements.txt
python -m pip install pytest
python -m pytest tests evaluation/benchmark_v1/tests -q -p no:cacheprovider
```

```bash
cd EchoMindFrontend
npm ci
node --test tests/*.test.js
npm run build
```

自动测试使用模型/Chroma 测试替身和真实 SQLite 文件，不代表真实模型效果或线上吞吐量。本轮没有完成云端部署、Docker 重建、真实依赖压测或付费模型评测。

`evaluation/benchmark_v1` 当前版本为 v1.1，包含 36 篇合成资料、276 条候选用例；已作 AI 场景复核并保存 312 条逐项记录，全部仍待独立人工复核，未自动导入运行知识库。资料描述虚构被支持产品 SupportLab，不是 EchoMind 功能清单；旧文档中未验证的目标仍属规划。

## 文档

- [Python 实施、接口、启动与验收](EchoMind/wiki/Python纯链路实施与验收.md)
- [管理员与用户权限说明](EchoMind/wiki/管理员与用户权限说明.md)
- [证据反馈与客服诊断报告](EchoMind/wiki/证据反馈与诊断报告.md)
- [验证记录](EchoMind/docs/native-python-verification.md)
- [项目改进说明书](EchoMind/wiki/项目改进说明书.md)
- [测试数据集与评测指标](EchoMind/wiki/测试数据集构建与评测指标.md)
- [Agent 评测体系审查与改进优先级](EchoMind/wiki/Agent评测体系改进建议.md)
- [评测包使用说明](EchoMind/evaluation/benchmark_v1/README.md)
- [评测集 v1.1 场景复核与修正](EchoMind/evaluation/benchmark_v1/SCENARIO_REVIEW.md)
- [前端说明](EchoMindFrontend/README.md)

## 来源

本仓库是当前工作区的清理后快照，包含在原项目基础上进行的功能改进。原始来源见 [来源说明](THIRD_PARTY_NOTICES.md)。未复制本地旧 Git 历史、IDE 状态、运行数据库、依赖目录或个人求职资料。
