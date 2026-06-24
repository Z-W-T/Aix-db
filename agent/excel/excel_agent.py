import asyncio
import json
import logging
import time
import traceback
import uuid
from typing import Any, Dict, Optional, Union

from langgraph.graph.state import CompiledStateGraph

from agent.excel.excel_agent_state import ExcelAgentState
from agent.excel.excel_duckdb_manager import (
    close_duckdb_manager,
    get_chat_duckdb_manager,
)
from agent.excel.excel_graph import create_excel_graph
from common.langfuse_util import (
    create_langfuse_callback_handler,
    is_tracing_enabled,
    langfuse_trace_context,
)
from constants.code_enum import DataTypeEnum
from services.user_service import (
    add_user_record,
    decode_jwt_token,
    query_user_qa_record,
)

# Langfuse 延迟导入，仅在启用 tracing 时导入

logger = logging.getLogger(__name__)

# 步骤名称映射（中文）
STEP_NAME_MAP = {
    "excel_parsing": "文件解析...",
    "early_recommender": "推荐问题生成...",
    "sql_generator": "SQL生成...",
    "sql_executor": "SQL执行...",
    "chart_generator": "图表配置...",
    "summarize": "结果总结...",
    "parallel_collector": "并行处理（图表配置与结果总结）...",
    "unified_collector": "统一收集（结果总结→图表数据→推荐问题）...",
    "data_render": "数据渲染...",
    "data_render_apache": "数据渲染...",
    "question_recommender": "推荐问题...",
}


class ExcelAgent:
    """
    表格问答智能体
    """

    def __init__(self):
        # 存储运行中的任务
        self.running_tasks = {}
        self.excel_graph = create_excel_graph()
        # 是否启用链路追踪
        self.ENABLE_TRACING = is_tracing_enabled()
        # 存储步骤开始时间（用于计算耗时）
        self.step_start_times = {}
        # 存储步骤的 progressId
        self.step_progress_ids = {}

    async def run_excel_agent(
        self,
        query: str,
        response=None,
        chat_id: str = None,
        uuid_str: str = None,
        user_token=None,
        file_list: list = None,
    ) -> None:
        """
        运行表格智能体
        :param query:
        :param response:
        :param chat_id:
        :param uuid_str:
        :param user_token:
        :param file_list
        :return:
        """
        t02_answer_data = []  # 用于保存 summarize 内容
        t04_answer_data = {}  # 用于保存图表数据
        summarize_content = ""  # 用于单独保存 summarize 信息（markdown格式）
        sql_statement = ""  # 用于保存 SQL 语句
        current_step = None

        # 实现上传一次多次对话的效果 默认单轮对话取最新上传的文件
        if file_list is None or len(file_list) == 0:
            user_qa_record = query_user_qa_record(chat_id)[0]
            if user_qa_record:
                file_list = json.loads(user_qa_record["file_key"])
        try:
            initial_state = ExcelAgentState(
                user_query=query,
                file_list=file_list,
                chat_id=chat_id,  # chat_id
                file_metadata={},  # 文件元数据
                sheet_metadata={},  # Sheet元数据
                db_info=[],  # 支持多个表结构
                catalog_info={},  # Catalog信息
                generated_sql="",
                chart_type="",
                chart_config=None,  # 图表配置（和数据问答一致）
                execution_result=None,  # 修改：使用ExecutionResult对象
                report_summary="",
                render_data=None,  # 渲染数据（和数据问答一致）
            )
            graph: CompiledStateGraph = self.excel_graph

            # 获取用户信息 标识对话状态
            user_dict = await decode_jwt_token(user_token)
            task_id = user_dict["id"]
            task_context = {"cancelled": False}
            self.running_tasks[task_id] = task_context

            # 准备 tracing 配置
            config = {}
            if self.ENABLE_TRACING:
                langfuse_handler = create_langfuse_callback_handler()
                callbacks = [langfuse_handler]
                config = {
                    "callbacks": callbacks,
                    "metadata": {
                        "langfuse_session_id": chat_id,
                    },
                }

            # 异步流式执行参数
            stream_kwargs = {
                "input": initial_state,
                "stream_mode": "updates",
                "config": config,
            }

            # 如果启用 tracing，包裹在 trace 上下文中
            if self.ENABLE_TRACING:
                user_info = await decode_jwt_token(user_token)
                user_id = user_info.get("id")
                with langfuse_trace_context(
                    name="表格问答",
                    input_value=query,
                    session_id=chat_id,
                    user_id=user_id,
                ):
                    async for chunk_dict in graph.astream(**stream_kwargs):
                        (
                            current_step,
                            t02_answer_data,
                            summarize_content,
                            sql_statement,
                        ) = await self._process_chunk(
                            chunk_dict,
                            response,
                            task_id,
                            current_step,
                            t02_answer_data,
                            t04_answer_data,
                            summarize_content,
                            sql_statement,
                        )
                        # 跟踪 sql_generator 节点后的 SQL 语句
                        if "sql_generator" in chunk_dict:
                            step_value = chunk_dict.get("sql_generator", {})
                            generated_sql = step_value.get("generated_sql", "")
                            if (
                                generated_sql
                                and generated_sql != "No SQL query generated"
                            ):
                                sql_statement = generated_sql
            else:
                async for chunk_dict in graph.astream(**stream_kwargs):
                    current_step, t02_answer_data, summarize_content, sql_statement = (
                        await self._process_chunk(
                            chunk_dict,
                            response,
                            task_id,
                            current_step,
                            t02_answer_data,
                            t04_answer_data,
                            summarize_content,
                            sql_statement,
                        )
                    )
                    # 跟踪 sql_generator 节点后的 SQL 语句
                    if "sql_generator" in chunk_dict:
                        step_value = chunk_dict.get("sql_generator", {})
                        generated_sql = step_value.get("generated_sql", "")
                        if generated_sql and generated_sql != "No SQL query generated":
                            sql_statement = generated_sql

            # 只有在未取消的情况下才保存记录
            if not self.running_tasks[task_id]["cancelled"]:
                # t02_answer 保存 summarize 信息（markdown格式）
                # 如果没有 summarize，则保存空字符串
                final_t02_answer = [summarize_content] if summarize_content else []

                record_id = await add_user_record(
                    uuid_str,
                    chat_id,
                    query,
                    final_t02_answer,  # 只保存 summarize 信息
                    t04_answer_data,  # 保存图表数据
                    "FILEDATA_QA",
                    user_token,
                    file_list,
                    sql_statement=sql_statement,  # 保存 SQL 语句
                )
                # 发送record_id到前端，用于实时对话时显示SQL图标
                if record_id and response:
                    await self._send_response(
                        response=response,
                        content={"record_id": record_id},
                        data_type=DataTypeEnum.RECORD_ID.value[0],
                    )

        except asyncio.CancelledError:
            await response.write(
                self._create_response(
                    "\n> 这条消息已停止", "info", DataTypeEnum.ANSWER.value[0]
                )
            )
            await response.write(
                self._create_response("", "end", DataTypeEnum.STREAM_END.value[0])
            )
        except Exception as e:
            traceback.print_exception(e)
            logger.error(f"表格问答智能体运行异常: {e}")
            error_msg = f"处理过程中发生错误: {str(e)}"
            await self._send_response(response, error_msg, "error")

    async def _process_chunk(
        self,
        chunk_dict,
        response,
        task_id,
        current_step,
        t02_answer_data,
        t04_answer_data,
        summarize_content,
        sql_statement,
    ):
        """
        处理单个流式块数据
        """
        # 检查是否已取消
        if task_id in self.running_tasks and self.running_tasks[task_id]["cancelled"]:
            await response.write(
                self._create_response(
                    "\n> 这条消息已停止", "info", DataTypeEnum.ANSWER.value[0]
                )
            )
            # 发送最终停止确认消息
            await response.write(
                self._create_response("", "end", DataTypeEnum.STREAM_END.value[0])
            )
            raise asyncio.CancelledError()

        langgraph_step, step_value = next(iter(chunk_dict.items()))

        # 处理步骤变更（发送前一个步骤的完成信息和新步骤的开始信息）
        current_step, t02_answer_data = await self._handle_step_change(
            response, current_step, langgraph_step, t02_answer_data
        )

        # 处理具体步骤内容
        if step_value:
            summarize_content, sql_statement = await self._process_step_content(
                response,
                langgraph_step,
                step_value,
                t02_answer_data,
                t04_answer_data,
                summarize_content,
                sql_statement,
            )

            # 步骤内容处理完成后，发送完成信息（如果是最后一个步骤，确保发送完成信息）
            if langgraph_step in self.step_progress_ids:
                progress_id = self.step_progress_ids.get(langgraph_step)
                if progress_id:
                    step_name_cn = STEP_NAME_MAP.get(langgraph_step, langgraph_step)
                    await self._send_step_progress(
                        response=response,
                        step=langgraph_step,
                        step_name=step_name_cn,
                        status="complete",
                        progress_id=progress_id,
                    )
                    # 清理已完成的步骤 progressId
                    del self.step_progress_ids[langgraph_step]

        return current_step, t02_answer_data, summarize_content, sql_statement

    async def _handle_step_change(
        self,
        response,
        current_step: Optional[str],
        new_step: str,
        t02_answer_data: list,
    ) -> tuple:
        """
        处理步骤变更
        """
        # 记录新步骤开始时间（用于计算耗时）
        if new_step and new_step not in self.step_start_times:
            self.step_start_times[new_step] = time.perf_counter()
            logger.debug(f"步骤 {new_step} 开始")

            # 生成新的 progressId 并发送步骤开始信息
            progress_id = str(uuid.uuid4())
            self.step_progress_ids[new_step] = progress_id
            step_name_cn = STEP_NAME_MAP.get(new_step, new_step)
            await self._send_step_progress(
                response=response,
                step=new_step,
                step_name=step_name_cn,
                status="start",
                progress_id=progress_id,
            )

        return new_step, t02_answer_data

    async def _process_step_content(
        self,
        response,
        step_name: str,
        step_value: Dict[str, Any],
        t02_answer_data: list,
        t04_answer_data: Dict[str, Any],
        summarize_content: str,
        sql_statement: str,
    ) -> tuple:
        """
        处理各个步骤的内容
        """
        # 计算步骤耗时（用于日志记录）
        elapsed_time = None
        if step_name in self.step_start_times:
            start_time = self.step_start_times[step_name]
            end_time = time.perf_counter()
            elapsed_time = end_time - start_time
            logger.debug(f"步骤 {step_name} 耗时: {elapsed_time:.3f}秒")
            del self.step_start_times[step_name]

        content_map = {
            "excel_parsing": lambda: self._format_multi_file_table_info(step_value),
            "sql_generator": lambda: self._format_sql_generator_output(step_value),
            "sql_executor": lambda: self._format_execution_result(
                step_value.get("execution_result")
            ),
            "chart_generator": lambda: self._format_chart_generator_output(step_value),
            "summarize": lambda: step_value.get("report_summary", ""),
            "data_render": lambda: (
                step_value.get("render_data", {})
                if step_value.get("render_data")
                else {}
            ),
            "data_render_apache": lambda: (
                step_value.get("render_data", {})
                if step_value.get("render_data")
                else {}
            ),
        }

        if step_name in content_map:
            content = content_map[step_name]()

            # 特殊处理：收集 SQL 语句
            if step_name == "sql_generator":
                sql_from_state = step_value.get("generated_sql", "")
                if sql_from_state and sql_from_state != "No SQL query generated":
                    sql_statement = sql_from_state

            # 特殊处理：收集 summarize 信息（markdown格式）
            if step_name == "summarize":
                summarize_from_state = step_value.get("report_summary", "")
                if summarize_from_state:
                    # 确保是字符串格式（markdown格式）
                    if isinstance(summarize_from_state, dict):
                        if "content" in summarize_from_state:
                            summarize_content = str(summarize_from_state["content"])
                        elif "summary" in summarize_from_state:
                            summarize_content = str(summarize_from_state["summary"])
                        else:
                            md_lines = []
                            for key, value in summarize_from_state.items():
                                md_lines.append(f"**{key}**: {value}")
                            summarize_content = "\n\n".join(md_lines)
                    else:
                        summarize_content = str(summarize_from_state)

            # 数据渲染节点返回业务数据
            data_type = (
                DataTypeEnum.BUS_DATA.value[0]
                if step_name in ["data_render", "data_render_apache"]
                else DataTypeEnum.ANSWER.value[0]
            )

            # 只输出 summarize 步骤到前端，其他步骤信息不输出
            # 但保留 data_render 和 data_render_apache 的业务数据输出
            should_send = step_name in [
                "summarize",
                "data_render",
                "data_render_apache",
            ]

            if should_send:
                # data_render 和 data_render_apache 步骤的内容是字典，直接作为业务数据发送
                if step_name in ["data_render", "data_render_apache"]:
                    await self._send_response(
                        response=response, content=content, data_type=data_type
                    )
                elif step_name == "summarize":
                    # summarize 步骤：直接输出内容，不包含标题和耗时信息
                    # 确保 content 是字符串类型
                    if isinstance(content, dict):
                        content = json.dumps(content, ensure_ascii=False, indent=2)
                    await self._send_response(
                        response=response, content=content, data_type=data_type
                    )
                else:
                    # 其他步骤需要格式化输出（虽然现在不会执行到这里，但保留以防万一）
                    step_display_name = STEP_NAME_MAP.get(step_name, step_name)
                    # 确保 content 是字符串类型
                    if isinstance(content, dict):
                        content = json.dumps(content, ensure_ascii=False, indent=2)
                    formatted_content = self._format_step_output(
                        step_display_name, content, step_name, None
                    )
                    await self._send_response(
                        response=response,
                        content=formatted_content,
                        data_type=data_type,
                    )

                # 只收集 summarize 步骤的内容到 t02_answer_data
                if (
                    step_name == "summarize"
                    and data_type == DataTypeEnum.ANSWER.value[0]
                ):
                    t02_answer_data.append(content)

            # 这里设置渲染数据（和数据问答一致）
            if (
                step_name in ["data_render", "data_render_apache"]
                and data_type == DataTypeEnum.BUS_DATA.value[0]
            ):
                render_data = step_value.get("render_data")
                if render_data is not None and render_data:
                    t04_answer_data.clear()
                    t04_answer_data.update({"data": render_data, "dataType": data_type})
                else:
                    t04_answer_data.clear()

            # 对于非渲染步骤，刷新响应
            if step_name not in ["data_render", "data_render_apache"]:
                if hasattr(response, "flush"):
                    await response.flush()
                await asyncio.sleep(0)

        # 处理统一收集节点：按顺序推送 summarize → 图表数据 → 推荐问题
        # 注意：unified_collector 节点不在 content_map 中处理，避免发送格式化消息到前端
        if step_name == "unified_collector":
            updated_summarize_content = await self._process_unified_collector(
                response,
                step_value,
                t02_answer_data,
                t04_answer_data,
                summarize_content,
            )
            # 处理完 unified_collector 后直接返回，不再通过 content_map 发送内容
            return updated_summarize_content or summarize_content, sql_statement

        # 处理推荐问题：将推荐问题合并到已有的图表数据中发送到前端（在 content_map 之外处理）
        # 注意：如果使用了 unified_collector，这个分支可能不会执行
        if step_name == "question_recommender":
            recommended_questions = step_value.get("recommended_questions", [])
            logger.info(
                f"question_recommender 步骤: 获取到推荐问题数量: "
                f"{len(recommended_questions) if recommended_questions else 0}, "
                f"t04_answer_data: {t04_answer_data}"
            )

            if (
                recommended_questions
                and isinstance(recommended_questions, list)
                and len(recommended_questions) > 0
            ):
                # 获取已有的图表数据，如果没有则创建新的数据结构
                if (
                    t04_answer_data
                    and "data" in t04_answer_data
                    and isinstance(t04_answer_data["data"], dict)
                    and t04_answer_data["data"]
                ):
                    # 将推荐问题添加到已有的图表数据中
                    t04_answer_data["data"][
                        "recommended_questions"
                    ] = recommended_questions
                    payload = t04_answer_data["data"]
                    data_type = t04_answer_data.get(
                        "dataType", DataTypeEnum.BUS_DATA.value[0]
                    )
                else:
                    # 如果没有图表数据，仅使用推荐问题构建数据结构
                    logger.warning(
                        f"question_recommender 步骤: t04_answer_data 为空或无效，"
                        f"t04_answer_data: {t04_answer_data}"
                    )
                    payload = {"recommended_questions": recommended_questions}
                    data_type = DataTypeEnum.BUS_DATA.value[0]
                    # 同步更新 t04_answer_data，确保会被保存到数据库
                    t04_answer_data.clear()
                    t04_answer_data.update({"data": payload, "dataType": data_type})

                # 推送推荐问题数据到前端
                await self._send_response(
                    response=response,
                    content=payload,
                    data_type=data_type,
                )
                logger.info(
                    f"已发送 {len(recommended_questions)} 个推荐问题到前端，"
                    f"完整数据: {t04_answer_data}"
                )
            else:
                logger.warning(
                    f"question_recommender 步骤: 推荐问题为空或格式错误，"
                    f"recommended_questions: {recommended_questions}"
                )

        return summarize_content, sql_statement

    async def _process_unified_collector(
        self,
        response,
        step_value: Dict[str, Any],
        t02_answer_data: list,
        t04_answer_data: Dict[str, Any],
        summarize_content: str,
    ) -> str:
        """
        处理统一收集节点：按顺序推送 summarize → 图表数据 → 推荐问题

        要求：
        1. 首先推送 summarize（文本总结）
        2. 然后推送图表数据（render_data）
        3. 最后推送推荐问题（recommended_questions）

        Returns:
            更新后的 summarize_content
        """
        logger.info("📦 开始处理统一收集节点")
        logger.info(f"📋 step_value keys: {list(step_value.keys())}")
        logger.info(
            f"📋 step_value recommended_questions: {step_value.get('recommended_questions')}"
        )

        # 1. 推送 summarize（结果总结）
        report_summary = step_value.get("report_summary")
        if report_summary:
            logger.info("📤 推送 summarize（结果总结）")
            # 确保 report_summary 是字符串格式
            if isinstance(report_summary, dict):
                if "content" in report_summary:
                    report_summary = str(report_summary["content"])
                elif "summary" in report_summary:
                    report_summary = str(report_summary["summary"])
                else:
                    report_summary = json.dumps(
                        report_summary, ensure_ascii=False, indent=2
                    )
            else:
                report_summary = str(report_summary)

            await self._send_response(
                response=response,
                content=report_summary,
                data_type=DataTypeEnum.ANSWER.value[0],
            )
            # 收集到 t02_answer_data
            t02_answer_data.append(report_summary)
            # 更新 summarize_content
            summarize_content = report_summary

        # 2. 推送图表数据（render_data）
        render_data = step_value.get("render_data", {})
        if render_data:
            logger.info("📤 推送图表数据")
            # 更新 t04_answer_data
            t04_answer_data.clear()
            t04_answer_data.update(
                {"data": render_data, "dataType": DataTypeEnum.BUS_DATA.value[0]}
            )

            # 发送图表数据
            await self._send_response(
                response=response,
                content=render_data,
                data_type=DataTypeEnum.BUS_DATA.value[0],
            )

        # 3. 推送推荐问题（recommended_questions）
        recommended_questions = step_value.get("recommended_questions", [])
        logger.info(
            f"📋 检查推荐问题: {recommended_questions}, 类型: {type(recommended_questions)}, 长度: {len(recommended_questions) if isinstance(recommended_questions, list) else 'N/A'}"
        )

        if (
            recommended_questions
            and isinstance(recommended_questions, list)
            and len(recommended_questions) > 0
        ):
            logger.info(f"📤 推送推荐问题，数量: {len(recommended_questions)}")

            # 将推荐问题添加到已有的图表数据中
            if (
                t04_answer_data
                and "data" in t04_answer_data
                and isinstance(t04_answer_data["data"], dict)
            ):
                t04_answer_data["data"]["recommended_questions"] = recommended_questions
                payload = t04_answer_data["data"]
                data_type = t04_answer_data.get(
                    "dataType", DataTypeEnum.BUS_DATA.value[0]
                )
                logger.info(
                    f"📊 将推荐问题合并到已有图表数据中，payload keys: {list(payload.keys())}"
                )
            else:
                # 如果没有图表数据，仅使用推荐问题构建数据结构
                logger.info("📊 没有图表数据，仅使用推荐问题构建数据结构")
                payload = {"recommended_questions": recommended_questions}
                data_type = DataTypeEnum.BUS_DATA.value[0]
                t04_answer_data.clear()
                t04_answer_data.update({"data": payload, "dataType": data_type})

            # 发送推荐问题
            logger.info(f"📤 准备发送推荐问题到前端，payload: {payload}")
            await self._send_response(
                response=response,
                content=payload,
                data_type=data_type,
            )
            logger.info(f"✅ 已发送 {len(recommended_questions)} 个推荐问题到前端")
        else:
            logger.warning(
                f"⚠️ 推荐问题为空或格式错误: recommended_questions={recommended_questions}, type={type(recommended_questions)}"
            )

        logger.info("✅ 统一收集节点处理完成")
        return summarize_content

    def _format_step_output(
        self,
        step_display_name: str,
        content: str,
        step_name: str,
        elapsed_time: Optional[float] = None,
    ) -> str:
        """
        格式化步骤输出为 markdown 格式，包含步骤名称和分隔
        :param step_display_name: 步骤显示名称（中文）
        :param content: 步骤内容（字符串）
        :param step_name: 步骤名称（英文）
        :param elapsed_time: 耗时（秒，已废弃，不再使用）
        :return: 格式化后的 markdown 字符串
        """
        # 确保 content 是字符串类型
        if not isinstance(content, str):
            if isinstance(content, dict):
                content = json.dumps(content, ensure_ascii=False, indent=2)
            else:
                content = str(content)

        # 构建步骤标题（不包含耗时信息）
        step_header = f"## 📋 {step_display_name}\n\n"

        # 根据步骤类型决定是否包装为代码块
        if step_name == "summarize":
            # summarize 步骤：内容已经是 markdown 格式，直接使用，不包装成代码块
            formatted = step_header + content
        elif content.strip().startswith("```"):
            # 内容已经是代码块格式，直接使用
            formatted = step_header + content
        else:
            # 其他步骤：根据步骤类型选择代码块语言
            code_lang = (
                "json"
                if step_name
                in ["sql_generator", "chart_generator", "question_recommender"]
                else "markdown"
            )
            formatted = step_header + f"```{code_lang}\n{content}\n```"

        # 添加分隔线，确保每个步骤独立分开显示
        separator = "\n\n---\n\n"

        return formatted + separator

    @staticmethod
    async def _send_step_progress(
        response,
        step: str,
        step_name: str,
        status: str,
        progress_id: str,
    ) -> None:
        """
        发送步骤进度信息
        :param response: 响应对象
        :param step: 步骤标识（英文）
        :param step_name: 步骤名称（中文）
        :param status: 状态（"start" 或 "complete"）
        :param progress_id: 进度ID（唯一标识）
        """
        if response:
            progress_data = {
                "type": "step_progress",
                "step": step,
                "stepName": step_name,
                "status": status,
                "progressId": progress_id,
            }
            formatted_message = {
                "data": progress_data,
                "dataType": DataTypeEnum.STEP_PROGRESS.value[0],
            }
            await response.write(
                "data:" + json.dumps(formatted_message, ensure_ascii=False) + "\n\n"
            )

    @staticmethod
    async def _send_response(
        response,
        content: Union[str, Dict[str, Any]],
        message_type: str = "continue",
        data_type: str = DataTypeEnum.ANSWER.value[0],
    ) -> None:
        """
        发送响应数据
        :param response: 响应对象
        :param content: 响应内容，可以是字符串或字典
        :param message_type: 消息类型
        :param data_type: 数据类型
        """
        if response:
            if data_type == DataTypeEnum.ANSWER.value[0]:
                formatted_message = {
                    "data": {
                        "messageType": message_type,
                        "content": content,
                    },
                    "dataType": data_type,
                }
            else:
                # 业务数据（表格/图表），content 是字典
                formatted_message = {"data": content, "dataType": data_type}

            await response.write(
                "data:" + json.dumps(formatted_message, ensure_ascii=False) + "\n\n"
            )

    @staticmethod
    def _create_response(
        content: str,
        message_type: str = "continue",
        data_type: str = DataTypeEnum.ANSWER.value[0],
    ) -> str:
        """
        封装响应结构（保持向后兼容）
        """
        res = {
            "data": {"messageType": message_type, "content": content},
            "dataType": data_type,
        }
        return "data:" + json.dumps(res, ensure_ascii=False) + "\n\n"

    async def cancel_task(self, task_id: str) -> bool:
        """
        取消指定的任务
        :param task_id: 任务ID
        :return: 是否成功取消
        """
        if task_id in self.running_tasks:
            self.running_tasks[task_id]["cancelled"] = True
            return True
        return False

    def cleanup_chat_session(self, chat_id: str) -> bool:
        """
        清理指定chat_id的DuckDB会话
        :param chat_id: 聊天ID
        :return: 是否成功清理
        """
        try:
            return close_duckdb_manager(chat_id=chat_id)
        except Exception as e:
            logger.error(f"清理chat_id '{chat_id}' 的DuckDB会话失败: {str(e)}")
            return False

    def get_chat_session_stats(self) -> dict:
        """
        获取聊天会话统计信息
        :return: 会话统计信息
        """
        try:
            chat_manager = get_chat_duckdb_manager()
            return {
                "active_chat_count": chat_manager.get_active_chat_count(),
                "chat_list": chat_manager.get_chat_list(),
            }
        except Exception as e:
            logger.error(f"获取聊天会话统计失败: {str(e)}")
            return {"active_chat_count": 0, "chat_list": []}

    @staticmethod
    def _format_multi_file_table_info(state: Dict[str, Any]) -> str:
        """
        格式化多文件多Sheet信息为HTML格式
        :param state: 状态字典
        :return: 格式化后的HTML字符串
        """
        file_metadata = state.get("file_metadata", {})
        sheet_metadata = state.get("sheet_metadata", {})
        db_info = state.get("db_info", [])

        if not file_metadata and not db_info:
            return "未找到文件信息"

        html_content = """
        文件解析结果<br>
        """

        # 显示文件信息
        if file_metadata:
            html_content += "文件列表：<br><ol>"
            for file_key, file_info in file_metadata.items():
                html_content += f"<li>file_name: {file_info.file_name}  |"
                f"Catalog: {file_info.catalog_name} |"
                f"Sheets: {file_info.sheet_count} |"
                f"上传时间: {file_info.upload_time} </li>"
            html_content += "</ol><br>"

        # 显示表信息
        if db_info:
            html_content += "数据表：<br><ol>"
            for table in db_info:
                table_name = table.get("table_name", "未知表")
                table_comment = table.get("table_comment", "")
                columns = table.get("columns", {})
                html_content += f"<li>table_name:{table_name} | table_comment:{table_comment} | 列数: {len(columns)} </li>"

            html_content += "</ol><br>"

        html_content += "<br>"
        return html_content

    @staticmethod
    def _format_sql_generator_output(step_value: Dict[str, Any]) -> str:
        """
        格式化 SQL 生成器输出为 JSON 代码块格式
        """
        # 优先使用保存的完整 JSON 响应
        sql_response_json = step_value.get("sql_response_json")

        if sql_response_json:
            # 将完整的 JSON 响应格式化为 markdown 代码块
            json_str = json.dumps(sql_response_json, ensure_ascii=False, indent=2)
            return f"```json\n{json_str}\n```"

        # 如果没有保存的 JSON，从现有字段构建
        generated_sql = step_value.get("generated_sql", "")
        chart_type = step_value.get("chart_type", "")
        used_tables = step_value.get("used_tables", [])

        if not generated_sql or generated_sql == "No SQL query generated":
            return (
                '```json\n{\n  "success": false,\n  "message": "SQL 生成失败"\n}\n```'
            )

        # 构建 JSON 响应
        sql_response = {
            "success": True,
            "sql": generated_sql,
            "tables": used_tables if used_tables else [],
            "chart-type": chart_type if chart_type else "table",
        }

        json_str = json.dumps(sql_response, ensure_ascii=False, indent=2)
        return f"```json\n{json_str}\n```"

    @staticmethod
    def _format_chart_generator_output(step_value: Dict[str, Any]) -> str:
        """
        格式化图表生成器输出为 JSON 代码块格式
        """
        chart_config = step_value.get("chart_config")

        if not chart_config:
            return "图表配置生成完成"

        # 将图表配置格式化为 JSON 代码块
        if isinstance(chart_config, dict):
            config_json = json.dumps(chart_config, ensure_ascii=False, indent=2)
            return f"```json\n{config_json}\n```"
        else:
            return f"```json\n{str(chart_config)}\n```"

    @staticmethod
    def _format_execution_result(execution_result) -> str:
        """
        格式化SQL执行结果
        :param execution_result: ExecutionResult对象
        :return: 格式化后的字符串
        """
        if not execution_result:
            return "未找到执行结果"

        if execution_result.success:
            row_count = len(execution_result.data) if execution_result.data else 0
            column_count = (
                len(execution_result.columns) if execution_result.columns else 0
            )
            return f"✅ 查询执行成功！返回 {row_count} 行数据，{column_count} 列"
        else:
            return f"❌ 查询执行失败：{execution_result.error or '未知错误'}"

    @staticmethod
    def _format_table_columns_info(db_info: Dict[str, Any]) -> str:
        """
        格式化表格列信息为HTML details标签格式
        :param db_info: 数据库信息字典
        :return: 格式化后的HTML字符串
        """
        db_info = db_info["db_info"]
        if not db_info or "columns" not in db_info:
            return ""

        columns_info = db_info["columns"]

        html_content = """
        <ul>
        """
        for column_name, column_details in columns_info.items():
            comment = column_details.get("comment", column_name)
            type_ = column_details.get("type", "未知")
            html_content += (
                f"<li><strong>{column_name}</strong>: {comment} (类型: {type_})</li>\n"
            )

        html_content += """</ul>"""

        return html_content
