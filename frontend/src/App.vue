<script setup>
import { ref } from 'vue';
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
  toolLogs.value = [];
  resetStages();
  cancelled = false;
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
    answer: (d) => {
      report.value = d.content;
      setStage('writing', 'done');
      setStage('critiquing', 'done');
      setStage('done', 'done');
    },
    task_end: () => {
      setStage('done', 'done');
      running.value = false;
      stopTimer();
    },
    error: (d) => {
      if (!cancelled) error.value = d.message || '出错了';
      running.value = false;
      stopTimer();
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
</script>

<template>
  <div class="page">
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

    <p v-if="notice" class="notice">⏸ {{ notice }}</p>
    <div v-if="error" class="error">
      <span>❌ {{ error }}</span>
      <button class="retry-btn" @click="retry">重试</button>
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

    <section v-if="report" class="card report-card">
      <div class="report-header">
        <h2 class="card-title">
          <span class="icon">📄</span> 最终报告
          <span class="elapsed-final">· ⏱ {{ fmtElapsed(elapsed) }}</span>
        </h2>
        <div class="export-actions">
          <button class="btn-export" @click="exportWord(report, 'deep-research-report.docx')">
            导出 Word
          </button>
          <button class="btn-export" @click="exportPdf(reportEl, 'deep-research-report.pdf')">
            导出 PDF
          </button>
        </div>
      </div>
      <div ref="reportEl">
        <MarkdownView :source="report" />
      </div>
    </section>

    <section v-if="toolLogs.length" class="card">
      <button class="log-toggle" @click="showTools = !showTools">
        {{ showTools ? '▾' : '▸' }} 工具日志 · {{ toolLogs.length }}
      </button>
      <pre v-if="showTools" class="log-panel">{{ toolLogs.join('\n') }}</pre>
    </section>
  </div>
</template>
