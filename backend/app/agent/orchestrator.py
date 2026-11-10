import asyncio
import json
import re
import time

from app.agent.loop import EmitFn, TraceFn, run_agent
from app.agent.prompts import (
    CRITIC_PROMPT,
    PLANNER_PROMPT,
    RESEARCHER_PROMPT,
    WRITER_PROMPT,
)
from app.core.config import get_settings
from app.core.cost import compute_cost
from app.core.llm.base import LLMProvider
from app.rag.store import VectorStore
from app.tools.base import Tool


class BudgetExceeded(RuntimeError):
    """单任务 token 超出预算，触发中止。"""


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


async def _chat(
    provider: LLMProvider,
    system_prompt: str,
    user_content: str,
    trace: TraceFn | None = None,
    label: str = "chat",
) -> str:
    """单次无工具 LLM 调用，返回正文。"""
    resp = await provider.chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        [],
    )
    if trace:
        await trace({
            "agent": label,
            "kind": "llm",
            "name": "chat",
            "input_preview": user_content[:200],
            "output_preview": (resp.content or "")[:200],
            "prompt_tokens": resp.prompt_tokens,
            "completion_tokens": resp.completion_tokens,
            "total_tokens": resp.total_tokens,
            "duration_ms": resp.duration_ms,
        })
    return (resp.content or "").strip()


async def _writer_stream(
    provider: LLMProvider,
    system_prompt: str,
    user_content: str,
    trace: TraceFn | None = None,
    label: str = "writer",
) -> tuple[str, list[str]]:
    """流式生成一版报告：逐段累积但不实时推给前端，返回 (完整正文, 分块列表)。"""
    parts: list[str] = []
    usage: dict = {}
    start = time.perf_counter()
    async for chunk in provider.stream(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        [],
    ):
        if chunk.delta:
            parts.append(chunk.delta)
        if chunk.usage:
            usage = chunk.usage

    text = "".join(parts).strip()
    if trace:
        await trace({
            "agent": label,
            "kind": "llm",
            "name": "chat",
            "input_preview": user_content[:200],
            "output_preview": text[:200],
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "duration_ms": int((time.perf_counter() - start) * 1000),
        })
    return text, parts


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
    """多智能体深度研究：Planner → 并行 Researcher → Writer ⇄ Critic。

    支持断点恢复：每个阶段完成后把中间产物写入 task_checkpoint，
    恢复时读取检查点跳过已完成阶段，最多重复一次在跑的 LLM 调用。
    """
    if emit:
        await emit("plan_start", {})

    model = getattr(provider, "model", "")
    token_budget = get_settings().token_budget

    cp = await asyncio.to_thread(store.load_checkpoint, task_id)
    stage = cp["stage"] if cp else "created"
    plan: dict = cp.get("plan") if cp else {}
    findings: list = cp.get("findings") if cp else []
    draft: str = cp.get("draft") if cp else ""
    critic_round: int = cp.get("critic_round") if cp else 0
    critic_rounds: list = cp.get("critic_rounds") if cp else []
    used_tokens: int = cp.get("used_tokens") if cp else 0

    async def record(ev: dict) -> None:
        """把一次 LLM/工具调用记入 trace：算成本 → 实时推送 → 落库。"""
        nonlocal used_tokens
        used_tokens += ev.get("prompt_tokens", 0) + ev.get("completion_tokens", 0)
        if token_budget > 0 and used_tokens > token_budget:
            raise BudgetExceeded(
                f"超出 token 预算（已用 {used_tokens} / 上限 {token_budget}），已中止研究"
            )
        cost = compute_cost(model, ev.get("prompt_tokens", 0), ev.get("completion_tokens", 0))
        ev = {**ev, "cost": cost}
        if emit:
            await emit("trace", ev)
        await asyncio.to_thread(
            store.add_trace,
            task_id,
            ev.get("agent", ""),
            ev.get("kind", ""),
            ev.get("name", ""),
            ev.get("input_preview", ""),
            ev.get("output_preview", ""),
            ev.get("prompt_tokens", 0),
            ev.get("completion_tokens", 0),
            ev.get("total_tokens", 0),
            cost,
            ev.get("duration_ms", 0),
        )

    # 1. Planner：判断复杂度 + 拆子问题（已完成则从检查点恢复）
    if stage == "created":
        plan_text = await _chat(provider, PLANNER_PROMPT, question, trace=record, label="planner")
        plan = _extract_json(plan_text)
        await asyncio.to_thread(
            store.save_checkpoint, task_id, "planned",
            plan=plan, critic_rounds=critic_rounds, used_tokens=used_tokens,
        )

    complexity = plan.get("complexity", "research")  # 默认 research（安全侧）
    direct_answer = (plan.get("direct_answer") or "").strip()

    if emit:
        await emit("plan", {"complexity": complexity, "sub_questions": plan.get("sub_questions", [])})

    # 2. 快路径：简单问题直接回答，不搜、不抓、不审
    if complexity == "simple" and direct_answer:
        return direct_answer

    # 3. 并行 Researcher（已完成则从检查点恢复）
    sub_questions = (plan.get("sub_questions") or [])[:max_sub_questions]
    if not sub_questions:
        sub_questions = [question]  # 兜底：至少研究原问题

    if stage in ("created", "planned"):
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
                trace=record,
                label=f"researcher-{idx + 1}",
            )
            if emit:
                await emit("researcher_done", {"index": idx, "sub_question": sub_q})
            return f"## 子问题 {idx + 1}：{sub_q}\n{resp.content.strip()}"

        findings = await asyncio.gather(
            *[research_one(i, q) for i, q in enumerate(sub_questions)]
        )
        await asyncio.to_thread(
            store.save_checkpoint, task_id, "researched",
            plan=plan, findings=findings, critic_rounds=critic_rounds, used_tokens=used_tokens,
        )
    else:
        # 恢复：补发 researcher_done 让前端正确显示已完成的研究
        if emit:
            for i, q in enumerate(sub_questions):
                await emit("researcher_done", {"index": i, "sub_question": q})

    findings_text = "\n\n".join(findings)

    # 4. 引用表（来源已在库中，恢复时也能取到）
    citations = await asyncio.to_thread(store.list_task_sources, task_id)
    citations_text = "\n".join(
        f"[{c['citation_id']}] {c['title'] or c['url']} — {c['url']}" for c in citations
    )

    # 5. Writer ⇄ Critic 循环（从检查点的 draft + critic_round 续跑）
    chunks: list[str] | None = None
    if stage not in ("writing",):
        draft, chunks = await _writer_stream(
            provider,
            WRITER_PROMPT,
            _writer_input(question, sub_questions, findings_text, citations_text),
            trace=record,
            label="writer",
        )
        critic_round = 0
        await asyncio.to_thread(
            store.save_checkpoint, task_id, "writing",
            plan=plan, findings=findings, draft=draft, critic_round=0,
            critic_rounds=critic_rounds, used_tokens=used_tokens,
        )

    for r in range(critic_round, max_critic_rounds):
        if emit:
            await emit("critic_start", {"round": r + 1})
        verdict_text = await _chat(
            provider,
            CRITIC_PROMPT,
            f"原问题：{question}\n\n引用表：\n{citations_text or '(无)'}\n\n报告：\n{draft}",
            trace=record,
            label="critic",
        )
        verdict = _extract_json(verdict_text)
        approved = bool(verdict.get("approved"))
        feedback = (verdict.get("feedback") or "").strip()
        if emit:
            await emit("critic_result", {"round": r + 1, "approved": approved, "feedback": feedback})
        critic_rounds.append({"round": r + 1, "approved": approved, "feedback": feedback})
        if approved:
            await asyncio.to_thread(
                store.save_checkpoint, task_id, "writing",
                plan=plan, findings=findings, draft=draft, critic_round=r + 1,
                critic_rounds=critic_rounds, used_tokens=used_tokens,
            )
            break
        if emit:
            await emit("writer_rewrite", {"round": r + 1, "feedback": feedback})
        draft, chunks = await _writer_stream(
            provider,
            WRITER_PROMPT,
            _writer_input(
                question,
                sub_questions,
                findings_text,
                citations_text,
                feedback=feedback or "请改进报告。",
            ),
            trace=record,
            label="writer",
        )
        await asyncio.to_thread(
            store.save_checkpoint, task_id, "writing",
            plan=plan, findings=findings, draft=draft, critic_round=r + 1,
            critic_rounds=critic_rounds, used_tokens=used_tokens,
        )

    # 审核结束后，才把最终版报告流式推给前端（本次若无新撰写则依赖 answer 事件收尾）
    if emit and chunks is not None:
        await emit("answer_start", {})
        for chunk in chunks:
            await emit("answer_chunk", {"content": chunk})
    return draft
