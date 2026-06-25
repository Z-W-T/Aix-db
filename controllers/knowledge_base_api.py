"""
RAG 知识库 API
"""
import logging

from sanic import Blueprint, Request

from common.res_decorator import async_json_resp
from common.token_decorator import check_token
from common.minio_util import MinioUtils
from common.exception import MyException
from constants.code_enum import SysCodeEnum
from services.knowledge_base_service import (
    index_uploaded_file_async,
    retrieve_knowledge_context,
    list_knowledge_bases,
)

logger = logging.getLogger(__name__)
bp = Blueprint("knowledgeBase", url_prefix="/knowledge-base")

minio_utils = MinioUtils()


@bp.post("/upload")
@check_token
@async_json_resp
async def upload_file(request: Request):
    """
    上传文件并索引到知识库

    使用 multipart/form-data 上传文件，系统会自动：
    1. 上传文件到 MinIO
    2. 解析文件文本内容
    3. 分块并生成 embedding
    4. 存入 pgvector

    Request: multipart/form-data
        - file: 上传的文件
        - kb_name: 知识库名称（可选，默认 "default"）
        - kb_id: 知识库 ID（可选，优先于 kb_name）

    Response: {"kb_id": int, "chunk_count": int}
    """
    kb_name = request.form.get("kb_name")
    kb_id = request.form.get("kb_id")
    if kb_id:
        kb_id = int(kb_id)

    # 上传文件并解析
    file_info = minio_utils.upload_file_and_parse_from_request(request)

    # 索引到知识库
    result = await index_uploaded_file_async(
        file_info=file_info,
        kb_id=kb_id,
        kb_name=kb_name,
    )

    return result


@bp.post("/search")
@check_token
@async_json_resp
async def search_knowledge(request: Request):
    """
    检索知识库

    Request (JSON):
        - question: 查询问题
        - kb_name: 知识库名称（可选）
        - kb_id: 知识库 ID（可选）
        - top_k: 返回结果数量（可选，默认 6）

    Response: {"context": str}
    """
    body = request.json
    question = body.get("question", "")
    kb_name = body.get("kb_name")
    kb_id = body.get("kb_id")
    top_k = body.get("top_k")

    if not question:
        raise MyException(SysCodeEnum.c_9999, "question 不能为空")

    context = await retrieve_knowledge_context(
        question=question,
        kb_id=kb_id,
        kb_name=kb_name,
        top_k=top_k,
    )

    return {"context": context}


@bp.get("/list")
@check_token
@async_json_resp
async def list_knowledge_base(request: Request):
    """
    列出所有知识库及其包含的文件和分块

    Response: [
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
    return list_knowledge_bases(oid=1)