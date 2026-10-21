"""Validate fixture integrity and publish versioned counts/hashes; not human QA."""
import argparse
import ast
from collections import Counter
import json
import re

from common import CASE_FILES, DATA, ROOT, load_cases, read_jsonl, sha256, write_json


def normalized(text):
    return re.sub(r"[\W_]+", "", text.casefold(), flags=re.UNICODE)


def current_labels_and_templates():
    tree = ast.parse((ROOT.parents[1] / "core" / "intent_recognizer.py").read_text(encoding="utf-8-sig"))
    labels, templates = set(), set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "IntentCategory":
            for item in node.body:
                if isinstance(item, ast.Assign) and isinstance(item.value, ast.Constant):
                    labels.add(item.value.value)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "_TEMPLATES" and isinstance(node.value, ast.Dict):
                for value in node.value.values:
                    templates.update(normalized(text) for text in ast.literal_eval(value))
    return labels, templates


def validate():
    docs, cases = read_jsonl(DATA / "corpus.jsonl"), load_cases()
    labels, templates = current_labels_and_templates()
    errors, exact_overlaps = [], []
    document_by_id, seen_cases, group_splits, seen_queries = {}, set(), {}, {}
    content_keys, all_facts = set(), {}

    def check(condition, message):
        if not condition:
            errors.append(message)

    def group(row):
        key, split = row.get("group_id"), row.get("split")
        check(bool(key), f"{row.get('id')}: missing group")
        check(split in {"dev", "test"}, f"{row.get('id')}: unsupported split")
        check(key not in group_splits or group_splits[key] == split,
              f"{key}: group crosses split")
        group_splits[key] = split

    for doc in docs:
        identifier = doc["id"]
        check(identifier not in document_by_id, f"duplicate document: {identifier}")
        document_by_id[identifier] = doc
        group(doc)
        check(doc.get("origin") == "synthetic_original", f"{identifier}: origin")
        check(doc.get("source_kind") == "fictional_policy_fixture", f"{identifier}: source_kind")
        check(doc.get("access_scope") == "public_fixture", f"{identifier}: access_scope")
        check(doc.get("title") and doc.get("content") and doc.get("version"), f"{identifier}: missing content")
        key = normalized(doc.get("content", ""))
        check(key not in content_keys, f"{identifier}: duplicate normalized document")
        content_keys.add(key)
        for fact in doc.get("facts", []):
            check(fact["id"] not in all_facts, f"duplicate fact id: {fact['id']}")
            all_facts[fact["id"]] = (identifier, fact["text"])
            check(fact["text"] in doc["content"], f"{identifier}: fact not verbatim in source: {fact['id']}")
    check(len({doc["title"] for doc in docs}) == len(docs), "document titles must be unique")

    for case in cases:
        identifier = case["id"]
        check(identifier not in seen_cases, f"duplicate case id: {identifier}")
        seen_cases.add(identifier)
        group(case)
        check(case.get("origin") == "synthetic_original", f"{identifier}: origin")
        check(case.get("human_review") == "pending", f"{identifier}: review status needs evidence")
        check(isinstance(case.get("query"), str) and bool(case["query"].strip()), f"{identifier}: query")
        history = case.get("history", [])
        check(isinstance(history, list), f"{identifier}: history")
        for turn in history:
            check(turn.get("role") in {"user", "assistant"} and isinstance(turn.get("content"), str),
                  f"{identifier}: malformed history")
        query_key = normalized(json.dumps(history, ensure_ascii=False) + case["query"])
        check(query_key not in seen_queries, f"{identifier}: duplicate query+history: {seen_queries.get(query_key)}")
        seen_queries[query_key] = identifier
        if case["kind"] == "intent":
            check(case["expected_intent"] in labels, f"{identifier}: label absent in current enum")
            check(bool(case.get("expected_agents")), f"{identifier}: routing gold missing")
            if normalized(case["query"]) in templates:
                exact_overlaps.append(identifier)
        elif case["kind"] == "rag":
            relevant = case["relevant_document_ids"]
            check(len(relevant) == len(set(relevant)), f"{identifier}: duplicate qrel")
            check(case.get("expected_behavior") in {"answer", "refuse", "clarify"}, f"{identifier}: behavior")
            if case["expected_behavior"] == "answer":
                check(bool(relevant) and bool(case["required_facts"]), f"{identifier}: answer needs evidence")
            else:
                check(not relevant and not case["required_facts"], f"{identifier}: noanswer must not have answer gold")
            for doc_id in relevant:
                check(doc_id in document_by_id, f"{identifier}: unknown document: {doc_id}")
                if doc_id in document_by_id:
                    source = document_by_id[doc_id]
                    check(source["group_id"] == case["group_id"] and source["split"] == case["split"],
                          f"{identifier}: evidence crosses source-family split")
            fact_ids = []
            for fact in case["required_facts"]:
                fact_ids.append(fact["id"])
                check(fact["id"] in all_facts, f"{identifier}: unknown fact {fact['id']}")
                if fact["id"] in all_facts:
                    source_id, text = all_facts[fact["id"]]
                    check(fact["text"] == text, f"{identifier}: changed fact {fact['id']}")
                    check(source_id in fact["evidence_document_ids"], f"{identifier}: wrong fact source")
                check(set(fact["evidence_document_ids"]).issubset(set(relevant)),
                      f"{identifier}: fact evidence absent from qrels")
            check(len(fact_ids) == len(set(fact_ids)), f"{identifier}: repeated fact in question")
        elif case["kind"] == "contract":
            check(case.get("execution_tier") == "future_contract", f"{identifier}: contract must not imply implementation")
            assertions = case.get("expected_assertions", [])
            check(bool(assertions) and len(assertions) == len(set(assertions)),
                  f"{identifier}: expected assertions")
        else:
            errors.append(f"{identifier}: unknown kind")

    check(not exact_overlaps, "intent questions exactly overlap prompt templates")
    manifest = {
        "dataset_version": "echomind-benchmark-v1.0",
        "schema_version": "1.0",
        "created_date": "2026-10-21",
        "origin": "synthetic_original",
        "human_verified_cases": 0,
        "review_status": "all_pending_independent_human_review",
        "documents": len(docs),
        "cases": len(cases),
        "counts_by_kind": dict(sorted(Counter(c["kind"] for c in cases).items())),
        "counts_by_split": dict(sorted(Counter(c["split"] for c in cases).items())),
        "counts_by_kind_split": {
            kind: dict(sorted(Counter(c["split"] for c in cases if c["kind"] == kind).items()))
            for kind in sorted({c["kind"] for c in cases})},
        "intent_labels": sorted({c["expected_intent"] for c in cases if c["kind"] == "intent"}),
        "rag_categories": dict(sorted(Counter(c["category"] for c in cases if c["kind"] == "rag").items())),
        "contract_suites": dict(sorted(Counter(c["suite"] for c in cases if c["kind"] == "contract").items())),
        "source_family_groups": len(group_splits),
        "prompt_template_exact_overlaps": exact_overlaps,
        "files": {name: {"sha256": sha256(DATA / name)}
                  for name in ("corpus.jsonl",) + CASE_FILES},
        "limitations": [
            "Synthetic fixtures; not production traffic or independently human-verified gold.",
            "Frozen grouped test fixtures are visible in this repository, not a blind benchmark.",
            "Corpus is intentionally indexed across dev/test; test questions, labels and facts are never index inputs.",
            "Contracts describe future acceptance requirements; no implementation pass is implied.",
            "No external benchmark rows or user data are bundled.",
        ],
    }
    return {"valid": not errors, "errors": errors, "manifest": manifest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-manifest", action="store_true")
    args = parser.parse_args()
    result = validate()
    path = ROOT / "manifest.json"
    if path.exists() and not args.write_manifest:
        saved = json.loads(path.read_text(encoding="utf-8-sig"))
        if saved != result["manifest"]:
            result["valid"] = False
            result["errors"].append("manifest differs: regenerate intentionally with --write-manifest")
    if result["valid"] and args.write_manifest:
        write_json(path, result["manifest"])
    write_json(ROOT / "reports" / "validation.json", result)
    print(json.dumps({"valid": result["valid"], "errors": result["errors"],
                      "documents": result["manifest"]["documents"],
                      "cases": result["manifest"]["cases"]}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["valid"] else 1)


if __name__ == "__main__":
    main()
