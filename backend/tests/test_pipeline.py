"""流水线冒烟测试：用假的 LLM provider，验证 Planner→Researcher→Writer⇄Critic 全链路。

不碰真实 LLM、不联网，只验证编排逻辑、断点与 trace 落库是否正确。
"""

import json

import pytest

from app.agent.loop import run_agent
from app.agent.orchestrator import run_deep_research
from app.agent.prompts import CRITIC_PROMPT, PLANNER_PROMPT, RESEARCHER_PROMPT
from app.core.llm.base import LLMProvider, LLMResponse, StreamChunk, ToolCall
from app.tools.base import Tool


class FakeProvider(LLMProvider):
    """按 system prompt 区分角色，返回脚本化响应。无状态，可被并行调用。"""

    model = "deepseek-chat"

    def __init__(self, planner_json: dict):
        self._planner_json = planner_json

    async def chat(self, messages, tools):
        system = messages[0]["content"] if messages else ""
        if system == PLANNER_PROMPT:
            return LLMResponse(content=json.dumps(self._planner_json, ensure_ascii=False))
        if system == RESEARCHER_PROMPT:
            last = messages[-1] if messages else {}
            if last.get("role") == "tool":
                return LLMResponse(content="这是子问题的发现摘要。")
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(id="c1", name="fake_search", arguments={"query": "x"})],
            )
        if system == CRITIC_PROMPT:
            return LLMResponse(content=json.dumps({"approved": True, "feedback": ""}))
        return LLMResponse(content="")

    async def stream(self, messages, tools):
        yield StreamChunk(
            delta="# 测试报告\n\n正文内容。",
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )


class FakeSearchTool(Tool):
    name = "fake_search"
    description = "假的搜索工具"
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }

    def __init__(self):
        self.calls: list[str] = []

    async def run(self, query: str = "", **kwargs):
        self.calls.append(query)
        return "找到 1 个来源：https://example.com/a"


@pytest.fixture()
def research_plan():
    return {
        "complexity": "research",
        "sub_questions": ["子问题A", "子问题B"],
        "direct_answer": "",
    }


async def test_full_research_pipeline(store, research_plan):
    provider = FakeProvider(research_plan)
    task_id = store.create_task("测试问题", status="running")
    tool = FakeSearchTool()

    result = await run_deep_research(
        provider=provider,
        store=store,
        tools=[tool],
        question="测试问题",
        task_id=task_id,
        max_sub_questions=3,
        max_critic_rounds=2,
        researcher_steps=5,
    )

    assert result.strip() == "# 测试报告\n\n正文内容。"
    assert tool.calls  # 两个 researcher 都调用过工具

    # 断点已推进到 writing，两个子问题的发现都已保存
    cp = store.load_checkpoint(task_id)
    assert cp["stage"] == "writing"
    assert len(cp["findings"]) == 2

    # trace 覆盖所有角色：planner + 2 个 researcher + writer + critic
    agents = {t["agent"] for t in store.list_trace(task_id)}
    assert "planner" in agents
    assert "writer" in agents
    assert "critic" in agents
    assert any(a.startswith("researcher-") for a in agents)


async def test_simple_question_fast_path(store):
    provider = FakeProvider(
        {
            "complexity": "simple",
            "sub_questions": [],
            "direct_answer": "向量数据库是用于存储和检索向量的数据库。",
        }
    )
    task_id = store.create_task("什么是向量数据库", status="running")

    result = await run_deep_research(
        provider=provider,
        store=store,
        tools=[],
        question="什么是向量数据库",
        task_id=task_id,
        max_sub_questions=3,
        max_critic_rounds=2,
        researcher_steps=5,
    )

    assert result == "向量数据库是用于存储和检索向量的数据库。"
    # 快路径不产生 researcher / critic 的 trace（只跑 planner）
    agents = {t["agent"] for t in store.list_trace(task_id)}
    assert not any(a.startswith("researcher-") for a in agents)
    assert "critic" not in agents


async def test_researcher_tool_loop():
    provider = FakeProvider(
        {"complexity": "research", "sub_questions": ["a"], "direct_answer": ""}
    )
    tool = FakeSearchTool()
    resp = await run_agent(
        provider=provider,
        tools=[tool],
        query="研究子问题",
        max_steps=3,
        system_prompt=RESEARCHER_PROMPT,
    )
    # 第一次调用模型发起工具调用，工具执行后结果回填，第二次返回最终答案
    assert tool.calls == ["x"]
    assert resp.content == "这是子问题的发现摘要。"
    assert resp.tool_calls == []
