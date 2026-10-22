"""Persisted answer reviews and deterministic, organization-scoped diagnostics.

Only the chat server can write answer snapshots. Reports use SQL-complete counts,
not a vector search sample or model-invented statistics. No model/provider calls.
"""
from collections import Counter
import json
import math
import re
import uuid

from business.store import BusinessError, MANAGERS, _identifier, _json
from core.evidence import public_sources


SCHEMA = """
CREATE TABLE IF NOT EXISTS answer_snapshots (
 request_id TEXT PRIMARY KEY REFERENCES request_ownership(request_id),
 organization_id TEXT NOT NULL REFERENCES organizations(id), owner_id TEXT NOT NULL,
 question TEXT NOT NULL, answer_json TEXT NOT NULL, created_at REAL NOT NULL,
 FOREIGN KEY(owner_id,organization_id) REFERENCES memberships(user_id,organization_id)
);
CREATE TABLE IF NOT EXISTS answer_feedback (
 request_id TEXT PRIMARY KEY REFERENCES answer_snapshots(request_id),
 revision INTEGER NOT NULL, reviewer_id TEXT NOT NULL REFERENCES users(id),
 payload_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS answer_feedback_history (
 request_id TEXT NOT NULL REFERENCES answer_snapshots(request_id), revision INTEGER NOT NULL,
 reviewer_id TEXT NOT NULL REFERENCES users(id), payload_json TEXT NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(request_id,revision)
);
CREATE TABLE IF NOT EXISTS diagnostic_reports (
 id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
 created_by TEXT NOT NULL REFERENCES users(id), start_at REAL NOT NULL, end_at REAL NOT NULL,
 snapshot_json TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS answers_org_time ON answer_snapshots(organization_id,created_at,request_id);
CREATE INDEX IF NOT EXISTS diagnostics_org_time ON diagnostic_reports(organization_id,created_at,id);
"""

DECISIONS = {"accepted", "edited", "discarded"}
REASONS = {"none", "incorrect", "incomplete", "irrelevant", "outdated", "missing_knowledge", "unsafe", "other"}
OUTCOMES = {"unknown", "resolved", "unresolved"}
REASON_LABELS = {"incorrect": "事实或政策有误", "incomplete": "回答不完整", "irrelevant": "答非所问",
                 "outdated": "资料过期", "missing_knowledge": "缺少知识", "unsafe": "不当建议", "other": "其他问题"}


def ratio(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "value": round(numerator / denominator, 6) if denominator else None}


class InsightService:
    def __init__(self, store):
        self.store = store

    def _answer(self, conn, actor, request_id):
        role = self.store._actor(conn, actor)
        row = conn.execute("SELECT * FROM answer_snapshots WHERE request_id=? AND organization_id=?",
                           (request_id, actor.organization_id)).fetchone()
        if row is None or (row["owner_id"] != actor.user_id and role not in MANAGERS):
            raise BusinessError("not_found", "Answer not found", 404)
        return row

    def _admin(self, conn, actor):
        if self.store._actor(conn, actor) != "admin":
            raise BusinessError("forbidden", "Organization administrator required", 403)

    @staticmethod
    def _feedback(row):
        return {**json.loads(row["payload_json"]), "revision": row["revision"],
                "reviewer_id": row["reviewer_id"], "updated_at": row["updated_at"]} if row else None

    def _view(self, conn, row):
        feedback = conn.execute("SELECT * FROM answer_feedback WHERE request_id=?", (row["request_id"],)).fetchone()
        return {"request_id": row["request_id"], "question": row["question"],
                "created_at": row["created_at"], **json.loads(row["answer_json"]),
                "feedback": self._feedback(feedback)}

    def record_answer(self, actor, request_id, question, answer):
        """Trusted internal operation: identity and evidence never come from feedback input."""
        _identifier(question, "question", 10000)
        content = answer.get("response")
        _identifier(content, "response", 100000)
        snapshot = {"response": content, "sources": public_sources(answer.get("sources", [])),
                    "intent": str(answer.get("intent") or "other")[:80],
                    "intent_group": str(answer.get("intent_group") or "other")[:80],
                    "escalated": bool(answer.get("escalated")),
                    "warnings": [str(item)[:1000] for item in answer.get("warnings", [])[:30]],
                    "duration_ms": max(0, float(answer.get("duration_ms", 0)))}
        serialized = _json(snapshot)
        with self.store._transaction() as conn:
            role = self.store._actor(conn, actor)
            binding = conn.execute("SELECT * FROM request_ownership WHERE request_id=? AND organization_id=?",
                                   (request_id, actor.organization_id)).fetchone()
            if binding is None or (binding["owner_id"] != actor.user_id and role not in MANAGERS):
                raise BusinessError("not_found", "Request not found", 404)
            previous = conn.execute("SELECT * FROM answer_snapshots WHERE request_id=?", (request_id,)).fetchone()
            if previous:
                if previous["question"] != question or previous["answer_json"] != serialized:
                    raise BusinessError("answer_conflict", "Answer snapshots are immutable", 409)
                return self._view(conn, previous)
            conn.execute("INSERT INTO answer_snapshots VALUES(?,?,?,?,?,?)",
                         (request_id, actor.organization_id, binding["owner_id"], question, serialized, self.store.now()))
            return self._view(conn, conn.execute("SELECT * FROM answer_snapshots WHERE request_id=?", (request_id,)).fetchone())

    def get_answer(self, actor, request_id):
        with self.store._transaction() as conn:
            return self._view(conn, self._answer(conn, actor, request_id))

    def list_answers(self, actor, limit=20, offset=0):
        self._page(limit, offset)
        with self.store._transaction() as conn:
            role = self.store._actor(conn, actor)
            clause, params = "organization_id=?", [actor.organization_id]
            if role not in MANAGERS:
                clause += " AND owner_id=?"
                params.append(actor.user_id)
            total = conn.execute(f"SELECT COUNT(*) FROM answer_snapshots WHERE {clause}", params).fetchone()[0]
            rows = conn.execute(f"SELECT * FROM answer_snapshots WHERE {clause} ORDER BY created_at DESC,request_id DESC LIMIT ? OFFSET ?",
                                (*params, limit, offset)).fetchall()
            return {"items": [self._view(conn, row) for row in rows], "total": total, "limit": limit, "offset": offset}

    def save_feedback(self, actor, request_id, payload, revision=0):
        if type(revision) is not int or revision < 0:
            raise BusinessError("invalid_feedback", "A nonnegative revision is required")
        if not isinstance(payload, dict) or set(payload) - {"decision", "reason", "comment", "final_answer", "outcome"}:
            raise BusinessError("invalid_feedback", "Unknown feedback fields")
        decision, reason, outcome = payload.get("decision"), payload.get("reason", "none"), payload.get("outcome", "unknown")
        if not all(isinstance(value, str) for value in (decision, reason, outcome)) or decision not in DECISIONS or reason not in REASONS or outcome not in OUTCOMES:
            raise BusinessError("invalid_feedback", "Unknown decision, reason or outcome")
        comment, final_answer = payload.get("comment", ""), payload.get("final_answer", "")
        if not isinstance(comment, str) or len(comment) > 2000 or not isinstance(final_answer, str) or len(final_answer) > 100000:
            raise BusinessError("invalid_feedback", "Feedback text exceeds its permitted length")
        if decision != "accepted" and reason == "none":
            raise BusinessError("invalid_feedback", "Editing or discarding requires a reason")
        if decision == "accepted" and reason != "none":
            raise BusinessError("invalid_feedback", "Unchanged acceptance cannot carry an error reason")
        if reason == "other" and not comment.strip():
            raise BusinessError("invalid_feedback", "Describe the other issue")
        with self.store._transaction() as conn:
            answer = self._answer(conn, actor, request_id)
            original = json.loads(answer["answer_json"])["response"]
            if decision == "edited" and (not final_answer.strip() or final_answer.strip() == original.strip()):
                raise BusinessError("invalid_feedback", "An edited answer must contain a substantive text change")
            if decision == "accepted" and final_answer.strip() not in {"", original.strip()}:
                raise BusinessError("invalid_feedback", "Use edited to record changes")
            if decision == "discarded" and final_answer.strip():
                raise BusinessError("invalid_feedback", "Discarded suggestions cannot carry an adopted answer")
            value = {"decision": decision, "reason": reason, "comment": comment.strip(), "outcome": outcome,
                     "final_answer": original if decision == "accepted" else final_answer.strip()}
            existing = conn.execute("SELECT * FROM answer_feedback WHERE request_id=?", (request_id,)).fetchone()
            current_revision = existing["revision"] if existing else 0
            if revision != current_revision:
                # Network retry of the same successful write is safe; changed edits conflict.
                if existing and revision == current_revision - 1 and existing["reviewer_id"] == actor.user_id and existing["payload_json"] == _json(value):
                    return self._feedback(existing)
                raise BusinessError("feedback_conflict", "Feedback changed; reload it before saving", 409)
            timestamp, next_revision = self.store.now(), current_revision + 1
            serialized = _json(value)
            conn.execute("INSERT INTO answer_feedback VALUES(?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET revision=excluded.revision,reviewer_id=excluded.reviewer_id,payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                         (request_id, next_revision, actor.user_id, serialized, timestamp, timestamp))
            conn.execute("INSERT INTO answer_feedback_history VALUES(?,?,?,?,?)",
                         (request_id, next_revision, actor.user_id, serialized, timestamp))
            self.store._event(conn, actor, "answer.feedback", request_id=request_id, revision=next_revision,
                              decision=decision, reason=reason, outcome=outcome)
            return self._feedback(conn.execute("SELECT * FROM answer_feedback WHERE request_id=?", (request_id,)).fetchone())

    @staticmethod
    def _page(limit, offset):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise BusinessError("invalid_pagination", "limit must be 1..100 and offset nonnegative")

    def create_report(self, actor, days=7, end_at=None):
        if type(days) is not int or not 1 <= days <= 90:
            raise BusinessError("invalid_window", "Use 1..90 days and a finite past/current end time")
        with self.store._transaction() as conn:
            self._admin(conn, actor)
            timestamp = self.store.now()
            end_at = timestamp if end_at is None else end_at
            if type(end_at) not in (float, int) or not math.isfinite(end_at) or end_at > timestamp or end_at <= 0:
                raise BusinessError("invalid_window", "Use 1..90 days and a finite past/current end time")
            start_at = end_at - days * 86400
            # One transaction freezes answers and feedback together. No top-K sampling.
            rows = conn.execute("SELECT a.*, f.payload_json AS feedback_json, f.revision AS feedback_revision FROM answer_snapshots a LEFT JOIN answer_feedback f ON f.request_id=a.request_id WHERE a.organization_id=? AND a.created_at>=? AND a.created_at<? ORDER BY a.created_at,a.request_id",
                                (actor.organization_id, start_at, end_at)).fetchall()
            report = self._aggregate(rows, timestamp, start_at, end_at)
            report.update(id=uuid.uuid4().hex, organization_id=actor.organization_id,
                          created_by=actor.user_id, created_at=timestamp)
            conn.execute("INSERT INTO diagnostic_reports VALUES(?,?,?,?,?,?,?)",
                         (report["id"], actor.organization_id, actor.user_id, start_at, end_at, _json(report), timestamp))
            self.store._event(conn, actor, "diagnostic.created", report_id=report["id"], answer_count=len(rows))
            return report

    @staticmethod
    def _aggregate(rows, timestamp, start_at, end_at):
        decisions, reasons, outcomes, intents, questions, sources = (Counter() for _ in range(6))
        issues, durations, followups = [], [], []
        with_evidence = escalated = warnings = reviewed = 0
        for row in rows:
            answer = json.loads(row["answer_json"])
            feedback = json.loads(row["feedback_json"]) if row["feedback_json"] else None
            reviewed += bool(feedback)
            with_evidence += bool(answer["sources"])
            escalated += answer["escalated"]
            warnings += bool(answer["warnings"])
            intents[answer["intent_group"]] += 1
            normalized = re.sub(r"\s+", " ", row["question"]).strip()
            questions[normalized] += 1
            durations.append(answer["duration_ms"])
            seen_sources = set()
            for source in answer["sources"]:
                key = (source.get("document_id", ""), source.get("document_version", ""), source.get("title", ""))
                if key[0] and key not in seen_sources:
                    sources[key] += 1
                    seen_sources.add(key)
            if feedback:
                decisions[feedback["decision"]] += 1
                outcomes[feedback["outcome"]] += 1
                if feedback["reason"] != "none":
                    reasons[feedback["reason"]] += 1
                if feedback["reason"] != "none" or feedback["outcome"] == "unresolved":
                    issues.append({"request_id": row["request_id"], "question": row["question"][:300],
                                   "reason": feedback["reason"], "decision": feedback["decision"],
                                   "outcome": feedback["outcome"], "feedback_revision": row["feedback_revision"]})
            elif not answer["sources"] or answer["warnings"] or answer["escalated"]:
                followups.append({"request_id": row["request_id"], "question": row["question"][:300],
                                  "reason": "unreviewed", "has_evidence": bool(answer["sources"])})
        total = len(rows)
        known_outcomes = outcomes["resolved"] + outcomes["unresolved"]
        ordered = sorted(durations)
        recommendations = []
        if not total:
            recommendations.append("本时间段没有已保存回答，请先收集实际咨询；不能据此判断效果。")
        if total > reviewed:
            recommendations.append(f"还有 {total - reviewed} 条回答未反馈，优先复核检索异常、无来源和转人工案例。")
        for reason, count in reasons.most_common():
            recommendations.append(f"有 {count} 条反馈标记“{REASON_LABELS[reason]}”，请回看原回答与证据后修订资料或处理流程。")
        if reviewed and not known_outcomes:
            recommendations.append("已有采纳反馈，但解决结果均未知，请补录实际处理结果。")
        return {
            "schema_version": "diagnostic-v1", "method": "deterministic_python",
            "window": {"start_at": start_at, "end_at": end_at, "bounds": "[start,end)", "timezone": "UTC"},
            "feedback_as_of": timestamp,
            "counts": {"answers": total, "reviewed": reviewed, "unreviewed": total - reviewed,
                       "accepted": decisions["accepted"], "edited": decisions["edited"], "discarded": decisions["discarded"],
                       "with_evidence": with_evidence, "without_evidence": total - with_evidence,
                       "escalated": escalated, "with_warnings": warnings,
                       "resolved": outcomes["resolved"], "unresolved": outcomes["unresolved"],
                       "outcome_unknown": total - known_outcomes},
            "metrics": {"review_coverage": ratio(reviewed, total), "acceptance_rate": ratio(decisions["accepted"], reviewed),
                        "edit_rate": ratio(decisions["edited"], reviewed), "discard_rate": ratio(decisions["discarded"], reviewed),
                        "resolution_rate": ratio(outcomes["resolved"], known_outcomes)},
            "duration_ms": {"count": total, "mean": round(sum(durations) / total, 1) if total else None,
                            "p95": ordered[math.ceil(total * .95) - 1] if total else None},
            "intent_distribution": dict(intents), "issue_distribution": dict(reasons),
            "frequent_questions": [{"question": q[:300], "count": n} for q, n in questions.most_common(10)],
            "source_usage": [{"document_id": key[0], "document_version": key[1], "title": key[2], "answer_count": n}
                             for key, n in sources.most_common(10)],
            "issue_examples": issues[:20], "review_queue": followups[:20], "recommendations": recommendations,
            "limitations": [
                "仅统计本时间窗内成功持久化的回答，不包含失败、超时或取消请求，不能当作系统成功率。",
                "每条回答只计最新一版反馈；反馈截至报告生成时刻，后续修改不会改变已保存报告。",
                "采纳、修改与问题解决为人工反馈，未经独立正确性核验；未反馈或未确认解决均为未知。",
                "无检索来源不等于知识缺失，命中来源不等于回答正确；来源使用次数不代表引用准确率。",
                "高频问题按原文空白归一后精确计数，未做语义聚类；示例最多20条、问题和来源各10条，汇总计数覆盖全部样本。",
                "响应耗时含记忆读取、检索和生成，不含人工审核与发送时间；小样本仅供发现问题。",
            ],
        }

    def list_reports(self, actor, limit=20, offset=0):
        self._page(limit, offset)
        with self.store._transaction() as conn:
            self._admin(conn, actor)
            total = conn.execute("SELECT COUNT(*) FROM diagnostic_reports WHERE organization_id=?", (actor.organization_id,)).fetchone()[0]
            rows = conn.execute("SELECT id,start_at,end_at,created_at,snapshot_json FROM diagnostic_reports WHERE organization_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                                (actor.organization_id, limit, offset)).fetchall()
            return {"items": [{"id": row["id"], "start_at": row["start_at"], "end_at": row["end_at"],
                               "created_at": row["created_at"], "counts": json.loads(row["snapshot_json"])["counts"]} for row in rows],
                    "total": total, "limit": limit, "offset": offset}

    def get_report(self, actor, report_id):
        with self.store._transaction() as conn:
            self._admin(conn, actor)
            row = conn.execute("SELECT snapshot_json FROM diagnostic_reports WHERE id=? AND organization_id=?",
                               (report_id, actor.organization_id)).fetchone()
            if not row:
                raise BusinessError("not_found", "Report not found", 404)
            return json.loads(row["snapshot_json"])
