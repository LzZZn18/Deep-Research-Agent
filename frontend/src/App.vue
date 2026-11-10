<script setup>
import { ref, computed, onMounted } from 'vue';
import { startResearchAsync, resumeTaskAsync, subscribeEvents } from './lib/sse.js';
import { exportWord, exportPdf } from './lib/export.js';
import MarkdownView from './components/MarkdownView.vue';

const question = ref('');
const running = ref(false);
const error = ref('');
const notice = ref('');
const plan = ref(null);
const simpleMode = ref(false);
const researchers = ref([]);
const criticRounds = ref([]);
const report = ref('');
const reportEl = ref(null);
const toolLogs = ref([]);
const showTools = ref(false);
const showStats = ref(false);

const view = ref('new');
const tasks = ref([]);
const currentTask = ref(null);
const resumeTargetId = ref(null);

function emptyStats() {
  return {
    prompt_tokens: 0,
    completion_tokens: 0,
    total_tokens: 0,
    total_cost: 0,
    total_duration_ms: 0,
    calls: 0,
    by_agent: {},
  };
}
const stats = ref(emptyStats());

const stages = ref([
  { key: 'planning', label: '规划', status: 'pending' },
  { key: 'researching', label: '研究', status: 'pending' },
  { key: 'writing', label: '撰写', status: 'pending' },
  { key: 'critiquing', label: '审核', status: 'pending' },
  { key: 'done', label: '完成', status: 'pending' },
]);

const elapsed = ref(0);
let timerId = null;
let pollTimer = null;
let sseController = null;
let cancelled = false;
let currentTaskId = null;
let streamBuf = '';
let streamPending = false;

const activeReport = computed(() =>
  view.value === 'history' ? currentTask.value?.report || '' : report.value
);

const activeStats = computed(() =>
  view.value === 'history' ? currentTask.value?.stats || null : stats.value
);

const byAgentRows = computed(() => {
  const s = activeStats.value;
  if (!s || !s.by_agent) return [];
  return Object.entries(s.by_agent).map(([agent, v]) => ({
    agent: agentLabel(agent),
    tokens: v.tokens,
    cost: v.cost,
    duration_ms: v.duration_ms,
    calls: v.calls,
  }));
});

// —— 历史详情：用后端读回的过程数据重现「研究时的画面」 ——
const historyPlan = computed(() => currentTask.value?.plan || null);
const historySimpleMode = computed(() => historyPlan.value?.complexity === 'simple');
const historySubQuestions = computed(() => historyPlan.value?.sub_questions || []);
const historyResearchers = computed(() =>
  historySubQuestions.value.map((sq, i) => ({ index: i, sub_question: sq, status: 'done' }))
);
const historyCriticRounds = computed(() => currentTask.value?.critic_rounds || []);
const historyToolLogs = computed(() => {
  const logs = [];
  for (const t of currentTask.value?.trace || []) {
    if (t.kind !== 'tool') continue;
    logs.push(`▸ ${t.name}(${t.input_preview || ''})\n  ${t.output_preview || ''}`);
  }
  return logs;
});

// 历史详情的进度条：按 status + stage 回退到「当时中断的状态」
const historyStages = computed(() => {
  const status = currentTask.value?.status;
  const stage = currentTask.value?.stage;
  const s = {
    planning: 'pending',
    researching: 'pending',
    writing: 'pending',
    critiquing: 'pending',
    done: 'pending',
  };
  if (status === 'done') {
    for (const k of Object.keys(s)) s[k] = 'done';
    return s;
  }
  if (stage === 'planned') {
    s.planning = 'done';
    s.researching = 'active';
  } else if (stage === 'researched') {
    s.planning = 'done';
    s.researching = 'done';
    s.writing = 'active';
  } else if (stage === 'writing') {
    s.planning = 'done';
    s.researching = 'done';
    s.writing = 'done';
    s.critiquing = 'active';
  } else {
    s.planning = 'active';
  }
  return s;
});

function setStage(key, status) {
  const s = stages.value.find((x) => x.key === key);
  if (s) s.status = status;
}

function resetStages() {
  stages.value.forEach((s) => (s.status = 'pending'));
}

function fmtElapsed(s) {
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}m ${r}s`;
}

function startTimer() {
  stopTimer();
  elapsed.value = 0;
  timerId = setInterval(() => {
    elapsed.value += 1;
  }, 1000);
}

function stopTimer() {
  if (timerId) {
    clearInterval(timerId);
    timerId = null;
  }
}

function statusLabel(s) {
  return s === 'done' ? '完成' : s === 'running' ? '研究中' : '待开始';
}

function taskStatusText(s) {
  return s === 'done' ? '完成'
    : s === 'failed' ? '失败'
    : s === 'cancelled' ? '已取消'
    : s === 'pending' ? '排队中'
    : '进行中';
}

function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function agentLabel(a) {
  if (a === 'planner') return '规划';
  if (a === 'writer') return '撰写';
  if (a === 'critic') return '审核';
  const m = /^researcher-(\d+)$/.exec(a);
  if (m) return `研究员 ${m[1]}`;
  return a;
}

function fmtTokens(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(2) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n || 0);
}

function fmtCost(c) {
  if (!c) return '0';
  return c >= 0.01 ? c.toFixed(2) : c.toFixed(4);
}

function fmtMs(ms) {
  if (ms >= 1000) return (ms / 1000).toFixed(1) + 's';
  return (ms || 0) + 'ms';
}

function allResearchersDone() {
  return researchers.value.length > 0 && researchers.value.every((r) => r.status === 'done');
}

function reset() {
  stopPolling();
  stopSSE();
  error.value = '';
  notice.value = '';
  plan.value = null;
  simpleMode.value = false;
  researchers.value = [];
  criticRounds.value = [];
  report.value = '';
  streamBuf = '';
  streamPending = false;
  toolLogs.value = [];
  stats.value = emptyStats();
  resetStages();
  cancelled = false;
}

function flushStream() {
  if (streamBuf) {
    report.value += streamBuf;
    streamBuf = '';
  }
  streamPending = false;
}

function appendStream(text) {
  streamBuf += text;
  if (!streamPending) {
    streamPending = true;
    requestAnimationFrame(flushStream);
  }
}

async function loadTasks() {
  try {
    const res = await fetch('/api/tasks');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    tasks.value = await res.json();
  } catch {
    // 静默失败，不影响主流程
  }
}

async function openTask(id) {
  error.value = '';
  notice.value = '';
  try {
    const res = await fetch(`/api/tasks/${id}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    currentTask.value = await res.json();
    view.value = 'history';
  } catch (e) {
    error.value = `加载历史记录失败：${e.message}`;
  }
}

function newResearch() {
  if (!running.value) reset();
  view.value = 'new';
  currentTask.value = null;
  resumeTargetId.value = null;
}

function makeHandlers() {
  return {
    task_start: (d) => {
      currentTaskId = d.task_id;
      loadTasks();
    },
    snapshot: (d) => applySnapshot(d),
    plan_start: () => setStage('planning', 'active'),
    plan: (d) => {
      plan.value = d;
      simpleMode.value = d.complexity === 'simple';
      setStage('planning', 'done');
      if (!simpleMode.value) {
        setStage('researching', 'active');
        researchers.value = d.sub_questions.map((sq, i) => ({
          index: i,
          sub_question: sq,
          status: 'pending',
        }));
      }
    },
    researcher_start: (d) => {
      setStage('researching', 'active');
      const r = researchers.value[d.index];
      if (r) r.status = 'running';
    },
    researcher_done: (d) => {
      const r = researchers.value[d.index];
      if (r) r.status = 'done';
      if (allResearchersDone()) {
        setStage('researching', 'done');
        setStage('writing', 'active');
      }
    },
    tool_call: (d) => toolLogs.value.push(`▸ ${d.tool}(${JSON.stringify(d.arguments)})`),
    tool_result: (d) => toolLogs.value.push(`  ${d.result_preview || ''}`),
    trace: (d) => {
      const s = stats.value;
      s.prompt_tokens += d.prompt_tokens || 0;
      s.completion_tokens += d.completion_tokens || 0;
      s.total_tokens += d.total_tokens || 0;
      s.total_cost += d.cost || 0;
      s.total_duration_ms += d.duration_ms || 0;
      s.calls += 1;
      const a = s.by_agent[d.agent] || (s.by_agent[d.agent] = { tokens: 0, cost: 0, duration_ms: 0, calls: 0 });
      a.tokens += d.total_tokens || 0;
      a.cost += d.cost || 0;
      a.duration_ms += d.duration_ms || 0;
      a.calls += 1;
    },
    critic_start: () => {
      setStage('writing', 'done');
      setStage('critiquing', 'active');
    },
    critic_result: (d) => {
      criticRounds.value.push(d);
      if (d.approved) setStage('critiquing', 'done');
    },
    writer_rewrite: () => {
      setStage('critiquing', 'pending');
      setStage('writing', 'active');
    },
    answer_start: () => {
      streamBuf = '';
      streamPending = false;
      report.value = '';
    },
    answer_chunk: (d) => appendStream(d.content || ''),
    answer: (d) => {
      streamBuf = '';
      streamPending = false;
      report.value = d.content;
      setStage('writing', 'done');
      setStage('critiquing', 'done');
      setStage('done', 'done');
    },
    task_end: () => {
      setStage('done', 'done');
      running.value = false;
      stopTimer();
      stopSSE();
      loadTasks();
    },
    error: (d) => {
      if (!cancelled) error.value = d.message || '出错了';
      running.value = false;
      stopTimer();
      stopSSE();
      loadTasks();
    },
    close: () => {
      running.value = false;
      stopTimer();
      stopSSE();
    },
  };
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function stopSSE() {
  if (sseController) {
    sseController.abort();
    sseController = null;
  }
}

function applyStageToStepper(stage) {
  resetStages();
  if (stage === 'planned') {
    setStage('planning', 'done');
    setStage('researching', 'active');
  } else if (stage === 'researched') {
    setStage('planning', 'done');
    setStage('researching', 'done');
    setStage('writing', 'active');
  } else if (stage === 'writing') {
    setStage('planning', 'done');
    setStage('researching', 'done');
    setStage('writing', 'done');
    setStage('critiquing', 'active');
  }
}

function applySnapshot(t) {
  if (!t) return;
  plan.value = t.plan && Object.keys(t.plan).length ? t.plan : null;
  simpleMode.value = t.plan?.complexity === 'simple';
  const researched = t.stage === 'researched' || t.stage === 'writing';
  researchers.value = (t.plan?.sub_questions || []).map((sq, i) => ({
    index: i,
    sub_question: sq,
    status: researched ? 'done' : 'pending',
  }));
  criticRounds.value = t.critic_rounds || [];
  toolLogs.value = (t.trace || [])
    .filter((x) => x.kind === 'tool')
    .map((x) => `▸ ${x.name}(${x.input_preview || ''})\n  ${x.output_preview || ''}`);
  if (t.stats) stats.value = t.stats;
  if (t.report) {
    streamBuf = '';
    streamPending = false;
    report.value = t.report;
  }
  applyStageToStepper(t.stage);

  if (t.status === 'done') {
    setStage('planning', 'done');
    setStage('researching', 'done');
    setStage('writing', 'done');
    setStage('critiquing', 'done');
    setStage('done', 'done');
    running.value = false;
    stopTimer();
    stopPolling();
    loadTasks();
  } else if (t.status === 'failed') {
    if (!cancelled) error.value = '研究失败，可点击「继续研究」从断点恢复';
    running.value = false;
    stopTimer();
    stopPolling();
    loadTasks();
  }
}

function startPolling(taskId) {
  stopPolling();
  const tick = async () => {
    try {
      const res = await fetch(`/api/tasks/${taskId}`);
      if (!res.ok) return;
      applySnapshot(await res.json());
    } catch {
      // 静默失败，下一轮重试
    }
  };
  tick();
  pollTimer = setInterval(tick, 1500);
}

function startSSE(taskId) {
  stopPolling();
  stopSSE();
  const handlers = makeHandlers();
  sseController = subscribeEvents(taskId, {
    onEvent: (event, data) => handlers[event]?.(data),
    onError: () => {
      // SSE 断线：降级为轮询兜底，保证最终状态能拉到
      if (!cancelled && currentTaskId != null) startPolling(currentTaskId);
    },
    onClose: () => {},
  });
}

async function findDuplicate(q) {
  try {
    const res = await fetch(`/api/tasks/lookup?question=${encodeURIComponent(q)}`);
    if (!res.ok) return null;
    const data = await res.json();
    return data.task || null;
  } catch {
    return null;
  }
}

async function submit() {
  const q = question.value.trim();
  if (!q || running.value) return;
  resumeTargetId.value = null;
  const dup = await findDuplicate(q);
  if (dup) {
    const reuse = window.confirm(
      '该问题已有研究结果，是否直接查看？\n「确定」= 复用旧结果\n「取消」= 重新研究一遍'
    );
    if (reuse) {
      openTask(dup.id);
      return;
    }
  }
  reset();
  running.value = true;
  startTimer();
  try {
    const { task_id } = await startResearchAsync(q);
    currentTaskId = task_id;
    startSSE(task_id);
  } catch (e) {
    running.value = false;
    stopTimer();
    error.value = e.message || '提交失败';
    loadTasks();
  }
}

async function resume(id) {
  if (running.value) return;
  const t = tasks.value.find((x) => x.id === id);
  resumeTargetId.value = null;
  reset();
  if (t) question.value = t.question;
  view.value = 'new';
  running.value = true;
  startTimer();
  try {
    await resumeTaskAsync(id);
    currentTaskId = id;
    startSSE(id);
  } catch (e) {
    running.value = false;
    stopTimer();
    error.value = e.message || '续跑失败';
  }
}

function primaryAction() {
  if (resumeTargetId.value) resume(resumeTargetId.value);
  else submit();
}

function cancel() {
  cancelled = true;
  stopPolling();
  stopSSE();
  if (currentTaskId != null) {
    fetch(`/api/tasks/${currentTaskId}/cancel`, { method: 'POST' }).catch(() => {});
    resumeTargetId.value = currentTaskId;
  }
  running.value = false;
  stopTimer();
  error.value = '';
  notice.value = '已取消';
  // 等后端把状态标成 cancelled 后再刷新历史列表
  setTimeout(loadTasks, 500);
}

function retry() {
  error.value = '';
  notice.value = '';
  submit();
}

onMounted(loadTasks);
</script>

<template>
  <div class="app">
    <aside class="sidebar">
      <div class="sidebar-logo">Deep<span class="grad">Research</span></div>
      <button class="btn-new" @click="newResearch">＋ 新研究</button>
      <div class="sidebar-title">历史记录</div>
      <div class="history-list">
        <div v-if="!tasks.length" class="history-empty">暂无历史记录</div>
        <div
          v-for="t in tasks"
          :key="t.id"
          class="history-item"
          :class="{ active: view === 'history' && currentTask?.id === t.id }"
          @click="openTask(t.id)"
        >
          <div class="history-q">{{ t.question }}</div>
          <div class="history-meta">
            <span class="dot" :class="t.status"></span>
            {{ fmtDate(t.created_at) }} · {{ taskStatusText(t.status) }}
          </div>
        </div>
      </div>
    </aside>

    <main class="main">
      <div class="page">
        <p v-if="notice" class="notice">⏸ {{ notice }}</p>
        <div v-if="error" class="error">
          <span>❌ {{ error }}</span>
          <button v-if="view === 'new'" class="retry-btn" @click="retry">重试</button>
        </div>

        <template v-if="view === 'new'">
          <header class="hero">
            <div class="hero-badge">Multi-Agent · Deep Research</div>
            <h1 class="logo">Deep<span class="grad">Research</span></h1>
            <p class="tagline">Planner → Researcher → Writer ⇄ Critic</p>
          </header>

          <div class="input-card">
            <textarea
              v-model="question"
              placeholder="输入你的研究问题…（Ctrl+Enter 提交）"
              @keydown.ctrl.enter="primaryAction"
              @input="resumeTargetId = null"
            ></textarea>
            <div class="input-actions">
              <button class="btn-primary" @click="primaryAction" :disabled="running">
                {{ running ? '研究中…' : (resumeTargetId ? '继续研究' : '开始研究') }}
              </button>
              <button v-if="running" class="btn-ghost" @click="cancel">取消</button>
            </div>
          </div>

          <section v-if="plan && !simpleMode" class="card">
            <div class="stepper-row">
              <div class="stepper-track">
                <template v-for="(s, i) in stages" :key="s.key">
                  <div class="step" :class="s.status">
                    <div class="step-dot">
                      <span v-if="s.status === 'done'">✓</span>
                      <span v-else>{{ i + 1 }}</span>
                    </div>
                    <div class="step-label">{{ s.label }}</div>
                  </div>
                  <div
                    v-if="i < stages.length - 1"
                    class="step-line"
                    :class="{ done: s.status === 'done' }"
                  ></div>
                </template>
              </div>
              <div class="elapsed">⏱ {{ fmtElapsed(elapsed) }}</div>
            </div>
          </section>

          <section v-if="plan" class="card">
            <h2 class="card-title"><span class="icon">📋</span> 研究计划</h2>
            <p v-if="simpleMode" class="muted">简单问题，直接回答</p>
            <ul v-else class="plan-list">
              <li v-for="(sq, i) in plan.sub_questions" :key="i" class="plan-item">
                <span class="idx">{{ String(i + 1).padStart(2, '0') }}</span>
                <span>{{ sq }}</span>
              </li>
            </ul>
          </section>

          <section v-if="researchers.length" class="card">
            <h2 class="card-title"><span class="icon">🔍</span> 研究员</h2>
            <div v-for="r in researchers" :key="r.index" class="agent-row">
              <span class="dot" :class="r.status"></span>
              <span class="agent-q">{{ r.sub_question }}</span>
              <span class="agent-state">{{ statusLabel(r.status) }}</span>
            </div>
          </section>

          <section v-if="criticRounds.length" class="card">
            <h2 class="card-title"><span class="icon">✅</span> 审核</h2>
            <div v-for="c in criticRounds" :key="c.round" class="critic-row">
              <span class="round">第 {{ c.round }} 轮</span>
              <span class="badge" :class="c.approved ? 'ok' : 'fail'">
                {{ c.approved ? '通过' : '打回' }}
              </span>
              <span v-if="!c.approved && c.feedback" class="feedback">{{ c.feedback }}</span>
            </div>
          </section>

          <section v-if="toolLogs.length" class="card">
            <button class="log-toggle" @click="showTools = !showTools">
              {{ showTools ? '▾' : '▸' }} 工具日志 · {{ toolLogs.length }}
            </button>
            <pre v-if="showTools" class="log-panel">{{ toolLogs.join('\n') }}</pre>
          </section>
        </template>

        <template v-else>
          <div class="history-head">
            <button class="back-link" @click="newResearch">← 返回新研究</button>
            <h1>{{ currentTask?.question }}</h1>
            <p class="muted">
              {{ currentTask ? fmtDate(currentTask.created_at) : '' }}
              · {{ currentTask ? taskStatusText(currentTask.status) : '' }}
            </p>
            <button
              v-if="currentTask && (currentTask.status === 'failed' || currentTask.status === 'cancelled')"
              class="btn-primary"
              @click="resume(currentTask.id)"
            >
              ↻ 继续研究
            </button>
          </div>

          <section v-if="historyPlan && !historySimpleMode" class="card">
            <div class="stepper-row">
              <div class="stepper-track">
                <template v-for="(s, i) in stages" :key="s.key">
                  <div class="step" :class="historyStages[s.key]">
                    <div class="step-dot">
                      <span v-if="historyStages[s.key] === 'done'">✓</span>
                      <span v-else>{{ i + 1 }}</span>
                    </div>
                    <div class="step-label">{{ s.label }}</div>
                  </div>
                  <div
                    v-if="i < stages.length - 1"
                    class="step-line"
                    :class="{ done: historyStages[s.key] === 'done' }"
                  ></div>
                </template>
              </div>
            </div>
          </section>

          <section v-if="historyPlan" class="card">
            <h2 class="card-title"><span class="icon">📋</span> 研究计划</h2>
            <p v-if="historySimpleMode" class="muted">简单问题，直接回答</p>
            <ul v-else class="plan-list">
              <li v-for="(sq, i) in historySubQuestions" :key="i" class="plan-item">
                <span class="idx">{{ String(i + 1).padStart(2, '0') }}</span>
                <span>{{ sq }}</span>
              </li>
            </ul>
          </section>

          <section v-if="historyResearchers.length" class="card">
            <h2 class="card-title"><span class="icon">🔍</span> 研究员</h2>
            <div v-for="r in historyResearchers" :key="r.index" class="agent-row">
              <span class="dot done"></span>
              <span class="agent-q">{{ r.sub_question }}</span>
              <span class="agent-state">完成</span>
            </div>
          </section>

          <section v-if="historyCriticRounds.length" class="card">
            <h2 class="card-title"><span class="icon">✅</span> 审核</h2>
            <div v-for="c in historyCriticRounds" :key="c.round" class="critic-row">
              <span class="round">第 {{ c.round }} 轮</span>
              <span class="badge" :class="c.approved ? 'ok' : 'fail'">
                {{ c.approved ? '通过' : '打回' }}
              </span>
              <span v-if="!c.approved && c.feedback" class="feedback">{{ c.feedback }}</span>
            </div>
          </section>

          <section v-if="historyToolLogs.length" class="card">
            <button class="log-toggle" @click="showTools = !showTools">
              {{ showTools ? '▾' : '▸' }} 工具日志 · {{ historyToolLogs.length }}
            </button>
            <pre v-if="showTools" class="log-panel">{{ historyToolLogs.join('\n') }}</pre>
          </section>
        </template>

        <section v-if="activeStats && activeStats.calls" class="card stats-card">
          <button class="stats-toggle-btn" @click="showStats = !showStats">
            <span class="card-title"><span class="icon">📊</span> 成本 / 耗时统计</span>
            <span v-if="!showStats" class="stats-inline">
              总 Token {{ fmtTokens(activeStats.total_tokens) }} · ¥{{ fmtCost(activeStats.total_cost) }} · {{ fmtMs(activeStats.total_duration_ms) }}
            </span>
            <span class="stats-arrow">{{ showStats ? '▾' : '▸' }}</span>
          </button>
          <template v-if="showStats">
            <div class="stats-summary">
              <div class="stat">
                <span class="stat-num">{{ fmtTokens(activeStats.total_tokens) }}</span>
                <span class="stat-label">总 Token</span>
              </div>
              <div class="stat">
                <span class="stat-num">¥{{ fmtCost(activeStats.total_cost) }}</span>
                <span class="stat-label">总成本</span>
              </div>
              <div class="stat">
                <span class="stat-num">{{ fmtMs(activeStats.total_duration_ms) }}</span>
                <span class="stat-label">总耗时</span>
              </div>
              <div class="stat">
                <span class="stat-num">{{ activeStats.calls }}</span>
                <span class="stat-label">调用次数</span>
              </div>
            </div>
            <table v-if="byAgentRows.length" class="stats-table">
              <thead>
                <tr><th>Agent</th><th>Token</th><th>成本</th><th>耗时</th><th>调用</th></tr>
              </thead>
              <tbody>
                <tr v-for="r in byAgentRows" :key="r.agent">
                  <td>{{ r.agent }}</td>
                  <td>{{ fmtTokens(r.tokens) }}</td>
                  <td>¥{{ fmtCost(r.cost) }}</td>
                  <td>{{ fmtMs(r.duration_ms) }}</td>
                  <td>{{ r.calls }}</td>
                </tr>
              </tbody>
            </table>
          </template>
        </section>

        <template v-if="activeReport">
          <section class="card report-card">
            <div class="report-header">
              <h2 class="card-title">
                <span class="icon">📄</span> 最终报告
                <span v-if="view === 'new'" class="elapsed-final">· ⏱ {{ fmtElapsed(elapsed) }}</span>
              </h2>
              <div class="export-actions">
                <button class="btn-export" @click="exportWord(activeReport, 'deep-research-report.docx')">
                  导出 Word
                </button>
                <button class="btn-export" @click="exportPdf(reportEl, 'deep-research-report.pdf')">
                  导出 PDF
                </button>
              </div>
            </div>
            <div ref="reportEl">
              <MarkdownView :source="activeReport" />
            </div>
          </section>

          <section v-if="view === 'history' && currentTask?.sources?.length" class="card">
            <h2 class="card-title"><span class="icon">🔗</span> 引用来源</h2>
            <ol class="sources-list">
              <li v-for="s in currentTask.sources" :key="s.citation_id">
                <a :href="s.url" target="_blank" rel="noopener">{{ s.title || s.url }}</a>
              </li>
            </ol>
          </section>
        </template>
        <p v-else-if="view === 'history' && currentTask" class="muted">
          {{ currentTask.status === 'cancelled' ? '该任务已取消。' : '该任务没有生成报告（可能生成失败）。' }}
        </p>
      </div>
    </main>
  </div>
</template>
