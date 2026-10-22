"""Export corpus-only import payloads and a limited legacy evaluation payload."""
from common import DATA, ROOT, load_cases, read_jsonl, write_json, write_jsonl


def main():
    docs, cases = read_jsonl(DATA / "corpus.jsonl"), load_cases()
    output = ROOT / "exports"
    # Never put questions, reference facts, qrels or labels in the search index.
    write_json(output / "knowledge_add.json",
               {"documents": [{"title": d["title"], "content": d["content"]} for d in docs]})
    # Version and stable IDs are source metadata, not question/answer labels.
    write_json(output / "knowledge_add_versioned.json", {"documents": [
        {"document_id": d["id"], "title": d["title"], "content": d["content"],
         "product_version": d["version"], "knowledge_base_id": "supportlab-fixture-v1-1"}
        for d in docs]})
    write_json(output / "title_to_document_id.json", {d["title"]: d["id"] for d in docs})
    for split in ("dev", "test"):
        single_turn = [c for c in cases if c["kind"] == "intent" and c["split"] == split and not c["history"]]
        write_json(output / f"legacy_intent_{split}.json",
                   {"intent_cases": [{"message": c["query"], "expected_intent": c["expected_intent"]}
                                     for c in single_turn],
                    "dialog_cases": []})
    write_jsonl(output / "all_cases.jsonl", cases)
    # Deliberately skipped placeholders are not score claims.
    pending = []
    for case in cases:
        row = {"case_id": case["id"], "status": "skipped", "reason": "not_run"}
        if case["kind"] == "rag":
            row["fact_checks"] = {f["id"]: None for f in case["required_facts"]}
        if case["kind"] == "contract":
            row["assertions"] = {name: None for name in case["expected_assertions"]}
        pending.append(row)
    write_jsonl(output / "pending_predictions.jsonl", pending)
    print("Exported corpus-only payload, title mapping, legacy single-turn intent payloads and pending rows.")


if __name__ == "__main__":
    main()
