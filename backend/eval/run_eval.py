"""轻量输出质量评测：固定问题集跑真实流水线 + 引用完整性校验 + LLM-as-judge 打分。

用法（在 backend 目录下执行）：
    .venv/Scripts/python.exe eval/run_eval.py             # 跑 questions.json 里全部问题
    .venv/Scripts/python.exe eval/run_eval.py --limit 3   # 只跑前 3 个
    .venv/Scripts/python.exe eval/run_eval.py --skip-judge  # 只跑流水线 + 引用校验，不打分

流程：对每个问题 → 建 task → 跑真实多智能体流水线 → 取报告/引用/成本
      → 程序化校验引用完整性（编造引用率）→ LLM-as-judge 按 rubric 打分 → 汇总。

结果写到 eval/results/<时间戳>.json，并在控制台打印汇总表。
"""

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg

# Windows 下 stdout/stderr 默认可能是 GBK，强制 UTF-8 避免中文日志乱码
# （stderr 也要改：retry 里的 logger.warning 走 stderr）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]  # backend/
sys.path.insert(0, str(ROOT))

from app.agent.orchestrator import run_deep_research
from app.core.config import get_settings
from app.core.llm.deepseek import DeepSeekProvider
from app.rag.embedding import FastEmbedEmbedder
from app.rag.store import VectorStore
from app.tools.fetch import FetchTool
from app.tools.search import SearchTool
from app.tools.vector_search import VectorSearchTool

EVAL_DB = "deepsearch_eval"
QUESTIONS_FILE = Path(__file__).parent / "questions.json"
RESULTS_DIR = Path(__file__).parent / "results"

JUDGE_SYSTEM = """你是一名严谨的研究报告评审专家。请根据原始问题、期望关键点和引用表，对一份研究报告打分。

评分维度（每项 1~5 分，整数）：
- relevance 相关性：报告是否紧扣原问题，没有跑题或答非所问。
- accuracy 准确性：事实陈述是否准确，是否覆盖期望关键点（key_points 若提供）。
- citation 引用正确性：报告中的 [n] 引用是否真实存在且支撑论断；若报告本身不含任何引用（例如简单直答），本项填 null。
- completeness 完整性：是否覆盖问题的主要方面，没有重大遗漏。
- structure 结构：组织是否清晰、有层次、便于阅读。

只输出一个 JSON 对象，不要输出其他任何文字，格式：
{"relevance": 5, "accuracy": 4, "citation": 4, "completeness": 4, "structure": 4, "summary": "一句话中文总评"}"""


def _extract_json(text: str) -> dict:
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


def _swap_db(dsn: str, dbname: str) -> str:
    p = urlparse(dsn)
    return urlunparse(p._replace(path=f"/{dbname}"))


def ensure_eval_db(settings) -> str:
    """创建独立的 deepsearch_eval 库（避免污染 dev 数据），返回 eval DSN。"""
    admin = _swap_db(settings.db_url, "postgres")
    eval_dsn = _swap_db(settings.db_url, EVAL_DB)
    last_err = None
    for attempt in range(30):
        try:
            with psycopg.connect(admin, autocommit=True) as conn:
                exists = conn.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (EVAL_DB,)
                ).fetchone()
                if not exists:
                    conn.execute(f'CREATE DATABASE "{EVAL_DB}"')
            return eval_dsn
        except psycopg.OperationalError as e:
            last_err = e
            time.sleep(2)
    raise RuntimeError(f"无法连接 PostgreSQL（{admin}）：{last_err}")


def check_citations(report: str, citations: list[dict]) -> dict:
    """程序化校验引用完整性：报告里的 [n] 是否都对应真实来源。"""
    valid = {c["citation_id"] for c in citations}
    refs = [int(m) for m in re.findall(r"\[(\d+)\]", report)]
    unique = set(refs)
    fabricated = sorted({n for n in unique if n not in valid})
    return {
        "total_refs": len(refs),
        "unique_refs": len(unique),
        "available_sources": len(valid),
        "fabricated": fabricated,
        "fabricated_rate": round(len(fabricated) / len(unique), 4) if unique else 0.0,
    }


async def judge(
    provider,
    question: str,
    key_points: list[str],
    citations: list[dict],
    report: str,
) -> dict:
    citations_text = "\n".join(
        f"[{c['citation_id']}] {c['title'] or c['url']} — {c['url']}" for c in citations
    )
    kp = "、".join(key_points) if key_points else "（无）"
    user = (
        f"原问题：{question}\n"
        f"期望关键点：{kp}\n"
        f"引用表（[n] → 标题/链接）：\n{citations_text or '(无)'}\n\n"
        f"报告：\n{report}"
    )
    resp = await provider.chat(
        [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}],
        [],
    )
    return _extract_json(resp.content or "")


async def run_one(settings, provider, store, embedder, q: dict, skip_judge: bool) -> dict:
    question = q["question"]
    qtype = q.get("type", "research")
    key_points = q.get("key_points", [])
    print(f"[eval] 开始：{question}", flush=True)
    task_id = await asyncio.to_thread(store.create_task, question, "running")
    tools = [
        SearchTool(api_key=settings.tavily_api_key),
        FetchTool(store=store, embedder=embedder, task_id=task_id),
        VectorSearchTool(store=store, embedder=embedder, task_id=task_id),
    ]
    t0 = time.perf_counter()
    try:
        report = await asyncio.wait_for(
            run_deep_research(
                provider=provider,
                store=store,
                tools=tools,
                question=question,
                task_id=task_id,
                max_sub_questions=settings.max_sub_questions,
                max_critic_rounds=settings.max_critic_rounds,
                researcher_steps=settings.researcher_steps,
                emit=None,
            ),
            timeout=settings.task_timeout,
        )
        await asyncio.to_thread(store.save_report, task_id, report)
        await asyncio.to_thread(store.finish_if_running, task_id, "done")
        sources = await asyncio.to_thread(store.list_task_sources, task_id)
        stats = await asyncio.to_thread(store.task_stats, task_id)
        result = {
            "id": q["id"],
            "question": question,
            "type": qtype,
            "status": "done",
            "report": report,
            "elapsed_s": round(time.perf_counter() - t0, 1),
            "sources": sources,
            "citations": check_citations(report, sources),
            "stats": stats,
        }
    except asyncio.TimeoutError:
        result = {
            "id": q["id"], "question": question, "type": qtype, "status": "timeout",
            "elapsed_s": round(time.perf_counter() - t0, 1),
            "error": f"超时（>{settings.task_timeout:g}s）",
        }
    except Exception as e:
        result = {
            "id": q["id"], "question": question, "type": qtype, "status": "failed",
            "elapsed_s": round(time.perf_counter() - t0, 1), "error": str(e),
        }

    if result.get("report") and not skip_judge:
        print(f"[eval] 打分：{question}", flush=True)
        try:
            result["judge"] = await judge(
                provider, question, key_points, result.get("sources", []), result["report"]
            )
        except Exception as e:
            result["judge_error"] = str(e)
    print(f"[eval] 结束：{question} -> {result['status']}", flush=True)
    return result


async def main(skip_judge: bool, limit: int) -> None:
    settings = get_settings()
    print("[eval] 连接/创建 eval 库…", flush=True)
    eval_dsn = ensure_eval_db(settings)
    store = VectorStore(eval_dsn, settings.embedding_dim)
    store.init_schema()
    print("[eval] 加载 embedding 模型…", flush=True)
    embedder = FastEmbedEmbedder(settings.embedding_model, settings.embedding_dim)
    provider = DeepSeekProvider(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        timeout=settings.llm_timeout,
        retries=settings.llm_retries,
    )
    questions = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
    if limit:
        questions = questions[:limit]
    print(f"[eval] 共 {len(questions)} 个问题，开始跑…", flush=True)

    results = []
    for q in questions:
        results.append(await run_one(settings, provider, store, embedder, q, skip_judge))

    print("\n" + "=" * 70)
    print("评测汇总")
    print("=" * 70)
    dims = ["relevance", "accuracy", "citation", "completeness", "structure"]
    score_map = {d: [] for d in dims}
    total_fab = 0
    for r in results:
        j = r.get("judge") or {}
        if r.get("status") == "done":
            fab = r.get("citations", {}).get("fabricated", [])
            total_fab += len(fab)
            parts = [
                f"相关={j.get('relevance', '-')}",
                f"准确={j.get('accuracy', '-')}",
                f"引用={j.get('citation', '-')}",
                f"完整={j.get('completeness', '-')}",
                f"结构={j.get('structure', '-')}",
                f"编造={len(fab)}",
                f"耗时={r.get('elapsed_s')}s",
            ]
            print(f"[{r['id']}] ({r['type']}) {r['question']}")
            print(f"      {'  '.join(parts)}")
            for d in dims:
                v = j.get(d)
                if isinstance(v, (int, float)):
                    score_map[d].append(v)
        else:
            print(
                f"[{r['id']}] ({r['type']}) {r['question']} -> {r['status']}: "
                f"{r.get('error', '')}"
            )
    print()
    print("各维度平均分（仅统计已打分的项）：")
    for d in dims:
        vals = score_map[d]
        avg = f"{sum(vals) / len(vals):.2f}" if vals else "  -"
        print(f"  {d:<12} {avg}  (n={len(vals)})")
    print(f"  编造引用总数        {total_fab}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存：{out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DeepResearch 输出质量评测")
    parser.add_argument("--skip-judge", action="store_true", help="只跑流水线 + 引用校验，不做 LLM 打分")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 个问题（0 = 全部）")
    args = parser.parse_args()
    asyncio.run(main(args.skip_judge, args.limit))
