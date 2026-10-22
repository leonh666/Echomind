"""Regression checks for realistic scope, fair grading and review provenance."""
from copy import deepcopy
import contextlib
import io
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import DATA, load_cases, read_jsonl
from metrics import evaluate
from review_evidence import check_review_evidence, fingerprint
from run_benchmark import main as run_main


class ScenarioReviewTests(unittest.TestCase):
    def setUp(self):
        self.cases = {case["id"]: case for case in load_cases()}

    def test_fictional_product_is_not_a_claim_about_echomind_runtime(self):
        docs = read_jsonl(DATA / "corpus.jsonl")
        self.assertEqual(len(docs), 36)
        self.assertTrue(all(doc["reference_product"].startswith("SupportLab") for doc in docs))
        self.assertTrue(all("非 EchoMind 功能说明" in doc["content"] for doc in docs))
        self.assertTrue(all(case["human_review"] == "pending" for case in self.cases.values()))

    def test_versioned_import_preserves_source_metadata_without_gold_labels(self):
        payload = json.loads((ROOT / "exports" / "knowledge_add_versioned.json").read_text(encoding="utf-8"))
        docs = read_jsonl(DATA / "corpus.jsonl")
        self.assertEqual(len(payload["documents"]), len(docs))
        for exported, original in zip(payload["documents"], docs):
            self.assertEqual(set(exported), {"document_id", "title", "content", "product_version", "knowledge_base_id"})
            self.assertEqual(exported["product_version"], original["version"])
            self.assertEqual(exported["document_id"], original["id"])

    def test_contracts_have_specific_native_preconditions_and_outcome_oracles(self):
        contracts = [case for case in self.cases.values() if case["kind"] == "contract"]
        self.assertEqual(len(contracts), 48)
        for case in contracts:
            self.assertEqual(case["target_system"], "echomind_native_python")
            self.assertEqual(case["execution_status"], "not_run")
            self.assertGreater(len(case["preconditions"]), 1)
            self.assertTrue(case["outcome_oracle"])
            self.assertFalse(any(word in case["query"] for word in ("Dify", "checkpoint", "图版本", "租约")))
        self.assertIn("customer_a", self.cases["security-005"]["preconditions"][1])
        self.assertIn("确认先提交", self.cases["security-006"]["outcome_oracle"])

    def test_deadline_question_excludes_used_and_replaced_reset_links(self):
        question = self.cases["rag-033"]["query"]
        self.assertIn("没使用过", question)
        self.assertIn("没重新申请", question)

    def test_slow_webhook_answer_requires_response_deadline_evidence(self):
        facts = {fact["id"] for fact in self.cases["rag-069"]["required_facts"]}
        self.assertIn("g09-webhook-d2-f2", facts)
        self.assertIn("g09-webhook-d3-f3", facts)

    def test_conditional_answer_is_accepted_only_after_all_semantic_checks(self):
        case = self.cases["rag-097"]
        keys = case["alternative_behavior_requirements"]["answer"]
        prediction = {"case_id": case["id"], "status": "ok", "behavior": "answer",
                      "behavior_checks": {key: True for key in keys}}
        report = evaluate([case], [prediction])
        self.assertEqual(report["behavior"]["strict_accuracy"], 0)  # Exact label remains diagnostic.
        self.assertEqual(report["behavior"]["acceptable_response"]["strict_accuracy"], 1)
        for value in (False, None):
            changed = deepcopy(prediction)
            changed["behavior_checks"][keys[0]] = value
            score = evaluate([case], [changed])["behavior"]["acceptable_response"]
            self.assertEqual(score["accepted_cases"], 0)
            self.assertEqual(score["assessment_coverage"], 1 if value is False else 0)

    def test_missing_alternative_checks_do_not_receive_credit(self):
        case = self.cases["rag-097"]
        prediction = {"case_id": case["id"], "status": "ok", "behavior": "answer"}
        score = evaluate([case], [prediction])["behavior"]["acceptable_response"]
        self.assertEqual(score["strict_accuracy"], 0)
        self.assertIsNone(score["observed_accuracy"])
        self.assertEqual(score["unassessed_alternative_cases"], 1)

    def test_clarification_still_counts_without_conditional_answer_checks(self):
        case = self.cases["rag-097"]
        score = evaluate([case], [{"case_id": case["id"], "status": "ok", "behavior": "clarify"}])
        self.assertEqual(score["behavior"]["acceptable_response"]["strict_accuracy"], 1)

    def test_unknown_or_unguarded_alternative_is_rejected(self):
        case = self.cases["rag-097"]
        prediction = {"case_id": case["id"], "status": "ok", "behavior": "answer", "behavior_checks": {"made_up": True}}
        with self.assertRaisesRegex(ValueError, "unknown behavior"):
            evaluate([case], [prediction])
        changed = deepcopy(case)
        changed["alternative_behavior_requirements"] = {}
        with self.assertRaisesRegex(ValueError, "explicit grading requirements"):
            evaluate([changed], [])

    def test_all_clarifications_have_two_verified_conditional_sources(self):
        facts = {fact["id"]: fact["text"] for doc in read_jsonl(DATA / "corpus.jsonl") for fact in doc["facts"]}
        questions = [case for case in self.cases.values() if case.get("category") == "clarification"]
        self.assertEqual(len(questions), 12)
        for case in questions:
            self.assertEqual(len(case["conditional_reference_facts"]), 2)
            for fact in case["conditional_reference_facts"]:
                self.assertEqual(fact["text"], facts[fact["id"]])

    def test_review_is_not_forged_human_approval_and_stale_content_is_detected(self):
        row = {"id": "one", "query": "original"}
        evidence = {"record_id": "one", "review_type": "ai_scenario_review", "human_approved": False,
                    "before_sha256": "baseline", "after_sha256": fingerprint(row), "rationale": "scenario checked"}
        self.assertEqual(check_review_evidence([row], [evidence]), [])
        self.assertTrue(check_review_evidence([{**row, "query": "modified"}], [evidence]))
        self.assertTrue(check_review_evidence([row], [{**evidence, "human_approved": True}]))

    def test_legacy_logistics_is_separate_and_known_router_gap_is_not_hidden(self):
        legacy = [case for case in self.cases.values() if case.get("evaluation_track") == "legacy_taxonomy_compatibility"]
        self.assertEqual(len(legacy), 7)
        self.assertTrue(all(case["expected_intent"] == "logistics" for case in legacy))
        account = self.cases["intent-085"]
        self.assertEqual(account["expected_agents"], ["general"])
        self.assertIn("billing", account["known_implementation_gap"])

    def test_runner_track_selection_excludes_legacy_without_real_api_calls(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            output, report = Path(folder) / "prediction.jsonl", Path(folder) / "metrics.json"
            argv = ["run_benchmark.py", "api", "--mode", "chat", "--split", "test",
                    "--track", "support_intent_classification", "--limit", "1",
                    "--base-url", "http://example.invalid", "--output", str(output), "--report", str(report)]
            with patch("sys.argv", argv), patch("run_benchmark.http_json", return_value={
                    "conv_id": "fake", "response": "测试替身回答", "intent": "query", "agent_types": ["general"]}) as api:
                with contextlib.redirect_stdout(io.StringIO()):
                    run_main()
            measured = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(measured["dataset"]["total_cases"], 75)
            self.assertEqual(measured["run_metadata"]["selected_track"], "support_intent_classification")
            self.assertEqual(api.call_count, 1)


if __name__ == "__main__":
    unittest.main()
