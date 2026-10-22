"""Answer provenance, review races, report denominators and tenant boundaries."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from business.insights import InsightService
from business.store import BusinessError, BusinessStore, Principal


@pytest.fixture
def insights(tmp_path):
    clock = [1000.0]
    store = BusinessStore(tmp_path / "insights.sqlite3", now=lambda: clock[0])
    with store._transaction() as conn:
        for org in ("a", "b"):
            conn.execute("INSERT INTO organizations VALUES(?,?,?)", (org, org, clock[0]))
        for uid, org, role in (("alice", "a", "customer"), ("bob", "a", "customer"),
                               ("admin", "a", "admin"), ("support", "a", "support"), ("other", "b", "admin")):
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)", (uid, uid + "@example.invalid", uid, "hash", "00", 1, clock[0]))
            conn.execute("INSERT INTO memberships VALUES(?,?,?)", (uid, org, role))
    return InsightService(store), clock, Principal("alice", "a", "customer"), Principal("admin", "a", "admin")


def record(service, actor, request_id="r1", **overrides):
    service.store.claim_conversation(actor, "c-" + request_id)
    service.store.bind_request(actor, request_id, "c-" + request_id)
    return service.record_answer(actor, request_id, "这个商品怎么清洗？", {
        "response": "请手洗。", "intent_group": "general", "duration_ms": 120,
        "sources": [{"document_id": "d1", "chunk_id": "c1", "title": "说明", "content": "只能手洗。",
                     "document_version": "v1", "source_url": "https://example.invalid/manual",
                     "organization_id": "private", "internal_secret": "must-not-leak"}], **overrides})


def review(**overrides):
    return {"decision": "accepted", "reason": "none", "outcome": "unknown", **overrides}


def test_snapshot_is_authoritative_bounded_and_survives_restart(insights):
    service, _, alice, _ = insights
    answer = record(service, alice)
    assert "organization_id" not in answer["sources"][0]
    assert "internal_secret" not in answer["sources"][0]
    restarted = InsightService(BusinessStore(service.store.db_path))
    assert restarted.get_answer(alice, "r1")["sources"] == answer["sources"]
    with pytest.raises(BusinessError, match="immutable"):
        service.record_answer(alice, "r1", "forged", {"response": "forged"})
    with pytest.raises(BusinessError, match="Request not found"):
        service.record_answer(alice, "unbound", "q", {"response": "a"})
    spaced = record(service, alice, "spaced", response="  原始回复\n")
    assert spaced["response"] == "  原始回复\n"


def test_evidence_roundtrip_preserves_truncation_and_blocks_unsafe_links():
    from core.evidence import public_sources
    source = {"document_id": "d1", "content": "证据" * 4000, "internal_secret": "private",
              "source_url": "javascript:alert(1)", "score": float("nan"), "distance": float("inf")}
    first = public_sources([source])
    assert public_sources(first) == first
    assert first[0]["content_truncated"] is True and len(first[0]["content"]) == 6000
    assert "internal_secret" not in first[0] and "score" not in first[0] and "distance" not in first[0]
    assert first[0]["source_url"] == ""
    for unsafe in ("file:///secret", "https://user:password@example.invalid", "https://example.invalid/\nsecret"):
        assert public_sources([{**source, "source_url": unsafe}])[0]["source_url"] == ""


def test_customer_and_org_visibility_is_rechecked_in_store(insights):
    service, _, alice, admin = insights
    record(service, alice)
    for actor in (Principal("bob", "a", "customer"), Principal("other", "b", "admin")):
        assert service.list_answers(actor)["total"] == 0
        with pytest.raises(BusinessError) as error:
            service.save_feedback(actor, "r1", review())
        assert error.value.http_status == 404
    assert service.get_answer(admin, "r1")["response"] == "请手洗。"
    service.store.assign_membership("admin", "a", "customer")
    with pytest.raises(BusinessError) as error:
        service.list_reports(admin)
    assert error.value.http_status == 403


def test_feedback_preserves_original_and_all_revisions_with_retry(insights):
    service, _, alice, admin = insights
    record(service, alice)
    payload = review(decision="edited", reason="incomplete", final_answer="请手洗，并自然晾干。")
    first = service.save_feedback(alice, "r1", payload)
    assert service.save_feedback(alice, "r1", payload) == first
    service.save_feedback(admin, "r1", review(outcome="resolved"), revision=1)
    answer = service.get_answer(alice, "r1")
    assert answer["response"] == "请手洗。"
    assert answer["feedback"]["revision"] == 2
    with service.store._transaction() as conn:
        history = conn.execute("SELECT payload_json FROM answer_feedback_history ORDER BY revision").fetchall()
    assert len(history) == 2
    assert json.loads(history[0][0])["final_answer"] == payload["final_answer"]
    with pytest.raises(BusinessError) as error:
        service.save_feedback(alice, "r1", review(decision="discarded", reason="incorrect"), revision=1)
    assert error.value.code == "feedback_conflict"


@pytest.mark.parametrize("payload", [
    review(decision="edited", reason="incomplete", final_answer="请手洗。"),
    review(decision="edited", reason="incomplete"),
    review(decision="discarded"),
    review(decision="accepted", reason="incorrect"),
    review(final_answer="完全不同"),
    review(decision="discarded", reason="incorrect", final_answer="不能采纳"),
    review(decision="discarded", reason="other"),
    review(outcome="silent"), review(question="forged"), review(comment="x" * 2001),
])
def test_invalid_or_spoofed_feedback_rejected(insights, payload):
    service, _, alice, _ = insights
    record(service, alice)
    with pytest.raises(BusinessError):
        service.save_feedback(alice, "r1", payload)


def test_concurrent_different_reviews_do_not_overwrite_each_other(insights):
    service, _, alice, _ = insights
    record(service, alice)
    def save(outcome):
        try:
            return service.save_feedback(alice, "r1", review(outcome=outcome))
        except BusinessError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ["resolved", "unresolved"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "feedback_conflict" in results


def test_report_counts_all_answers_latest_reviews_and_unknown_outcomes(insights):
    service, clock, alice, admin = insights
    record(service, alice, "r1")
    record(service, alice, "r2", sources=[])
    record(service, alice, "r3", sources=[])
    service.save_feedback(alice, "r1", review(outcome="resolved"))
    service.save_feedback(alice, "r2", review(decision="edited", reason="missing_knowledge", final_answer="已核实新规则。", outcome="unresolved"))
    # Latest review supersedes the original, without increasing sample count.
    service.save_feedback(alice, "r1", review(decision="discarded", reason="incorrect"), revision=1)
    other = Principal("other", "b", "admin")
    record(service, other, "foreign")
    clock[0] = 1100
    report = service.create_report(admin)
    assert report["counts"]["answers"] == 3
    assert report["counts"]["reviewed"] == 2
    assert report["counts"]["outcome_unknown"] == 2
    assert report["metrics"]["edit_rate"] == {"numerator": 1, "denominator": 2, "value": .5}
    assert report["metrics"]["resolution_rate"] == {"numerator": 0, "denominator": 1, "value": 0}
    assert report["counts"]["without_evidence"] == 2
    assert report["issue_distribution"]["missing_knowledge"] == 1
    assert "foreign" not in json.dumps(report)
    # Re-reviewing answers cannot retroactively mutate a stored report.
    service.save_feedback(alice, "r1", review(outcome="resolved"), revision=2)
    assert service.get_report(admin, report["id"]) == report
    assert service.list_reports(admin)["total"] == 1
    with pytest.raises(BusinessError) as error:
        service.get_report(other, report["id"])
    assert error.value.http_status == 404


def test_report_empty_denominators_and_half_open_window(insights):
    service, clock, alice, admin = insights
    empty = service.create_report(admin)
    assert empty["metrics"]["resolution_rate"]["value"] is None
    assert empty["metrics"]["acceptance_rate"]["value"] is None
    record(service, alice)
    assert service.create_report(admin, end_at=1000)["counts"]["answers"] == 0
    clock[0] = 1000 + 86400
    # An answer exactly on the left boundary is included.
    report = service.create_report(admin, days=1)
    assert report["counts"]["answers"] == 1
    assert report["metrics"]["resolution_rate"]["denominator"] == 0


def test_counts_are_not_limited_by_ui_pagination_and_sources_deduplicate(insights):
    service, clock, alice, admin = insights
    for index in range(25):
        record(service, alice, f"r{index}", sources=[{"document_id": "d1", "document_version": "v1", "chunk_id": f"c{n}"} for n in (1, 2)])
    assert len(service.list_answers(admin, limit=20)["items"]) == 20
    assert service.list_answers(admin, limit=20, offset=20)["total"] == 25
    clock[0] += 1
    report = service.create_report(admin)
    assert report["counts"]["answers"] == 25
    assert report["source_usage"][0]["answer_count"] == 25
    assert len(report["review_queue"]) == 0  # Has evidence, no warnings/escalation.


@pytest.mark.parametrize("days,end", [(0, None), (91, None), (7, float("nan")), (7, 2000)])
def test_report_invalid_window(insights, days, end):
    service, _, _, admin = insights
    with pytest.raises(BusinessError):
        service.create_report(admin, days, end)
