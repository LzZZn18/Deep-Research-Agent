import json
from typing import Awaitable, Callable

from app.agent.prompts import SYSTEM_PROMPT
from app.core.llm.base import LLMProvider, LLMResponse
from app.tools.base import Tool

# emit: 推送 SSE 事件的回调 (event, data) -> awaitable
EmitFn = Callable[[str, dict], Awaitable[None]]


async def run_agent(
    provider: LLMProvider,
    tools: list[Tool],
    query: str,
    max_steps: int,
    emit: EmitFn | None = None,
    system_prompt: str = SYSTEM_PROMPT,
) -> LLMResponse:
    """手写 tool-calling 循环：

       给模型消息 + 可用工具列表
       模型要调工具 → 执行 → 结果塞回上下文 → 继续
       模型不再调工具 → 返回最终回答
    """
    tool_map = {t.name: t for t in tools}
    tool_schemas = [t.to_schema() for t in tools]

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": query},
    ]

    for _ in range(max_steps):
        response = await provider.chat(messages, tool_schemas)

        if not response.tool_calls:
            return response

        # 记录 assistant 的 tool_calls 消息（content 为空时省略）
        assistant_msg: dict = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in response.tool_calls
            ],
        }
        if response.content:
            assistant_msg["content"] = response.content
        messages.append(assistant_msg)

        for tc in response.tool_calls:
            if emit:
                await emit("tool_call", {"tool": tc.name, "arguments": tc.arguments})

            tool = tool_map.get(tc.name)
            if tool is None:
                result = f"错误：未知工具 {tc.name}"
            else:
                try:
                    result = await tool.run(**tc.arguments)
                except Exception as e:  # 工具异常不让整个任务崩溃
                    result = f"工具执行出错：{e}"

            if emit:
                await emit("tool_result", {"tool": tc.name, "result_preview": result[:200]})

            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    return LLMResponse(content="达到最大步数，研究未完成。", tool_calls=[])
