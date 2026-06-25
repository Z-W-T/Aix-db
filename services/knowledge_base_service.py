import logging
import os
from typing import Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from common.minio_util import MinioUtils
from common.exception import MyException
from constants.code_enum import SysCodeEnum
from model.db_connection_pool import get_db_pool
from model.db_models import TKnowledgeBase, TKnowledgeChunk
from services.embedding_service import generate_embeddings_batch

logger = logging.getLogger(__name__)

pool = get_db_pool()
minio_utils = MinioUtils()

DEFAULT_KB_NAME = os.getenv("RAG_DEFAULT_KB_NAME", "default")
RAG_CHUNK_SIZE = int(os.getenv("RAG_CHUNK_SIZE", "800"))
RAG_CHUNK_OVERLAP = int(os.getenv("RAG_CHUNK_OVERLAP", "120"))
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "6"))
RAG_SIMILARITY = float(os.getenv("RAG_SIMILARITY", "0.3"))
# 单个文档分块数量上限，防止超大文档一次性加载过多 embedding 导致内存耗尽
RAG_MAX_CHUNKS = int(os.getenv("RAG_MAX_CHUNKS", "500"))


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
        # 已经处理到文本末尾，必须退出，避免 overlap 导致死循环
        if end >= text_len:
            break
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
    logger.info(f"[KB-INDEX] parse_file_key={parse_file_key}, text_len={len(text_content)}, chunks={len(chunks)}")

    if not chunks:
        return {"kb_id": kb_id or 0, "chunk_count": 0}

    # 限制分块数量，防止超大文档一次性生成过多 embedding 导致内存耗尽
    if len(chunks) > RAG_MAX_CHUNKS:
        logger.warning(
            f"Document chunk count {len(chunks)} exceeds limit {RAG_MAX_CHUNKS}, "
            f"truncating. Consider increasing RAG_CHUNK_SIZE or RAG_MAX_CHUNKS."
        )
        chunks = chunks[:RAG_MAX_CHUNKS]

    # 批量生成 embedding，避免逐条调用在低内存环境（如 WSL）中触发 OOM
    embeddings = await generate_embeddings_batch(chunks)
    valid_count = sum(1 for e in embeddings if e is not None)
    logger.info(f"[KB-INDEX] embeddings generated, valid={valid_count}/{len(embeddings)}")

    # 校验 embedding 结果：若全部为 None 说明模型不可用（如内存不足无法加载本地模型）
    if all(emb is None for emb in embeddings):
        logger.error(
            "All embeddings are None, embedding model unavailable. "
            "Please configure an online Embedding model (model_type=2) in the database."
        )
        raise MyException(
            SysCodeEnum.c_9999,
            "Embedding 模型不可用，请在系统中配置 Embedding 模型或确保本地模型有足够内存加载",
        )

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

        logger.info(f"[KB-INDEX] indexed {len(chunks)} chunks into kb_id={kb.id}")
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

    embeddings = await generate_embeddings_batch([question])
    embedding = embeddings[0] if embeddings else None
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


def list_knowledge_bases(oid: int = 1) -> List[Dict]:
    """
    列出所有知识库及其包含的文件（按 parse_file_key 聚合 chunks）。

    Returns:
        [
            {
                "kb_id": int, "kb_name": str, "enabled": bool, "create_time": str,
                "chunk_count": int,
                "files": [
                    {
                        "source_file_key": str, "parse_file_key": str,
                        "file_name": str, "chunk_count": int, "create_time": str,
                        "chunks": [{"chunk_index": int, "content": str}]
                    }
                ]
            }
        ]
    """
    with pool.get_session() as session:
        kbs = session.query(TKnowledgeBase).filter(
            TKnowledgeBase.oid == oid
        ).order_by(TKnowledgeBase.id.desc()).all()

        if not kbs:
            return []

        kb_ids = [kb.id for kb in kbs]
        # 一次性查出所有 chunks，按 kb_id 分组
        all_chunks = session.query(
            TKnowledgeChunk.kb_id,
            TKnowledgeChunk.source_file_key,
            TKnowledgeChunk.parse_file_key,
            TKnowledgeChunk.chunk_index,
            TKnowledgeChunk.content,
            TKnowledgeChunk.create_time,
        ).filter(TKnowledgeChunk.kb_id.in_(kb_ids)).order_by(
            TKnowledgeChunk.kb_id, TKnowledgeChunk.parse_file_key, TKnowledgeChunk.chunk_index
        ).all()

        # 按 kb_id -> parse_file_key 聚合
        kb_map: Dict[int, Dict] = {
            kb.id: {
                "kb_id": kb.id,
                "kb_name": kb.name,
                "enabled": bool(kb.enabled),
                "create_time": kb.create_time.strftime("%Y-%m-%d %H:%M:%S") if kb.create_time else "",
                "chunk_count": 0,
                "files": {},
            }
            for kb in kbs
        }

        for row in all_chunks:
            kb_id, source_key, parse_key, chunk_idx, content, create_time = row
            kb_item = kb_map.get(kb_id)
            if not kb_item:
                continue
            kb_item["chunk_count"] += 1

            file_item = kb_item["files"].get(parse_key)
            if not file_item:
                # 从 key 中提取文件名：格式为 {uuid}__{filename}.ext
                file_name = parse_key
                if "__" in parse_key:
                    file_name = parse_key.split("__", 1)[1]
                file_item = {
                    "source_file_key": source_key or "",
                    "parse_file_key": parse_key or "",
                    "file_name": file_name,
                    "chunk_count": 0,
                    "create_time": create_time.strftime("%Y-%m-%d %H:%M:%S") if create_time else "",
                    "chunks": [],
                }
                kb_item["files"][parse_key] = file_item

            file_item["chunk_count"] += 1
            # 限制单个 chunk 展示内容长度，避免前端渲染过多文本
            preview = (content or "")[:500]
            file_item["chunks"].append({
                "chunk_index": chunk_idx or 0,
                "content": preview,
            })

        # files dict -> list
        result = []
        for kb_item in kb_map.values():
            kb_item["files"] = list(kb_item["files"].values())
            result.append(kb_item)
        return result
