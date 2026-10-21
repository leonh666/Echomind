"""
RAG 知识库 —— 基于 ChromaDB 的真实检索实现。

功能：
  1. 文档导入：将文本切片后存入 ChromaDB（自动生成 Embedding）
  2. 语义检索：根据 query 从知识库中检索最相关的文档片段
  3. 与 MCP 工具框架集成：作为 knowledge_search 工具的真实 handler

ChromaDB 在这里的角色：
  - memory/ 中用于存储对话记忆（情景记忆 + 用户画像）
  - 这里用于存储知识库文档（RAG 检索）
  两者是不同的 collection，互不干扰。
"""
import asyncio
import hashlib
import json
import logging
import math
import re
import threading
from collections import Counter
from typing import Any, Dict, List, Optional
from uuid import uuid4

import chromadb

logger = logging.getLogger(__name__)


class KnowledgeBase:
    """
    基于 ChromaDB 的 RAG 知识库。

    默认 embedding 由固定版本的 Chroma 客户端配置决定，不能假定由服务端执行。
    发布/回滚由本实例的 RLock 串行保护，不是跨进程或跨服务的数据库事务。
    多 worker 部署必须使用外部协调/版本清单；禁止多个实例同时修改该 collection。
    旧片段缺少 organization_id 时不会匹配授权公共库过滤，应显式迁移，避免启动重建向量。
    """

    COLLECTION_NAME = "knowledge_base"

    def __init__(
        self,
        chroma_host: str = "localhost",
        chroma_port: int = 8000,
        chroma_path: str = "./data/chroma",
        allow_local_fallback: bool = True,
        seed_defaults: bool = True,
    ):
        self._lock = threading.RLock()
        self._generation = 0
        self._cache_namespace = uuid4().hex
        self._quarantined = set()
        # 优先连接独立 ChromaDB 服务。
        self._use_server = False
        try:
            # HttpClient 默认也会初始化 ChromaDB telemetry；显式关闭避免 posthog 兼容性错误日志。
            self._client = chromadb.HttpClient(
                host=chroma_host,
                port=chroma_port,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            self._client.heartbeat()
            self._use_server = True
            logger.info(f"知识库 ChromaDB 已连接: {chroma_host}:{chroma_port}")
        except Exception:
            if not allow_local_fallback:
                raise RuntimeError("Knowledge Chroma service unavailable; local fallback is disabled")
            logger.info(f"知识库 ChromaDB 服务不可用，使用本地模式: {chroma_path}")
            self._client = chromadb.PersistentClient(
                path=chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )

        # 使用客户端默认 embedding 配置；部署镜像须准备其模型依赖。
        self._collection = self._client.get_or_create_collection(
            name=self.COLLECTION_NAME,
            metadata={"description": "EchoMind RAG 知识库"},
        )

        # 如果知识库为空，导入默认文档
        if seed_defaults and self._collection.count() == 0:
            self._load_default_docs()

    # ── 文档管理 ──────────────────────────────────────────────────────────────

    def _state(self):
        # Also supports dependency-free __new__ fixtures used by older callers.
        if not hasattr(self, "_lock"):
            self._lock = threading.RLock()
            self._generation, self._cache_namespace, self._quarantined = 0, uuid4().hex, set()
        return self._lock

    @property
    def cache_version(self) -> str:
        with self._state():
            return f"{self._cache_namespace}:{self._generation}"

    @staticmethod
    def combine_where(*filters):
        filters = [f for f in filters if f]
        if any(not isinstance(f, dict) for f in filters):
            raise ValueError("where must be a metadata filter object")
        return {"$and": filters} if len(filters) > 1 else (filters[0] if filters else None)

    def _rows(self, where=None):
        kwargs = {"include": ["documents", "metadatas"]}
        if where:
            kwargs["where"] = where
        result = self._collection.get(**kwargs)
        return [(identifier, text, meta or {}) for identifier, text, meta in zip(
            result.get("ids", []), result.get("documents", []), result.get("metadatas", []))]

    def _published(self, rows):
        # A leftover staged write after a crash quarantines the whole document.
        self._quarantined.update(meta.get("document_id") for _, _, meta in rows
                                 if meta.get("_publication") == "staged")
        return [row for row in rows if row[2].get("_publication", "published") == "published"
                and row[2].get("document_id") not in self._quarantined]

    @staticmethod
    def _string(value, name, default=""):
        if value is None:
            return default
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        return value.strip()

    def _prepare(self, doc):
        if not isinstance(doc, dict):
            raise ValueError("document must be an object")
        content = self._string(doc.get("content"), "content")
        chunks = self._chunk_text(content)
        if not chunks:
            raise ValueError("document content must not be empty")
        meta = dict(doc.get("metadata") or {})
        if not all(isinstance(k, str) and not k.startswith("_") and
                   isinstance(v, (str, int, float, bool)) and
                   (not isinstance(v, float) or math.isfinite(v)) for k, v in meta.items()):
            raise ValueError("metadata must contain non-internal scalar fields")
        for key in ("title", "source_url", "product_version", "locator", "organization_id",
                    "access_scope", "knowledge_base_id", "corpus_version"):
            meta[key] = self._string(doc.get(key, meta.get(key)), key)
        meta["organization_id"] = meta["organization_id"] or "public"
        meta["access_scope"] = meta["access_scope"] or "public"
        meta["knowledge_base_id"] = meta["knowledge_base_id"] or "default"
        identity = [meta["organization_id"], meta["knowledge_base_id"], meta["source_url"] or meta["title"]]
        if not identity[-1] and not doc.get("document_id"):
            raise ValueError("document_id, source_url or title is required")
        document_id = self._string(doc.get("document_id"), "document_id") or "doc-" + hashlib.sha256(
            json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:32]
        version = self._string(doc.get("document_version"), "document_version") or hashlib.sha256(
            json.dumps([content, meta], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
        rows = []
        for i, chunk in enumerate(chunks):
            chunk_id = "chunk-" + hashlib.sha256(
                json.dumps([document_id, version, i, chunk], ensure_ascii=False).encode()).hexdigest()
            locator = meta["locator"] or "text"
            row_meta = {**meta, "document_id": document_id, "document_version": version,
                        "chunk_id": chunk_id, "chunk_index": i, "total_chunks": len(chunks),
                        "locator": f"{locator}#chunk={i + 1}", "_publication": "published"}
            rows.append((chunk_id, chunk, row_meta))
        return document_id, rows

    def _upsert(self, rows, publication=None):
        if rows:
            self._collection.upsert(ids=[r[0] for r in rows], documents=[r[1] for r in rows],
                                    metadatas=[{**r[2], **({"_publication": publication} if publication else {})}
                                               for r in rows])

    def _replace(self, document_id, new_rows):
        existing = self._rows({"document_id": document_id})
        if any(meta.get("_publication") == "staged" for _, _, meta in existing):
            self._quarantined.add(document_id)
        if document_id in self._quarantined:
            raise RuntimeError("document has an incomplete publication; delete/reimport or restore it explicitly")
        if any(meta.get("organization_id", "public") != new_rows[0][2]["organization_id"]
               or meta.get("knowledge_base_id", "default") != new_rows[0][2]["knowledge_base_id"]
               for _, _, meta in existing):
            raise ValueError("document_id cannot replace another organization or knowledge base")
        old_ids, new_ids = {r[0] for r in existing}, {r[0] for r in new_rows}
        self._generation += 1  # invalidate caches even if mutation/rollback fails
        try:
            self._upsert(new_rows, "staged")
            stale = sorted(old_ids - new_ids)
            if stale:
                self._collection.delete(ids=stale)
            self._upsert(new_rows)  # publish only after old-version deletion succeeds
        except Exception as error:
            restored = True
            try:
                self._upsert(existing)
            except Exception:
                restored = False
            try:
                created = sorted(new_ids - old_ids)
                if created:
                    self._collection.delete(ids=created)
            except Exception:
                restored = False
            if not restored:
                self._quarantined.add(document_id)
                logger.exception("Knowledge publication rollback incomplete: %s", document_id)
            raise RuntimeError(f"knowledge publication failed; rollback {'completed' if restored else 'incomplete'}") from error

    def add_documents(self, documents: List[Dict[str, Any]]) -> int:
        """Upsert by stable document identity; each document publishes independently.

        Returns submitted chunk count for API compatibility. An empty batch is a no-op;
        empty documents are rejected before any mutation. No batch-wide transaction.
        """
        prepared = [self._prepare(doc) for doc in documents]
        if len({identifier for identifier, _ in prepared}) != len(prepared):
            raise ValueError("duplicate document_id in batch")
        with self._state():
            for document_id, rows in prepared:
                if not callable(getattr(self._collection, "get", None)):
                    # Legacy write-only adapters support idempotent add, not lifecycle.
                    self._upsert(rows)
                    self._generation += 1
                else:
                    self._replace(document_id, rows)
        return sum(len(rows) for _, rows in prepared)

    async def add_documents_async(self, documents):
        return await asyncio.to_thread(self.add_documents, documents)

    def update_document(self, document_id, document):
        with self._state():
            existing = self.get_document(document_id)
            if existing is None:
                raise KeyError(document_id)
            merged = {**existing, **document, "document_id": document_id}
            if "document_version" not in document:
                merged.pop("document_version", None)
            # list/get locators identify chunks; preserve source locator only on explicit input.
            if "locator" not in document:
                merged.pop("locator", None)
            return self.add_documents([merged])

    async def update_document_async(self, document_id, document):
        return await asyncio.to_thread(self.update_document, document_id, document)

    def delete_document(self, document_id):
        with self._state():
            rows = self._rows({"document_id": document_id})
            if not rows:
                return 0
            self._generation += 1
            try:
                self._collection.delete(ids=[r[0] for r in rows])
            except Exception as error:
                try:
                    self._upsert(rows)
                except Exception:
                    self._quarantined.add(document_id)
                raise RuntimeError("document deletion failed") from error
            self._quarantined.discard(document_id)
            return len(rows)

    async def delete_document_async(self, document_id):
        return await asyncio.to_thread(self.delete_document, document_id)

    def list_documents(self, where=None):
        with self._state():
            grouped = {}
            for identifier, text, meta in self._published(self._rows(where)):
                doc_id = meta.get("document_id", "legacy-" + identifier)
                grouped.setdefault(doc_id, {**{k: v for k, v in meta.items() if not k.startswith("_")},
                                           "document_id": doc_id, "chunks": []})["chunks"].append(
                    {"chunk_id": identifier, "content": text, "chunk_index": meta.get("chunk_index", 0)})
            for doc in grouped.values():
                doc["chunks"].sort(key=lambda c: c["chunk_index"])
                doc["chunk_count"] = len(doc["chunks"])
                doc["content"] = "".join(c["content"] for c in doc["chunks"])
                known = {"title", "source_url", "product_version", "locator", "organization_id", "access_scope",
                         "knowledge_base_id", "corpus_version", "document_id", "document_version", "chunk_id",
                         "chunk_index", "total_chunks", "chunks", "chunk_count", "content"}
                doc["metadata"] = {key: value for key, value in doc.items() if key not in known}
            return sorted(grouped.values(), key=lambda d: d["document_id"])

    async def list_documents_async(self, where=None):
        return await asyncio.to_thread(self.list_documents, where)

    def get_document(self, document_id, where=None):
        return next(iter(self.list_documents(self.combine_where(where, {"document_id": document_id}))), None)

    async def get_document_async(self, document_id, where=None):
        return await asyncio.to_thread(self.get_document, document_id, where)

    def stats(self, where=None):
        with self._state():
            raw = self._rows(where)
            active = self._published(raw)
            return {"total_chunks": len(active), "documents": len({r[2].get("document_id", r[0]) for r in active}),
                    "legacy_chunks": sum("organization_id" not in r[2] for r in raw),
                    "unpublished_chunks": len(raw) - len(active),
                    "quarantined_documents": sorted(d for d in self._quarantined if d and
                                                     any(meta.get("document_id") == d for _, _, meta in raw)),
                    "cache_version": self.cache_version, "publication_mode": "single_instance_lock"}

    async def stats_async(self, where=None):
        return await asyncio.to_thread(self.stats, where)

    def migrate_legacy_documents(self, organization_id="public"):
        """Explicit metadata-only migration; never regenerates existing embeddings.

        Legacy IDs remain chunk IDs and get deterministic document identities. The
        operator must authorize the target organization; this is not run on startup.
        """
        organization_id = self._string(organization_id, "organization_id")
        if not organization_id:
            raise ValueError("organization_id is required")
        with self._state():
            rows = [r for r in self._rows() if "organization_id" not in r[2]]
            if not rows:
                return 0
            ids, metas = [], []
            for identifier, _, meta in rows:
                identity = [organization_id, "default", meta.get("title", identifier)]
                document_id = "doc-" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:32]
                ids.append(identifier)
                metas.append({**meta, "organization_id": organization_id, "knowledge_base_id": "default",
                              "access_scope": "public" if organization_id == "public" else "organization",
                              "document_id": document_id, "chunk_id": identifier, "document_version": "legacy",
                              "source_url": "", "product_version": "", "locator": f"chunk:{meta.get('chunk_index', 0) + 1}",
                              "_publication": "published"})
            self._generation += 1
            self._collection.update(ids=ids, metadatas=metas)
            return len(ids)

    @staticmethod
    def _validate_top_k(top_k):
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 100:
            raise ValueError("top_k must be an integer between 1 and 100")

    @staticmethod
    def _item(row):
        identifier, text, meta = row
        return {**{k: v for k, v in meta.items() if not k.startswith("_")}, "title": meta.get("title", ""),
                "content": text, "chunk_id": identifier, "document_id": meta.get("document_id", "legacy-" + identifier),
                "chunk": meta.get("chunk_index", 0), "distance": None}

    @staticmethod
    def _tokens(text):
        text = text.casefold()
        tokens = re.findall(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", text)
        for segment in re.findall(r"[\u4e00-\u9fff]+", text):
            tokens.extend(segment[i:i + 2] for i in range(len(segment) - 1))
            if len(segment) == 1:
                tokens.append(segment)
        return tokens

    def _bm25(self, query, rows, top_k):
        # Rows were fetched with the authorization filter BEFORE term statistics.
        counters = [Counter(self._tokens(r[2].get("title", "") + " " + r[1])) for r in rows]
        lengths = [sum(c.values()) for c in counters]
        average = sum(lengths) / len(rows) if rows else 1
        df = Counter(token for counter in counters for token in counter)
        ranked = []
        for row, counter, length in zip(rows, counters, lengths):
            score = 0.0
            for term in set(self._tokens(query)):
                tf = counter[term]
                if tf:
                    idf = math.log(1 + (len(rows) - df[term] + .5) / (df[term] + .5))
                    score += idf * tf * 2.5 / (tf + 1.5 * (.25 + .75 * length / (average or 1)))
            if score > 0:
                ranked.append({**self._item(row), "score": score, "score_type": "bm25"})
        return sorted(ranked, key=lambda item: (-item["score"], item["chunk_id"]))[:top_k]

    def search(self, query: str, top_k: int = 5, *, where=None, strategy="vector"):
        self._validate_top_k(top_k)
        query = self._string(query, "query")
        if not query:
            raise ValueError("query must not be empty")
        if strategy not in {"vector", "bm25", "hybrid"}:
            raise ValueError("strategy must be vector, bm25 or hybrid")
        with self._state():
            raw = self._rows(where)
            rows = self._published(raw)
            if not rows:
                return []
            recall_k = min(len(rows), max(top_k * 4, 20)) if strategy == "hybrid" else top_k
            keyword = self._bm25(query, rows, recall_k) if strategy != "vector" else []
            if strategy == "bm25":
                return keyword
            # Overfetch only the number of known unpublished/quarantined rows, so
            # post-publication filtering cannot displace otherwise valid top-K.
            kwargs = {"query_texts": [query], "n_results": min(recall_k + len(raw) - len(rows), len(raw)),
                      "include": ["documents", "metadatas", "distances"]}
            if where:
                kwargs["where"] = where
            result = self._collection.query(**kwargs)
            active = {r[0]: r for r in rows}
            vector = []
            for identifier, distance in zip((result.get("ids") or [[]])[0], (result.get("distances") or [[]])[0]):
                if identifier in active:
                    if isinstance(distance, bool) or not isinstance(distance, (float, int)) or not math.isfinite(distance):
                        raise ValueError("vector backend returned invalid distance")
                    vector.append({**self._item(active[identifier]), "distance": float(distance),
                                   "score": -float(distance), "score_type": "negative_distance",
                                   "distance_metric": (getattr(self._collection, "metadata", None) or {}).get("hnsw:space", "unspecified")})
            vector = vector[:recall_k]
            if strategy == "vector":
                return vector[:top_k]
            fused = {}
            for method, candidates in (("vector", vector), ("bm25", keyword)):
                for rank, candidate in enumerate(candidates, 1):
                    item = fused.setdefault(candidate["chunk_id"], {**candidate, "score": 0.0, "score_type": "rrf", "ranks": {}})
                    item["score"] += 1 / (60 + rank)
                    item["ranks"][method] = rank
            return sorted(fused.values(), key=lambda item: (-item["score"], item["chunk_id"]))[:top_k]

    async def search_async(self, query, top_k=5, *, where=None, strategy="vector"):
        return await asyncio.to_thread(self.search, query, top_k, where=where, strategy=strategy)

    @property
    def doc_count(self):
        return self.stats()["total_chunks"]

    async def doc_count_async(self):
        return await asyncio.to_thread(lambda: self.doc_count)

    # ── MCP 工具 handler ─────────────────────────────────────────────────────

    async def search_handler(self, params: Dict[str, Any], context: Any) -> List[Dict]:
        """
        作为 MCP 工具的 handler 注册。

        MCPToolManager.register(Tool(
            name="knowledge_search",
            handler=kb.search_handler,
            ...
        ))
        """
        query = params.get("query", "")
        top_k = params.get("top_k", 5)
        context = context or {}
        return await self.search_async(query, top_k=top_k,
                                       where=self.combine_where(context.get("where"), params.get("where")),
                                       strategy=params.get("strategy", "vector"))

    # ── 内部方法 ──────────────────────────────────────────────────────────────

    def _chunk_text(self, text: str, chunk_size: int = 500) -> List[str]:
        """Preserve punctuation/newlines; hard-split a paragraph above the limit."""
        if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        text = text.strip()
        chunks = []
        while text:
            end = min(chunk_size, len(text))
            if end < len(text):
                boundary = max(text.rfind(sep, 0, end) for sep in ("。", "\n", ". ", "！", "？"))
                if boundary >= chunk_size // 2:
                    end = boundary + 1
            chunks.append(text[:end])
            text = text[end:]
        return chunks

    def _load_default_docs(self) -> None:
        """导入默认知识库文档（客服场景常见问题）。"""
        default_docs = [
            {
                "title": "退款政策",
                "content": (
                    "退款政策说明。"
                    "用户在购买后 7 天内可以申请无理由退款。"
                    "退款申请提交后，系统会在 1-3 个工作日内审核。"
                    "审核通过后，款项将在 5-7 个工作日内退回原支付账户。"
                    "如果商品已发货，需要先完成退货流程才能退款。"
                    "退货运费由用户承担，除非是商品质量问题。"
                    "超过 7 天但未超过 30 天的订单，需要提供商品质量问题的证据才能退款。"
                ),
            },
            {
                "title": "订单查询",
                "content": (
                    "订单查询指南。"
                    "用户可以通过订单号查询订单状态。"
                    "订单状态包括：待支付、已支付、已发货、运输中、已签收、已完成。"
                    "如果订单显示已发货但超过 7 天未收到，可以联系客服申请查件。"
                    "物流信息通常在发货后 24 小时内更新。"
                    "如果订单显示异常，请提供订单号联系客服处理。"
                ),
            },
            {
                "title": "账户安全",
                "content": (
                    "账户安全说明。"
                    "建议用户定期修改密码，密码长度至少 8 位，包含字母和数字。"
                    "如果忘记密码，可以通过绑定的手机号或邮箱重置。"
                    "发现账户异常登录时，系统会自动锁定账户并发送通知。"
                    "用户可以在安全设置中开启两步验证，提高账户安全性。"
                    "不要将密码分享给他人，客服人员不会索要用户密码。"
                ),
            },
            {
                "title": "技术故障排查",
                "content": (
                    "常见技术问题排查。"
                    "应用崩溃：请尝试清除缓存后重启应用，如果问题持续请更新到最新版本。"
                    "登录失败 401 错误：表示认证失败，请检查用户名密码是否正确，或尝试重置密码。"
                    "页面加载慢：检查网络连接，尝试切换 WiFi 或移动数据。"
                    "支付失败：确认银行卡余额充足，检查是否开启了网上支付功能。"
                    "500 服务器错误：这是服务端问题，请稍后重试，如果持续出现请联系技术支持。"
                ),
            },
            {
                "title": "会员与积分",
                "content": (
                    "会员积分规则。"
                    "每消费 1 元累积 1 积分。"
                    "积分可以在下次购物时抵扣，100 积分 = 1 元。"
                    "会员等级分为：普通会员、银卡会员（累计消费 1000 元）、金卡会员（累计消费 5000 元）。"
                    "银卡会员享受 95 折优惠，金卡会员享受 9 折优惠。"
                    "积分有效期为 1 年，过期自动清零。"
                    "生日当月消费可获得双倍积分。"
                ),
            },
            {
                "title": "配送说明",
                "content": (
                    "配送服务说明。"
                    "标准配送：3-5 个工作日送达，免运费（订单满 99 元）。"
                    "加急配送：1-2 个工作日送达，运费 15 元。"
                    "同城配送：当日达或次日达，运费 10 元。"
                    "偏远地区可能需要额外 2-3 天。"
                    "配送时间为每天 9:00-18:00，节假日可能延迟。"
                    "如果需要修改收货地址，请在发货前联系客服。"
                ),
            },
        ]
        self.add_documents(default_docs)
        logger.info(f"已导入默认知识库: {len(default_docs)} 篇文档")
