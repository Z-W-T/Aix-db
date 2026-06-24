import logging
import os
from typing import Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from common.minio_util import MinioUtils
from model.db_connection_pool import get_db_pool
from model.db_models import TKnowledgeBase, TKnowledgeChunk
from services.embedding_service import generate_embedding

logger = logging.getLogger(__name__)

pool = get_db_pool()
minio_utils = MinioUtils()

DEFAULT_KB_NAME = os.getenv("RAG_DEFAULT_KB_NAME", "default")
RAG_CHUNK_SIZE = int(os.getenv("RAG_CHUNK_SIZE", "800"))
RAG_CHUNK_OVERLAP = int(os.getenv("RAG_CHUNK_OVERLAP", "120"))
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "6"))
RAG_SIMILARITY = float(os.getenv("RAG_SIMILARITY", "0.3"))


def _split_text(text: str, chunk_size: int, overlap: int) -> List[str]:
    if not text:
        return []

    safe_chunk_size = max(1, chunk_size)
    safe_overlap = max(0, min(overlap, safe_chunk_size - 1))

    chunks: List[str] = []
    start = 0
    text_len = len(text)
    while start < text_len:
        end = min(text_len, start + safe_chunk_size)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end - safe_overlap
        if start < 0:
            start = 0
    return chunks


def _read_minio_text(parse_file_key: str, bucket_name: str = "filedata") -> str:
    response = minio_utils.client.get_object(bucket_name=bucket_name, object_name=parse_file_key)
    try:
        raw = response.data
    finally:
        response.close()
        response.release_conn()

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="ignore")


def _get_or_create_kb(session: Session, kb_id: Optional[int], kb_name: Optional[str], oid: int) -> TKnowledgeBase:
    if kb_id:
        kb = session.query(TKnowledgeBase).filter(TKnowledgeBase.id == kb_id).first()
        if kb:
            return kb

    name = kb_name or DEFAULT_KB_NAME
    kb = session.query(TKnowledgeBase).filter(
        TKnowledgeBase.name == name,
        TKnowledgeBase.oid == oid,
    ).first()
    if kb:
        return kb

    kb = TKnowledgeBase(name=name, oid=oid, enabled=True)
    session.add(kb)
    session.flush()
    return kb


async def index_uploaded_file_async(
    file_info: Dict[str, str],
    kb_id: Optional[int] = None,
    kb_name: Optional[str] = None,
    oid: int = 1,
) -> Dict[str, int]:
    parse_file_key = file_info.get("parse_file_key")
    source_file_key = file_info.get("source_file_key")

    if not parse_file_key:
        raise ValueError("parse_file_key is required")

    text_content = _read_minio_text(parse_file_key)
    chunks = _split_text(text_content, RAG_CHUNK_SIZE, RAG_CHUNK_OVERLAP)

    if not chunks:
        return {"kb_id": kb_id or 0, "chunk_count": 0}

    embeddings: List[Optional[List[float]]] = []
    for chunk in chunks:
        embedding = await generate_embedding(chunk)
        embeddings.append(embedding)

    with pool.get_session() as session:
        kb = _get_or_create_kb(session, kb_id, kb_name, oid)

        session.query(TKnowledgeChunk).filter(
            TKnowledgeChunk.kb_id == kb.id,
            TKnowledgeChunk.parse_file_key == parse_file_key,
        ).delete(synchronize_session=False)

        for idx, chunk in enumerate(chunks):
            session.add(
                TKnowledgeChunk(
                    kb_id=kb.id,
                    source_file_key=source_file_key,
                    parse_file_key=parse_file_key,
                    chunk_index=idx,
                    content=chunk,
                    embedding=embeddings[idx],
                )
            )

        return {"kb_id": kb.id, "chunk_count": len(chunks)}


async def retrieve_knowledge_context(
    question: str,
    kb_id: Optional[int] = None,
    kb_name: Optional[str] = None,
    oid: int = 1,
    top_k: Optional[int] = None,
) -> str:
    if not question or not question.strip():
        return ""

    embedding = await generate_embedding(question)
    if not embedding:
        return ""

    embedding_str = "[" + ",".join(map(str, embedding)) + "]"
    limit = top_k or RAG_TOP_K

    with pool.get_session() as session:
        kb = _get_or_create_kb(session, kb_id, kb_name, oid)

        sql = text(
            """
            SELECT content, source_file_key,
                   (1 - (embedding <=> CAST(:embedding_array AS vector))) AS similarity
            FROM t_knowledge_chunk
            WHERE kb_id = :kb_id
              AND embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:embedding_array AS vector)
            LIMIT :top_k
            """
        )

        rows = session.execute(
            sql,
            {
                "embedding_array": embedding_str,
                "kb_id": kb.id,
                "top_k": limit,
            },
        ).fetchall()

    if not rows:
        return ""

    lines = ["Knowledge base context:"]
    hit_index = 1
    for row in rows:
        similarity = row[2]
        if similarity is None or similarity < RAG_SIMILARITY:
            continue
        content = row[0] or ""
        source_key = row[1] or ""
        lines.append(f"[{hit_index}] {content} (source: {source_key})")
        hit_index += 1

    if hit_index == 1:
        return ""

    return "\n".join(lines)
