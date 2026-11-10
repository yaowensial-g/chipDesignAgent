"""版本化文档入库、结构化分块与关键词/语义混合检索。"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from uuid import uuid4

from app.domain.enums import DocumentKind
from app.domain.models import (
    DocumentChunkView,
    DocumentIngestRequest,
    RetrievalRequest,
    RetrievalResult,
    StoredDocumentView,
)
from app.services.memory_service import MemoryService, memory_service
from app.services.token_service import TokenCounter, token_counter
from app.services.workspace_service import utc_now
from app.storage.database import Database, database


# 将中英文工程同义词映射成共同概念，可让本地语义特征在没有外部 Embedding API 时
# 仍识别“backpressure/背压”“handshake/握手”等常见跨语言表达。
CONCEPT_ALIASES = {
    "backpressure": ("backpressure", "背压", "反压"),
    "handshake": ("handshake", "握手", "ready/valid", "ready valid"),
    "completion": ("completion", "complete", "完成状态", "完成上报"),
    "reset": ("reset", "复位"),
    "clock": ("clock", "时钟"),
    "command": ("command", "cmd", "命令"),
    "interface": ("interface", "接口"),
    "queue": ("queue", "fifo", "队列"),
    "interrupt": ("interrupt", "irq", "中断"),
    "error": ("error", "错误", "异常"),
}


def _normalize(text: str) -> str:
    normalized = text.lower()
    for concept, aliases in CONCEPT_ALIASES.items():
        for alias in aliases:
            normalized = normalized.replace(alias, f" {concept} ")
    return normalized


def _terms(text: str) -> list[str]:
    normalized = _normalize(text)
    words = re.findall(r"[a-z_][a-z0-9_]{1,}|\d+(?:\.\d+)?", normalized)
    chinese_runs = re.findall(r"[\u3400-\u9fff]+", normalized)
    for run in chinese_runs:
        words.extend(run[index : index + 2] for index in range(max(0, len(run) - 1)))
    return words


def _feature_vector(terms: list[str], dimensions: int = 192) -> list[float]:
    """生成确定性本地特征向量；接口可在后续替换为真实 Embedding Provider。"""
    vector = [0.0] * dimensions
    for term, frequency in Counter(terms).items():
        digest = hashlib.sha256(term.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign * (1.0 + math.log(frequency))
    magnitude = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / magnitude for value in vector]


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    return max(0.0, sum(a * b for a, b in zip(left, right)))


class DocumentService:
    def __init__(
        self,
        db: Database = database,
        tokens: TokenCounter = token_counter,
        memories: MemoryService = memory_service,
    ) -> None:
        self.db = db
        self.tokens = tokens
        self.memories = memories

    def ingest(self, request: DocumentIngestRequest) -> StoredDocumentView:
        """原子地写入新版本、分块并执行来源级记忆失效。"""
        content_hash = hashlib.sha256(request.content.encode("utf-8")).hexdigest()
        chunks = self._chunk(request.content)
        now = utc_now()
        version_id = str(uuid4())
        with self.db.transaction() as connection:
            document = connection.execute(
                """SELECT * FROM documents
                WHERE project_id = ? AND module_name = ? AND kind = ? AND name = ?""",
                (request.project_id, request.module_name, request.kind.value, request.name),
            ).fetchone()
            if document is None:
                document_id = str(uuid4())
                connection.execute(
                    """INSERT INTO documents
                    (document_id, project_id, module_name, kind, name, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        document_id, request.project_id, request.module_name,
                        request.kind.value, request.name, now,
                    ),
                )
            else:
                document_id = document["document_id"]

            existing = connection.execute(
                """SELECT version_id FROM document_versions
                WHERE document_id = ? AND version = ? AND content_hash = ?""",
                (document_id, request.version, content_hash),
            ).fetchone()
            if existing:
                version_id = existing["version_id"]
            else:
                previous_ids = [
                    row["version_id"]
                    for row in connection.execute(
                        "SELECT version_id FROM document_versions WHERE document_id = ? AND status = 'active'",
                        (document_id,),
                    ).fetchall()
                ]
                previous_chunk_ids = [
                    row["chunk_id"]
                    for row in connection.execute(
                        """SELECT chunk_id FROM document_chunks
                        WHERE version_id IN (SELECT version_id FROM document_versions
                        WHERE document_id = ? AND status = 'active')""",
                        (document_id,),
                    ).fetchall()
                ]
                connection.execute(
                    "UPDATE document_versions SET status = 'superseded' WHERE document_id = ? AND status = 'active'",
                    (document_id,),
                )
                connection.execute(
                    """UPDATE document_chunks SET status = 'superseded'
                    WHERE document_id = ? AND status = 'active'""",
                    (document_id,),
                )
                self.memories.invalidate_sources(
                    previous_ids + previous_chunk_ids,
                    connection=connection,
                    reason=f"document_version_replaced_by:{version_id}",
                )
                connection.execute(
                    """INSERT INTO document_versions
                    (version_id, document_id, version, content_hash, content, status, created_at)
                    VALUES (?, ?, ?, ?, ?, 'active', ?)""",
                    (version_id, document_id, request.version, content_hash, request.content, now),
                )
                for ordinal, (section, content) in enumerate(chunks, start=1):
                    terms = _terms(content)
                    connection.execute(
                        """INSERT INTO document_chunks
                        (chunk_id, document_id, version_id, section, ordinal, content, token_count,
                         terms_json, embedding_json, status, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
                        (
                            str(uuid4()), document_id, version_id, section, ordinal,
                            content, self.tokens.count(content), self.db.dumps(terms),
                            self.db.dumps(_feature_vector(terms)), now,
                        ),
                    )
        stored = self.get_version(version_id)
        if stored is None:
            raise RuntimeError("document version transaction committed without a readable version")
        # 延迟导入避免 DocumentService/MemoryExtractor 的模块初始化环；提取失败只会
        # 写入审计事件，不影响已原子提交的文档版本。
        if self.db is database:
            from app.services.memory_extractor_service import memory_extractor_service

            memory_extractor_service.extract_from_event(
                source_type="document_version",
                source_id=version_id,
                project_id=request.project_id,
                module_name=request.module_name,
                agent_role=None,
                content=request.content[:20_000],
            )
        return stored

    def get_version(self, version_id: str) -> StoredDocumentView | None:
        row = self.db.fetch_one(
            """SELECT d.document_id, v.version_id, d.project_id, d.module_name, d.kind,
                      d.name, v.version, v.content_hash, v.status, v.created_at,
                      COUNT(c.chunk_id) AS chunk_count
               FROM document_versions v JOIN documents d ON d.document_id = v.document_id
               LEFT JOIN document_chunks c ON c.version_id = v.version_id
               WHERE v.version_id = ? GROUP BY v.version_id""",
            (version_id,),
        )
        return StoredDocumentView(**row) if row else None

    def list(self, project_id: str, module_name: str | None = None) -> list[StoredDocumentView]:
        conditions = ["d.project_id = ?"]
        parameters: list[object] = [project_id]
        if module_name:
            conditions.append("d.module_name = ?")
            parameters.append(module_name)
        rows = self.db.fetch_all(
            f"""SELECT d.document_id, v.version_id, d.project_id, d.module_name, d.kind,
                       d.name, v.version, v.content_hash, v.status, v.created_at,
                       COUNT(c.chunk_id) AS chunk_count
                FROM document_versions v JOIN documents d ON d.document_id = v.document_id
                LEFT JOIN document_chunks c ON c.version_id = v.version_id
                WHERE {' AND '.join(conditions)} GROUP BY v.version_id
                ORDER BY v.created_at DESC""",
            tuple(parameters),
        )
        return [StoredDocumentView(**row) for row in rows]

    def retrieve(self, request: RetrievalRequest, *, pinned_version_ids: tuple[str, ...] = ()) -> RetrievalResult:
        conditions = ["d.project_id = ?"]
        parameters: list[object] = [request.project_id]
        if request.module_name:
            conditions.append("d.module_name = ?")
            parameters.append(request.module_name)
        if request.document_kinds:
            placeholders = ",".join("?" for _ in request.document_kinds)
            conditions.append(f"d.kind IN ({placeholders})")
            parameters.extend(kind.value for kind in request.document_kinds)
        if pinned_version_ids:
            placeholders = ",".join("?" for _ in pinned_version_ids)
            conditions.append(
                f"""(c.version_id IN ({placeholders}) OR (
                    c.status = 'active' AND d.document_id NOT IN (
                        SELECT document_id FROM document_versions WHERE version_id IN ({placeholders})
                    )))"""
            )
            parameters.extend((*pinned_version_ids, *pinned_version_ids))
        elif not request.include_superseded:
            conditions.append("c.status = 'active'")
        rows = self.db.fetch_all(
            f"""SELECT c.*, d.name AS document_name, d.kind AS document_kind,
                       d.module_name, v.version AS document_version
                FROM document_chunks c
                JOIN documents d ON d.document_id = c.document_id
                JOIN document_versions v ON v.version_id = c.version_id
                WHERE {' AND '.join(conditions)}""",
            tuple(parameters),
        )
        query_terms = _terms(request.query)
        query_counts = Counter(query_terms)
        query_vector = _feature_vector(query_terms)
        document_frequency: Counter[str] = Counter()
        row_terms: dict[str, list[str]] = {}
        for row in rows:
            terms = self.db.loads(row["terms_json"], [])
            row_terms[row["chunk_id"]] = terms
            document_frequency.update(set(terms))
        total = max(1, len(rows))
        ranked: list[DocumentChunkView] = []
        for row in rows:
            terms = row_terms[row["chunk_id"]]
            counts = Counter(terms)
            keyword = sum(
                min(query_counts[term], counts[term])
                * (math.log((total + 1) / (document_frequency[term] + 1)) + 1.0)
                for term in query_counts
            ) / max(1, len(query_terms))
            semantic = _cosine(query_vector, self.db.loads(row["embedding_json"], []))
            score = 0.58 * min(1.0, keyword) + 0.42 * semantic
            ranked.append(
                DocumentChunkView(
                    **{
                        key: value
                        for key, value in row.items()
                        if key not in {"terms_json", "embedding_json", "status", "created_at"}
                    },
                    score=round(score, 6),
                    keyword_score=round(keyword, 6),
                    semantic_score=round(semantic, 6),
                )
            )
        ranked.sort(key=lambda item: (item.version_id in pinned_version_ids, item.score, -item.ordinal), reverse=True)
        return RetrievalResult(query=request.query, chunks=ranked[: request.top_k])

    def _chunk(self, content: str, target_tokens: int = 700) -> list[tuple[str, str]]:
        """优先按标题/段落分块，再把超长段落切开；不跨章节盲目拼接。"""
        lines = content.replace("\r\n", "\n").split("\n")
        section = "document"
        paragraphs: list[tuple[str, str]] = []
        buffer: list[str] = []

        def flush() -> None:
            text = "\n".join(buffer).strip()
            if text:
                paragraphs.append((section, text))
            buffer.clear()

        heading_pattern = re.compile(r"^\s*(?:#{1,6}\s+|\d+(?:\.\d+)*[、.\s]+|[一二三四五六七八九十]+、)(.+)$")
        for line in lines:
            heading = heading_pattern.match(line)
            if heading:
                flush()
                section = heading.group(1).strip() or line.strip()
                buffer.append(line)
            elif not line.strip():
                flush()
            else:
                buffer.append(line)
        flush()

        chunks: list[tuple[str, str]] = []
        for paragraph_section, paragraph in paragraphs or [(section, content)]:
            remaining = paragraph
            while remaining:
                piece = self.tokens.trim(remaining, target_tokens)
                marker = "\n[内容因本次 Token 预算截断]"
                if piece.endswith(marker):
                    piece = piece[: -len(marker)].rstrip()
                if not piece:
                    break
                chunks.append((paragraph_section, piece))
                remaining = remaining[len(piece) :].lstrip()
        return chunks or [("document", content)]


document_service = DocumentService()
