# 场景复核证据

2026-10-22 已对 36 篇合成资料及 276 条候选用例作 AI 场景复核。逐条检查问题、来源规则、版本与必要前提、预期行为、角色和当前业务能力边界。

- [复核说明与具体修改](../SCENARIO_REVIEW.md)
- [逐条记录](scenario_review_v1_1.jsonl)：312 条，含修改前后记录指纹、理由、问题分类及旧版本提交号。
- [汇总](scenario_review_v1_1.summary.json)
- [v1.0 历史 manifest](v1_0_manifest.json)：用于识别旧报告及旧数据哈希，非当前版本。

这属于 AI 复核，不是独立业务专家或真人客户审核。`human_review=pending` 和 `human_verified_cases=0` 保留；没有调用付费模型，没有生成实际 Agent 的任务完成分数。

生成器中可重复的修正规则位于 [scenario_review.py](../scenario_review.py)。`build_all.py` 会重建数据和导出包，**不会重写这里的复核证据**。验证器检查记录 ID、覆盖范围和修改后的 SHA-256；后续改题后旧复核会变成 stale，需要重新复核并发布新记录，不能仅重算哈希冒充已审核。

v1.0 的固定 BM25 报告保持原样，v1.1 单独运行并保存。题目、必要事实和语料变化后的分数差异不是检索系统优化效果。
