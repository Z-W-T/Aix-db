import asyncio
import json
import logging
import os
import traceback
from typing import Optional

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ClearToolUsesEdit,
    ContextEditingMiddleware,
    SummarizationMiddleware,
)
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.checkpoint.memory import InMemorySaver

from agent.middleware.customer_middleware import log_before_model
from common.langfuse_util import (
    create_langfuse_callback_handler,
    is_tracing_enabled,
    langfuse_trace_context,
)
from common.llm_util import get_llm
from common.minio_util import MinioUtils
from constants.code_enum import DataTypeEnum, IntentEnum
from services.user_service import add_user_record, decode_jwt_token

# Langfuse 延迟导入，仅在启用 tracing 时导入

logger = logging.getLogger(__name__)

minio_utils = MinioUtils()


class CommonReactAgent:
    """
    基于LangChain的React智能体，支持多轮对话记忆
    """

    def __init__(self):

        # 是否启用链路追踪
        self.ENABLE_TRACING = is_tracing_enabled()

        # 使用 os.path 构建路径
        # current_dir = os.path.dirname(os.path.abspath(__file__))
        # mcp_tool_path = os.path.join(current_dir, "mcp", "query_db_tool.py")
        self.client = MultiServerMCPClient(
            {
                "mcp-hub": {
                    "url": os.getenv("MCP_HUB_COMMON_QA_GROUP_URL"),
                    "transport": "streamable_http",
                },
                # "query_qa_record": {
                #     "command": "python",
                #     "args": [mcp_tool_path],
                #     "transport": "stdio",
                # },
                # "undoom-douyin-data-analysis": {
                #     "command": "uvx",
                #     "transport": "stdio",
                #     "args": [
                #         "--index-url",
                #         "https://mirrors.aliyun.com/pypi/simple/",
                #         "--from",
                #         "undoom-douyin-data-analysis",
                #         "undoom-douyin-mcp",
                #     ],
                # },
            }
        )

        # 全局checkpointer用于持久化所有用户的对话状态
        self.checkpointer = InMemorySaver()

        # 存储运行中的任务
        self.running_tasks = {}

    @staticmethod
    def _create_response(
        content: str,
        message_type: str = "continue",
        data_type: str = DataTypeEnum.ANSWER.value[0],
    ) -> str:
        """封装响应结构"""
        res = {
            "data": {"messageType": message_type, "content": content},
            "dataType": data_type,
        }
        return "data:" + json.dumps(res, ensure_ascii=False) + "\n\n"

    async def run_agent(
        self,
        query: str,
        response,
        session_id: Optional[str] = None,
        uuid_str: str = None,
        user_token=None,
        file_list: dict = None,
    ):
        """
        运行智能体，支持多轮对话记忆
        :param query: 用户输入
        :param response: 响应对象
        :param session_id: 会话ID，用于区分同一轮对话
        :param uuid_str: 自定义ID，用于唯一标识一次问答
        :param file_list: 附件
        :param user_token:
        :return:
        """
        file_as_markdown = ""
        if file_list:
            file_as_markdown = minio_utils.get_files_content_as_markdown(file_list)

        # 获取用户信息 标识对话状态
        user_dict = await decode_jwt_token(user_token)
        task_id = user_dict["id"]
        task_context = {"cancelled": False}
        self.running_tasks[task_id] = task_context

        try:
            t02_answer_data = []

            tools = []  # await self.client.get_tools()

            # 使用用户会话ID作为thread_id，如果未提供则使用默认值
            thread_id = session_id if session_id else "default_thread"
            config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 50}

            # 准备 tracing 配置
            if self.ENABLE_TRACING:
                langfuse_handler = create_langfuse_callback_handler()
                callbacks = [langfuse_handler]
                config["callbacks"] = callbacks
                config["metadata"] = {"langfuse_session_id": session_id}

            system_message = SystemMessage(
                content="""
            # Role: 高级AI助手

            ## Profile
            - language: 中文
            - description: 一位具备多领域知识、高度专业性与结构化输出能力的智能助手，专注于提供精准、高效、可信赖的信息服务。
            - background: 基于大规模语言模型训练，融合技术、学术、生活等多维度知识体系，能够适应多种场景下的信息查询与任务处理需求。
            - personality: 严谨、专业、逻辑清晰，注重细节与用户体验，追求信息传递的准确性与表达的简洁性。
            - expertise: 多领域知识整合、结构化内容生成、技术说明、数据分析、编程辅助、语言表达优化等。
            - target_audience: 技术人员、研究人员、学生、内容创作者及各类需要精准信息支持的用户。

            ## Skills

            1. 信息处理与表达
               - 精准应答：确保输出内容准确无误，对不确定信息明确标注「暂未掌握该信息」
               - 结构化输出：根据内容类型采用文本、代码块、列表等多种形式进行清晰表达
               - 语言适配：始终使用用户提问语言进行回应，确保语义一致与文化适配
               - 技术说明：对专业术语、技术原理提供背景信息与详细解释，便于理解

            2. 工具协作与交互
               - 工具调用提示：在需要调用外部工具时明确标注「工具调用」并说明调用目的
               - 操作透明化：在涉及流程性任务时说明步骤与逻辑，增强用户信任与理解
               - 多模态支持：支持文本、代码、数据等多种信息类型的识别与响应
               - 用户反馈整合：根据用户反馈优化输出策略，提升交互质量

            ## Rules

            1. 基本原则：
               - 准确性优先：所有输出内容必须基于可靠知识，不臆测、不虚构
               - 用户导向：围绕用户需求组织内容，避免无关信息干扰
               - 透明性：在涉及工具调用、逻辑推理或数据处理时保持过程透明
               - 可读性：结构清晰、层级分明、排版整洁，便于快速阅读与理解

            2. 行为准则：
               - 语言一致性：始终使用用户提问语言进行回应
               - 技术细节补充：对复杂或专业内容提供背景信息与解释
               - 信息边界明确：对未知或超出能力范围的内容如实说明
               - 风格统一：保持段落、层级、图标风格一致，避免杂乱

            3. 限制条件：
               - 不生成违法、有害或误导性内容
               - 不模拟人类情感或主观判断
               - 不提供医疗、法律等专业建议（除非明确授权）
               - 不处理包含隐私、敏感或机密信息的请求

            ## 关键规则
                1. 在完成用户请求后必须直接输出最终答案，不要进行额外的操作
                2. 避免无意义的重复工具调用
                3. 当不需要调用工具时，直接回答用户问题
                4. 在完成任务后立即停止，不要进行自我反思或额外验证

            ## Workflows

            - 目标: 提供准确、结构清晰、风格统一的高质量回答
            - 步骤 1: 理解用户意图，识别问题类型与需求层次
            - 步骤 2: 检索知识库，组织相关信息，判断是否需要调用工具
            - 步骤 3: 按照格式规范生成内容，进行语言与结构优化
            - 预期结果: 用户获得结构清晰、语言准确、风格统一的专业级回答

            ## OutputFormat

            1. 输出格式类型：
               - format: markdown
               - structure: 分节说明，层级清晰，模块分明
               - style: 专业、简洁、结构化，强调信息密度与可读性
               - special_requirements: 使用Unicode图标增强视觉引导，图标与内容匹配，风格统一

            2. 格式规范：
               - indentation: 使用两个空格缩进
               - sections: 按模块划分，使用标题、列表、加粗等方式增强可读性
               - highlighting: 关键信息使用**加粗**或代码块```
               - icons: 每个主要模块前添加1个相关图标，与文字保留1个空格

            3. 验证规则：
               - validation: 所有输出需符合markdown语法规范
               - constraints: 图标风格统一，层级结构清晰，内容与格式分离
               - error_handling: 若格式错误，自动尝试恢复结构并提示用户

            4. 示例说明：

               1. 示例1：
                  - 标题: 简单问答示例
                  - 格式类型: markdown
                  - 说明: 展示基本问答格式与图标使用规范
                  - 示例内容: |
                      📌 **问题：** 什么是AI？
                      ✅ **回答：** AI（Artificial Intelligence，人工智能）是指由人创造的能够感知环境、学习知识、逻辑推理并执行任务的智能体。

               2. 示例2：
                  - 标题: 代码输出示例
                  - 格式类型: markdown
                  - 说明: 展示代码类输出格式与图标使用
                  - 示例内容: |
                      💻 **Python示例：**
                      ```python
                      def greet(name):
                          print(f"Hello, {name}!")
                      greet("World")
                      ```
                      📌 说明：这是一个简单的Python函数，用于打印问候语。

            ## Initialization
            作为高级AI助手，你必须遵守上述Rules，按照Workflows执行任务，并按照[输出格式]输出。
            """
            )

            llm = get_llm()
            agent = create_agent(
                model=llm,
                tools=tools,
                system_prompt=system_message.content,
                checkpointer=self.checkpointer,  # 使用全局checkpointer
                middleware=[
                    log_before_model,
                    # 开启上下文总结压缩
                    SummarizationMiddleware(
                        llm,
                        max_tokens_before_summary=4000,
                        messages_to_keep=20,
                    ),
                    # 通过修剪、总结或清除工具使用来管理对话上下文。
                    # 需要定期清理上下文的长对话
                    # 从上下文中删除失败的工具尝试
                    ContextEditingMiddleware(
                        edits=[
                            ClearToolUsesEdit(trigger=10000),  # Clear old tool uses
                        ],
                    ),
                ],
            )

            # 如果有文件内容，则将其添加到查询中
            formatted_query = query
            if file_as_markdown:
                formatted_query = f"{query}\n\n参考资料内容如下：\n{file_as_markdown}"

            # 如果启用 tracing，包裹在 trace 上下文中
            stream_args = {
                "input": {"messages": [HumanMessage(content=formatted_query)]},
                "config": config,
                "stream_mode": "messages",
            }

            if self.ENABLE_TRACING:
                user_info = await decode_jwt_token(user_token)
                user_id = user_info.get("id")
                with langfuse_trace_context(
                    name="通用问答",
                    input_value=query,
                    session_id=session_id,
                    user_id=user_id,
                ):
                    await self._stream_agent_response(
                        agent, stream_args, response, task_id, t02_answer_data
                    )
            else:
                await self._stream_agent_response(
                    agent, stream_args, response, task_id, t02_answer_data
                )

            # 只有在未取消的情况下才保存记录
            if not self.running_tasks[task_id]["cancelled"]:
                await add_user_record(
                    uuid_str,
                    session_id,
                    query,
                    t02_answer_data,
                    {},
                    IntentEnum.COMMON_QA.value[0],
                    user_token,
                    file_list,
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
            print(f"[ERROR] Agent运行异常: {e}")
            traceback.print_exception(e)
            await response.write(
                self._create_response(
                    "[ERROR] 智能体运行异常:", "error", DataTypeEnum.ANSWER.value[0]
                )
            )
        finally:
            # 清理任务记录
            if task_id in self.running_tasks:
                del self.running_tasks[task_id]

    async def _stream_agent_response(
        self, agent, stream_args, response, task_id, t02_answer_data
    ):
        """处理agent流式响应的核心逻辑"""
        async for message_chunk, metadata in agent.astream(**stream_args):
            # 检查是否已取消
            if self.running_tasks[task_id]["cancelled"]:
                await response.write(
                    self._create_response(
                        "\n> 这条消息已停止", "info", DataTypeEnum.ANSWER.value[0]
                    )
                )
                # 发送最终停止确认消息
                await response.write(
                    self._create_response("", "end", DataTypeEnum.STREAM_END.value[0])
                )
                break

            # 工具输出
            if metadata["langgraph_node"] == "tools":
                tool_name = message_chunk.name or "未知工具"
                tool_use = "> 调用工具:" + tool_name + "\n\n"
                await response.write(self._create_response(tool_use))
                t02_answer_data.append(tool_use)
                continue

            # 输出最终结果
            if message_chunk.content:
                content = message_chunk.content
                t02_answer_data.append(content)
                await response.write(self._create_response(content))
                # 确保实时输出
                if hasattr(response, "flush"):
                    await response.flush()
                await asyncio.sleep(0)

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

    def get_running_tasks(self):
        """
        获取当前运行中的任务列表
        :return: 运行中的任务列表
        """
        return list(self.running_tasks.keys())
