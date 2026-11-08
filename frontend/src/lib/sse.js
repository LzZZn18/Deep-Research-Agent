import { fetchEventSource } from '@microsoft/fetch-event-source';

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
  });
  return controller;
}
