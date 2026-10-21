"""Evaluator regression tests with fake clients; no provider/network calls."""

import asyncio
import json
import math
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from evaluation.evaluator import (
    EndToEndEvaluator, IntentEvaluator, IntentTestCase, LLMJudge, QualityScores,
)


def response(text, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[{"type": "text", "text": text}])


def client_for(text=None, exception=None, stop_reason="end_turn"):
    create = AsyncMock(side_effect=exception) if exception else AsyncMock(return_value=response(text, stop_reason))
    return SimpleNamespace(messages=SimpleNamespace(create=create))


def judge_json(value=.9):
    return json.dumps({name: value for name in ("relevance", "accuracy", "completeness", "helpfulness")})


def recognized(label="refund"):
    return SimpleNamespace(intent=SimpleNamespace(value=label), confidence=.9, reasoning="fake")


class FakeJudge:
    _model = "fake-model"

    def __init__(self, values=None):
        self.judge = AsyncMock(side_effect=values) if values is not None else AsyncMock(
            return_value=QualityScores(.9, .9, .9, .9))


class JudgeTests(unittest.TestCase):
    def test_schema_accepts_boundary_numbers_and_keeps_old_call_signature(self):
        client = client_for('{"relevance":0,"accuracy":1,"completeness":0.5,"helpfulness":0.5}')
        result = asyncio.run(LLMJudge(client, "fake-model").judge("question", "response", "history"))
        self.assertFalse(result.judge_failed)
        self.assertEqual(result.overall, .5)
        self.assertEqual(result.accuracy, 1)

    def test_invalid_schema_and_nonfinite_scores_are_failures_without_fallback(self):
        valid = json.loads(judge_json())
        variants = [{}, {**valid, "extra": 1}, {key: value for key, value in valid.items() if key != "accuracy"}]
        variants += [{**valid, "accuracy": bad} for bad in (-.1, 1.1, True, "0.9", None, math.nan, math.inf)]
        for data in variants:
            with self.subTest(data=data):
                result = asyncio.run(LLMJudge(client_for(json.dumps(data)), "fake-model").judge("q", "a"))
                self.assertTrue(result.judge_failed)
                self.assertIsNotNone(result.error)
                self.assertIsNone(result.overall)
                self.assertTrue(all(getattr(result, key) is None for key in valid))

    def test_provider_failure_and_truncation_are_not_scores(self):
        clients = [client_for(exception=RuntimeError("provider unavailable")),
                   client_for(judge_json(), stop_reason="max_tokens")]
        for client in clients:
            with self.subTest(client=client):
                result = asyncio.run(LLMJudge(client, "fake-model").judge("q", "a"))
                self.assertTrue(result.judge_failed)
                self.assertIsNone(result.relevance)
                self.assertIsNone(result.overall)

    def test_reference_and_actual_evidence_reach_prompt_without_claiming_faithfulness(self):
        client = client_for(judge_json())
        result = asyncio.run(LLMJudge(client, "fake-model").judge(
            "退款多久", "三个工作日", reference_facts=[{"text": "三个工作日"}],
            actual_evidence=[{"document_id": "refund-v2", "content": "三个工作日"}]))
        prompt = client.messages.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("参考事实", prompt)
        self.assertIn("refund-v2", prompt)
        self.assertTrue(result.metadata["actual_evidence_provided"])
        self.assertFalse(result.metadata["faithfulness_evaluated"])

    def test_missing_retrieval_context_is_explicit_even_with_history_and_references(self):
        client = client_for(judge_json())
        result = asyncio.run(LLMJudge(client, "fake-model").judge("q", "a", context="history",
                                                                 reference_facts=["fact"]))
        prompt = client.messages.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("current retrieval evidence not provided", prompt)
        self.assertFalse(result.metadata["actual_evidence_provided"])
        self.assertFalse(result.metadata["faithfulness_evaluated"])

    def test_failed_or_invalid_dataclass_has_no_overall_score(self):
        self.assertIsNone(QualityScores(.5, .5, .5, .5, judge_failed=True).overall)
        self.assertIsNone(QualityScores(.5, .5, .5, math.nan).overall)
        self.assertIsNone(QualityScores(.5, .5, .5, None).overall)


class IntentTests(unittest.TestCase):
    def test_context_history_is_passed_as_history_keyword(self):
        history = [{"role": "user", "content": "我要退款"}, {"role": "assistant", "content": "请提供订单号"}]
        recognizer = SimpleNamespace(recognize=AsyncMock(return_value=recognized()))
        cases = [IntentTestCase("订单123", "refund", context={"history": history})]
        result = asyncio.run(IntentEvaluator(recognizer).evaluate(cases))
        recognizer.recognize.assert_awaited_once_with("订单123", history=history)
        self.assertEqual(result["accuracy"], 1)
        self.assertEqual(result["coverage"], 1)

    def test_partial_failures_use_strict_denominator_and_gold_supported_macro_labels(self):
        recognizer = SimpleNamespace(recognize=AsyncMock(side_effect=[recognized("refund"),
                         recognized("predicted-only"), RuntimeError("failed")]))
        cases = [IntentTestCase("a", "refund"), IntentTestCase("b", "refund"), IntentTestCase("c", "invoice")]
        result = asyncio.run(IntentEvaluator(recognizer).evaluate(cases))
        self.assertEqual(result["accuracy"], .3333)
        self.assertEqual(result["observed_accuracy"], .5)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["coverage"], .6667)
        self.assertEqual(result["macro_f1_labels"], ["invoice", "refund"])
        self.assertEqual(result["per_class"]["refund"]["support"], 2)
        self.assertEqual(result["macro_f1"], .3333)
        self.assertIsNone(result["cases"][2]["predicted"])

    def test_invalid_history_is_failed_without_recognizer_call(self):
        recognizer = SimpleNamespace(recognize=AsyncMock(return_value=recognized()))
        result = asyncio.run(IntentEvaluator(recognizer).evaluate([
            IntentTestCase("q", "refund", context={"history": "invalid"})]))
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["accuracy"], 0)
        recognizer.recognize.assert_not_awaited()


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        # 使用工作区临时目录，兼容受限 Windows 环境。
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.orchestrator = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(
            response="fake answer", agent_type=SimpleNamespace(value="general"), intent=SimpleNamespace(value="refund"))))
        self.recognizer = SimpleNamespace(model="fake-intent", recognize=AsyncMock(return_value=recognized()))

    def evaluator(self, judge=None, **kwargs):
        return EndToEndEvaluator(self.orchestrator, self.recognizer, api_key="unused-fake-key",
                                 baseline_path=str(self.root / "baseline.json"),
                                 judge=judge or FakeJudge(), **kwargs)

    def test_judge_failure_is_preserved_excluded_from_mean_and_strict_pass_rate(self):
        judge = FakeJudge([QualityScores(.9, .9, .9, .9),
                           QualityScores(.5, .5, .5, .5, judge_failed=True, error="bad schema")])
        evaluator = self.evaluator(judge)
        report = asyncio.run(evaluator.run(dialog_cases=[{"question": "one"}, {"question": "two"}]))
        self.assertEqual(report.total, 2)
        self.assertEqual(report.passed, 1)
        self.assertEqual(report.pass_rate, .5)
        self.assertEqual(report.evaluated, 1)
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.coverage, .5)
        self.assertEqual(report.avg_scores["accuracy"], .9)
        self.assertEqual(report.results[1].scores, {})
        self.assertTrue(report.results[1].metadata["judge_failed"])
        self.assertEqual(report.results[1].error, "bad schema")

    def test_all_judge_failures_produce_no_average_or_success_recommendation(self):
        evaluator = self.evaluator(FakeJudge([RuntimeError("judge unavailable")]))
        report = asyncio.run(evaluator.run(dialog_cases=[{"question": "q"}]))
        self.assertEqual(report.avg_scores, {})
        self.assertEqual(report.coverage, 0)
        self.assertEqual(report.failed, 1)
        self.assertFalse(any("均达标" in text for text in report.recommendations))

    def test_failed_or_empty_reports_cannot_be_promoted(self):
        evaluator = self.evaluator(FakeJudge([RuntimeError("judge unavailable")]))
        failed = asyncio.run(evaluator.run(dialog_cases=[{"question": "q"}]))
        empty = asyncio.run(evaluator.run())
        for report in (failed, empty):
            with self.assertRaises(ValueError):
                evaluator.promote_baseline(report)
        self.assertIsNone(evaluator.baseline)

    def test_orchestrator_failure_is_a_result_instead_of_aborting_run(self):
        self.orchestrator.run.side_effect = RuntimeError("agent unavailable")
        judge = FakeJudge()
        report = asyncio.run(self.evaluator(judge).run(dialog_cases=[{"question": "q"}]))
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.results[0].metadata["stage"], "orchestrator")
        judge.judge.assert_not_awaited()

    def test_case_and_turn_evidence_are_forwarded_with_multiturn_history(self):
        judge = FakeJudge()
        evaluator = self.evaluator(judge)
        report = asyncio.run(evaluator.run(dialog_cases=[{
            "reference_facts": ["case fact"], "evidence": ["case evidence"],
            "turns": ["first", {"question": "second", "reference_facts": ["turn fact"],
                                  "actual_evidence": ["turn evidence"]}],
        }]))
        first, second = judge.judge.await_args_list
        self.assertEqual(first.kwargs["actual_evidence"], ["case evidence"])
        self.assertEqual(second.kwargs["reference_facts"], ["turn fact"])
        self.assertEqual(second.kwargs["actual_evidence"], ["turn evidence"])
        self.assertIn("first", second.kwargs["context"])
        second_request = self.orchestrator.run.await_args_list[1].args[0]
        self.assertEqual(second_request.history[0]["content"], "first")
        self.assertFalse(report.results[1].metadata["faithfulness_evaluated"])

    def test_missing_evidence_is_explicit_in_saved_report(self):
        report = asyncio.run(self.evaluator().run(dialog_cases=[{"question": "q"}]))
        self.assertEqual(report.results[0].metadata["evidence_status"], "current retrieval evidence not provided")

    def test_unique_immutable_reports_never_automatically_overwrite_baseline(self):
        evaluator = self.evaluator()
        first = asyncio.run(evaluator.run(dialog_cases=[{"question": "q"}]))
        self.assertFalse((self.root / "baseline.json").exists())
        second = asyncio.run(evaluator.run(dialog_cases=[{"question": "q"}]))
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual(len(list((self.root / "reports").glob("*.json"))), 2)
        self.assertEqual(first.metadata["baseline_comparison"]["status"], "no_baseline")
        with self.assertRaises(FileExistsError):
            evaluator._save_report(first)
        first.avg_scores["accuracy"] = 0
        self.assertEqual(evaluator.get_report(first.run_id).avg_scores["accuracy"], .9)
        self.assertEqual(len(evaluator.list_reports()), 2)

    def test_explicit_promotion_survives_restart_without_mutating_original_report(self):
        evaluator = self.evaluator()
        report = asyncio.run(evaluator.run(dialog_cases=[{"question": "q"}]))
        report_file = self.root / "reports" / f"{report.run_id}.json"
        original = report_file.read_bytes()
        report.avg_scores["accuracy"] = 0
        promoted = evaluator.promote_baseline(report)
        self.assertEqual(promoted.avg_scores["accuracy"], .9)
        restarted = self.evaluator()
        self.assertEqual(restarted.baseline.run_id, report.run_id)
        self.assertEqual(report_file.read_bytes(), original)
        asyncio.run(restarted.run(dialog_cases=[{"question": "q"}]))
        self.assertEqual(restarted.baseline.run_id, report.run_id)

    def test_regression_uses_explicit_baseline_not_last_run(self):
        judge = FakeJudge([QualityScores(1, 1, 1, 1), QualityScores(.2, .2, .2, .2), QualityScores(.8, .8, .8, .8)])
        evaluator = self.evaluator(judge)
        first = asyncio.run(evaluator.run(dialog_cases=[{"question": "same"}]))
        evaluator.promote_baseline(first.run_id)
        asyncio.run(evaluator.run(dialog_cases=[{"question": "same"}]))
        last = asyncio.run(evaluator.run(dialog_cases=[{"question": "same"}]))
        self.assertTrue(last.metadata["baseline_comparison"]["comparable"])
        self.assertTrue(last.regressions)
        self.assertEqual(evaluator.baseline.run_id, first.run_id)

    def test_version_mismatch_or_changed_inputs_are_explicitly_incomparable(self):
        evaluator = self.evaluator()
        first = asyncio.run(evaluator.run(dialog_cases=[{"question": "same"}], dataset_version="d1", config_version="c1"))
        evaluator.promote_baseline(first.run_id)
        for kwargs in ({"dataset_version": "d2", "config_version": "c1"},
                       {"dataset_version": "d1", "config_version": "c2"}):
            report = asyncio.run(evaluator.run(dialog_cases=[{"question": "same"}], **kwargs))
            self.assertFalse(report.metadata["baseline_comparison"]["comparable"])
            self.assertTrue(report.metadata["baseline_comparison"]["reasons"])
            self.assertEqual(report.regressions, [])
        changed = asyncio.run(evaluator.run(dialog_cases=[{"question": "changed"}], dataset_version="d1", config_version="c1"))
        self.assertFalse(changed.metadata["baseline_comparison"]["comparable"])

    def test_legacy_baseline_loads_but_has_no_comparable_versions(self):
        legacy = {"timestamp": "2020", "total": 1, "passed": 1, "pass_rate": 1,
                  "avg_scores": {"accuracy": 1}, "regressions": [], "recommendations": [], "results": []}
        (self.root / "baseline.json").write_text(json.dumps(legacy), encoding="utf-8")
        evaluator = self.evaluator()
        report = asyncio.run(evaluator.run(dialog_cases=[{"question": "q"}]))
        self.assertEqual(report.metadata["baseline_comparison"]["status"], "not_comparable")

    def test_path_traversal_missing_ids_and_bad_limit_are_rejected(self):
        evaluator = self.evaluator()
        with self.assertRaises(ValueError):
            evaluator.get_report("../baseline")
        with self.assertRaises(FileNotFoundError):
            evaluator.promote_baseline("a" * 32)
        with self.assertRaises(ValueError):
            evaluator.list_reports(limit=0)

    def test_legacy_report_fields_remain_json_serializable(self):
        report = asyncio.run(self.evaluator().run(intent_cases=[IntentTestCase("refund", "refund")],
                                                dialog_cases=[{"question": "q"}]))
        payload = asdict(report)
        for key in ("timestamp", "total", "passed", "pass_rate", "avg_scores", "regressions", "recommendations", "results"):
            self.assertIn(key, payload)
        self.assertEqual(report.total, 2)
        self.assertEqual(report.coverage, 1)
        json.dumps(payload, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
