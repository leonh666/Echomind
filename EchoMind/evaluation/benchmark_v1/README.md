# EchoMind 评测集 v1：数据、运行与指标

当前版本为 **echomind-benchmark-v1.1**。包含 **36 篇原创虚构知识文档、276 条候选测试用例**，覆盖客服分类、知识问答和当前原生 Python 工单/权限验收规格，无需第三方库即可验证结构和运行检索基线。

全部资料、业务规则和样本均为 synthetic_original，所有用例的 human_review 为 pending。它们不是真实客户日志，也不是独立人工核验完成的金标准。当前原始候选集共 144 个来源事实；题目按需引用这些事实。

2026-10-22 已完成一次覆盖全部资料与用例的 **AI 场景复核**，修正前提、证据、条件答案判据和契约范围，详见 [SCENARIO_REVIEW.md](SCENARIO_REVIEW.md) 与 [312 条复核记录](reviews/scenario_review_v1_1.jsonl)。人工核验数仍为 0。资料里的 SupportLab 是虚构被支持产品，不能据此声称 EchoMind 已实现账号锁定、PDF、Webhook 或支付能力。

调研依据与指标公式见[测试数据集构建与评测指标](../../wiki/测试数据集构建与评测指标.md)，回答复核方法见 [JUDGE_RUBRIC.md](JUDGE_RUBRIC.md)。
意图边界与当前单 Agent 路由范围见 [LABEL_GUIDE.md](LABEL_GUIDE.md)。

## 1. 实际包含什么

| 数据 | 文件 | 数量 | dev / test |
|---|---|---:|---:|
| 知识文档 | [corpus.jsonl](data/corpus.jsonl) | 36 | 12 / 24 |
| 意图与路由 | [intent_cases.jsonl](data/intent_cases.jsonl) | 120，覆盖现有 19 类意图 | 40 / 80 |
| RAG 与行为 | [rag_cases.jsonl](data/rag_cases.jsonl) | 108 | 36 / 72 |
| 原生工单与权限契约（尚未执行） | [contract_cases.jsonl](data/contract_cases.jsonl) | 48 | 16 / 32 |
| 全部测试题 | 上述三类 case 文件 | 276 | 92 / 184 |

RAG 题型为单文档 48 题，以及多文档、多轮、无答案、版本比较、缺条件澄清各 12 题。行为标签为 answer 84、refuse 12、clarify 12。工单契约分 workflow 24、security 24，目前只是具有预期断言的验收规格，尚未执行。

数据按来源主题或语义家族 group_id 划分，同组改写不跨 dev/test。RAG 的 test 文档可以进入检索索引；测试问题、答案标签和 qrels 不得进入索引。测试文件在仓库中可见，因此这是冻结回归集，不是盲测基准。

意图题中 113 条为 support_intent_classification，7 条物流题为 legacy_taxonomy_compatibility。它们可以分别运行和报告，不能把物流兼容能力当成当前软件产品业务覆盖率。

## 2. 最快运行方法

使用 Python 3.10+，在项目根目录运行。以下步骤不读取应用 .env，不启动业务服务，也不调用模型：

```powershell
Set-Location D:/EchoMind/EchoMind
python evaluation/benchmark_v1/validate.py
python -m unittest discover -s evaluation/benchmark_v1/tests -v
python evaluation/benchmark_v1/run_benchmark.py baseline --split dev
```

默认输出文件名包含随机 run_id，终端会给出预测与指标报告路径。重复运行保留旧报告；显式指定已存在路径时默认拒绝覆盖。
数据和脚本固定使用 LF 换行，并在本目录设置 Git 属性，避免 Windows 与其他系统检出后 manifest 哈希失配。

正式比较之前用 dev 调整，冻结配置后再评 test：

```powershell
python evaluation/benchmark_v1/run_benchmark.py baseline --split test
```

只验证连通性可使用 --limit，但未运行题会保留为 skipped 并降低覆盖率，不会从分母消失：

```powershell
python evaluation/benchmark_v1/run_benchmark.py baseline --split dev --limit 3
```

## 3. 已验证结果与限制

本包已经做了结构、引用、分组、重复项和指标逻辑验证，并运行了纯本地 BM25 基线。历史 v1.0 记录保留在 [VERIFICATION.md](VERIFICATION.md)；v1.1 固定快照为：

- [v1.1 dev 检索指标](reports/bm25_v1_1_dev.metrics.json)
- [v1.1 test 检索指标](reports/bm25_v1_1_test.metrics.json)

BM25 采用中文二元切分、英文和数字 token，k1=1.5、b=0.75，以与当前后端一致的约 500 字句子分块作为排名单元。v1.1 文档最长 388 字，因此此版本是一文一片。查询只使用当前问题和最近两条用户历史，不使用参考事实或金标准改写。

v1.1 test 的 56 道有答案题上，BM25 Hit@1 为 85.71%，Hit@5 与 Recall@5 均为 100%。这表明 **36 篇短文语料上的前五项召回很容易饱和**。题目与资料已修订，不能将它与 v1.0 的差值解释为算法提升或退化；这些分数不能代表模型回答质量或真实生产表现。

本轮未调用当前 EchoMind API 或任何付费模型，没有测出实际回答正确性、Faithfulness、引用质量、延迟 SLA、费用或未来工单通过率。报告中相关 observed 指标为 null 或覆盖率为 0，不把它们解释为“已通过”。

## 4. 文件职责

| 文件 | 职责 |
|---|---|
| build_corpus.py | 生成原创虚构资料与 RAG 候选题 |
| build_cases.py | 生成意图候选题与尚未执行的原生业务契约 |
| scenario_review.py | 可重复的 v1.1 场景修正规则、前提与终态判据 |
| reviews/、review_evidence.py | 独立保存逐条 AI 复核，并检查记录是否对应当前内容 |
| build_all.py | 顺序重建、导出并校验，更新 manifest |
| validate.py | 检查来源事实、标签、分组、重复项和文件哈希 |
| manifest.json | 数据版本、计数、SHA-256 和能力边界 |
| export.py | 导出仅含正文的导入包、旧 API 适配体和待评测模板 |
| run_benchmark.py | 本地 BM25 或显式调用当前 API |
| metrics.py | 确定性指标，严格区分缺测、错误与成功 |
| JUDGE_RUBRIC.md | 行为、事实、Faithfulness、引用与业务断言核验 |
| tests/ | 指标边界与适配器验证 |
| reports/ | 本地合成基线与结构校验报告 |

如需从生成器完整重建：

```powershell
python evaluation/benchmark_v1/build_all.py
```

生成器会重写其负责的数据、导出文件与 manifest。独立人工复核和实际运行结果放在单独的 annotation / reports 文件，不要直接修改生成文件后又运行重建。

## 5. 接入当前 EchoMind

### 5.1 准备独立测试实例

准备可丢弃或独立的测试实例与数据目录。当前 /knowledge/add 会真实写入知识库，/chat 会写入会话和记忆；不要把虚构政策导入业务使用的知识库。

核对测试实例中的候选资料。当前应用在空库时自动加载默认文档，导入这 36 篇不会自动删除默认或已有文档。进行方法对比时必须保证候选库一致，或明确记录背景文档及索引版本差异。本包不执行数据库清空或自动迁移。

当前接口优先导入 [knowledge_add_versioned.json](exports/knowledge_add_versioned.json)，保留 document_id、product_version 和测试知识库标识，不含问题、参考事实、dev/test 标签或组织身份声明。旧接口可继续使用仅含 title / content 的 [knowledge_add.json](exports/knowledge_add.json)：

```powershell
$evalBaseUrl = 'http://127.0.0.1:8000'
$evalHeaders = @{ Authorization = "Bearer $env:ECHOMIND_EVAL_BEARER" }
$fixtureBody = Get-Content evaluation/benchmark_v1/exports/knowledge_add_versioned.json -Raw -Encoding UTF8
Invoke-RestMethod -Uri "$evalBaseUrl/knowledge/add" -Method Post -Headers $evalHeaders -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($fixtureBody))
```

ECHOMIND_EVAL_BEARER 应是独立测试组织 admin/kb_admin 的登录凭据，不是模型 API Key。以上是供你操作测试实例的命令，本次交付没有执行它；聊天业务任务应使用相应客户测试身份，不能为方便导入而绕过鉴权。

### 5.2 运行检索接口

当前 /search 是 POST，query 与 top_k 位于 URL 查询参数，适配器已按当前实现处理：

```powershell
python evaluation/benchmark_v1/run_benchmark.py api --mode retrieval --split dev --base-url http://127.0.0.1:8000 --limit 5
```

该接口会调用现有查询改写或重排模型，可能产生费用。适配器不自动重试、不自动导入知识，不加载应用密钥。需要 Bearer 时从 ECHOMIND_EVAL_BEARER 环境变量读取，令牌不写入报告。

返回结果优先采用语料已知 document_id；应用生成的其他文档 ID 再按唯一标题映射回语料 ID。重复文档保留原始片段位置；无法映射的候选也保留，按不相关结果计入排名并记录 unmapped_candidates。候选带 fallback=True 或明确失败时记 error，不能算普通未命中。新版 API 公开 rerank_status，可区分跳过、成功和失败；不要只根据旧版 reranked 字段推断执行情况。

### 5.3 运行对话接口

```powershell
python evaluation/benchmark_v1/run_benchmark.py api --mode chat --split dev --base-url http://127.0.0.1:8000 --limit 5
```

仅评软件客服主分类场景时，增加 `--track support_intent_classification`；旧物流分类可单独用 `--track legacy_taxonomy_compatibility`。检索模式可用 `--track reference_product_knowledge`。默认 all 保留旧行为，但报告会记录 selected_track，不应混称真实业务流量分布。

chat 模式选取当前 split 的意图题和 RAG 题。每题使用独立会话；同一题的多轮只重放用户发言，让应用实际生成中间回答。历史中的候选参考回复不会伪装成模型输出。

2026-10-21 的 Python 版本已启用服务端认证，同一 Bearer 下正文 user_id 会被覆盖。不同会话仍可能共享该账号的画像。请使用隔离的测试主体或受控的记忆清理机制；不能把合成 user_id 当成账号隔离证据。内置 evaluator 不读取真实用户记忆，可用于另一个明确区分的实验协议。

当前 /chat 可自动记录 intent、agents 和实际 response；新版还提供 sources、tasks 和 warnings。适配器目前仍不从这些字段自动判定 behavior、fact_checks、Faithfulness、引用支持或业务成功。这些按复核规范补充。

默认 api_* 报告被本包 .gitignore 忽略；自定义输出路径仍需自行确认脱敏和版本管理范围。

### 5.4 旧 /eval/run 的有限兼容

[legacy_intent_dev.json](exports/legacy_intent_dev.json) 和 [legacy_intent_test.json](exports/legacy_intent_test.json) 分别包含 38 / 76 条单轮意图题，dialog_cases 显式置空。

保留原先只含单轮题的导出以兼容旧版本。新版 IntentEvaluator 已将 context.history 传给 recognizer，Judge 可接收参考事实和实际证据，评测报告不再自动覆盖基线。本包导出未因此伪造新的测得分数；逐主张 Faithfulness、qrels 检索指标仍应按本包协议单独评测。

旧 API 返回可用于检查旧版意图表现；新包指标请使用下述预测协议。未自动调用 /eval/run。

## 6. 预测文件与评分

预测 JSONL 每行至少含 case_id 和 status。status 允许 ok / error / skipped；缺少字段表示未测，不能用 gold 填充预测。

```json
{
  "case_id": "intent-001",
  "status": "ok",
  "intent": "query",
  "agents": ["general"],
  "latency_ms": 120.5
}
```

RAG 可提供 retrieved_document_ids、behavior、fact_checks、faithfulness_checks、citation_checks；契约可提供 assertions。v1.1 的 12 条版本澄清题还允许经 behavior_checks 核验的条件式回答，分数见 behavior.acceptable_response；严格行为分类仍单独保留。缺少替代答案核验不能自动给分，详见 [复核说明](SCENARIO_REVIEW.md)。

只评检索预测时，使用 RAG case 文件：

```powershell
python evaluation/benchmark_v1/metrics.py --cases evaluation/benchmark_v1/data/rag_cases.jsonl --predictions evaluation/benchmark_v1/reports/bm25_v1_1_test.predictions.jsonl --split test --output evaluation/benchmark_v1/reports/retrieval_recheck_v1_1.json
```

评完整预测时可提供多个 case 文件：

```powershell
python evaluation/benchmark_v1/metrics.py --cases evaluation/benchmark_v1/data/intent_cases.jsonl evaluation/benchmark_v1/data/rag_cases.jsonl evaluation/benchmark_v1/data/contract_cases.jsonl --predictions evaluation/benchmark_v1/exports/pending_predictions.jsonl --split all --output evaluation/benchmark_v1/reports/pending_coverage_v1_1.json
```

最后一条命令只演示“全未运行”的覆盖率报告，不是实际系统测试。不要把待评测模板中的 skipped 改成 ok，却没有执行证据。

复算时必须匹配题库版本与预测报告中的 case/corpus 哈希；旧 v1.0 预测不能直接搭配 v1.1 标签重算并冒充可比实验。

严格指标把缺失、错误和跳过纳入所选样本分母；observed 只描述实际测得字段。阅读二者时同时查看 coverage、样本数与错误数。未来 contract 单列，不与当前意图或 RAG 混算一个总分。

## 7. 建议重点看哪些指标

| 层次 | 首要指标 | 必须一起看 |
|---|---|---|
| 意图 | Accuracy、Macro-F1 | 每类 support、混淆和覆盖率 |
| 路由 | Agent 集合完全匹配率 | primary 与辅助需求的标签约定 |
| 检索 | Hit@1/5、Recall@5、MRR@5、nDCG@5 | K 的排名单位、答案题分母、相同语料 |
| 回答 | 参考事实覆盖、Faithfulness | 人工/Judge 核验覆盖率和失败数 |
| 引用 | 引用覆盖率、引用支持率 | 所需主张数、已核验主张数 |
| 边界行为 | 无答案正确处理率、澄清正确率 | 可回答问题误拒答/误澄清率 |
| 工单 | 全部业务断言通过率、重复写入 | 数据库/API/审计证据 |
| 运行 | 错误率、p50/p95、已知费用小计 | 测试条件、样本数、usage 与费用覆盖率 |

自动评分不会生成任意加权综合分。BLEU、ROUGE、整段字符串完全匹配不作为客服开放回答的主要质量结论。

2026-10-22 的分层评测、人工复核、执行器与失败回流建议见 [Agent 评测体系改进建议](../../wiki/Agent评测体系改进建议.md)。规划项尚未实现，不代表已取得新的模型效果分数。

## 8. 如何扩充成更可信的项目证据

1. 先逐组复核当前虚构规则与问题标签，保存复核人、时间、分歧和修订版本。
2. 用允许使用的真实产品资料建立另一套版本化 corpus；保留来源、适用版本、更新时间和许可。
3. 从问题主题、故障或源资料家族划分 dev/test，同一问题的翻译和改写保持同组。
4. 每题标注最小支持事实、相关文档与行为，加入同词异义、版本冲突、近似配置项等干扰。
5. 用真实失败案例补充问题，保持独立最终测试集，避免反复按测试答案调 Prompt。
6. 当前已有 SQLite 工单与权限业务实现及工程测试；仍需将本包 contract 接入通用执行器并观察真实 Agent 业务终态，不能用工程测试通过数代替模型任务成功率。
7. 只在相同语料、分块、模型与调用预算下比较检索、改写、重排方案。

公开基准可作为独立辅助实验。CLINC150、BANKING77、MASSIVE 的原标签不直接等于 EchoMind 标签；BEIR 的工具许可不覆盖所有子数据集；Ragas 是评测框架；τ-bench 的任务也不能直接当静态客服问答导入。详见调研说明中的官方来源。
