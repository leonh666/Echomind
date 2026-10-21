"""Offline, standard-library metrics for the EchoMind benchmark.

Missing observations are never inferred from answer text.  Strict scores use
every eligible case; observed scores describe only explicitly measured fields.
No weighted overall score is produced.  See ``evaluate`` for the public API.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


BEHAVIORS = {"answer", "refuse", "clarify"}
STATUSES = {"ok", "error", "skipped"}
KINDS = {"intent", "rag", "contract"}


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}: expected a nonempty string")
    return value


def _strings(value: Any, context: str, *, unique: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{context}: expected a list of strings")
    for item in value:
        _string(item, context)
    if unique and len(set(value)) != len(value):
        raise ValueError(f"{context}: duplicate values")
    return value


def _number(value: Any, context: str, *, integer: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context}: expected a nonnegative finite number")
    if (isinstance(value, float) and not math.isfinite(value)) or value < 0:
        raise ValueError(f"{context}: expected a nonnegative finite number")
    if integer and not isinstance(value, int):
        raise ValueError(f"{context}: expected an integer")
    return value


def _checks(value: Any, context: str) -> dict[str, bool | None]:
    if not isinstance(value, dict):
        raise ValueError(f"{context}: expected an object")
    for key, checked in value.items():
        _string(key, context)
        if checked is not None and not isinstance(checked, bool):
            raise ValueError(f"{context}.{key}: expected true, false or null")
    return value


def _validate_cases(cases: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not isinstance(cases, list):
        raise ValueError("cases: expected a list")
    indexed = {}
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("case: expected an object")
        case_id = _string(case.get("id"), "case.id")
        if case_id in indexed:
            raise ValueError(f"duplicate case id: {case_id}")
        kind = case.get("kind")
        if not isinstance(kind, str) or kind not in KINDS:
            raise ValueError(f"{case_id}: invalid kind {kind!r}")
        if "split" in case:
            _string(case["split"], f"{case_id}.split")
        if "group_id" in case:
            _string(case["group_id"], f"{case_id}.group_id")
        if kind == "intent":
            _string(case.get("expected_intent"), f"{case_id}.expected_intent")
            _strings(case.get("expected_agents"), f"{case_id}.expected_agents", unique=True)
        elif kind == "rag":
            if not isinstance(case.get("expected_behavior"), str) or case["expected_behavior"] not in BEHAVIORS:
                raise ValueError(f"{case_id}: invalid expected_behavior")
            relevant = _strings(case.get("relevant_document_ids"),
                                f"{case_id}.relevant_document_ids", unique=True)
            if case["expected_behavior"] == "answer" and not relevant:
                raise ValueError(f"{case_id}: answerable RAG case needs relevant documents")
            facts = case.get("required_facts")
            if not isinstance(facts, list):
                raise ValueError(f"{case_id}.required_facts: expected a list")
            fact_ids = set()
            for fact in facts:
                if not isinstance(fact, dict):
                    raise ValueError(f"{case_id}: fact must be an object")
                fact_id = _string(fact.get("id"), f"{case_id}.fact.id")
                if fact_id in fact_ids:
                    raise ValueError(f"{case_id}: duplicate fact id {fact_id}")
                fact_ids.add(fact_id)
                _string(fact.get("text"), f"{case_id}.{fact_id}.text")
                evidence = _strings(fact.get("evidence_document_ids"),
                                    f"{case_id}.{fact_id}.evidence_document_ids", unique=True)
                if not evidence or not set(evidence).issubset(relevant):
                    raise ValueError(f"{case_id}.{fact_id}: evidence must reference relevant documents")
        else:
            assertions = _strings(case.get("expected_assertions"),
                                  f"{case_id}.expected_assertions", unique=True)
            if not assertions:
                raise ValueError(f"{case_id}: contract needs at least one assertion")
            if case.get("execution_tier") != "future_contract":
                raise ValueError(f"{case_id}: contract execution_tier must be future_contract")
        indexed[case_id] = case
    return indexed


def _validate_predictions(predictions: list[dict[str, Any]],
                          cases: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not isinstance(predictions, list):
        raise ValueError("predictions: expected a list")
    indexed = {}
    for pred in predictions:
        if not isinstance(pred, dict):
            raise ValueError("prediction: expected an object")
        case_id = _string(pred.get("case_id"), "prediction.case_id")
        if case_id not in cases:
            raise ValueError(f"unknown prediction case_id: {case_id}")
        if case_id in indexed:
            raise ValueError(f"duplicate prediction case_id: {case_id}")
        if not isinstance(pred.get("status"), str) or pred["status"] not in STATUSES:
            raise ValueError(f"{case_id}: invalid prediction status")
        kind = cases[case_id]["kind"]
        permitted = {
            "intent": {"intent", "agents"},
            "rag": {"retrieved_document_ids", "behavior", "fact_checks", "citation_checks", "faithfulness_checks"},
            "contract": {"assertions"},
        }[kind]
        metric_fields = {"intent", "agents", "retrieved_document_ids", "behavior",
                         "fact_checks", "citation_checks", "faithfulness_checks", "assertions"}
        wrong_fields = (set(pred) & metric_fields) - permitted
        if wrong_fields:
            raise ValueError(f"{case_id}: metric fields incompatible with {kind}: {sorted(wrong_fields)}")
        if "intent" in pred:
            _string(pred["intent"], f"{case_id}.intent")
        if "agents" in pred:
            _strings(pred["agents"], f"{case_id}.agents", unique=True)
        if "retrieved_document_ids" in pred:
            # Repeated documents represent actual chunk positions, so keep duplicates.
            _strings(pred["retrieved_document_ids"], f"{case_id}.retrieved_document_ids")
        if "behavior" in pred and (not isinstance(pred["behavior"], str) or pred["behavior"] not in BEHAVIORS):
            raise ValueError(f"{case_id}: invalid behavior")
        for field in ("latency_ms", "cost_usd"):
            if field in pred:
                _number(pred[field], f"{case_id}.{field}")
        if "judge_method" in pred:
            _string(pred["judge_method"], f"{case_id}.judge_method")
        if "fact_checks" in pred:
            checked = _checks(pred["fact_checks"], f"{case_id}.fact_checks")
            expected = {fact["id"] for fact in cases[case_id]["required_facts"]}
            if not set(checked).issubset(expected):
                raise ValueError(f"{case_id}: unknown fact id in fact_checks")
        if "assertions" in pred:
            checked = _checks(pred["assertions"], f"{case_id}.assertions")
            expected = set(cases[case_id]["expected_assertions"])
            if not set(checked).issubset(expected):
                raise ValueError(f"{case_id}: unknown assertion name")
        if "citation_checks" in pred:
            checked = pred["citation_checks"]
            names = {"required", "cited", "assessed", "supported"}
            if not isinstance(checked, dict) or set(checked) != names:
                raise ValueError(f"{case_id}: citation_checks requires exactly {sorted(names)}")
            for name in names:
                _number(checked[name], f"{case_id}.citation_checks.{name}", integer=True)
            if not (checked["supported"] <= checked["assessed"] <= checked["cited"] <= checked["required"]):
                raise ValueError(f"{case_id}: expected supported <= assessed <= cited <= required")
        if "faithfulness_checks" in pred:
            checked = pred["faithfulness_checks"]
            names = {"assessed", "supported"}
            if not isinstance(checked, dict) or set(checked) != names:
                raise ValueError(f"{case_id}: faithfulness_checks requires exactly {sorted(names)}")
            for name in names:
                _number(checked[name], f"{case_id}.faithfulness_checks.{name}", integer=True)
            if checked["supported"] > checked["assessed"]:
                raise ValueError(f"{case_id}: expected faithfulness supported <= assessed")
        indexed[case_id] = pred
    return indexed


def _classification(golds: list[str], observed: list[str | None]) -> dict[str, Any]:
    labels = sorted(set(golds))
    details = {}
    observed_details = {}
    for label in labels:
        support = sum(gold == label for gold in golds)
        observed_support = sum(gold == label and pred is not None for gold, pred in zip(golds, observed))
        true_positive = sum(gold == label and pred == label for gold, pred in zip(golds, observed))
        false_positive = sum(gold != label and pred == label for gold, pred in zip(golds, observed))
        false_negative = support - true_positive
        observed_fn = observed_support - true_positive
        precision = _ratio(true_positive, true_positive + false_positive)
        details[label] = {
            "support": support, "observed_support": observed_support,
            "true_positive": true_positive, "false_positive": false_positive,
            "false_negative": false_negative,
            "precision": precision, "recall": _ratio(true_positive, support),
            "f1": _ratio(2 * true_positive, 2 * true_positive + false_positive + false_negative),
        }
        if observed_support:
            observed_details[label] = _ratio(2 * true_positive,
                                             2 * true_positive + false_positive + observed_fn)
    n = len(golds)
    measured = sum(pred is not None for pred in observed)
    correct = sum(gold == pred for gold, pred in zip(golds, observed))
    return {
        "eligible_cases": n, "observed_cases": measured, "correct_cases": correct,
        "coverage": _ratio(measured, n), "strict_accuracy": _ratio(correct, n),
        "observed_accuracy": _ratio(correct, measured),
        "strict_macro_f1": _ratio(sum(item["f1"] for item in details.values()), len(details)),
        "observed_macro_f1": _ratio(sum(observed_details.values()), len(observed_details)),
        "per_class": details,
        "macro_f1_note": "Strict macro averages gold-supported classes; missing/error/skipped predictions are false negatives. Observed macro averages classes with observed gold support.",
    }


def _retrieval_scores(documents: list[str], relevant: set[str], k: int) -> dict[str, float]:
    seen = set()
    hit_count = 0
    first_rank = None
    dcg = 0.0
    for rank, doc_id in enumerate(documents[:k], 1):
        if doc_id in relevant and doc_id not in seen:
            hit_count += 1
            dcg += 1 / math.log2(rank + 1)
            if first_rank is None:
                first_rank = rank
        seen.add(doc_id)
    idcg = sum(1 / math.log2(rank + 1) for rank in range(1, min(len(relevant), k) + 1))
    return {"hit": float(hit_count > 0), "recall": hit_count / len(relevant),
            "mrr": 1 / first_rank if first_rank else 0.0, "ndcg": dcg / idcg}


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def evaluate(cases: list[dict[str, Any]], predictions: list[dict[str, Any]],
             ks: Iterable[int] = (1, 3, 5)) -> dict[str, Any]:
    """Return transparent metrics; invalid inputs raise ``ValueError``.

    ``None`` means N/A, never zero. An empty retrieved list is an observed miss;
    an absent field is unmeasured. Only status=ok fields earn task credit.
    Contract results are separate from today's intent/RAG capability scores.
    """
    indexed_cases = _validate_cases(cases)
    indexed_predictions = _validate_predictions(predictions, indexed_cases)
    ks = tuple(ks)
    if not ks or any(isinstance(k, bool) or not isinstance(k, int) or k <= 0 for k in ks):
        raise ValueError("ks must contain positive integers")
    if len(set(ks)) != len(ks):
        raise ValueError("ks contains duplicate values")
    ks = tuple(sorted(ks))

    def successful(case: dict[str, Any]) -> dict[str, Any]:
        pred = indexed_predictions.get(case["id"], {})
        return pred if pred.get("status") == "ok" else {}

    total = len(cases)
    statuses = Counter(pred["status"] for pred in predictions)
    statuses["missing"] = total - len(predictions)
    counts = {status: statuses[status] for status in ("ok", "error", "skipped", "missing")}
    intent_cases = [case for case in cases if case["kind"] == "intent"]
    rag_cases = [case for case in cases if case["kind"] == "rag"]
    answerable = [case for case in rag_cases if case["expected_behavior"] == "answer"]
    contract_cases = [case for case in cases if case["kind"] == "contract"]
    intent = _classification([case["expected_intent"] for case in intent_cases],
                             [successful(case).get("intent") for case in intent_cases])
    routing_observed = [case for case in intent_cases if "agents" in successful(case)]
    routing_correct = sum(set(successful(case)["agents"]) == set(case["expected_agents"])
                          for case in routing_observed)
    routing = {"eligible_cases": len(intent_cases), "observed_cases": len(routing_observed),
               "correct_cases": routing_correct,
               "coverage": _ratio(len(routing_observed), len(intent_cases)),
               "strict_exact_set_accuracy": _ratio(routing_correct, len(intent_cases)),
               "observed_exact_set_accuracy": _ratio(routing_correct, len(routing_observed))}

    retrieved = [case for case in answerable if "retrieved_document_ids" in successful(case)]
    retrieval = {"eligible_cases": len(answerable), "observed_cases": len(retrieved),
                 "excluded_non_answerable_cases": len(rag_cases) - len(answerable),
                 "coverage": _ratio(len(retrieved), len(answerable)), "at_k": {},
                 "ranking_note": "K counts original chunk positions, including duplicate document IDs. Each relevant document earns gain once. Binary IDCG uses min(relevant document count, K)."}
    for k in ks:
        sums = Counter()
        for case in retrieved:
            sums.update(_retrieval_scores(successful(case)["retrieved_document_ids"],
                                           set(case["relevant_document_ids"]), k))
        retrieval["at_k"][str(k)] = {
            metric: {"strict": _ratio(sums[metric], len(answerable)),
                     "observed": _ratio(sums[metric], len(retrieved))}
            for metric in ("hit", "recall", "mrr", "ndcg")
        }

    behavior = _classification([case["expected_behavior"] for case in rag_cases],
                               [successful(case).get("behavior") for case in rag_cases])
    behavior["by_expected_behavior"] = {}
    for expected in sorted(BEHAVIORS):
        cohort = [case for case in rag_cases if case["expected_behavior"] == expected]
        measured = [case for case in cohort if "behavior" in successful(case)]
        correct = sum(successful(case)["behavior"] == expected for case in measured)
        behavior["by_expected_behavior"][expected] = {
            "eligible_cases": len(cohort), "observed_cases": len(measured), "correct_cases": correct,
            "strict_accuracy": _ratio(correct, len(cohort)),
            "observed_accuracy": _ratio(correct, len(measured)),
            "coverage": _ratio(len(measured), len(cohort)),
            "predicted_behavior_counts": {label: sum(successful(case)["behavior"] == label for case in measured)
                                          for label in sorted(BEHAVIORS)},
        }
    no_answer = [case for case in rag_cases if case["expected_behavior"] in {"refuse", "clarify"}]
    no_answer_observed = [case for case in no_answer if "behavior" in successful(case)]
    no_answer_correct = sum(successful(case)["behavior"] == case["expected_behavior"]
                            for case in no_answer_observed)
    answer_observed = [case for case in answerable if "behavior" in successful(case)]
    false_refusals = sum(successful(case)["behavior"] == "refuse" for case in answer_observed)
    false_clarifications = sum(successful(case)["behavior"] == "clarify" for case in answer_observed)
    behavior["no_answer"] = {
        "eligible_cases": len(no_answer), "observed_cases": len(no_answer_observed),
        "exact_correct_cases": no_answer_correct,
        "strict_exact_accuracy": _ratio(no_answer_correct, len(no_answer)),
        "observed_exact_accuracy": _ratio(no_answer_correct, len(no_answer_observed)),
        "coverage": _ratio(len(no_answer_observed), len(no_answer)),
        "note": "A refusal does not satisfy an expected clarification, or vice versa.",
    }
    behavior["answerable_errors"] = {
        "eligible_cases": len(answerable), "observed_cases": len(answer_observed),
        "false_refusal_cases": false_refusals, "false_clarification_cases": false_clarifications,
        "observed_false_refusal_rate": _ratio(false_refusals, len(answer_observed)),
        "observed_false_clarification_rate": _ratio(false_clarifications, len(answer_observed)),
        "false_refusal_fraction_all_cases": _ratio(false_refusals, len(answerable)),
        "note": "Observed error rates require the accompanying coverage; missing behavior is unmeasured, not a correct answer.",
    }

    required_facts = assessed_facts = passed_facts = failed_facts = 0
    facts_case_count = assessed_fact_cases = 0
    for case in rag_cases:
        facts = case["required_facts"]
        if facts:
            facts_case_count += 1
        checked = successful(case).get("fact_checks", {})
        assessed_here = 0
        for fact in facts:
            required_facts += 1
            value = checked.get(fact["id"])
            if value is not None:
                assessed_facts += 1
                assessed_here += 1
                passed_facts += int(value)
                failed_facts += int(not value)
        assessed_fact_cases += int(assessed_here > 0)
    facts = {"eligible_cases": facts_case_count, "cases_with_assessments": assessed_fact_cases,
             "required_facts": required_facts, "assessed_facts": assessed_facts,
             "passed_facts": passed_facts, "failed_facts": failed_facts,
             "unassessed_facts": required_facts - assessed_facts,
             "assessment_coverage": _ratio(assessed_facts, required_facts),
             "strict_required_fact_coverage": _ratio(passed_facts, required_facts),
             "observed_fact_pass_rate": _ratio(passed_facts, assessed_facts),
             "note": "Uses explicit boolean fact_checks only; lexical overlap is not evidence of semantic correctness."}

    citation_totals = Counter({name: 0 for name in ("required", "cited", "assessed", "supported")})
    citation_samples = 0
    for case in answerable:
        checked = successful(case).get("citation_checks")
        if checked is not None:
            citation_samples += 1
            citation_totals.update(checked)
    citations = {"eligible_cases": len(answerable), "observed_cases": citation_samples,
                 "sample_coverage": _ratio(citation_samples, len(answerable)),
                 **dict(citation_totals),
                 "observed_citation_coverage": _ratio(citation_totals["cited"], citation_totals["required"]),
                 "observed_citation_support": _ratio(citation_totals["supported"], citation_totals["assessed"]),
                 "citation_assessment_coverage": _ratio(citation_totals["assessed"], citation_totals["cited"]),
                 "note": "Micro ratios describe reported checks only. Missing checks have unknown denominators and must be read alongside sample_coverage; zero denominators yield null."}

    faithfulness_samples = faithfulness_assessed_cases = claims_assessed = claims_supported = 0
    for case in rag_cases:
        checked = successful(case).get("faithfulness_checks")
        if checked is not None:
            faithfulness_samples += 1
            faithfulness_assessed_cases += int(checked["assessed"] > 0)
            claims_assessed += checked["assessed"]
            claims_supported += checked["supported"]
    faithfulness = {
        "eligible_cases": len(rag_cases), "observed_samples": faithfulness_samples,
        "assessed_cases": faithfulness_assessed_cases,
        "unobserved_samples": len(rag_cases) - faithfulness_samples,
        "observed_samples_without_claims": faithfulness_samples - faithfulness_assessed_cases,
        "claims_assessed": claims_assessed, "claims_supported": claims_supported,
        "claims_unsupported": claims_assessed - claims_supported,
        "observed_support_rate": _ratio(claims_supported, claims_assessed),
        "sample_coverage": _ratio(faithfulness_samples, len(rag_cases)),
        "assessed_case_coverage": _ratio(faithfulness_assessed_cases, len(rag_cases)),
        "note": "External human/LLM checks must assess all verifiable claims in the actual response against the actual retrieved context. This is separate from required-fact coverage. Missing fields remain unmeasured; zero claims yield null support rate. All RAG behaviors are eligible, and status error/skipped receives no credit.",
    }

    contract_passed = complete_contracts = assertion_total = assertion_assessed = assertion_passed = 0
    assertion_failed = partial_contracts = assertion_field_cases = 0
    for case in contract_cases:
        pred = successful(case)
        assertion_field_cases += int("assertions" in pred)
        checked = pred.get("assertions", {})
        expected = case["expected_assertions"]
        values = [checked.get(name) for name in expected]
        n_assessed = sum(value is not None for value in values)
        assertion_total += len(expected)
        assertion_assessed += n_assessed
        assertion_passed += sum(value is True for value in values)
        assertion_failed += sum(value is False for value in values)
        complete_contracts += int(n_assessed == len(expected))
        partial_contracts += int(0 < n_assessed < len(expected))
        contract_passed += int(all(value is True for value in values))
    contracts = {"execution_tier": "future_contract", "eligible_cases": len(contract_cases),
                 "cases_with_assertion_field": assertion_field_cases,
                 "fully_assessed_cases": complete_contracts, "partially_assessed_cases": partial_contracts,
                 "unassessed_cases": len(contract_cases) - complete_contracts - partial_contracts,
                 "passed_cases": contract_passed,
                 "strict_pass_rate": _ratio(contract_passed, len(contract_cases)),
                 "observed_complete_pass_rate": _ratio(contract_passed, complete_contracts),
                 "complete_assessment_coverage": _ratio(complete_contracts, len(contract_cases)),
                 "required_assertions": assertion_total, "assessed_assertions": assertion_assessed,
                 "passed_assertions": assertion_passed, "failed_assertions": assertion_failed,
                 "unassessed_assertions": assertion_total - assertion_assessed,
                 "assertion_assessment_coverage": _ratio(assertion_assessed, assertion_total)}

    attempted = statuses["ok"] + statuses["error"]
    measured_operations = [pred for pred in predictions if pred["status"] in {"ok", "error"}]
    latencies = [pred["latency_ms"] for pred in measured_operations if "latency_ms" in pred]
    costs = [pred["cost_usd"] for pred in measured_operations if "cost_usd" in pred]
    operations = {
        "attempted_cases": attempted, "error_cases": statuses["error"],
        "attempted_error_rate": _ratio(statuses["error"], attempted),
        "error_fraction_all_cases": _ratio(statuses["error"], total),
        "strict_non_success_rate": _ratio(total - statuses["ok"], total),
        "latency_ms": {"sample_count": len(latencies), "attempted_coverage": _ratio(len(latencies), attempted),
                       "p50": _nearest_rank(latencies, .50), "p95": _nearest_rank(latencies, .95),
                       "method": "nearest-rank; observed ok/error executions only"},
        "cost_usd": {"sample_count": len(costs), "attempted_coverage": _ratio(len(costs), attempted),
                     "known_total": sum(costs) if costs else None,
                     "known_mean": _ratio(sum(costs), len(costs)),
                     "unknown_attempted_cases": attempted - len(costs),
                     "note": "Known subtotal only; missing costs are unknown, not zero. Skipped predictions are not executions and are excluded."},
    }
    return {
        "schema_version": "echomind-benchmark-metrics-v1",
        "dataset": {"total_cases": total, "prediction_rows": len(predictions),
                    "prediction_coverage": _ratio(len(predictions), total), "statuses": counts,
                    "kind_counts": {kind: sum(case["kind"] == kind for case in cases) for kind in sorted(KINDS)},
                    "split_counts": dict(sorted(Counter(case.get("split", "unspecified") for case in cases).items()))},
        "intent": intent, "routing": routing, "retrieval": retrieval, "behavior": behavior,
        "facts": facts, "citations": citations, "faithfulness": faithfulness,
        "contracts": contracts, "operations": operations,
        "judge_methods": dict(sorted(Counter(pred["judge_method"] for pred in predictions if "judge_method" in pred).items())),
        "interpretation": "Strict scores include every eligible case; observed scores require coverage. Future contracts are reported separately. No weighted overall score.",
    }


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read JSONL without accepting JavaScript NaN/Infinity constants."""
    result = []

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant {value}")

    with Path(path).open("r", encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line, parse_constant=reject_constant)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected an object")
            result.append(value)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", action="append", nargs="+", required=True,
                        help="one or more case JSONL paths; may be repeated")
    parser.add_argument("--predictions", required=True, help="prediction JSONL path")
    parser.add_argument("--output", required=True, help="output metrics JSON path")
    parser.add_argument("--split", choices=("dev", "test", "all"), default="all")
    args = parser.parse_args(argv)
    try:
        cases = [case for paths in args.cases for path in paths for case in read_jsonl(path)]
        predictions = read_jsonl(args.predictions)
        # Validate the complete input before split selection: a typo/duplicate
        # cannot disappear merely because its row belongs to another split.
        indexed = _validate_cases(cases)
        _validate_predictions(predictions, indexed)
        if args.split != "all":
            cases = [case for case in cases if case.get("split") == args.split]
            selected = {case["id"] for case in cases}
            predictions = [pred for pred in predictions if pred["case_id"] in selected]
        report = evaluate(cases, predictions)
        report["selection"] = {"split": args.split,
                               "input_case_count": len(indexed),
                               "selected_case_count": len(cases)}
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                          encoding="utf-8", newline="\n")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
