# 评测包验证记录

验证日期：2026-10-21。环境：Windows / Python 3.12.7。数据版本：echomind-benchmark-v1.0。

本记录验证的是合成数据包和离线工具；没有调用当前 EchoMind 服务、模型 API 或未来工单业务后端。

## 1. 已完成的检查

- 36 篇资料、144 个来源事实、276 条候选测试用例。
- 120 条意图、108 条 RAG、24 条工单契约、24 条权限契约。
- dev 92 条、test 184 条；同一来源或语义 group 不跨集合。
- 文档 ID、标题、事实 ID、case ID 唯一；事实逐字存在于对应源文档。
- RAG qrels、事实与来源组关联有效；无答案与澄清题不含假造的答案证据。
- 19 类意图均来自现有 IntentCategory；未发现与当前 Prompt 示例完全相同的题目。
- 仅正文与标题进入知识导入包，问题和答案标签没有额外导入。
- manifest 文件哈希一致；[结构校验报告](reports/validation.json)为 valid=true。
- **44 项 unittest 全部通过**，包括缺测、错误、重复排名、指标分母、事实核验、Faithfulness、费用未知、API 适配边界与跨平台文件哈希稳定性。

抽查和自动检查不能代表独立人工复核，全部 human_review 继续保持 pending。跨集合语义近重复需要后续人工核验。

## 2. 已运行的离线基线

配置：仅标准库 BM25，k1=1.5、b=0.75；中文二元切分；以约 500 字句子片段排名、按源文档相关性计分。

该 corpus 最长 357 字，共 36 个索引片段，因此当前是一文一片。多轮查询采用最近两条用户发言拼接当前问题，不使用参考答案。

| 集合 | 已执行 RAG 题 | 有答案检索分母 | Hit@1 | Hit@5 | Recall@5 | MRR@5 | nDCG@5 |
|---|---:|---:|---:|---:|---:|---:|---:|
| dev | 36 | 28 | 0.8929 | 1.0000 | 1.0000 | 0.9315 | 0.9486 |
| test | 72 | 56 | 0.8929 | 1.0000 | 1.0000 | 0.9435 | 0.9542 |

两组均未发生离线执行错误。无答案和澄清题也执行了检索，但不进入上述有答案检索指标的分母。

- [dev 原始预测](reports/bm25_v1_dev.predictions.jsonl)
- [dev 指标与配置](reports/bm25_v1_dev.metrics.json)
- [test 原始预测](reports/bm25_v1_test.predictions.jsonl)
- [test 指标与配置](reports/bm25_v1_test.metrics.json)

这些结果不是 EchoMind 当前检索链路的成绩，也不是“加入重排后的提升”。小规模、主题清晰的虚构资料使 Hit@5 很容易饱和；后续需要更大的真实来源资料、困难负例和独立人工核验。

## 3. 尚未测量的部分

| 部分 | 状态 |
|---|---|
| 当前模型意图与路由 | 未调用应用 API |
| 回答行为、参考事实覆盖 | 未生成实际模型回答、未完成复核 |
| Faithfulness 与引用 | 未取得实际模型检索上下文并逐项核验 |
| 工单、权限与故障恢复 | 48 条仅是未来验收规格 |
| 应用 p95、并发能力与费用 | 未做线上或模型调用测试 |
| 独立人工金标准 | 0 条，全部候选标签仍待复核 |

离线 BM25 报告的行为 coverage 为 0，Faithfulness 支持率为 null。不能把 strict 字段中的 0 解读为已测模型质量为 0，也不能把未测项目算作通过。

## 4. 复现命令

在 D:/EchoMind/EchoMind 执行：

```powershell
python evaluation/benchmark_v1/validate.py
python -m unittest discover -s evaluation/benchmark_v1/tests -v
python evaluation/benchmark_v1/run_benchmark.py baseline --split dev
python evaluation/benchmark_v1/run_benchmark.py baseline --split test
```

新运行默认生成包含 run_id 的新文件，不替换上方固定快照。API 执行方法、费用和隔离约定见 [README](README.md)。
