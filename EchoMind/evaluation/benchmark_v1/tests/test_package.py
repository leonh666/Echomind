"""Integration checks on packaged data and adapters; never calls a live service."""
import json
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import DATA, load_cases, read_jsonl, write_json, write_jsonl
from metrics import evaluate
from run_benchmark import BM25, call_api, main as run_main, query_with_history
from validate import validate


class DatasetTests(unittest.TestCase):
    def test_generated_bytes_use_lf_so_git_checkout_preserves_manifest_hashes(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            for name, writer, value in (("data.jsonl", write_jsonl, [{"text": "中文"}]),
                                        ("meta.json", write_json, {"text": "中文"})):
                target = Path(folder) / name
                writer(target, value)
                self.assertNotIn(b"\r\n", target.read_bytes())
                self.assertTrue(target.read_bytes().endswith(b"\n"))

    def test_dataset_is_referentially_valid_and_review_is_not_overclaimed(self):
        result = validate()
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["manifest"]["documents"], 36)
        self.assertEqual(result["manifest"]["cases"], 276)
        self.assertEqual(len(result["manifest"]["intent_labels"]), 19)
        self.assertEqual(result["manifest"]["human_verified_cases"], 0)

    def test_legacy_export_excludes_unsupported_context_and_disables_dialog_judge(self):
        for split in ("dev", "test"):
            payload = json.loads((ROOT / "exports" / f"legacy_intent_{split}.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["dialog_cases"], [])
            eligible = [c for c in load_cases(split) if c["kind"] == "intent" and not c["history"]]
            self.assertEqual(len(payload["intent_cases"]), len(eligible))
            self.assertEqual({c["message"] for c in payload["intent_cases"]}, {c["query"] for c in eligible})

    def test_import_payload_contains_no_answer_or_case_labels(self):
        payload = json.loads((ROOT / "exports" / "knowledge_add.json").read_text(encoding="utf-8"))
        source = read_jsonl(DATA / "corpus.jsonl")
        self.assertEqual(len(payload["documents"]), 36)
        self.assertEqual(payload["documents"], [{"title": d["title"], "content": d["content"]} for d in source])
        self.assertTrue(all(set(doc) == {"title", "content"} for doc in payload["documents"]))


class AdapterTests(unittest.TestCase):
    def test_existing_output_rejected_before_live_api_calls(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            output = Path(folder) / "existing.jsonl"
            output.write_text("keep", encoding="utf-8")
            argv = ["run_benchmark.py", "api", "--base-url", "http://example.invalid",
                    "--output", str(output)]
            with patch("sys.argv", argv), patch("run_benchmark.http_json") as api:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    run_main()
            api.assert_not_called()
            self.assertEqual(output.read_text(encoding="utf-8"), "keep")

    def test_requested_depth_limits_reported_k_and_limit_preserves_coverage(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            output = Path(folder) / "result.jsonl"
            report = Path(folder) / "metrics.json"
            argv = ["run_benchmark.py", "baseline", "--split", "dev", "--top-k", "1",
                    "--limit", "1", "--output", str(output), "--report", str(report)]
            with patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()):
                run_main()
            measured = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(set(measured["retrieval"]["at_k"]), {"1"})
            self.assertEqual(measured["retrieval"]["observed_cases"], 1)
            self.assertLess(measured["retrieval"]["coverage"], 1)

    def test_context_protocol_uses_user_turns_not_reference_answers(self):
        case = {"query": "这次呢？", "history": [
            {"role": "user", "content": "第一次登录问题"},
            {"role": "assistant", "content": "不能作为查询的金标准答案"},
            {"role": "user", "content": "第二次登录问题"}],
            "required_facts": [{"text": "不可读取的标签"}]}
        query = query_with_history(case)
        self.assertEqual(query, "第一次登录问题 第二次登录问题 这次呢？")

    def test_retrieval_adapter_keeps_duplicate_depth_and_unknown_candidates(self):
        case = {"id": "r", "kind": "rag", "query": "q", "history": []}
        docs = [{"id": "d", "title": "唯一标题"}]
        with patch("run_benchmark.http_json", return_value={"results": [
            {"title": "唯一标题"}, {"title": "唯一标题"}, {"title": "其他资料"}]}) as mocked:
            output = call_api(case, "retrieval", "http://example.invalid", docs, None, 3, "run", 5)
        self.assertEqual(output["retrieved_document_ids"][:2], ["d", "d"])
        self.assertTrue(output["retrieved_document_ids"][2].startswith("unmapped-"))
        self.assertEqual(output["unmapped_candidates"], 1)
        self.assertIsNone(mocked.call_args.args[2])
        self.assertEqual(mocked.call_args.args[3]["top_k"], 5)

    def test_dependency_failure_is_not_silently_empty_retrieval(self):
        with patch("run_benchmark.http_json", return_value={"results": None}):
            with self.assertRaises(ValueError):
                call_api({"kind": "rag", "query": "q"}, "retrieval", "http://example.invalid", [], None, 3, "r", 5)

    def test_fallback_candidate_is_a_dependency_error_not_a_retrieval_miss(self):
        with patch("run_benchmark.http_json", return_value={
                "results": [{"title": "降级结果", "content": "暂不可用", "fallback": True}]}):
            with self.assertRaisesRegex(ValueError, "fallback"):
                call_api({"kind": "rag", "query": "q"}, "retrieval", "http://example.invalid", [], None, 3, "r", 5)

    def test_chat_rag_does_not_claim_semantic_or_classification_metric(self):
        case = {"id": "r", "kind": "rag", "query": "q", "history": [],
                "expected_behavior": "answer", "relevant_document_ids": ["d"], "required_facts": []}
        with patch("run_benchmark.http_json", return_value={
                "conv_id": "c", "response": "回答", "intent": "query", "agent_types": ["general"]}):
            output = call_api(case, "chat", "http://example.invalid", [], None, 3, "r", 5)
        self.assertNotIn("behavior", output)
        self.assertNotIn("fact_checks", output)
        self.assertNotIn("intent", output)
        self.assertEqual(output["observed_intent"], "query")
        report = evaluate([case], [{"case_id": "r", "status": "ok", **output}])
        self.assertEqual(report["retrieval"]["observed_cases"], 0)

    def test_baseline_ranking_does_not_read_gold_facts(self):
        docs = [{"id": "a", "title": "Token 认证", "content": "凭证超时需要重新获取令牌。",
                 "facts": [{"text": "此处标签不得影响排名"}]},
                {"id": "b", "title": "物流规则", "content": "承运商配送包裹。", "facts": []}]
        first = BM25(docs).search("Token 凭证")
        docs[0]["facts"] = [{"text": "改变标签不改变检索结果"}]
        self.assertEqual(first, BM25(docs).search("Token 凭证"))
        self.assertEqual(first[0][1], "a")

    def test_chat_cases_do_not_share_long_term_user_memory(self):
        seen = []
        def fake_api(base_url, path, body, **kwargs):
            seen.append(body["user_id"])
            return {"conv_id": "new", "response": "回答", "intent": "query", "agent_types": ["general"]}
        with patch("run_benchmark.http_json", side_effect=fake_api):
            for identifier in ("intent-a", "intent-b"):
                call_api({"id": identifier, "kind": "intent", "query": "q"}, "chat",
                         "http://example.invalid", [], None, 3, "run", 5)
        self.assertEqual(len(set(seen)), 2)

    def test_long_document_returns_actual_chunk_depth_with_duplicate_document_ids(self):
        engine = BM25([{"id": "long", "title": "共同主题", "content": "甲" * 400 + "。" + "乙" * 400 + "。"}])
        self.assertEqual(len(engine.documents), 2)
        self.assertEqual([doc_id for _, doc_id in engine.search("共同主题", top_k=5)], ["long", "long"])


if __name__ == "__main__":
    unittest.main()
