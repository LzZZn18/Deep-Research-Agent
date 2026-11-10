import { fetchEventSource } from '@microsoft/fetch-event-source';

function buildHandlers(handlers) {
  return {
    openWhenHidden: true,
    onopen(response) {
      if (response.ok) return;
      throw new Error(`HTTP ${response.status}`);
    },
    onmessage(ev) {
      let data = {};
      try {
        data = JSON.parse(ev.data || '{}');
      } catch {
        data = {};
      }
      handlers[ev.event]?.(data);
    },
    onerror(err) {
      handlers.error?.({ message: err?.message || String(err) });
      throw err; // 抛错以停止自动重连
    },
    onclose() {
      handlers.close?.();
    },
  };
}

/**
 * 发起一次深度研究（POST + SSE 流式）。
 * @param {string} question
 * @param {Record<string, (data: any) => void>} handlers  按事件名分发
 * @returns {AbortController} 用于取消
 */
export function startResearch(question, handlers) {
  const controller = new AbortController();
  fetchEventSource('/api/research', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
    signal: controller.signal,
    ...buildHandlers(handlers),
  });
  return controller;
}

/**
 * 从断点续跑一个任务（POST + SSE 流式）。
 * @param {number|string} taskId
 * @param {Record<string, (data: any) => void>} handlers
 * @returns {AbortController}
 */
export function resumeTask(taskId, handlers) {
  const controller = new AbortController();
  fetchEventSource(`/api/tasks/${taskId}/resume`, {
    method: 'POST',
    signal: controller.signal,
    ...buildHandlers(handlers),
  });
  return controller;
}

/**
 * 提交研究（非流式）：建任务并入队，立即返回 { task_id, job_id }。
 */
export async function startResearchAsync(question) {
  const res = await fetch('/api/research', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

/**
 * 续跑任务（非流式）：重新入队，立即返回 { task_id, job_id }。
 */
export async function resumeTaskAsync(taskId) {
  const res = await fetch(`/api/tasks/${taskId}/resume`, { method: 'POST' });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `HTTP ${res.status}`);
  }
  return res.json();
}
