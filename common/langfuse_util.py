import os
from contextlib import contextmanager
from typing import Any, Dict, Optional


def is_tracing_enabled() -> bool:
    return os.getenv("LANGFUSE_TRACING_ENABLED", "false").lower() == "true"


def create_langfuse_callback_handler():
    # Lazy import to avoid OpenTelemetry initialization at module load.
    from langfuse.langchain import CallbackHandler

    return CallbackHandler()


@contextmanager
def langfuse_trace_context(
    *,
    name: str,
    input_value: Any,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    # Lazy import to avoid initializing the Langfuse client unless tracing is enabled.
    from langfuse import get_client, propagate_attributes

    langfuse = get_client()
    with langfuse.start_as_current_observation(
        input=input_value,
        as_type="agent",
        name=name,
    ) as rootspan:
        update_kwargs: Dict[str, Any] = {}
        if session_id:
            update_kwargs["session_id"] = session_id
        if user_id:
            update_kwargs["user_id"] = user_id
        if metadata:
            update_kwargs["metadata"] = metadata
        if update_kwargs:
            rootspan.update_trace(**update_kwargs)
        if session_id:
            with propagate_attributes(session_id=session_id):
                yield rootspan
        else:
            yield rootspan
