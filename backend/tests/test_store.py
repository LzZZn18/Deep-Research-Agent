"""VectorStore 状态机、断点、引用编号的单元测试。

这些是「取消与并发一致性」「断点恢复」的核心逻辑，纯 DB 断言，不依赖 LLM / 网络。
"""


def test_start_if_pending_sets_running(store):
    tid = store.create_task("q", status="pending")
    assert store.start_if_pending(tid) is True
    assert store.get_task(tid)["status"] == "running"


def test_start_if_pending_ignores_cancelled(store):
    tid = store.create_task("q", status="cancelled")
    assert store.start_if_pending(tid) is False
    assert store.get_task(tid)["status"] == "cancelled"


def test_start_if_pending_ignores_already_running(store):
    tid = store.create_task("q", status="running")
    assert store.start_if_pending(tid) is False
    assert store.get_task(tid)["status"] == "running"


def test_finish_if_running_updates(store):
    tid = store.create_task("q", status="running")
    store.finish_if_running(tid, "done")
    assert store.get_task(tid)["status"] == "done"


def test_finish_if_running_does_not_overwrite_cancelled(store):
    # 核心不变量：取消（cancelled）不能被 worker 的收尾覆盖回 done
    tid = store.create_task("q", status="cancelled")
    store.finish_if_running(tid, "done")
    assert store.get_task(tid)["status"] == "cancelled"


def test_checkpoint_roundtrip(store):
    tid = store.create_task("q")
    plan = {"complexity": "research", "sub_questions": ["a", "b"], "direct_answer": ""}
    findings = ["发现1", "发现2"]
    critic_rounds = [{"round": 1, "approved": True, "feedback": ""}]
    store.save_checkpoint(
        tid,
        "writing",
        plan=plan,
        findings=findings,
        draft="草稿",
        critic_round=1,
        critic_rounds=critic_rounds,
        used_tokens=1234,
    )
    cp = store.load_checkpoint(tid)
    assert cp["stage"] == "writing"
    assert cp["plan"] == plan
    assert cp["findings"] == findings
    assert cp["draft"] == "草稿"
    assert cp["critic_round"] == 1
    assert cp["critic_rounds"] == critic_rounds
    assert cp["used_tokens"] == 1234


def test_checkpoint_missing_returns_none(store):
    assert store.load_checkpoint(999999) is None


def test_citation_number_reused_for_same_source(store):
    tid = store.create_task("q")
    n1 = store.add_source(tid, "https://example.com/a", "标题A", [], [])
    n2 = store.add_source(tid, "https://example.com/a", "标题A更新", [], [])
    assert n1 == n2 == 1  # 同一 url 复用同一引用号
    n3 = store.add_source(tid, "https://example.com/b", "标题B", [], [])
    assert n3 == 2  # 新来源递增


def test_fail_stale_running_only_fails_running(store):
    r1 = store.create_task("跑", status="running")
    r2 = store.create_task("跑", status="running")
    done = store.create_task("完成", status="done")
    n = store.fail_stale_running()
    assert n == 2
    assert store.get_task(r1)["status"] == "failed"
    assert store.get_task(r2)["status"] == "failed"
    assert store.get_task(done)["status"] == "done"
