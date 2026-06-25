"""
离线 Embedding 模型支持
当没有配置在线 embedding 模型时，使用本地 CPU 模式模型作为回退
"""

import logging
import os
import threading
from typing import List, Optional

logger = logging.getLogger(__name__)

# 全局锁，用于线程安全的模型初始化
_lock = threading.Lock()
_embedding_model: Optional[object] = None

# 默认模型配置
DEFAULT_LOCAL_MODEL_PATH = os.getenv("LOCAL_MODEL_PATH", "./models")
DEFAULT_EMBEDDING_MODEL_ID = os.getenv(
    "DEFAULT_EMBEDDING_MODEL", "shibing624/text2vec-base-chinese"
)

# 加载本地模型所需的最小可用内存（MB），低于此值直接拒绝加载，避免 OOM 导致系统崩溃
# 注意：PyTorch + sentence-transformers 加载时虚拟内存需求可达 6-10GB，
# 即使物理内存够用，也可能因虚拟内存/地址空间不足触发 OOM killer。
# 在 WSL 等内存受限环境，建议保持较高阈值（默认 3000MB）以确保安全。
# 如需在低内存环境强制启用，可设置环境变量 LOCAL_EMBEDDING_MIN_MEMORY_MB 为更小值。
MIN_MEMORY_REQUIRED_MB = int(os.getenv("LOCAL_EMBEDDING_MIN_MEMORY_MB", "3000"))
# 单次批量推理的最大文本数量，避免单批内存占用过高
LOCAL_EMBEDDING_BATCH_SIZE = int(os.getenv("LOCAL_EMBEDDING_BATCH_SIZE", "32"))


def _get_available_memory_mb() -> int:
    """获取当前可用内存（MB），包含 buffer/cache 可回收部分"""
    try:
        with open("/proc/meminfo", "r") as f:
            meminfo = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    # 值形如 "948 MiB" 或 "948 kB"
                    val = parts[1].strip().split()[0]
                    meminfo[key] = int(val)
        # MemAvailable 是系统真正可用的内存（含可回收 cache），单位 kB
        avail_kb = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
        return avail_kb // 1024
    except Exception as e:
        logger.warning(f"Failed to read memory info: {e}")
        return 0


def _get_total_memory_mb() -> int:
    """获取系统总物理内存（MB），用于判断是否处于低内存环境"""
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split(":")[1].strip().split()[0]) // 1024
    except Exception as e:
        logger.warning(f"Failed to read total memory: {e}")
    return 0


# 模型会下载到: {LOCAL_MODEL_PATH}/embedding/{model_name}/ 或标准 HuggingFace 缓存目录
def _get_local_model_path():
    """获取本地模型路径，支持多种路径格式"""
    model_id = DEFAULT_EMBEDDING_MODEL_ID

    # 1. 检查 HuggingFace 标准缓存目录结构: models/models--namespace--name/
    hf_cache_name = model_id.replace(
        "/", "--"
    )  # shibing624/text2vec-base-chinese -> shibing624--text2vec-base-chinese
    hf_cache_path = os.path.join(DEFAULT_LOCAL_MODEL_PATH, f"models--{hf_cache_name}")
    if os.path.exists(hf_cache_path):
        # 查找 snapshots 目录下的实际模型路径
        snapshots_dir = os.path.join(hf_cache_path, "snapshots")
        if os.path.exists(snapshots_dir):
            # 获取第一个快照目录
            snapshots = [
                d
                for d in os.listdir(snapshots_dir)
                if os.path.isdir(os.path.join(snapshots_dir, d))
            ]
            if snapshots:
                return os.path.join(snapshots_dir, snapshots[0])
        return hf_cache_path

    # 2. 检查自定义路径: models/embedding/shibing624_text2vec-base-chinese/
    custom_name = model_id.replace(
        "/", "_"
    )  # shibing624/text2vec-base-chinese -> shibing624_text2vec-base-chinese
    custom_path = os.path.join(DEFAULT_LOCAL_MODEL_PATH, "embedding", custom_name)
    if os.path.exists(custom_path):
        return custom_path

    # 3. 如果都找不到，返回 None（让 HuggingFace 使用模型 ID）
    return None


def _get_local_embedding_model():
    """
    获取本地 embedding 模型实例（单例模式，线程安全）

    Returns:
        Embeddings 实例，如果加载失败则返回 None
    """
    global _embedding_model

    if _embedding_model is not None:
        return _embedding_model

    with _lock:
        # 双重检查锁定
        if _embedding_model is not None:
            return _embedding_model

        try:
            # 内存安全检查：加载 PyTorch + 模型需要较多内存，
            # 在内存不足时（如 WSL 受限环境）强行加载会触发 OOM killer，导致系统崩溃/断连
            # 注意：PyTorch 加载时虚拟内存需求可达 6-10GB，远超物理内存占用，
            # 因此除了检查可用内存，还要检查系统总内存，在低内存系统中直接禁用本地模型。
            avail_mb = _get_available_memory_mb()
            total_mb = _get_total_memory_mb()
            # 系统总内存低于此阈值（默认 12GB）视为低内存环境，禁用本地模型加载
            # PyTorch + sentence-transformers 虚拟内存需求可达 6-10GB，
            # 在 8GB/16GB 的 WSL 环境中极易触发 OOM killer
            min_total_mb = int(os.getenv("LOCAL_EMBEDDING_MIN_TOTAL_MEMORY_MB", "12288"))

            if total_mb > 0 and total_mb < min_total_mb:
                logger.error(
                    f"System total memory {total_mb}MB is below the safe threshold "
                    f"{min_total_mb}MB for loading local embedding model. "
                    f"PyTorch requires 6-10GB virtual memory which would trigger OOM killer. "
                    f"Please configure an online Embedding model (model_type=2) in the database."
                )
                return None

            if 0 < avail_mb < MIN_MEMORY_REQUIRED_MB:
                logger.error(
                    f"Insufficient available memory to load local embedding model: "
                    f"available={avail_mb}MB, required>={MIN_MEMORY_REQUIRED_MB}MB. "
                    f"Please configure an online Embedding model (model_type=2) in the database."
                )
                return None
            if avail_mb > 0:
                logger.info(
                    f"Memory check passed: available={avail_mb}MB, total={total_mb}MB, "
                    f"required_available>={MIN_MEMORY_REQUIRED_MB}MB, required_total>={min_total_mb}MB"
                )

            # 设置环境变量，避免 tokenizers 并行警告
            os.environ["TOKENIZERS_PARALLELISM"] = "false"

            # 禁用 HuggingFace Hub 连接，避免网络不可达时的连接错误
            # 使用离线模式，仅使用本地缓存的模型
            os.environ["HF_HUB_OFFLINE"] = "1"
            logger.debug("Set HF_HUB_OFFLINE=1 to disable HuggingFace Hub connections")

            # 设置 HuggingFace 缓存目录到可写位置，避免在只读文件系统中写入错误
            # 固定使用 /tmp/huggingface_cache 作为缓存目录（容器内可写）
            cache_dir = "/tmp/huggingface_cache"
            os.environ["HF_HOME"] = cache_dir
            os.environ["HF_HUB_CACHE"] = cache_dir  # 设置 Hub 缓存目录
            os.environ["TRANSFORMERS_CACHE"] = cache_dir
            os.environ["SENTENCE_TRANSFORMERS_HOME"] = (
                cache_dir  # Sentence Transformers 缓存目录
            )
            # 确保缓存目录存在
            os.makedirs(cache_dir, exist_ok=True)
            logger.debug(f"Set HuggingFace cache directory to: {cache_dir}")

            # 优先使用本地路径，如果不存在则使用模型 ID（会自动下载）
            local_model_path = _get_local_model_path()
            # 将 cache_folder 设置为可写目录，避免在只读模型目录中写入缓存文件
            cache_folder = cache_dir
            model_id = DEFAULT_EMBEDDING_MODEL_ID

            # 检查本地路径是否存在
            if local_model_path and os.path.exists(local_model_path):
                model_name = local_model_path
                logger.info(f"Using local model path: {model_name}")
            else:
                # 使用模型 ID，会自动下载到 cache_folder
                # 注意：由于设置了 HF_HUB_OFFLINE=1，如果模型不存在会失败
                model_name = model_id
                logger.warning(
                    f"Model not found locally at {local_model_path or 'any path'}, "
                    f"will try to use model ID: {model_id} (offline mode enabled, download will fail if model not cached)"
                )

            # 尝试导入 HuggingFaceEmbeddings
            # 优先使用 langchain_huggingface（推荐，避免弃用警告）
            HuggingFaceEmbeddings = None
            try:
                from langchain_huggingface import HuggingFaceEmbeddings

                logger.debug("Using langchain_huggingface for local embedding model")
            except ImportError as e1:
                logger.warning(
                    f"langchain_huggingface not available: {e1}. "
                    "Falling back to langchain_community (deprecated). "
                    "Please install: pip install langchain-huggingface sentence-transformers"
                )
                # 回退到 langchain_community（已弃用，但作为备选）
                try:
                    from langchain_community.embeddings import HuggingFaceEmbeddings

                    logger.warning(
                        "Using deprecated langchain_community.embeddings.HuggingFaceEmbeddings"
                    )
                except ImportError as e2:
                    logger.error(
                        f"HuggingFaceEmbeddings not available. "
                        f"langchain_huggingface error: {e1}, "
                        f"langchain_community error: {e2}. "
                        "Please install: pip install langchain-huggingface sentence-transformers"
                    )
                    return None

            if HuggingFaceEmbeddings is None:
                return None

            # 创建模型实例（CPU 模式）
            # HuggingFaceEmbeddings 会自动处理：
            # - 如果 model_name 是本地路径，直接使用
            # - 如果 model_name 是模型 ID，会自动下载到 cache_folder
            try:
                _embedding_model = HuggingFaceEmbeddings(
                    model_name=model_name,
                    cache_folder=cache_folder,
                    model_kwargs={"device": "cpu"},
                    encode_kwargs={"normalize_embeddings": True},
                )

                logger.info("✅ Local embedding model loaded successfully")
                return _embedding_model
            except ImportError as import_err:
                # 捕获缺少依赖的错误（如 sentence-transformers）
                error_msg = str(import_err)
                if (
                    "sentence_transformers" in error_msg
                    or "sentence-transformers" in error_msg
                ):
                    logger.error(
                        f"Missing required dependency: sentence-transformers. "
                        f"Please install it with: pip install sentence-transformers"
                    )
                else:
                    logger.error(
                        f"Import error when loading embedding model: {import_err}"
                    )
                return None

        except Exception as e:
            logger.error(f"Failed to load local embedding model: {e}", exc_info=True)
            return None


async def generate_embedding_local(text: str) -> Optional[List[float]]:
    """
    使用本地模型生成 embedding（异步包装）

    Args:
        text: 要生成 embedding 的文本

    Returns:
        embedding 向量列表，如果失败则返回 None
    """
    if not text:
        return None

    model = _get_local_embedding_model()
    if not model:
        logger.warning("Local embedding model not available")
        return None

    try:
        # embed_query 是同步方法，在异步环境中需要在线程池中执行
        import asyncio

        loop = asyncio.get_event_loop()
        embedding = await loop.run_in_executor(None, model.embed_query, text)
        return embedding
    except Exception as e:
        logger.error(
            f"Failed to generate embedding with local model: {e}", exc_info=True
        )
        return None


async def generate_embeddings_batch_local(texts: List[str]) -> List[Optional[List[float]]]:
    """
    使用本地模型批量生成 embedding（异步包装）

    相比逐条调用 generate_embedding_local，批量推理可显著减少模型调用开销，
    并通过分批控制单批内存占用，避免在低内存环境（如 WSL）中触发 OOM。

    Args:
        texts: 要生成 embedding 的文本列表

    Returns:
        embedding 向量列表，与输入顺序一致；失败的位置为 None
    """
    if not texts:
        return []

    results: List[Optional[List[float]]] = [None] * len(texts)
    # 收集非空文本的索引
    valid_indices = [i for i, t in enumerate(texts) if t]
    if not valid_indices:
        return results

    model = _get_local_embedding_model()
    if not model:
        logger.warning("Local embedding model not available, batch embedding skipped")
        return results

    import asyncio

    loop = asyncio.get_event_loop()

    # 分批推理，控制单批内存占用
    batch_size = max(1, LOCAL_EMBEDDING_BATCH_SIZE)
    for start in range(0, len(valid_indices), batch_size):
        batch_slice = valid_indices[start : start + batch_size]
        batch_texts = [texts[i] for i in batch_slice]
        try:
            # embed_documents 是同步方法，在线程池中执行避免阻塞事件循环
            batch_embeddings = await loop.run_in_executor(
                None, model.embed_documents, batch_texts
            )
            for idx, emb in zip(batch_slice, batch_embeddings):
                results[idx] = emb
        except Exception as e:
            logger.error(
                f"Failed to generate batch embedding (batch start={start}): {e}",
                exc_info=True,
            )
            # 该批失败的位置保持 None，继续处理下一批

    return results


def generate_embedding_local_sync(text: str) -> Optional[List[float]]:
    """
    使用本地模型生成 embedding（同步版本）

    Args:
        text: 要生成 embedding 的文本

    Returns:
        embedding 向量列表，如果失败则返回 None
    """
    if not text:
        return None

    model = _get_local_embedding_model()
    if not model:
        logger.warning("Local embedding model not available")
        return None

    try:
        embedding = model.embed_query(text)
        return embedding
    except Exception as e:
        logger.error(
            f"Failed to generate embedding with local model: {e}", exc_info=True
        )
        return None
