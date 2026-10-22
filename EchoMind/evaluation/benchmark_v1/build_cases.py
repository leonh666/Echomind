"""Build synthetic intent and unexecuted native Python acceptance cases, without an LLM."""
from common import DATA, write_jsonl
from scenario_review import contract_cases as build_contract_cases, revise_intents

# Each pair is one semantic family. The complete family remains in one split.
# Labels follow core.intent_recognizer.IntentCategory; they are candidate gold,
# pending independent human review, not empirical production intent statistics.
INTENTS = [
    ("query", "general", [
        ("你们的在线支持窗口每天几点开放？", "周末能在什么时段联系在线支持？"),
        ("请说明帮助中心可以用哪些语言阅读。", "我想了解说明文档是否提供简体中文。"),
        ("在哪里可以看到服务规则的最新公告？", "服务说明更新以后会通过哪个页面公布？"),
    ]),
    ("complaint", "general", [
        ("等待处理的时间远超你们承诺，我对此很失望。", "你们总让我继续等，体验实在太糟糕了。"),
        ("每次都让我重复说明问题，服务让我很不满意。", "同一件事解释了好几遍，客服还是没弄清楚。"),
        ("回复态度很敷衍，我对这次服务提出投诉。", "这次接待毫无耐心，我要反映服务态度问题。"),
    ]),
    ("request", "general", [
        ("请替我预约周四下午的产品演示。", "帮我约一个工作日上午的使用培训。"),
        ("请把下次使用指导改到下周一。", "我预约的演示时间要改，请协助调整。"),
        ("请撤回刚才提交的产品体验预约。", "我不参加这次培训了，请取消预约。"),
    ]),
    ("greeting", "general", [
        ("下午好，客服同学。", "晚上好，打个招呼。"),
        ("您好，刚刚进入这个对话窗口。", "哈喽，先来问声好。"),
        ("客服朋友，上午好呀。", "见到您很高兴，先问好。"),
    ]),
    ("escalation", "escalation", [
        ("请把这次事件按二级升级流程处理。", "这个事项需要启动上一级处理流程。"),
        ("现有支持层级无法处理，请升级到主管级别。", "这件事需要提交到更高级别的支持流程。"),
        ("请对本事件启动升级审查程序。", "我申请按升级机制重新审查本问题。"),
    ]),
    ("technical", "technical", [
        ("容器的内存占用持续升高，怎样定位原因？", "服务的内存曲线一直上涨，该从哪项配置排查？"),
        ("接口延迟突然增加，但没有错误码，怎么诊断？", "请求响应很慢且没有报错，应该检查哪些环节？"),
        ("怎样给这个服务接入指标采集？", "我需要了解监控端点的配置步骤。"),
    ]),
    ("billing", "billing", [
        ("套餐的计费周期从开通当天还是自然月开始？", "订阅费用按自然月结算还是按开通日期滚动？"),
        ("请解释账单上的服务用量这一栏。", "账单里的计量单位代表什么含义？"),
        ("年度套餐和月度套餐怎样计算费用？", "改成按年计费以后收费规则是什么？"),
    ]),
    ("account", "general", [
        ("我想更新企业联系人姓名。", "组织联系人换了，需要修改资料里的姓名。"),
        ("个人资料里的展示名称在哪里编辑？", "我要变更账户页面显示的昵称。"),
        ("请说明如何调整账号的通知偏好。", "我想修改账户接收产品通知的设置。"),
    ]),
    ("feedback", "general", [
        ("你的说明很清楚，这次帮助确实解决了困惑。", "解释很耐心，这次服务值得肯定。"),
        ("按照建议操作已经恢复，感谢你们的支持。", "问题处理好了，给这次支持一个好评。"),
        ("新的帮助页面很好用，特地来表扬一下。", "这次文档改进让我很满意，想反馈给团队。"),
    ]),
    ("order_status", "general", [
        ("订单 D3028 目前还处于待审核状态吗？", "帮我看一下 D3028 是否已经审核完成。"),
        ("订单页面显示处理中，现在走到哪个环节了？", "这笔订单当前是在备货还是等待确认？"),
        ("订单已经完成还是被系统关闭了？", "我想核实订单现在是否仍然有效。"),
    ]),
    ("logistics", "general", [
        ("已经交给快递的包裹预计周几送到？", "包裹已经在运输中，还需要多久送达？"),
        ("快递轨迹两天没有变化，应怎样申请查件？", "物流节点一直停在中转站，下一步怎么查？"),
        ("收件地址偏远，承运方是否要增加配送时间？", "运输到偏远地区通常是否有额外时效？"),
    ]),
    ("refund", "billing", [
        ("签收后的第六天还可以提交退货申请吗？", "收到货六天后想退货，是否仍在申请期限内？"),
        ("退款审核通过以后，原路退回大约需要几天？", "退回款项已经获批，到账还需要等待多久？"),
        ("退回商品被仓库验收后怎样继续申请退款？", "退件已经入库，接下来要走哪一步退款流程？"),
    ]),
    ("invoice", "billing", [
        ("电子发票开具以后从哪个页面下载？", "已经开好的发票文件应该在哪里领取？"),
        ("发票上的公司抬头写错了，怎样更正？", "开票名称有误，需要重新填写企业抬头。"),
        ("申请企业发票需要提供哪些税务信息？", "开单位发票时纳税识别信息怎么提交？"),
    ]),
    ("payment_issue", "billing", [
        ("同一次购买生成两笔扣费流水，如何核验？", "银行卡出现两条相同消费记录，会不会扣了两次？"),
        ("付款界面失败，但银行已经扣走金额。", "页面提示交易没完成，账户却减少了余额。"),
        ("支付后页面仍显示未付款，该怎么排查？", "钱已付出，系统订单却一直等待支付。"),
    ]),
    ("account_security", "general", [
        ("我的账户被陌生设备操作过，担心已经被盗。", "发现不认识的设备访问了账号，需要保护账户。"),
        ("忘了原密码，想通过绑定信息重新设置密码。", "密码记不起来了，应通过什么方式重设？"),
        ("没有授权的情况下绑定手机号被改动了。", "账户的安全联系方式被陌生人修改了。"),
    ]),
    ("technical_login", "technical", [
        ("登录接口返回 401，凭证应该如何检查？", "提交登录请求后一直收到 401 认证错误。"),
        ("收不到登录用的一次性验证码，如何排查？", "验证码迟迟不来，导致我进不了账号。"),
        ("凭证仍有效，但认证页面一直循环跳转。", "登录完成又被带回认证页，怎么定位原因？"),
    ]),
    ("technical_crash", "technical", [
        ("启动程序以后立刻闪退，连首页都没出现。", "应用一打开就自动退出，需要排查启动崩溃。"),
        ("页面请求得到 500，应该收集哪些排障信息？", "服务端返回 500 错误，如何定位故障范围？"),
        ("运行到上传步骤进程突然崩溃，如何诊断？", "执行文件上传时整个程序停止运行了。"),
    ]),
    ("human_handoff", "escalation", [
        ("我希望接下来直接与人工客服对话。", "请安排一位真人客服继续接待我。"),
        ("请将当前聊天交给人工支持人员。", "我需要人工接手这段会话。"),
        ("不要继续自动回复，请转人工窗口。", "请结束机器人处理并连接人工客服。"),
    ]),
    ("other", "general", [
        ("帮我写一首描写海边日出的诗。", "请创作一个关于月亮的童话故事。"),
        ("明年欧洲杯哪支球队一定能夺冠？", "请预测下一届世界杯的冠军。"),
        ("解释一下黑洞的事件视界是什么。", "能讲讲火山喷发形成的地质过程吗？"),
    ]),
]

CONTEXT = [
    ("technical_login", "technical", "认证接口始终返回401。",
     "刚才那个错误换了浏览器还是存在。", "login", "test"),
    ("logistics", "general", "我的包裹已经从中转站发出。",
     "那接下来大概哪天能送到？", "shipping", "test"),
    ("invoice", "billing", "我正在更正已经开具的电子发票。",
     "其中的单位抬头应该去哪里修改？", "invoice", "dev"),
    ("refund", "billing", "我已经决定申请退货退款。",
     "如果商品已经验收，后面还需要等待什么？", "refund", "test"),
    ("account_security", "general", "发现有人盗用了我的账户。",
     "绑定的号码也被改了，接下来怎样保护账号？", "security", "dev"),
    ("payment_issue", "billing", "同一笔购买被扣了两次钱。",
     "现在还出现了两条交易流水，该如何核验？", "payment", "test"),
]

def common(case_id, kind, group, split, query):
    return {"id": case_id, "kind": kind, "group_id": group, "split": split,
            "query": query, "history": [], "origin": "synthetic_original",
            "human_review": "pending"}


def build():
    intents = []
    for label, agent, families in INTENTS:
        for family, queries in enumerate(families, 1):
            for query in queries:
                row = common(f"intent-{len(intents)+1:03}", "intent",
                             f"intent-{label}-family-{family}", "dev" if family == 1 else "test",
                             query)
                row.update(expected_intent=label, expected_agents=[agent],
                           tags=["single_intent", "candidate_gold"])
                intents.append(row)
    for label, agent, history, query, family, split in CONTEXT:
        row = common(f"intent-{len(intents)+1:03}", "intent",
                     "intent-context-" + family, split, query)
        row.update(history=[{"role": "user", "content": history}],
                   expected_intent=label, expected_agents=[agent],
                   tags=["multi_turn", "candidate_gold"])
        intents.append(row)
    revise_intents(intents)
    contracts = build_contract_cases(common)
    write_jsonl(DATA / "intent_cases.jsonl", intents)
    write_jsonl(DATA / "contract_cases.jsonl", contracts)
    print(f"Built {len(intents)} intent cases and {len(contracts)} unexecuted native Python contracts.")


if __name__ == "__main__":
    build()
