"""Build original, fictional EchoMind Demo retrieval fixtures without external services.

Run with Python 3.10+: python evaluation/benchmark_v1/build_corpus.py
Only corpus.jsonl belongs in the retrieval index. rag_cases.jsonl contains labels.
All policies, versions, limits and business rules below are invented for testing.
The split is source-group based: 4 complete groups in dev and 8 in test.
This is a deterministic seed benchmark, not independently human-verified gold data.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DOCUMENTS: list[dict[str, Any]] = []
CASES: list[dict[str, Any]] = []
_doc_map: dict[tuple[str, int], dict[str, Any]] = {}
_groups: dict[str, tuple[str, str]] = {}


def group(group_id: str, split: str, domain: str) -> None:
    _groups[group_id] = (split, domain)


def doc(
    group_id: str,
    number: int,
    title: str,
    version: str,
    scope: str,
    facts: list[str],
    exclusions: str,
) -> None:
    split, domain = _groups[group_id]
    fact_records = [
        {"id": f"{group_id}-d{number}-f{i}", "text": value}
        for i, value in enumerate(facts, 1)
    ]
    content = (
        f"【原创虚构测试资料】本页只描述 EchoMind Demo 服务的测试规则，"
        f"不代表任何真实平台、客户或合同。测试版本：{version}。\n"
        f"适用范围：{scope}\n规则：\n"
        + "\n".join(f"- {item['text']}" for item in fact_records)
        + f"\n限制与排除：{exclusions}"
    )
    record = {
        "id": f"{group_id}-d{number}",
        "title": title,
        "content": content,
        "domain": domain,
        "group_id": group_id,
        "split": split,
        "version": version,
        "origin": "synthetic_original",
        "source_kind": "fictional_policy_fixture",
        "access_scope": "public_fixture",
        "facts": fact_records,
    }
    DOCUMENTS.append(record)
    _doc_map[(group_id, number)] = record


def case(
    group_id: str,
    category: str,
    query: str,
    refs: list[tuple[int, int]],
    history: list[dict[str, str]] | None = None,
    tags: list[str] | None = None,
) -> None:
    split, domain = _groups[group_id]
    required = []
    relevant = []
    for doc_number, fact_number in refs:
        source = _doc_map[(group_id, doc_number)]
        if source["id"] not in relevant:
            relevant.append(source["id"])
        fact = source["facts"][fact_number - 1]
        required.append(
            {
                "id": fact["id"],
                "text": fact["text"],
                "evidence_document_ids": [source["id"]],
            }
        )
    CASES.append(
        {
            "id": f"rag-{len(CASES) + 1:03d}",
            "kind": "rag",
            "group_id": group_id,
            "split": split,
            "query": query,
            "history": history or [],
            "category": category,
            "expected_behavior": (
                "refuse" if category == "unanswerable"
                else "clarify" if category == "clarification"
                else "answer"
            ),
            "relevant_document_ids": relevant,
            "required_facts": required,
            "origin": "synthetic_original",
            "human_review": "pending",
            "tags": [domain, "fictional_service", *(tags or [])],
        }
    )


def define_dataset() -> None:
    # g01: workspace login; dev. All quantities are invented fixture values.
    group("g01-login", "dev", "general")
    doc("g01-login", 1, "EchoMind Demo 登录保护规则｜测试 v1.0", "test-v1.0",
        "使用邮箱和密码登录的测试工作区；仅在问题指定测试 v1.0 时使用本页。", [
            "测试 v1.0 中，同一账号连续 5 次密码错误会锁定登录 15 分钟。",
            "锁定期间再次提交密码不会延长这 15 分钟。",
            "锁定到期后需要重新输入密码，不会自动建立登录会话。",
            "登录锁定只影响当前账号，不会锁定同一工作区的其他成员。",
        ], "本页不适用于测试 v2.0，不包含账号实际是否被锁定的实时记录。")
    doc("g01-login", 2, "EchoMind Demo 登录保护规则｜测试 v2.0", "test-v2.0",
        "使用邮箱和密码登录的测试工作区；测试 v2.0 的登录保护。", [
            "测试 v2.0 中，同一账号连续 6 次密码错误会锁定登录 20 分钟。",
            "锁定期间再次提交密码不会延长这 20 分钟。",
            "用户成功登录后，该账号的连续密码错误计数清零。",
            "工作区管理员不能手动解除登录保护锁定。",
        ], "单点登录身份提供商自身的锁定策略不在本页范围内；不要把测试 v1.0 的阈值用于本版。")
    doc("g01-login", 3, "EchoMind Demo 登录故障排查｜测试 v2.0", "test-v2.0",
        "已确认使用测试 v2.0，且登录页提示账号被保护锁定的用户。", [
            "遇到登录保护锁定时，应停止重试并等锁定时间结束，再用正确密码登录。",
            "忘记密码可以从登录页发送重置邮件，但重置密码不会提前解除已有锁定。",
            "重置邮件 10 分钟内未到达时，先检查垃圾邮件，再核对输入的邮箱地址。",
            "联系支持时可提供错误码和报错时间，不得提供密码或重置链接。",
        ], "排查步骤不能证明某个邮箱已注册，也不能查询当前账号的解锁时刻。")
    case("g01-login", "single_hop", "测试 v1.0 连续输错几次密码会被锁，锁多久？", [(1, 1)])
    case("g01-login", "single_hop", "测试 v2.0 已锁定时还在不断重试，锁定时间会一直顺延吗？", [(2, 2)])
    case("g01-login", "single_hop", "登录被保护锁定后，我先重置密码能马上进去吗？这里用测试 v2.0。", [(3, 1), (3, 2)])
    case("g01-login", "single_hop", "测试 v2.0 重置邮件十分钟没收到，我应先做哪两项检查？", [(3, 3)])
    case("g01-login", "multi_hop", "我们用测试 v2.0，管理员能替我提前解锁吗？如果忘了密码应该怎么处理已有锁定？", [(2, 4), (3, 1), (3, 2)])
    case("g01-login", "multi_turn", "那我现在找客服要准备什么信息，密码也一起发吗？", [(3, 4)],
         [{"role": "user", "content": "我在 EchoMind Demo 测试 v2.0 登录页看到保护锁定，想联系支持。"},
          {"role": "assistant", "content": "你想确认联系支持时需要准备哪些信息吗？"}], ["context_required"])
    case("g01-login", "unanswerable", "请直接告诉我账号 user42@example.invalid 今天准确几点解除锁定。", [], tags=["missing_live_state"])
    case("g01-login", "version_specific", "把测试 v1.0 和 v2.0 的连续密码错误锁定阈值及持续时间对比一下。", [(1, 1), (2, 1)], tags=["version_conflict"])

    # g02: API rate limits; dev.
    group("g02-api", "dev", "technical")
    doc("g02-api", 1, "EchoMind Demo API 限流与鉴权｜测试 v1.0", "test-v1.0",
        "调用测试 API 的标准工作区；仅限测试 v1.0。", [
            "测试 v1.0 的标准工作区 API 限额为每分钟 60 个请求。",
            "该限额按工作区汇总，同一工作区的多个 API 密钥共享额度。",
            "超过限额时 API 返回 HTTP 429。",
            "API 密钥应放在 Authorization: Bearer 请求头中。",
        ], "不承诺请求均匀分布或突发容量；本页不描述测试 v2.0。")
    doc("g02-api", 2, "EchoMind Demo API 限流与鉴权｜测试 v2.0", "test-v2.0",
        "调用测试 API 的标准工作区；测试 v2.0 使用以下配额。", [
            "测试 v2.0 的标准工作区 API 限额为每分钟 120 个请求。",
            "该限额按工作区汇总，创建新的 API 密钥不会增加工作区额度。",
            "超过限额返回 HTTP 429，并通过 Retry-After 响应头给出建议等待秒数。",
            "缺少或无效的 API 密钥返回 HTTP 401，等待限流窗口不能修复鉴权错误。",
        ], "专属合同配额不在公开测试资料范围内；不可把 401 当成 429 重试。")
    doc("g02-api", 3, "EchoMind Demo API 重试与请求排查｜测试 v2.0", "test-v2.0",
        "客户端处理测试 v2.0 的 HTTP 错误；以下是排查规则，不是实际请求日志。", [
            "收到 HTTP 429 后，应至少等待 Retry-After 指定的秒数再重试。",
            "收到 HTTP 401 后，应检查密钥是否有效、是否属于目标工作区以及 Authorization 请求头。",
            "提交支持请求时，应附上 X-Request-ID、HTTP 状态码和请求时间，并遮蔽 API 密钥。",
            "单纯查询类请求可在网络超时后重试，具有写入效果的请求应先检查接口是否支持幂等键。",
        ], "不能从公开规则推断某个密钥当前剩余额度，也不提供真实密钥或租户配置。")
    case("g02-api", "single_hop", "测试 v1.0 标准工作区每分钟能调用多少次 API？", [(1, 1)])
    case("g02-api", "single_hop", "测试 v2.0 请求过多会返回什么状态码，哪里能看到建议等多久？", [(2, 3)])
    case("g02-api", "single_hop", "我调用测试 v2.0 总是 401，该优先检查哪些地方？", [(3, 2)])
    case("g02-api", "single_hop", "排查测试 v2.0 API 故障时，发给支持的材料有哪些，密钥能直接贴吗？", [(3, 3)])
    case("g02-api", "multi_hop", "测试 v2.0 遇到 429，新增一个密钥能多拿额度吗？客户端应该依据什么等待？", [(2, 2), (2, 3), (3, 1)])
    case("g02-api", "multi_turn", "那超时后能直接重试吗？", [(3, 4)],
         [{"role": "user", "content": "我们在接 EchoMind Demo 测试 v2.0 API，调用里既有只读查询，也有写入操作。"},
          {"role": "assistant", "content": "这两类调用需要分别考虑重试。"}], ["context_required", "side_effect_awareness"])
    case("g02-api", "unanswerable", "我的工作区现在还剩多少 API 调用额度？请给出精确数字。", [], tags=["missing_live_state"])
    case("g02-api", "version_specific", "测试 v1.0 和 v2.0 标准工作区的每分钟 API 配额分别是多少？", [(1, 1), (2, 1)], tags=["version_conflict"])

    # g03: subscription changes; dev.
    group("g03-plan", "dev", "billing")
    doc("g03-plan", 1, "EchoMind Demo 套餐变更规则｜测试 v1.0", "test-v1.0",
        "直接在测试控制台购买的按月订阅；仅限测试 v1.0。", [
            "测试 v1.0 的月付套餐升级在下一续费日生效。",
            "升级生效前仍使用原套餐额度，提交升级申请不会立即补充额度。",
            "测试 v1.0 的月付套餐降级也在下一续费日生效。",
            "当期已使用的额度不会因提交套餐变更申请而清零。",
        ], "不覆盖年度合同、渠道购买或真实支付处理；本页不适用于测试 v2.0。")
    doc("g03-plan", 2, "EchoMind Demo 套餐变更规则｜测试 v2.0", "test-v2.0",
        "直接在测试控制台购买的按月订阅；测试 v2.0 的标准套餐。", [
            "测试 v2.0 的月付套餐升级在差额支付成功后立即生效。",
            "升级差额按当前账期剩余天数折算，以确认页显示金额为准。",
            "月付套餐降级在下一续费日生效，当期不按降级退还差额。",
            "变更套餐不会重置当前账期的已用额度。",
        ], "本页不提供某个账号的差额金额；不得使用测试 v1.0 的升级时间规则。")
    doc("g03-plan", 3, "EchoMind Demo 续费取消与变更失败｜测试 v2.0", "test-v2.0",
        "测试 v2.0 月付订阅的自动续费和升级失败处理。", [
            "关闭自动续费后，已付费套餐可使用到当前账期结束。",
            "关闭自动续费本身不等于提交退款申请。",
            "升级差额支付失败时，保留原套餐和原账期，不应用新套餐权益。",
            "取消自动续费的入口是工作区的账单设置页，操作权限属于所有者。",
        ], "不说明线下合同终止办法；此处没有任何真实账单或支付记录。")
    case("g03-plan", "single_hop", "测试 v1.0 月付套餐今天申请升级，今天就能使用新额度吗？", [(1, 1), (1, 2)])
    case("g03-plan", "single_hop", "测试 v2.0 升级差额怎么计算，可以从文档直接算出我的金额吗？", [(2, 2)])
    case("g03-plan", "single_hop", "测试 v2.0 关闭自动续费之后，已付款的套餐还能用到什么时候？这算申请退款了吗？", [(3, 1), (3, 2)])
    case("g03-plan", "single_hop", "测试 v2.0 升级付款失败，会先给我新套餐权益吗，账期会变化吗？", [(3, 3)])
    case("g03-plan", "multi_hop", "我们是测试 v2.0 月付订阅，打算降级并关闭续费：降级什么时候生效，剩下的付费时间还能用吗？", [(2, 3), (3, 1)])
    case("g03-plan", "multi_turn", "去哪里关，普通成员有权限吗？", [(3, 4)],
         [{"role": "user", "content": "我想关掉 EchoMind Demo 测试 v2.0 月付订阅的自动续费。"},
          {"role": "assistant", "content": "关闭后现有订阅仍能使用到当前账期结束。"}], ["context_required", "authorization"])
    case("g03-plan", "unanswerable", "根据我的账号，告诉我今天升级专业版需要实际补付多少元。", [], tags=["missing_account_billing"])
    case("g03-plan", "version_specific", "月付升级在测试 v1.0 与 v2.0 中各自什么时候生效？", [(1, 1), (2, 1)], tags=["version_conflict"])

    # g04: knowledge sync; dev.
    group("g04-sync", "dev", "technical")
    doc("g04-sync", 1, "EchoMind Demo 知识同步规则｜测试 v1.0", "test-v1.0",
        "已连接的公开帮助中心知识源；仅限测试 v1.0 的定时同步。", [
            "测试 v1.0 的公开帮助中心知识源每 24 小时自动同步一次。",
            "自动同步只读取连接配置允许的目录，不会遍历整个网站。",
            "单次同步失败不会删除上一次成功导入的文档。",
            "测试 v1.0 的手动同步只允许工作区所有者执行。",
        ], "网页存在不等于已获抓取权限；此处不包含真实网站连接。")
    doc("g04-sync", 2, "EchoMind Demo 知识同步规则｜测试 v2.0", "test-v2.0",
        "已连接且授权读取的公开帮助中心知识源；测试 v2.0 的增量同步。", [
            "测试 v2.0 的公开帮助中心知识源每 6 小时自动同步一次。",
            "同一知识源通过源文档标识识别更新，内容变更后替换该文档的旧索引。",
            "自动同步失败会保留上一次成功版本，并把本次任务标记为失败。",
            "知识管理员和工作区所有者可以发起手动同步，普通成员不可以。",
        ], "同步间隔不保证远端内容立即可检索；不适用于本地上传文件。")
    doc("g04-sync", 3, "EchoMind Demo 知识删除与同步诊断｜测试 v2.0", "test-v2.0",
        "测试 v2.0 已连接知识源的删除传播、状态查询和失败排查。", [
            "源站删除文档后，下一次成功同步会删除对应检索索引。",
            "删除整个知识源会立即禁用该源检索，随后异步清理其索引。",
            "同步返回 403 时，应检查来源授权和允许读取的目录，不能反复重试绕过权限。",
            "同步任务页会显示最近成功时间、任务状态和失败原因。",
        ], "任务成功时间只能从实际任务页查询，不能由这份规则推算；不绕过来源访问控制。")
    case("g04-sync", "single_hop", "测试 v1.0 的帮助中心知识源多长时间自动同步一次，失败会删掉已有文档吗？", [(1, 1), (1, 3)])
    case("g04-sync", "single_hop", "测试 v2.0 普通成员可以手动同步知识源吗，谁可以操作？", [(2, 4)])
    case("g04-sync", "single_hop", "我把测试 v2.0 的整个知识源删除，索引还没清理完时用户还能搜到吗？", [(3, 2)])
    case("g04-sync", "single_hop", "测试 v2.0 同步报 403，应该检查什么，可以不停重试碰运气吗？", [(3, 3)])
    case("g04-sync", "multi_hop", "测试 v2.0 远端文章已经删掉，但刚才同步失败了，旧索引会马上消失吗？什么时候才删除？", [(2, 3), (3, 1)])
    case("g04-sync", "multi_turn", "我去哪里确认它最近一次成功的时间和这次失败原因？", [(3, 4)],
         [{"role": "user", "content": "EchoMind Demo 测试 v2.0 的帮助中心知识源同步失败了。"},
          {"role": "assistant", "content": "失败时会保留上一次成功版本。"}], ["context_required"])
    case("g04-sync", "unanswerable", "请报出我这个知识源今天最后一次成功同步的具体时间，不要让我去任务页看。", [], tags=["missing_live_state"])
    case("g04-sync", "version_specific", "测试 v1.0 与 v2.0 的帮助中心自动同步周期有什么变化？", [(1, 1), (2, 1)], tags=["version_conflict"])

    # g05: password reset and 2FA; test.
    group("g05-recovery", "test", "general")
    doc("g05-recovery", 1, "EchoMind Demo 密码重置规则｜测试 v1.0", "test-v1.0",
        "使用邮箱密码的测试账号；只说明测试 v1.0 的密码重置链接。", [
            "测试 v1.0 的密码重置链接自发送起 30 分钟内有效。",
            "每条密码重置链接只能成功使用一次。",
            "重新申请密码重置会使此前尚未使用的重置链接失效。",
            "重置成功后，需要使用新密码重新登录。",
        ], "不适用于单点登录账号；本页没有任何真实重置链接或验证码。")
    doc("g05-recovery", 2, "EchoMind Demo 密码重置规则｜测试 v2.0", "test-v2.0",
        "使用邮箱密码的测试账号；测试 v2.0 的密码重置与会话处理。", [
            "测试 v2.0 的密码重置链接自发送起 15 分钟内有效。",
            "每条密码重置链接只能成功使用一次，重新申请会立即使旧链接失效。",
            "密码重置成功后，所有已登录会话都会退出。",
            "通过登录页的忘记密码入口发送重置邮件，不需要先登录。",
        ], "不能协助读取用户邮箱；密码重置不等于清除双因素认证。")
    doc("g05-recovery", 3, "EchoMind Demo 双因素认证恢复｜测试 v2.0", "test-v2.0",
        "已启用双因素认证且仍可访问注册邮箱的测试账号。", [
            "丢失认证器时，可以在双因素验证页使用预先保存的恢复码。",
            "每个恢复码仅可成功使用一次，使用后不能再次用于登录。",
            "恢复码也全部丢失时，需要提交账号恢复工单并完成邮箱归属验证。",
            "支持人员不会索取当前动态验证码，也不会在聊天中直接关闭双因素认证。",
        ], "没有注册邮箱访问权时的进一步身份核验流程不在本页范围；不存在通用恢复码。")
    case("g05-recovery", "single_hop", "测试 v1.0 的重置邮件链接过了二十分钟还能用吗，有效期是多少？", [(1, 1)])
    case("g05-recovery", "single_hop", "测试 v2.0 我连续申请了两封重置邮件，第一封还有效吗？", [(2, 2)])
    case("g05-recovery", "single_hop", "测试 v2.0 忘了密码，要先登录才能发送重置邮件吗？", [(2, 4)])
    case("g05-recovery", "single_hop", "测试 v2.0 手机认证器丢了，但我留有恢复码，能用同一个码一直登录吗？", [(3, 1), (3, 2)])
    case("g05-recovery", "multi_hop", "测试 v2.0 我打算重置密码，旧设备的登录会保留吗？如果双因素恢复码也没了，但邮箱还在，该走什么流程？", [(2, 3), (3, 3)])
    case("g05-recovery", "multi_turn", "那客服会让我把当前动态验证码发给他，或者直接在聊天里帮我关掉吗？", [(3, 4)],
         [{"role": "user", "content": "我的 EchoMind Demo 测试 v2.0 账号双因素恢复码全部丢了，准备联系支持。"},
          {"role": "assistant", "content": "如果仍能访问注册邮箱，可以提交账号恢复工单并验证邮箱归属。"}], ["context_required", "sensitive_data"])
    case("g05-recovery", "unanswerable", "你帮我生成一个能登录我账号的真实双因素恢复码，我现在就用。", [], tags=["unavailable_secret"])
    case("g05-recovery", "version_specific", "密码重置链接在测试 v1.0 和 v2.0 分别能保留多久？", [(1, 1), (2, 1)], tags=["version_conflict"])

    # g06: member permissions; test.
    group("g06-members", "test", "general")
    doc("g06-members", 1, "EchoMind Demo 成员邀请规则｜测试 v1.0", "test-v1.0",
        "测试 v1.0 工作区通过邮件邀请新成员的场景。", [
            "测试 v1.0 的成员邀请链接有效期为 7 天。",
            "邀请链接只能由被邀请邮箱注册或登录的账号接受。",
            "工作区所有者可以撤销尚未接受的邀请。",
            "邀请过期后需要重新发送，原链接不会自动延长。",
        ], "不说明企业目录自动同步；邀请链接不应作为公开共享入口。")
    doc("g06-members", 2, "EchoMind Demo 成员邀请规则｜测试 v2.0", "test-v2.0",
        "测试 v2.0 工作区通过邮件邀请新成员的场景。", [
            "测试 v2.0 的成员邀请链接有效期为 72 小时。",
            "邀请链接仅允许被邀请邮箱对应的账号接受，转发给其他邮箱不能加入。",
            "工作区所有者和成员管理员都可以发送或撤销邀请。",
            "邀请撤销后立即失效，不需要等待 72 小时结束。",
        ], "现有成员的角色变更不通过重新接受邀请完成；本页不授予任何实际权限。")
    doc("g06-members", 3, "EchoMind Demo 工作区角色与退出｜测试 v2.0", "test-v2.0",
        "测试 v2.0 工作区的所有者、成员管理员、普通成员权限。", [
            "普通成员可以使用已发布的客服应用，但不能管理邀请和工作区账单。",
            "成员管理员可以管理普通成员，不能转移工作区所有权或管理账单。",
            "只有当前所有者可以转移工作区所有权，目标必须是已加入的成员。",
            "所有者需要先完成所有权转移，才能退出仍有其他成员的工作区。",
        ], "角色描述不证明当前用户持有什么角色；实际授权须由服务端检查。")
    case("g06-members", "single_hop", "测试 v1.0 发出的邀请有效几天，过期后原链接会延长吗？", [(1, 1), (1, 4)])
    case("g06-members", "single_hop", "测试 v2.0 我收到邀请，转发给另一个邮箱的同事可以直接用吗？", [(2, 2)])
    case("g06-members", "single_hop", "测试 v2.0 撤销邀请要等原来的 72 小时走完才失效吗？", [(2, 4)])
    case("g06-members", "single_hop", "测试 v2.0 我是所有者，工作区还有同事，能不转移所有权就退出吗？", [(3, 4)])
    case("g06-members", "multi_hop", "测试 v2.0 的成员管理员能邀请新人吗？还能直接把工作区所有权转给新人吗？", [(2, 3), (3, 2), (3, 3)])
    case("g06-members", "multi_turn", "那他能管理邀请和账单吗？", [(3, 1)],
         [{"role": "user", "content": "我们在用 EchoMind Demo 测试 v2.0，刚让同事以普通成员身份加入工作区。"},
          {"role": "assistant", "content": "普通成员可以使用已发布的客服应用。"}], ["context_required", "authorization"])
    case("g06-members", "unanswerable", "查一下我们工作区当前所有者的邮箱，把完整地址给我。", [], tags=["missing_private_directory"])
    case("g06-members", "version_specific", "测试 v1.0 和 v2.0 邮件邀请链接的有效期各是多少？", [(1, 1), (2, 1)], tags=["version_conflict"])

    # g07: sample device shipment; test, fictional orders only.
    group("g07-orders", "test", "general")
    doc("g07-orders", 1, "EchoMind Demo 演示设备配送规则｜测试 v1.0", "test-v1.0",
        "测试商城中状态为已支付、库存充足的标准演示设备订单；仅限测试 v1.0。", [
            "测试 v1.0 的标准演示设备订单在支付成功后 3 个工作日内出库。",
            "工作日不包含周六、周日和测试商城标记的休息日。",
            "预售设备的出库时间以订单页面展示的预售日期为准。",
            "出库时间不等于签收时间，运输时效不计入出库承诺。",
        ], "本测试资料没有真实物流单号、收货地址或实时库存。")
    doc("g07-orders", 2, "EchoMind Demo 演示设备配送规则｜测试 v2.0", "test-v2.0",
        "测试商城中状态为已支付、库存充足的标准演示设备订单；测试 v2.0。", [
            "测试 v2.0 的标准演示设备订单在支付成功后 2 个工作日内出库。",
            "工作日不包含周六、周日和测试商城标记的休息日。",
            "预售设备不适用 2 个工作日出库规则，应查看订单页面的预售日期。",
            "出库后可以在订单详情查看物流单号，出库承诺不包含运输时长。",
        ], "出库时限不是送达保证；库存不足订单不适用标准时限。")
    doc("g07-orders", 3, "EchoMind Demo 订单地址与取消｜测试 v2.0", "test-v2.0",
        "测试 v2.0 演示设备实物订单的修改与取消。", [
            "订单未进入拣货时，购买者可以在订单详情提交收货地址修改。",
            "订单已进入拣货或已出库时，不支持直接修改地址，需要联系支持确认可行处理方式。",
            "订单未出库时可以申请取消；已出库时需要走退货申请，不能直接取消。",
            "提交取消申请不代表已经取消成功，应以订单状态更新为准。",
        ], "联系支持不保证拦截成功；不能仅根据聊天内容确认真实订单已修改或取消。")
    case("g07-orders", "single_hop", "测试 v1.0 标准演示设备库存充足并已付款，最多几个工作日出库？", [(1, 1)])
    case("g07-orders", "single_hop", "测试 v2.0 预售演示设备也保证付款后两个工作日出库吗？", [(2, 3)])
    case("g07-orders", "single_hop", "测试 v2.0 我的订单已进入拣货，能直接在页面修改地址吗？", [(3, 2)])
    case("g07-orders", "single_hop", "测试 v2.0 设备订单已经出库了，还能直接取消吗？", [(3, 3)])
    case("g07-orders", "multi_hop", "测试 v2.0 设备已出库，想先查物流再改地址，应到哪里看单号，地址还能直接修改吗？", [(2, 4), (3, 2)])
    case("g07-orders", "multi_turn", "提交完就代表已经取消成功了吗？", [(3, 4)],
         [{"role": "user", "content": "EchoMind Demo 测试 v2.0 的设备订单还没出库，我刚提交取消申请。"},
          {"role": "assistant", "content": "未出库时允许申请取消。"}], ["context_required", "state_confirmation"])
    case("g07-orders", "unanswerable", "订单 DEMO-PRIVATE-8801 当前到哪个城市了，预计今天几点送到？", [], tags=["missing_order_tool"])
    case("g07-orders", "version_specific", "同样是已付款且有库存的标准演示设备订单，测试 v1.0 和 v2.0 分别承诺多久出库？", [(1, 1), (2, 1)], tags=["version_conflict"])

    # g08: human handoff; test.
    group("g08-support", "test", "general")
    doc("g08-support", 1, "EchoMind Demo 人工支持服务时间｜测试 v1.0", "test-v1.0",
        "测试 v1.0 的标准工作区人工聊天支持；时间统一为北京时间。", [
            "测试 v1.0 的人工聊天支持开放时间为工作日 09:00 至 18:00，北京时间。",
            "非开放时间可以提交工单，但不会立即接入人工聊天。",
            "工单提交成功后会返回一个可查询的工单编号。",
            "人工聊天的开放时间不代表所有工单都能当日解决。",
        ], "不适用于专属服务合同；时间规则不是实时坐席空闲状态。")
    doc("g08-support", 2, "EchoMind Demo 人工支持服务时间｜测试 v2.0", "test-v2.0",
        "测试 v2.0 的标准工作区人工聊天支持；时间统一为北京时间。", [
            "测试 v2.0 的人工聊天支持开放时间为工作日 09:00 至 20:00，北京时间。",
            "非开放时间允许提交工单，人工聊天会在下一个开放时段处理。",
            "标准工单首次响应目标为 1 个工作日，这不是问题解决时限。",
            "标记紧急不会自动获得专属合同响应承诺，需按实际服务等级处理。",
        ], "工作日以测试服务日历为准；本页不包含节假日日历或实际排队人数。")
    doc("g08-support", 3, "EchoMind Demo 人工交接与工单状态｜测试 v2.0", "test-v2.0",
        "测试 v2.0 中机器人转人工或创建支持工单的交接规则。", [
            "交接摘要应包含问题描述、已尝试步骤、错误码和用户期望，不应包含密码或完整密钥。",
            "用户确认创建工单后，系统才可提交；生成摘要本身不等于创建成功。",
            "只有收到创建工具返回的工单编号，才可以告诉用户工单已创建。",
            "已创建工单的处理进度需要通过工单查询接口获取，不能根据回复语气推测状态。",
        ], "本页没有真实工单；不得编造编号、处理进度或已通知人工的结果。")
    case("g08-support", "single_hop", "测试 v1.0 人工聊天在北京时间工作日几点开始、几点结束？", [(1, 1)])
    case("g08-support", "single_hop", "测试 v2.0 承诺一个工作日首响，是不是意味着一个工作日内一定解决？", [(2, 3)])
    case("g08-support", "single_hop", "测试 v2.0 转人工交接摘要要写什么，可以放我的完整 API 密钥吗？", [(3, 1)])
    case("g08-support", "single_hop", "测试 v2.0 机器人已经给了交接摘要，但没有工单编号，能说工单创建好了吗？", [(3, 2), (3, 3)])
    case("g08-support", "multi_hop", "测试 v2.0 晚上非开放时间我想提交工单，需要先确认吗，提交后会立即有人聊天吗？", [(2, 2), (3, 2)])
    case("g08-support", "multi_turn", "那能凭客服刚才说在跟进，就认定状态变成处理中吗？", [(3, 4)],
         [{"role": "user", "content": "我想查 EchoMind Demo 测试 v2.0 已创建工单的处理进度。"},
          {"role": "assistant", "content": "需要使用工单查询接口获取实际状态。"}], ["context_required", "state_confirmation"])
    case("g08-support", "unanswerable", "现在人工队列前面有几个人，我还有多少分钟能接通？", [], tags=["missing_live_queue"])
    case("g08-support", "version_specific", "测试 v1.0 到 v2.0 人工聊天的工作日服务时间如何变化？请按北京时间回答。", [(1, 1), (2, 1)], tags=["version_conflict"])

    # g09: webhooks; test.
    group("g09-webhook", "test", "technical")
    doc("g09-webhook", 1, "EchoMind Demo Webhook 投递规则｜测试 v1.0", "test-v1.0",
        "测试 v1.0 的 HTTPS 事件回调；以下次数只统计自动重试。", [
            "测试 v1.0 的 Webhook 首次投递失败后，最多自动重试 3 次。",
            "接收端返回任意 HTTP 2xx 状态码视为本次投递成功。",
            "接收端返回非 2xx 状态码或连接超时视为投递失败。",
            "自动重试次数不包含首次投递，也不包含管理员手动重放。",
        ], "不保证事件只送达一次；不说明测试 v2.0 的重试上限。")
    doc("g09-webhook", 2, "EchoMind Demo Webhook 投递规则｜测试 v2.0", "test-v2.0",
        "测试 v2.0 的 HTTPS 事件回调；以下次数只统计自动重试。", [
            "测试 v2.0 的 Webhook 首次投递失败后，最多自动重试 5 次。",
            "接收端必须在 5 秒内返回 HTTP 2xx，超时或非 2xx 均视为本次投递失败。",
            "Webhook 采用至少一次投递，同一个 event_id 可能重复送达。",
            "自动重试次数不包含首次投递，最多自动投递总次数为 6 次。",
        ], "手动重放另行计数；没有顺序投递保证，也不能承诺绝不重复。")
    doc("g09-webhook", 3, "EchoMind Demo Webhook 验签与消费｜测试 v2.0", "test-v2.0",
        "接收测试 v2.0 Webhook 的业务服务；先验签再接受事件。", [
            "验签使用 X-Demo-Signature 请求头和原始请求体，不能先重排 JSON 再计算签名。",
            "接收端应以 event_id 建立去重记录，重复事件不得再次执行同一业务写入。",
            "耗时业务应先把已验签事件可靠入队，再及时返回 HTTP 2xx。",
            "签名失败时应拒绝处理，并检查回调密钥及原始请求体是否被代理修改，不能关闭签名校验来放行。",
        ], "文档不包含回调密钥；不能为通过验签而关闭签名校验。")
    case("g09-webhook", "single_hop", "测试 v1.0 回调首次失败后自动重试几次，这个数含首次投递吗？", [(1, 1), (1, 4)])
    case("g09-webhook", "single_hop", "测试 v2.0 我的接收端要八秒才返回 200，这次算成功吗？", [(2, 2)])
    case("g09-webhook", "single_hop", "测试 v2.0 Webhook 验签时可以把 JSON 字段重新排序再计算吗，签名在哪个头里？", [(3, 1)])
    case("g09-webhook", "single_hop", "测试 v2.0 同一个 event_id 来了两遍，业务侧怎样避免扣两次或写两次？", [(3, 2)])
    case("g09-webhook", "multi_hop", "测试 v2.0 Webhook 可能重复吗？我的处理要十几秒，应怎样接收并回复才合适？", [(2, 3), (3, 3)])
    case("g09-webhook", "multi_turn", "那能临时关掉校验吗，应该先排查什么？", [(3, 4)],
         [{"role": "user", "content": "EchoMind Demo 测试 v2.0 的 Webhook 一直签名失败，前面还有一层代理。"},
          {"role": "assistant", "content": "签名验证失败的请求不能直接作为可信事件处理。"}], ["context_required", "signature_validation"])
    case("g09-webhook", "unanswerable", "把我们工作区的 Webhook 签名密钥直接发给我，顺便给 event_private_9 算一个有效签名。", [], tags=["unavailable_secret"])
    case("g09-webhook", "version_specific", "测试 v1.0 与 v2.0 在首次投递失败后，各最多自动重试多少次？", [(1, 1), (2, 1)], tags=["version_conflict"])

    # g10: uploads and indexing; test.
    group("g10-upload", "test", "technical")
    doc("g10-upload", 1, "EchoMind Demo 知识文件上传限制｜测试 v1.0", "test-v1.0",
        "测试 v1.0 控制台手动上传单个知识文件；大小使用 MiB。", [
            "测试 v1.0 的单个知识文件最大为 10 MiB。",
            "测试 v1.0 支持 UTF-8 编码的 TXT 和 Markdown 文件。",
            "超过单文件上限会拒绝上传，不会自动拆成多个文件。",
            "上传成功只代表文件接收成功，仍需等待解析与索引完成。",
        ], "MiB 与十进制 MB 不等价；此版本没有 PDF 上传能力。")
    doc("g10-upload", 2, "EchoMind Demo 知识文件上传限制｜测试 v2.0", "test-v2.0",
        "测试 v2.0 控制台手动上传单个知识文件；大小使用 MiB。", [
            "测试 v2.0 的单个知识文件最大为 25 MiB。",
            "测试 v2.0 支持 UTF-8 编码的 TXT、Markdown 和可复制文本的 PDF。",
            "仅含扫描图片的 PDF 不会自动 OCR，需要先转换为可提取文本再上传。",
            "密码加密的 PDF 需要先由有权限的用户解密，系统不会绕过文档密码。",
        ], "文件大小达标不代表内容必然可解析；不读取受密码保护的原文件。")
    doc("g10-upload", 3, "EchoMind Demo 解析索引与重复上传｜测试 v2.0", "test-v2.0",
        "测试 v2.0 已通过大小和类型检查的知识文件。", [
            "知识文件只有进入 ready 状态后才会参与检索，uploaded 和 processing 状态都不可检索。",
            "解析失败会标记为 failed，并在文件详情展示错误原因，不会把空内容当作成功索引。",
            "同一知识库内，文件内容哈希相同时复用已有文档，不因改文件名重复建索引。",
            "文件内容发生改变后需要提交更新并等待新版本 ready，原文件名相同不代表内容已更新。",
        ], "本页不承诺具体索引完成秒数；知识库之间的同内容文件不按本页去重。")
    case("g10-upload", "single_hop", "测试 v1.0 上传一个 11 MiB 的 TXT 会自动拆分吗，单文件上限是多少？", [(1, 1), (1, 3)])
    case("g10-upload", "single_hop", "测试 v2.0 是否支持只有扫描图片的 PDF 自动识别文字？应该怎样准备文件？", [(2, 3)])
    case("g10-upload", "single_hop", "测试 v2.0 文件状态是 processing，现在就可以被知识库检索吗？", [(3, 1)])
    case("g10-upload", "single_hop", "测试 v2.0 同一知识库里把相同内容改个文件名上传，会产生一份新的重复索引吗？", [(3, 3)])
    case("g10-upload", "multi_hop", "测试 v2.0 上传 20 MiB 的可复制文本 PDF，大小和类型是否允许？上传接收成功后就能检索吗？", [(2, 1), (2, 2), (3, 1)])
    case("g10-upload", "multi_turn", "这种情况在哪里看原因，会不会把空内容当成成功？", [(3, 2)],
         [{"role": "user", "content": "我在 EchoMind Demo 测试 v2.0 上传的知识文件已经变成 failed 状态。"},
          {"role": "assistant", "content": "failed 表示该文件的解析或索引没有完成。"}], ["context_required"])
    case("g10-upload", "unanswerable", "我的 PDF 刚上传完成，你保证它还要精确多少秒变成 ready？", [], tags=["unsupported_sla"])
    case("g10-upload", "version_specific", "测试 v1.0 和 v2.0 的单个知识文件上限分别是多少 MiB？", [(1, 1), (2, 1)], tags=["version_conflict"])

    # g11: invoices; test.
    group("g11-invoice", "test", "billing")
    doc("g11-invoice", 1, "EchoMind Demo 测试发票处理规则｜测试 v1.0", "test-v1.0",
        "已支付且可申请测试发票的控制台订单；仅限测试 v1.0。", [
            "测试 v1.0 的发票申请审核通过后，预计在 5 个工作日内生成测试电子票据。",
            "审核未通过的申请不进入票据生成时限。",
            "申请人应核对抬头和税号，不能使用占位符代替必要字段。",
            "该环境只生成测试电子票据，不具有真实税务凭证效力。",
        ], "这里的票据流程为虚构业务测试，不构成任何税务规则说明。")
    doc("g11-invoice", 2, "EchoMind Demo 测试发票处理规则｜测试 v2.0", "test-v2.0",
        "已支付且可申请测试发票的控制台订单；测试 v2.0 的票据流程。", [
            "测试 v2.0 的发票申请审核通过后，预计在 2 个工作日内生成测试电子票据。",
            "待支付、已全额退款的订单不能新申请测试发票。",
            "抬头和税号在申请提交前需要核对，审核状态可在订单的发票页查看。",
            "该环境只生成测试电子票据，不具有真实税务凭证效力。",
        ], "不说明真实税务机关要求，也不能由文档判定某一订单是否审核通过。")
    doc("g11-invoice", 3, "EchoMind Demo 测试票据更正与下载｜测试 v2.0", "test-v2.0",
        "测试 v2.0 已提交或已生成测试电子票据的订单。", [
            "申请仍在待审核状态时，可以撤回后修改抬头或税号并重新提交。",
            "测试电子票据已经生成后，不能直接覆盖原票据，应提交票据更正工单。",
            "票据生成后，工作区所有者可在对应订单的发票页下载 PDF。",
            "更正工单需要订单编号、更正原因和正确抬头；不要在公开聊天中粘贴完整税号。",
        ], "提交更正工单不等于票据已更正；本页不含任何真实税号或票据文件。")
    case("g11-invoice", "single_hop", "测试 v1.0 的发票申请从提交时就开始算五个工作日吗？", [(1, 1), (1, 2)])
    case("g11-invoice", "single_hop", "测试 v2.0 已全额退款的订单还能新申请测试发票吗？", [(2, 2)])
    case("g11-invoice", "single_hop", "测试 v2.0 发票申请还在待审核，我把抬头填错了，能怎样改？", [(3, 1)])
    case("g11-invoice", "single_hop", "测试 v2.0 电子票据已生成，在哪里下载 PDF，谁有权限？", [(3, 3)])
    case("g11-invoice", "multi_hop", "测试 v2.0 票据已经生成但税号填错，应直接覆盖吗？这种票据能当真实税务凭证吗？", [(3, 2), (2, 4)])
    case("g11-invoice", "multi_turn", "工单里要附什么，我应该把完整税号直接发在公开聊天里吗？", [(3, 4)],
         [{"role": "user", "content": "EchoMind Demo 测试 v2.0 已生成的票据抬头不对，我准备提交更正工单。"},
          {"role": "assistant", "content": "已生成的票据需要通过更正工单处理。"}], ["context_required", "sensitive_data"])
    case("g11-invoice", "unanswerable", "请发我上个月订单 DEMO-INVOICE-21 的真实发票 PDF 下载链接。", [], tags=["missing_invoice_tool"])
    case("g11-invoice", "version_specific", "审核已经通过后，测试 v1.0 和 v2.0 各预计几个工作日生成测试电子票据？", [(1, 1), (2, 1)], tags=["version_conflict"])

    # g12: refunds; test.
    group("g12-refund", "test", "billing")
    doc("g12-refund", 1, "EchoMind Demo 未使用充值退款｜测试 v1.0", "test-v1.0",
        "测试 v1.0 控制台直接购买、完全未使用的付费充值包；不适用于订阅。", [
            "测试 v1.0 的完全未使用付费充值包，可在支付成功后 7 个自然日内申请退款。",
            "赠送额度不属于付费充值金额，不能折算现金退款。",
            "已经使用过的付费充值包不适用完全未使用退款流程。",
            "退款申请提交成功不等于审核通过或款项到账。",
        ], "这只是虚构产品测试规则，不说明任何真实消费者法定权利或支付渠道政策。")
    doc("g12-refund", 2, "EchoMind Demo 未使用充值退款｜测试 v2.0", "test-v2.0",
        "测试 v2.0 控制台直接购买、完全未使用的付费充值包；不适用于订阅。", [
            "测试 v2.0 的完全未使用付费充值包，可在支付成功后 14 个自然日内申请退款。",
            "付费充值包只要发生过使用，就不适用完全未使用退款流程。",
            "赠送额度不可折算现金，退款金额不包含赠送额度。",
            "超过 14 个自然日不适用本页自助申请规则，可以提交账单工单说明情况，但不保证批准。",
        ], "自然日包括周末；规则不适用于渠道购买，也不代表任何真实法律或合同。")
    doc("g12-refund", 3, "EchoMind Demo 退款审核与到账｜测试 v2.0", "test-v2.0",
        "测试 v2.0 已提交的付费充值包退款申请；以下时效从审核通过后计算。", [
            "退款审核通过后，款项沿原支付渠道退回，预计 3 至 7 个工作日到账。",
            "预计到账时间不从提交申请时开始计算，待审核状态不代表已退款。",
            "退款进度应在订单详情查看，超过预计到账时间后联系账单支持并提供订单编号。",
            "账单支持不会要求用户提供支付密码、银行卡验证码或远程控制设备才能办理退款。",
        ], "公开文档不能确认某一笔退款的实际到账状态；不保证具体时刻入账。")
    case("g12-refund", "single_hop", "测试 v1.0 完全未使用的付费充值包，付款后多少个自然日内能申请退款？", [(1, 1)])
    case("g12-refund", "single_hop", "测试 v2.0 充值包只用了一点点，还能走完全未使用的退款流程吗？", [(2, 2)])
    case("g12-refund", "single_hop", "测试 v2.0 退款审核通过后走哪条渠道，预计多少个工作日到账？", [(3, 1)])
    case("g12-refund", "single_hop", "测试 v2.0 办理退款时客服让我提供支付密码和银行卡验证码，这符合规则吗？", [(3, 4)])
    case("g12-refund", "multi_hop", "测试 v2.0 未使用充值包付款第十个自然日申请，是否还在窗口内？预计到账的三到七个工作日从申请时算吗？", [(2, 1), (3, 1), (3, 2)])
    case("g12-refund", "multi_turn", "那超出预计时间还没到，我应该去哪里查、联系谁，准备什么？", [(3, 3)],
         [{"role": "user", "content": "EchoMind Demo 测试 v2.0 的充值退款已经审核通过。"},
          {"role": "assistant", "content": "预计沿原支付渠道在 3 至 7 个工作日内到账。"}], ["context_required"])
    case("g12-refund", "unanswerable", "你确认我昨天的退款今天已经进银行卡了没有，直接给出实际到账状态。", [], tags=["missing_payment_tool"])
    case("g12-refund", "version_specific", "完全未使用充值包的退款申请窗口，在测试 v1.0 和 v2.0 分别是支付后多少个自然日？", [(1, 1), (2, 1)], tags=["version_conflict"])

    # Append after the original 96 cases to keep their identifiers stable.
    # Both versions are indexed, and no active version can be inferred from these
    # queries or their empty history. The desired action is to request the version,
    # not silently assume the latest policy or choose one conflicting value.
    clarification_queries = [
        ("g01-login", "我想知道自己账号连续输错多少次密码会锁定，以及会锁多久。"),
        ("g02-api", "我们的标准工作区 API 每分钟限额到底是多少次？请给适用于我们的具体值。"),
        ("g03-plan", "我今天提交月付套餐升级，适用于我的生效时间是什么时候？"),
        ("g04-sync", "我们连接的公开帮助中心知识源，每隔多少小时会自动同步一次？"),
        ("g05-recovery", "刚收到密码重置邮件，这个链接自发送起多久失效？"),
        ("g06-members", "我刚发出去的成员邮件邀请链接，具体能保留多久？"),
        ("g07-orders", "我的标准演示设备订单已付款且库存充足，适用的出库承诺是几个工作日？"),
        ("g08-support", "我们用的是标准工作区，人工聊天工作日具体几点结束服务？"),
        ("g09-webhook", "我们的 Webhook 首次投递失败后，最多还会自动重试几次？"),
        ("g10-upload", "我在控制台上传知识文件，适用于我们工作区的单文件大小上限是多少 MiB？"),
        ("g11-invoice", "我的测试发票申请已审核通过，按我们正在使用的规则，预计几个工作日生成？"),
        ("g12-refund", "控制台直接买的付费充值包完全没使用过，适用于我的退款申请窗口是支付后多少个自然日？"),
    ]
    for group_id, query in clarification_queries:
        case(group_id, "clarification", query, [], tags=["missing_version", "version_conflict"])


def validate() -> None:
    if len(DOCUMENTS) != 36 or len(CASES) != 108:
        raise ValueError("Expected 36 documents and 108 RAG cases")
    if len({x["id"] for x in DOCUMENTS}) != len(DOCUMENTS):
        raise ValueError("Duplicate document ID")
    if len({x["title"] for x in DOCUMENTS}) != len(DOCUMENTS):
        raise ValueError("Duplicate document title")
    if len({x["id"] for x in CASES}) != len(CASES):
        raise ValueError("Duplicate case ID")
    sources = {item["id"]: item for item in DOCUMENTS}
    facts = {fact["id"]: (source, fact) for source in DOCUMENTS for fact in source["facts"]}
    expected_categories = Counter({"single_hop": 4, "multi_hop": 1, "multi_turn": 1,
                                   "unanswerable": 1, "version_specific": 1,
                                   "clarification": 1})
    for group_id, (split, _) in _groups.items():
        group_docs = [item for item in DOCUMENTS if item["group_id"] == group_id]
        group_cases = [item for item in CASES if item["group_id"] == group_id]
        if len(group_docs) != 3 or Counter(item["category"] for item in group_cases) != expected_categories:
            raise ValueError(f"Unexpected group structure: {group_id}")
        if any(item["split"] != split for item in group_docs + group_cases):
            raise ValueError(f"Source group crosses split: {group_id}")
    for source in DOCUMENTS:
        for fact in source["facts"]:
            if fact["text"] not in source["content"]:
                raise ValueError(f"Fact is not verbatim in evidence: {fact['id']}")
    for item in CASES:
        if item["category"] in {"unanswerable", "clarification"}:
            expected = "refuse" if item["category"] == "unanswerable" else "clarify"
            if item["expected_behavior"] != expected or item["relevant_document_ids"] or item["required_facts"]:
                raise ValueError(f"Invalid non-answer case: {item['id']}")
        elif not item["required_facts"] or not item["relevant_document_ids"]:
            raise ValueError(f"Missing answer evidence: {item['id']}")
        if (item["category"] == "multi_turn") != bool(item["history"]):
            raise ValueError(f"Unexpected conversation history: {item['id']}")
        referenced = set()
        for required in item["required_facts"]:
            source, original_fact = facts[required["id"]]
            if required["text"] != original_fact["text"]:
                raise ValueError(f"Fact text mismatch: {item['id']}")
            if required["evidence_document_ids"] != [source["id"]]:
                raise ValueError(f"Incorrect fact evidence: {item['id']}")
            referenced.add(source["id"])
        if referenced != set(item["relevant_document_ids"]):
            raise ValueError(f"Relevance labels differ from fact evidence: {item['id']}")
        for source_id in item["relevant_document_ids"]:
            source = sources[source_id]
            if (source["group_id"], source["split"]) != (item["group_id"], item["split"]):
                raise ValueError(f"Case references another group or split: {item['id']}")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def main() -> None:
    DOCUMENTS.clear()
    CASES.clear()
    _doc_map.clear()
    _groups.clear()
    define_dataset()
    validate()
    write_jsonl(ROOT / "data" / "corpus.jsonl", DOCUMENTS)
    write_jsonl(ROOT / "data" / "rag_cases.jsonl", CASES)
    summary = {
        "documents": len(DOCUMENTS),
        "cases": len(CASES),
        "document_splits": dict(Counter(item["split"] for item in DOCUMENTS)),
        "case_splits": dict(Counter(item["split"] for item in CASES)),
        "case_categories": dict(Counter(item["category"] for item in CASES)),
        "case_domains": dict(Counter(item["tags"][0] for item in CASES)),
        "human_review": "pending",
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
