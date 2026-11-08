import asyncio
import json
import re

from app.agent.loop import EmitFn, run_agent
from app.agent.prompts import (
    CRITIC_PROMPT,
    PLANNER_PROMPT,
    RESEARCHER_PROMPT,
    WRITER_PROMPT,
)
from app.core.llm.base import LLMProvider
from app.rag.store import VectorStore
from app.tools.base import Tool


def _extract_json(text: str) -> dict:
    """从 LLM 输出里容错提取 JSON（剥掉 ``` 代码块、取首个 {...}）。"""
    if not text:
        return {}
    cleaned = re.sub(r"```[a-zA-Z]*", "", text)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return {}


async def _chat(provider: LLMProvider, system_prompt: str, user_content: str) -> str:
    """单次无工具 LLM 调用，返回正文。"""
    resp = await provider.chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        [],
    )
    return (resp.content or "").strip()


def _writer_input(
    question: str,
    sub_questions: list[str],
    findings: str,
    citations: str,
    feedback: str = "",
) -> str:
    parts = [
        f"原问题：{question}",
        "研究规划（子问题）：\n" + "\n".join(f"- {q}" for q in sub_questions),
        f"各子问题的发现：\n{findings}",
        f"引用表（[n] → 标题/链接）：\n{citations or '(无)'}",
    ]
    if feedback:
        parts.append(f"上一版报告的修改意见：\n{feedback}")
    return "\n\n".join(parts)


async def run_deep_research(
    provider: LLMProvider,
    store: VectorStore,
    tools: list[Tool],
    question: str,
    task_id: int,
    max_sub_questions: int,
    max_critic_rounds: int,
    researcher_steps: int,
    emit: EmitFn | None = None,
) -> str:
    """多智能体深度研究：Planner → 并行 Researcher → Writer ⇄ Critic。"""
    if emit:
        await emit("plan_start", {})

    # 1. Planner：判断复杂度 + 拆子问题
    plan_text = await _chat(provider, PLANNER_PROMPT, question)
    plan = _extract_json(plan_text)
    complexity = plan.get("complexity", "research")  # 默认 research（安全侧）
    direct_answer = (plan.get("direct_answer") or "").strip()

    if emit:
        await emit("plan", {"complexity": complexity, "sub_questions": plan.get("sub_questions", [])})

    # 2. 快路径：简单问题直接回答，不搜、不抓、不审
    if complexity == "simple" and direct_answer:
        return direct_answer

    # 3. 并行 Researcher
    sub_questions = (plan.get("sub_questions") or [])[:max_sub_questions]
    if not sub_questions:
        sub_questions = [question]  # 兜底：至少研究原问题

    async def research_one(idx: int, sub_q: str) -> str:
        if emit:
            await emit("researcher_start", {"index": idx, "sub_question": sub_q})
        resp = await run_agent(
            provider=provider,
            tools=tools,
            query=f"请研究以下子问题：{sub_q}",
            max_steps=researcher_steps,
            emit=emit,
            system_prompt=RESEARCHER_PROMPT,
        )
        if emit:
            await emit("researcher_done", {"index": idx, "sub_question": sub_q})
        return f"## 子问题 {idx + 1}：{sub_q}\n{resp.content.strip()}"

    findings = await asyncio.gather(
        *[research_one(i, q) for i, q in enumerate(sub_questions)]
    )
    findings_text = "\n\n".join(findings)

    # 4. 引用表（来自数据库，真实可追溯）
    citations = await asyncio.to_thread(store.list_task_sources, task_id)
    citations_text = "\n".join(
        f"[{c['citation_id']}] {c['title'] or c['url']} — {c['url']}" for c in citations
    )

    # 5. Writer ⇄ Critic 循环
    draft = await _chat(
        provider,
        WRITER_PROMPT,
        _writer_input(question, sub_questions, findings_text, citations_text),
    )
    for r in range(max_critic_rounds):
        if emit:
            await emit("critic_start", {"round": r + 1})
        verdict_text = await _chat(
            provider,
            CRITIC_PROMPT,
            f"原问题：{question}\n\n引用表：\n{citations_text or '(无)'}\n\n报告：\n{draft}",
        )
        verdict = _extract_json(verdict_text)
        approved = bool(verdict.get("approved"))
        feedback = (verdict.get("feedback") or "").strip()
        if emit:
            await emit("critic_result", {"round": r + 1, "approved": approved, "feedback": feedback})
        if approved:
            break
        if emit:
            await emit("writer_rewrite", {"round": r + 1, "feedback": feedback})
        draft = await _chat(
            provider,
            WRITER_PROMPT,
            _writer_input(
                question,
                sub_questions,
                findings_text,
                citations_text,
                feedback=feedback or "请改进报告。",
            ),
        )
    return draft
