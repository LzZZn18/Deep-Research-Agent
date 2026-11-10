<script setup>
import { ref, computed, onMounted } from 'vue';
import { startResearch } from './lib/sse.js';
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
let controller = null;
let cancelled = false;
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
  return s === 'done' ? '完成' : s === 'failed' ? '失败' : s === 'cancelled' ? '已取消' : '进行中';
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
}

async function submit() {
  const q = question.value.trim();
  if (!q || running.value) return;
  reset();
  running.value = true;
  startTimer();

  controller = startResearch(q, {
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
      loadTasks();
    },
    error: (d) => {
      if (!cancelled) error.value = d.message || '出错了';
      running.value = false;
      stopTimer();
      loadTasks();
    },
    close: () => {
      running.value = false;
      stopTimer();
    },
  });
}

function cancel() {
  cancelled = true;
  controller?.abort();
  running.value = false;
  stopTimer();
  error.value = '';
  notice.value = '已取消';
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
              @keydown.ctrl.enter="submit"
            ></textarea>
            <div class="input-actions">
              <button class="btn-primary" @click="submit" :disabled="running">
                {{ running ? '研究中…' : '开始研究' }}
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
          </div>
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
