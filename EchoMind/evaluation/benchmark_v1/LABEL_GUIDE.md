# 意图候选标签与人工复核约定

本文件解释当前 120 条意图候选样本的标注逻辑，不表示现有模型已经达到这些行为。

## 1. 标签边界

| 标签 | 当前候选集的含义 |
|---|---|
| query | 一般服务信息、帮助入口、支持时段等查询，未命中更具体业务类别 |
| complaint | 表达服务不满或投诉，未明确要求真人接待或启动升级流程 |
| request | 一般操作请求，例如预约、改期或取消预约，未命中更具体领域 |
| greeting | 单纯问候 |
| escalation | 明确请求升级流程、主管级别处理；与“直接找真人”分开 |
| technical | 一般配置、性能、监控等技术问题，未命中登录或崩溃子类 |
| billing | 一般计费周期、用量、套餐费用解释 |
| account | 常规资料、联系人、通知偏好管理 |
| feedback | 明确正向评价或感谢 |
| order_status | 业务订单处理状态；不等同承运商物流 |
| logistics | 交承运方以后的轨迹、配送时效和查件 |
| refund | 退货、退款政策、申请与到账流程 |
| invoice | 开票、抬头、税务字段和发票下载 |
| payment_issue | 重复扣款、支付失败、付款与订单状态不一致 |
| account_security | 盗号、安全联系方式被改和密码重设 |
| technical_login | 登录、认证、验证码和会话登录故障 |
| technical_crash | 闪退、进程崩溃、500 等服务错误 |
| human_handoff | 明确希望真人客服接手 |
| other | 相对于当前客服范围的域外请求 |

优先使用确实符合的具体类别；不得因所有句子都在“询问信息”就统一标 query。

## 2. 冲突与上下文

复核时同时看历史与当前任务。密码重设在本候选集归安全管理；认证故障归登录。二者重叠或用户同时提出多个要求时，记录分歧，扩展为复合意图或修改样本，不能无理由强压单标签。

escalation 与 human_handoff、query 与具体领域之间的边界在现有项目中存在重叠。这里的标签约定用于把问题暴露出来，后续可以重构 taxonomy，但需同时更新题库版本与模型分类定义，再做可比回归。

## 3. 当前路由范围

120 条均只要求一个主要 Agent。按现有业务类别标为 general、technical、billing 或 escalation，exact-set 指标检查该单元素集合。

v1.1 复核保留账号及账号安全题的 general 业务预期，并记录 13 条 known_implementation_gap：当前实现偏向路由到 billing，与通用账号指导职责不匹配。不要为迎合实现修改 gold。涉及实际敏感变更仍需身份核验或人工处理。intent-097 的“立刻闪退”描述故障发生时序，不是 critical 的充分证据；新增 urgency 标注仅供诊断，当前尚不自动统计该指标。

7 条物流题保留为 legacy_taxonomy_compatibility，其余 113 条为 support_intent_classification。可用 runner 的 --track 分开运行；默认 all 不代表真实流量分布。升级意图表达已改为更自然的主管/二线/工单升级请求，避免把单纯要求真人与升级层级混为一谈。

此版本没有足够的复合意图与多个辅助 Agent gold，不能用单 Agent 路由通过率声称已经验证多 Agent 协作。

## 4. 尚需扩展

- 复合意图、primary/supporting Agents 与部分正确的诊断指标。
- urgency 与实体槽位标注及对应 Precision/Recall/F1。
- 语义近重复、自然口误、噪声与真实口语分布。
- 独立人工复核与争议样本处理。

其他回答与业务结果的标注见 [JUDGE_RUBRIC.md](JUDGE_RUBRIC.md)。
