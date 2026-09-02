/* OpenManus Web - pure UI helpers.
 * Framework-free, DOM-free where possible so node:test can cover the
 * decision logic that guards data loss and bounded rendering.
 * Loaded before app.js; app.js consumes these via window.UIUtils.
 */
(function (global) {
  "use strict";

  /**
   * Decide what the composer should do with a submit attempt.
   *
   * @param {string} text - raw input value
   * @param {boolean} wsOpen - WebSocket readyState === OPEN
   * @param {boolean} running - a run is in flight (per server events)
   * @param {boolean} pending - a run request was sent, run_start not yet echoed
   * @returns {{action:"send"|"block-empty"|"block-running"|"block-pending"|"block-offline",
   *            clearInput:boolean, reason:string}}
   */
  function composerDecision(text, wsOpen, running, pending) {
    const trimmed = String(text || "").trim();
    if (!trimmed) return { action: "block-empty", clearInput: false, reason: "empty" };
    if (!wsOpen) return { action: "block-offline", clearInput: false, reason: "not-connected" };
    if (running) return { action: "block-running", clearInput: false, reason: "agent-busy" };
    if (pending) return { action: "block-pending", clearInput: false, reason: "request-in-flight" };
    return { action: "send", clearInput: true, reason: "" };
  }

  /**
   * Capped exponential backoff for WS reconnect attempts (1s base, 15s cap).
   *
   * @param {number} attempt - 1-based attempt number
   * @returns {number} delay in milliseconds
   */
  function backoffDelay(attempt) {
    const base = 1000;
    const cap = 15000;
    const a = Math.max(1, Number(attempt) || 1);
    return Math.min(cap, base * Math.pow(2, a - 1));
  }

  /**
   * How many leading children to remove so a container keeps at most `max`.
   * Skips a leading notice element (data-prune-notice) so it survives.
   *
   * @param {Element} el - container (timeline, terminal, messages)
   * @param {number} max - maximum children to keep
   * @returns {number} count to remove (0 if within bound)
   */
  function pruneCount(el, max) {
    if (!el || typeof el.children !== "object") return 0;
    const total = el.children.length;
    return total > max ? total - max : 0;
  }

  /**
   * Local, human-readable absolute timestamp for tooltips (UTC value shown too).
   *
   * @param {number} epochMs - epoch milliseconds
   * @returns {string} e.g. "Sep 1, 2026, 9:41:07 PM (UTC: 2026-09-01T19:41:07Z)"
   */
  function absoluteTime(epochMs) {
    const d = new Date(epochMs);
    if (isNaN(d.getTime())) return "";
    return `${d.toLocaleString()} (UTC: ${d.toISOString()})`;
  }

  const api = { composerDecision, backoffDelay, pruneCount, absoluteTime };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api; // node:test
  }
  global.UIUtils = api; // browser
})(typeof window !== "undefined" ? window : globalThis);
