"""Contract tests for the offline scoring rules (no services required)."""

import importlib.util
import json
import math
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "metrics.py"
SPEC = importlib.util.spec_from_file_location("benchmark_metrics", MODULE_PATH)
metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metrics)


def intent(case_id="i1", label="technical", agents=None, split="test"):
    return {"id": case_id, "kind": "intent", "split": split, "group_id": case_id,
            "expected_intent": label, "expected_agents": agents if agents is not None else ["technical"]}


def rag(case_id="r1", expected="answer", relevant=None, facts=None, split="test"):
    if relevant is None:
        relevant = ["d1", "d2"] if expected == "answer" else []
    return {"id": case_id, "kind": "rag", "split": split, "group_id": case_id,
            "expected_behavior": expected, "relevant_document_ids": relevant,
            "required_facts": facts if facts is not None else []}


def contract(case_id="c1"):
    return {"id": case_id, "kind": "contract", "split": "test", "group_id": case_id,
            "execution_tier": "future_contract", "expected_assertions": ["persisted", "idempotent"]}


def pred(case_id, **fields):
    return {"case_id": case_id, "status": "ok", **fields}


class ClassificationTests(unittest.TestCase):
    def test_missing_error_and_skipped_are_strict_failures(self):
        cases = [intent("i1"), intent("i2"), intent("i3"), intent("i4")]
        report = metrics.evaluate(cases, [pred("i1", intent="technical", agents=["technical"]),
                                         pred("i2", status="error", intent="technical", agents=["technical"]),
                                         pred("i3", status="skipped")])
        self.assertEqual(report["intent"]["strict_accuracy"], .25)
        self.assertEqual(report["intent"]["observed_accuracy"], 1)
        self.assertEqual(report["intent"]["coverage"], .25)
        self.assertEqual(report["routing"]["strict_exact_set_accuracy"], .25)
        self.assertEqual(report["dataset"]["statuses"], {"ok": 1, "error": 1, "skipped": 1, "missing": 1})
        self.assertEqual(report["operations"]["attempted_error_rate"], .5)
        self.assertEqual(report["operations"]["strict_non_success_rate"], .75)

    def test_macro_f1_uses_gold_support_not_missing_class(self):
        cases = [intent("i1", "a"), intent("i2", "a"), intent("i3", "b")]
        report = metrics.evaluate(cases, [pred("i1", intent="a"), pred("i2", intent="b")])
        result = report["intent"]
        self.assertEqual(set(result["per_class"]), {"a", "b"})
        self.assertEqual(result["per_class"]["a"]["support"], 2)
        self.assertEqual(result["per_class"]["b"]["false_negative"], 1)
        self.assertAlmostEqual(result["strict_macro_f1"], (2 / 3 + 0) / 2)
        self.assertEqual(result["coverage"], 2 / 3)

    def test_unknown_predicted_label_is_incorrect_not_new_gold_class(self):
        result = metrics.evaluate([intent()], [pred("i1", intent="nonsense")])["intent"]
        self.assertEqual(result["strict_accuracy"], 0)
        self.assertEqual(result["strict_macro_f1"], 0)
        self.assertEqual(set(result["per_class"]), {"technical"})

    def test_routing_is_exact_set_with_order_ignored(self):
        cases = [intent("i1", agents=["technical", "billing"]), intent("i2", agents=[])]
        report = metrics.evaluate(cases, [pred("i1", agents=["billing", "technical"]), pred("i2", agents=["technical"])])
        self.assertEqual(report["routing"]["strict_exact_set_accuracy"], .5)
        self.assertIsNone(report["intent"]["observed_accuracy"])


class RetrievalTests(unittest.TestCase):
    def test_duplicate_documents_consume_depth_and_gain_once(self):
        result = metrics.evaluate([rag()], [pred("r1", retrieved_document_ids=["x", "d1", "d1", "d2"])],
                                  ks=(1, 3, 4))["retrieval"]
        self.assertEqual(result["at_k"]["1"]["hit"]["strict"], 0)
        self.assertEqual(result["at_k"]["3"]["recall"]["strict"], .5)
        self.assertEqual(result["at_k"]["3"]["mrr"]["strict"], .5)
        idcg = 1 + 1 / math.log2(3)
        self.assertAlmostEqual(result["at_k"]["3"]["ndcg"]["strict"], (1 / math.log2(3)) / idcg)
        self.assertEqual(result["at_k"]["4"]["recall"]["strict"], 1)
        self.assertAlmostEqual(result["at_k"]["4"]["ndcg"]["strict"],
                               (1 / math.log2(3) + 1 / math.log2(5)) / idcg)

    def test_empty_retrieval_is_measured_missing_is_not(self):
        cases = [rag("r1"), rag("r2"), rag("r3"), rag("r4")]
        result = metrics.evaluate(cases, [pred("r1", retrieved_document_ids=["d1"]),
                                         pred("r2", retrieved_document_ids=[]),
                                         pred("r3", status="error", retrieved_document_ids=["d1", "d2"])])["retrieval"]
        self.assertEqual(result["coverage"], .5)
        self.assertEqual(result["at_k"]["1"]["hit"]["strict"], .25)
        self.assertEqual(result["at_k"]["1"]["hit"]["observed"], .5)
        self.assertEqual(result["at_k"]["1"]["recall"]["strict"], .125)

    def test_refusal_and_clarification_excluded_from_retrieval(self):
        cases = [rag("r1"), rag("r2", "refuse"), rag("r3", "clarify")]
        result = metrics.evaluate(cases, [pred("r1", retrieved_document_ids=["d1", "d2"]),
                                         pred("r2", retrieved_document_ids=[]),
                                         pred("r3", retrieved_document_ids=[])])["retrieval"]
        self.assertEqual(result["eligible_cases"], 1)
        self.assertEqual(result["excluded_non_answerable_cases"], 2)
        self.assertEqual(result["at_k"]["3"]["recall"]["strict"], 1)

    def test_multi_evidence_recall_and_idcg(self):
        facts = [{"id": "f1", "text": "needs both", "evidence_document_ids": ["d1", "d2"]}]
        result = metrics.evaluate([rag(facts=facts)], [pred("r1", retrieved_document_ids=["d1"])], ks=(1, 3))
        self.assertEqual(result["retrieval"]["at_k"]["1"]["recall"]["strict"], .5)
        self.assertEqual(result["retrieval"]["at_k"]["1"]["ndcg"]["strict"], 1)
        self.assertAlmostEqual(result["retrieval"]["at_k"]["3"]["ndcg"]["strict"], 1 / (1 + 1 / math.log2(3)))
        self.assertEqual(result["facts"]["strict_required_fact_coverage"], 0)


class BehaviorTests(unittest.TestCase):
    def test_refuse_and_clarify_are_different_gold_behaviors(self):
        cases = [rag("r1", "refuse"), rag("r2", "clarify"), rag("r3"), rag("r4")]
        result = metrics.evaluate(cases, [pred("r1", behavior="clarify"), pred("r2", behavior="clarify"),
                                         pred("r3", behavior="refuse")])["behavior"]
        self.assertEqual(result["strict_accuracy"], .25)
        self.assertEqual(result["no_answer"]["strict_exact_accuracy"], .5)
        self.assertEqual(result["answerable_errors"]["observed_false_refusal_rate"], 1)
        self.assertEqual(result["answerable_errors"]["false_refusal_fraction_all_cases"], .5)
        self.assertEqual(result["by_expected_behavior"]["refuse"]["predicted_behavior_counts"]["clarify"], 1)

    def test_missing_behavior_never_correct_noanswer(self):
        result = metrics.evaluate([rag(expected="refuse")], [pred("r1")])["behavior"]
        self.assertEqual(result["no_answer"]["strict_exact_accuracy"], 0)
        self.assertIsNone(result["no_answer"]["observed_exact_accuracy"])


class JudgementTests(unittest.TestCase):
    def test_facts_only_explicit_checks_count(self):
        facts = [{"id": f"f{i}", "text": f"fact {i}", "evidence_document_ids": ["d1"]} for i in range(1, 5)]
        result = metrics.evaluate([rag(facts=facts)], [pred("r1", fact_checks={"f1": True, "f2": False, "f3": None})])["facts"]
        self.assertEqual(result["required_facts"], 4)
        self.assertEqual(result["assessed_facts"], 2)
        self.assertEqual(result["unassessed_facts"], 2)
        self.assertEqual(result["strict_required_fact_coverage"], .25)
        self.assertEqual(result["observed_fact_pass_rate"], .5)
        self.assertEqual(result["assessment_coverage"], .5)

    def test_error_status_cannot_earn_fact_or_citation_credit(self):
        facts = [{"id": "f1", "text": "fact", "evidence_document_ids": ["d1"]}]
        result = metrics.evaluate([rag(facts=facts)], [pred("r1", status="error", fact_checks={"f1": True},
                                  citation_checks={"required": 1, "cited": 1, "assessed": 1, "supported": 1})])
        self.assertEqual(result["facts"]["assessed_facts"], 0)
        self.assertEqual(result["facts"]["strict_required_fact_coverage"], 0)
        self.assertIsNone(result["citations"]["observed_citation_support"])

    def test_citations_report_observed_micro_ratios_and_coverage(self):
        cases = [rag("r1"), rag("r2"), rag("r3")]
        result = metrics.evaluate(cases, [pred("r1", citation_checks={"required": 4, "cited": 3, "assessed": 2, "supported": 1}),
                                         pred("r2", citation_checks={"required": 0, "cited": 0, "assessed": 0, "supported": 0})])["citations"]
        self.assertEqual(result["sample_coverage"], 2 / 3)
        self.assertEqual(result["observed_citation_coverage"], .75)
        self.assertEqual(result["observed_citation_support"], .5)
        self.assertEqual(result["citation_assessment_coverage"], 2 / 3)

    def test_no_citation_checks_or_zero_denominator_is_not_perfect(self):
        for predictions in ([], [pred("r1", citation_checks={"required": 0, "cited": 0, "assessed": 0, "supported": 0})]):
            result = metrics.evaluate([rag()], predictions)["citations"]
            self.assertIsNone(result["observed_citation_support"])
            self.assertIsNone(result["observed_citation_coverage"])

    def test_faithfulness_assesses_actual_claims_independently_of_required_facts(self):
        facts = [{"id": "f1", "text": "required fact", "evidence_document_ids": ["d1"]}]
        cases = [rag("r1", facts=facts), rag("r2", "refuse")]
        result = metrics.evaluate(cases, [pred("r1", fact_checks={"f1": True},
                                               faithfulness_checks={"assessed": 3, "supported": 1}),
                                         pred("r2", faithfulness_checks={"assessed": 1, "supported": 1})])
        self.assertEqual(result["facts"]["strict_required_fact_coverage"], 1)
        faithfulness = result["faithfulness"]
        self.assertEqual(faithfulness["eligible_cases"], 2)
        self.assertEqual(faithfulness["observed_samples"], 2)
        self.assertEqual(faithfulness["assessed_cases"], 2)
        self.assertEqual(faithfulness["claims_assessed"], 4)
        self.assertEqual(faithfulness["claims_supported"], 2)
        self.assertEqual(faithfulness["observed_support_rate"], .5)
        self.assertEqual(faithfulness["sample_coverage"], 1)

    def test_faithfulness_missing_and_zero_claims_are_unassessed_not_perfect(self):
        result = metrics.evaluate([rag("r1"), rag("r2")],
                                  [pred("r1", faithfulness_checks={"assessed": 0, "supported": 0})])["faithfulness"]
        self.assertEqual(result["sample_coverage"], .5)
        self.assertEqual(result["assessed_cases"], 0)
        self.assertEqual(result["unobserved_samples"], 1)
        self.assertEqual(result["observed_samples_without_claims"], 1)
        self.assertIsNone(result["observed_support_rate"])
        self.assertEqual(result["assessed_case_coverage"], 0)

    def test_faithfulness_error_and_skipped_status_do_not_earn_credit(self):
        cases = [rag("r1"), rag("r2"), rag("r3")]
        predictions = [pred("r1", status="error", faithfulness_checks={"assessed": 10, "supported": 10}),
                       pred("r2", status="skipped", faithfulness_checks={"assessed": 10, "supported": 10})]
        result = metrics.evaluate(cases, predictions)["faithfulness"]
        self.assertEqual(result["observed_samples"], 0)
        self.assertEqual(result["claims_assessed"], 0)
        self.assertEqual(result["sample_coverage"], 0)
        self.assertIsNone(result["observed_support_rate"])

    def test_faithfulness_checks_validate_types_counts_and_case_kind(self):
        invalid = [None, {}, {"assessed": 1}, {"assessed": 1, "supported": 2},
                   {"assessed": -1, "supported": 0}, {"assessed": 1.0, "supported": 0},
                   {"assessed": True, "supported": 0}, {"assessed": math.nan, "supported": 0},
                   {"assessed": 1, "supported": 1, "extra": 1}]
        for checked in invalid:
            with self.subTest(checked=checked), self.assertRaises(ValueError):
                metrics.evaluate([rag()], [pred("r1", faithfulness_checks=checked)])
        with self.assertRaises(ValueError):
            metrics.evaluate([intent()], [pred("i1", faithfulness_checks={"assessed": 1, "supported": 1})])

    def test_contract_requires_every_assertion_and_status_ok(self):
        cases = [contract(f"c{i}") for i in range(1, 6)]
        predictions = [pred("c1", assertions={"persisted": True, "idempotent": True}),
                       pred("c2", assertions={"persisted": True}),
                       pred("c3", assertions={"persisted": True, "idempotent": False}),
                       pred("c4", status="error", assertions={"persisted": True, "idempotent": True})]
        result = metrics.evaluate(cases, predictions)["contracts"]
        self.assertEqual(result["strict_pass_rate"], .2)
        self.assertEqual(result["observed_complete_pass_rate"], .5)
        self.assertEqual(result["fully_assessed_cases"], 2)
        self.assertEqual(result["partially_assessed_cases"], 1)
        self.assertEqual(result["unassessed_cases"], 2)
        self.assertEqual(result["assessed_assertions"], 5)


class OperationsTests(unittest.TestCase):
    def test_nearest_rank_quantiles_cost_unknown_and_errors(self):
        cases = [intent(f"i{i}") for i in range(1, 5)]
        predictions = [pred("i1", latency_ms=10, cost_usd=.01), pred("i2", status="error", latency_ms=100),
                       pred("i3", latency_ms=20, cost_usd=0), pred("i4", status="skipped", latency_ms=999, cost_usd=99)]
        result = metrics.evaluate(cases, predictions)["operations"]
        self.assertEqual(result["latency_ms"]["p50"], 20)
        self.assertEqual(result["latency_ms"]["p95"], 100)
        self.assertEqual(result["latency_ms"]["sample_count"], 3)
        self.assertEqual(result["cost_usd"]["known_total"], .01)
        self.assertEqual(result["cost_usd"]["sample_count"], 2)
        self.assertEqual(result["cost_usd"]["unknown_attempted_cases"], 1)
        self.assertEqual(result["cost_usd"]["attempted_coverage"], 2 / 3)

    def test_no_observations_are_na(self):
        result = metrics.evaluate([intent()], [pred("i1")])["operations"]
        self.assertIsNone(result["latency_ms"]["p50"])
        self.assertIsNone(result["cost_usd"]["known_total"])
        self.assertEqual(result["cost_usd"]["unknown_attempted_cases"], 1)

    def test_empty_dataset_has_no_invented_zero_scores(self):
        result = metrics.evaluate([], [])
        self.assertIsNone(result["intent"]["strict_accuracy"])
        self.assertIsNone(result["retrieval"]["at_k"]["1"]["hit"]["strict"])
        self.assertIsNone(result["contracts"]["strict_pass_rate"])
        json.dumps(result, allow_nan=False)


class ValidationTests(unittest.TestCase):
    def test_unknown_duplicate_prediction_and_duplicate_case_ids_rejected(self):
        examples = [([intent()], [pred("unknown")]),
                    ([intent()], [pred("i1"), pred("i1")]),
                    ([intent(), intent()], [])]
        for cases, predictions in examples:
            with self.subTest(cases=cases, predictions=predictions), self.assertRaises(ValueError):
                metrics.evaluate(cases, predictions)

    def test_non_finite_negative_and_bool_numbers_rejected(self):
        for field in ("latency_ms", "cost_usd"):
            for value in (-1, math.nan, math.inf, -math.inf, True, "0", None):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    metrics.evaluate([intent()], [pred("i1", **{field: value})])

    def test_invalid_status_and_check_types_rejected(self):
        for status in (None, "success", 1, [], {}):
            with self.subTest(status=status), self.assertRaises(ValueError):
                metrics.evaluate([intent()], [pred("i1", status=status)])
        for value in (1, "true", [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                metrics.evaluate([contract()], [pred("c1", assertions={"persisted": value})])
        with self.assertRaises(ValueError):
            metrics.evaluate([contract()], [pred("c1", assertions={"unknown": True})])

    def test_invalid_facts_and_cross_kind_fields_rejected(self):
        facts = [{"id": "f1", "text": "fact", "evidence_document_ids": ["d1"]}]
        for checked in ({"f1": 1}, {"unknown": True}):
            with self.subTest(checked=checked), self.assertRaises(ValueError):
                metrics.evaluate([rag(facts=facts)], [pred("r1", fact_checks=checked)])
        with self.assertRaises(ValueError):
            metrics.evaluate([intent()], [pred("i1", assertions={})])
        with self.assertRaises(ValueError):
            metrics.evaluate([rag(facts=[{"id": "f1", "text": "fact", "evidence_document_ids": ["unknown"]}])], [])

    def test_invalid_citations_rejected(self):
        for checks in ({"required": 1}, {"required": 1, "cited": 2, "assessed": 1, "supported": 1},
                       {"required": 1, "cited": 1, "assessed": 1, "supported": 2},
                       {"required": 1.0, "cited": 1, "assessed": 1, "supported": 1},
                       {"required": 1, "cited": 1, "assessed": -1, "supported": 0}):
            with self.subTest(checks=checks), self.assertRaises(ValueError):
                metrics.evaluate([rag()], [pred("r1", citation_checks=checks)])

    def test_invalid_k_rejected(self):
        for ks in ((), (0,), (-1,), (True,), (1.0,), (1, 1)):
            with self.subTest(ks=ks), self.assertRaises(ValueError):
                metrics.evaluate([rag()], [], ks=ks)


class CliTests(unittest.TestCase):
    def test_multiple_case_files_split_filter_and_json_output(self):
        with tempfile.TemporaryDirectory(dir=MODULE_PATH.parent) as tmp:
            root = Path(tmp)
            a, b, p, out = [root / name for name in ("a.jsonl", "b.jsonl", "p.jsonl", "metrics.json")]
            a.write_text(json.dumps(intent("i1", split="dev")) + "\n", encoding="utf-8")
            b.write_text(json.dumps(intent("i2", split="test")) + "\n", encoding="utf-8")
            p.write_text("\n".join(json.dumps(pred(case_id, intent="technical")) for case_id in ("i1", "i2")), encoding="utf-8")
            result = metrics.main(["--cases", str(a), "--cases", str(b), "--predictions", str(p),
                                   "--output", str(out), "--split", "test"])
            self.assertEqual(result, 0)
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(report["dataset"]["total_cases"], 1)
            self.assertEqual(report["intent"]["strict_accuracy"], 1)
            self.assertEqual(report["selection"]["input_case_count"], 2)

    def test_jsonl_rejects_non_finite_values_and_non_objects(self):
        with tempfile.TemporaryDirectory(dir=MODULE_PATH.parent) as tmp:
            path = Path(tmp) / "bad.jsonl"
            for text in ('{"latency_ms": NaN}', '[]', '{"latency_ms": Infinity}'):
                path.write_text(text + "\n", encoding="utf-8")
                with self.subTest(text=text), self.assertRaises(ValueError):
                    metrics.read_jsonl(path)


if __name__ == "__main__":
    unittest.main()
