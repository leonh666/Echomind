"""Run an offline retrieval baseline or explicitly call an isolated EchoMind API.

No application imports, .env loading, automatic corpus import or model calls occur
in baseline mode. API mode may incur upstream model charges; it is opt-in.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from common import DATA, ROOT, load_cases, read_jsonl, sha256, write_json, write_jsonl


def tokenize(text):
    lower = text.casefold()
    tokens = re.findall(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", lower)
    for segment in re.findall(r"[\u4e00-\u9fff]+", lower):
        tokens.extend(segment[i:i+2] for i in range(len(segment)-1))
        if len(segment) == 1:
            tokens.append(segment)
    return tokens


def query_with_history(case):
    # Same deterministic protocol for offline and API retrieval. No gold rewrite,
    # reference facts or answer labels are used as query inputs.
    previous = [turn["content"] for turn in case.get("history", []) if turn["role"] == "user"]
    return " ".join(previous[-2:] + [case["query"]])


def legacy_chunks(text, chunk_size=500):
    """Match the current KnowledgeBase sentence/character chunking protocol.

    A sentence longer than the target remains intact, as in the legacy backend.
    Keeping this explicit permits like-for-like depth comparisons, without
    importing ChromaDB or application startup code.
    """
    if len(text) <= chunk_size:
        return [text] if text.strip() else []
    chunks, current = [], ""
    for sentence in text.replace("\n", "。").split("。"):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(current) + len(sentence) + 1 > chunk_size:
            if current:
                chunks.append(current)
            current = sentence
        else:
            current = current + "。" + sentence if current else sentence
    if current:
        chunks.append(current)
    return chunks


class BM25:
    def __init__(self, documents, k1=1.5, b=0.75):
        self.documents = [
            {"id": doc["id"], "chunk_id": doc["id"] + "-" + str(index),
             "title": doc["title"], "content": chunk}
            for doc in documents for index, chunk in enumerate(legacy_chunks(doc["content"]))
        ]
        self.k1, self.b = k1, b
        self.counters = [Counter(tokenize(d["title"] + " " + d["content"])) for d in self.documents]
        self.lengths = [sum(tokens.values()) for tokens in self.counters]
        self.average_length = sum(self.lengths) / len(self.lengths) if self.lengths else 1.0
        self.df = Counter(token for counts in self.counters for token in counts)

    def search(self, query, top_k=5):
        terms = sorted(set(tokenize(query)))
        scores = []
        for doc, counts, length in zip(self.documents, self.counters, self.lengths):
            score = 0.0
            for term in terms:
                frequency = counts[term]
                if not frequency:
                    continue
                idf = math.log(1 + (len(self.documents) - self.df[term] + 0.5) / (self.df[term] + 0.5))
                denominator = frequency + self.k1 * (1 - self.b + self.b * length / self.average_length)
                score += idf * frequency * (self.k1 + 1) / denominator
            if score > 0:
                scores.append((score, doc["id"]))
        scores.sort(key=lambda item: (-item[0], item[1]))
        return scores[:top_k]


def http_json(base_url, path, body, query=None, token=None, timeout=90):
    url = base_url.rstrip("/") + path
    if query:
        url += "?" + urlencode(query)
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method="POST")
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("API response exceeds 8 MiB")
    result = json.loads(raw.decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("API response is not an object")
    return result


def call_api(case, mode, base_url, docs, token, timeout, run_id, top_k):
    if mode == "retrieval":
        result = http_json(base_url, "/search", None,
                           {"query": query_with_history(case), "top_k": top_k}, token, timeout)
        items = result.get("results")
        if not isinstance(items, list):
            raise ValueError("/search results is not a list; check dependency failure")
        if result.get("success") is False or result.get("fallback") is True:
            raise ValueError("/search reported failure or fallback")
        if any(isinstance(item, dict) and item.get("fallback") is True for item in items):
            raise ValueError("/search contains fallback candidates; retrieval dependency degraded")
        title_map = {d["title"]: d["id"] for d in docs}
        known_ids = {d["id"] for d in docs}
        ids, unmapped = [], 0
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("/search candidate is not an object")
            actual_id = item.get("document_id")
            identifier = actual_id if actual_id in known_ids else title_map.get(item.get("title")) or actual_id
            if not identifier:
                unmapped += 1
                identifier = "unmapped-" + hashlib.sha256(str(item.get("title", "")).encode()).hexdigest()[:16]
            ids.append(identifier)
        return {"retrieved_document_ids": ids, "unmapped_candidates": unmapped,
                "query_protocol": "last_two_user_turns_plus_query",
                "api_reranked_flag": result.get("reranked")}
    conversation = None
    # Only replay user turns: assistant fixture history is never passed as a
    # fabricated actual model response. Each case gets an isolated conversation.
    messages = [turn["content"] for turn in case.get("history", []) if turn["role"] == "user"] + [case["query"]]
    for message in messages:
        result = http_json(base_url, "/chat",
                           {"message": message, "user_id": "bench-" + run_id + "-" + case["id"],
                            "conv_id": conversation},
                           token=token, timeout=timeout)
        conversation = result.get("conv_id")
        if not conversation or "response" not in result:
            raise ValueError("/chat missing conversation or response")
    agents = result.get("agent_types") or ([result["agent_type"]] if result.get("agent_type") else [])
    prediction = {"response": result["response"], "request_id": result.get("request_id"),
                  "history_protocol": "replay_user_turns", "conv_id": conversation}
    if "intent" in result:
        prediction["intent" if case["kind"] == "intent" else "observed_intent"] = result["intent"]
    if agents:
        prediction["agents" if case["kind"] == "intent" else "observed_agents"] = agents
    # Do not guess semantic behavior, faithfulness, reference coverage, tool
    # completion or cost from response strings. These require explicit evidence.
    return prediction


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("baseline", "api"))
    parser.add_argument("--split", choices=("dev", "test", "all"), default="dev")
    parser.add_argument("--mode", choices=("retrieval", "chat"), default="retrieval")
    parser.add_argument("--base-url")
    parser.add_argument("--token-env", default="ECHOMIND_EVAL_BEARER")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--overwrite", action="store_true",
                        help="Explicitly replace existing output paths; default preserves prior runs")
    args = parser.parse_args()
    if args.top_k < 1 or args.timeout <= 0 or (args.limit is not None and args.limit < 1):
        parser.error("top-k, timeout and limit must be positive")
    if args.command == "api":
        if not args.base_url or urlparse(args.base_url).scheme not in {"http", "https"}:
            parser.error("api requires an explicit http(s) --base-url")
    selected = [c for c in load_cases(args.split) if
                c["kind"] in ({"rag"} if args.command == "baseline" or args.mode == "retrieval"
                              else {"rag", "intent"})]
    if not selected:
        parser.error("no applicable cases")
    docs = read_jsonl(DATA / "corpus.jsonl")
    engine = BM25(docs) if args.command == "baseline" else None
    run_id = uuid4().hex[:12]
    name = "bm25" if engine else "api_" + args.mode
    output_path = args.output or ROOT / "reports" / f"{name}_{args.split}_{run_id}.predictions.jsonl"
    report_path = args.report or output_path.with_suffix(".metrics.json")
    if output_path.resolve() == report_path.resolve():
        parser.error("predictions and metrics must use different paths")
    if not args.overwrite and (output_path.exists() or report_path.exists()):
        parser.error("output already exists; choose a new path or explicitly use --overwrite")
    predictions, attempted = [], 0
    started_at = datetime.now(timezone.utc).isoformat()
    for case in selected:
        if args.limit is not None and attempted >= args.limit:
            predictions.append({"case_id": case["id"], "status": "skipped", "reason": "limit_reached"})
            continue
        attempted += 1
        start = time.perf_counter()
        try:
            if engine:
                ranked = engine.search(query_with_history(case), args.top_k)
                output = {"retrieved_document_ids": [identifier for _, identifier in ranked],
                          "retrieval_scores": [round(score, 8) for score, _ in ranked],
                          "query_protocol": "last_two_user_turns_plus_query"}
            else:
                output = call_api(case, args.mode, args.base_url, docs,
                                  os.environ.get(args.token_env), args.timeout, run_id, args.top_k)
            predictions.append({"case_id": case["id"], "status": "ok", **output,
                                "latency_ms": round((time.perf_counter()-start)*1000, 4)})
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            # No automatic retries: avoid duplicate charges and preserve failure.
            predictions.append({"case_id": case["id"], "status": "error",
                                "error_type": type(error).__name__,
                                "error": str(error)[:500],
                                "latency_ms": round((time.perf_counter()-start)*1000, 4)})
    write_jsonl(output_path, predictions)
    from metrics import evaluate
    report = evaluate(selected, predictions,
                      ks=tuple(sorted({k for k in (1, 3, 5, args.top_k) if k <= args.top_k})))
    report["run_metadata"] = {
        "run_id": run_id, "started_at": started_at, "split": args.split,
        "method": "stdlib_bm25_k1=1.5_b=0.75" if engine else "current_echo_api_" + args.mode,
        "ranking_unit": "chunk",
        "relevance_unit": "source_document",
        "chunking_protocol": "legacy_sentence_char_target_500" if engine else "current_api_actual_chunks",
        "index_chunk_count": len(engine.documents) if engine else None,
        "documents_are_ranking_units": False,
        "corpus_sha256": sha256(DATA / "corpus.jsonl"),
        "case_sha256": {name: sha256(DATA / name) for name in ("intent_cases.jsonl", "rag_cases.jsonl")},
        "runner_sha256": sha256(Path(__file__)),
        "metrics_sha256": sha256(ROOT / "metrics.py"),
        "manifest_sha256": sha256(ROOT / "manifest.json") if (ROOT / "manifest.json").exists() else None,
        "api_target": args.base_url if not engine else None,
        "api_calls_may_cost_money": not bool(engine),
        "top_k_requested": args.top_k,
        "limitations": [
            "Synthetic unreviewed fixture results are not production performance.",
            "No automatic semantic, citation, contract or cost judgments.",
            "API imports no corpus automatically; isolate and load corpus-only payload separately.",
            "API search may report reranked=true even when skipped/failed in current implementation.",
            "Legacy API uses one synthetic user per case; authenticated APIs need equivalent identity/memory isolation.",
        ],
    }
    write_json(report_path, report)
    print(json.dumps({"predictions": str(output_path), "report": str(report_path),
                      "selected": len(selected), "attempted": attempted,
                      "errors": sum(p["status"] == "error" for p in predictions)},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
