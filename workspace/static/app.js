/* OpenManus Web - Manus-style frontend client.
 * Handles the WebSocket event stream, chat rendering, and the right-hand
 * "computer" panel tabs (Live timeline, Browser, Terminal, Editor).
 */
(() => {
  "use strict";

  // ------------------------------------------------------------- state
  const state = {
    ws: null,
    sessionId: sessionStorage.getItem("openmanus_sid") || crypto.randomUUID(),
    running: false,
    replaying: false,
    pinnedTab: false,
    activeTab: "live",
    step: { current: 0, max: 0 },
    startedAt: null,
    timer: null,
    toolCards: new Map(), // event id -> timeline element
    activityPill: null,   // single chat activity pill (replaces per-tool cards)
    actionCount: 0,
    runOutcome: "",       // completed | stopped | failed | ended (for Live report)
    submitPending: false, // run request sent, run_start echo not yet received
    lastPrompt: "",       // most recent submitted prompt, for the Retry action
    tokens: { in: 0, out: 0 },        // cumulative per session (server truth)
    runTokens: { in: 0, out: 0 },     // deltas for the current run
    files: new Map(),     // path -> content cache
    activeFile: null,
    filesDirty: true,
    outputsDirty: true,
    intentionalClose: false,
    zoom: 1,
  };
  // Deep-link support: #s=<sessionId> wins over stored session (shareable URL).
  const hashSession = (location.hash.match(/^#s=([\w-]+)$/) || [])[1];
  if (hashSession) state.sessionId = hashSession;
  sessionStorage.setItem("openmanus_sid", state.sessionId);

  const $ = (id) => document.getElementById(id);
  const messages = $("messages");
  const timeline = $("timeline");
  const terminal = $("terminal");

  // Bounded rendering: nothing in the DOM may grow without limit (P1-2).
  const TIMELINE_MAX = 400;
  const TERMINAL_MAX = 150;
  const prunedCounts = new Map();

  // Tool-call inspector data, keyed by call id (P1-8). Bounded by pruning below —
  // entries are dropped the moment their timeline row is pruned, so this never
  // outlives what's actually rendered.
  const toolCallData = new Map();

  function pruneContainer(el, max, onRemove) {
    const n = pruneCount(el, max);
    if (!n) return;
    for (let i = 0; i < n; i++) {
      const removed = el.firstElementChild;
      if (!removed) break;
      removed.remove();
      onRemove?.(removed);
      // an expanded inspector panel belonging to the row just removed would
      // otherwise become an orphaned first child — drop it too.
      if (el.firstElementChild?.classList.contains("tl-inspector")) {
        el.firstElementChild.remove();
      }
    }
    prunedCounts.set(el, (prunedCounts.get(el) || 0) + n);
    let notice = el.querySelector("[data-prune-notice]");
    if (!notice) {
      notice = document.createElement("div");
      notice.dataset.pruneNotice = "1";
      notice.className = "prune-notice";
      el.prepend(notice);
    }
    notice.textContent = `… ${prunedCounts.get(el).toLocaleString()} earlier entries pruned to keep the tab responsive`;
  }

  // ---------------------------------------------------- toast / clipboard
  // Non-blocking status/error surface (P0-3, P1-9). Doubles as the local
  // stand-in for a monitoring sink: no Sentry is configured in this
  // deployment, so client errors are tagged + logged to console and
  // surfaced here instead of failing silently.
  function showToast(msg, kind = "info", ms = 4000) {
    const root = $("toastRoot");
    if (!root) return;
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.setAttribute("role", kind === "error" ? "alert" : "status");
    el.textContent = msg;
    root.appendChild(el);
    requestAnimationFrame(() => el.classList.add("show"));
    setTimeout(() => {
      el.classList.remove("show");
      setTimeout(() => el.remove(), 200);
    }, ms);
  }

  async function copyToClipboard(text, btnEl) {
    try {
      await navigator.clipboard.writeText(text);
      showToast("Copied to clipboard", "info", 1500);
      if (btnEl) {
        btnEl.classList.add("copied");
        setTimeout(() => btnEl.classList.remove("copied"), 1000);
      }
    } catch {
      showToast("Copy failed — clipboard permission blocked", "error");
    }
  }

  function copyBtnHtml(idx, label) {
    return `<button class="copy-btn" data-copy-idx="${idx}" aria-label="Copy ${esc(label)}" title="Copy">${icon("i-copy")}</button>`;
  }

  function wireCopyButtons(root, texts) {
    root.querySelectorAll(".copy-btn[data-copy-idx]").forEach((btn) => {
      const idx = Number(btn.dataset.copyIdx);
      btn.onclick = () => copyToClipboard(texts[idx], btn);
    });
  }

  // Global safety net (P0-3): a broken click handler or rejected promise
  // anywhere must never fail silently. No external monitoring sink is wired
  // up in this local deployment — this is the honest equivalent (console tag
  // + on-screen toast) rather than a real ingestion pipeline.
  window.addEventListener("error", (e) => {
    console.error("[client-error]", {
      session: state.sessionId,
      route: state.activeTab,
      message: e.message,
      source: e.filename,
      line: e.lineno,
    });
    showToast("Something went wrong in the UI — see console for details.", "error");
  });
  window.addEventListener("unhandledrejection", (e) => {
    console.error("[client-error:unhandled-rejection]", {
      session: state.sessionId,
      route: state.activeTab,
      reason: String(e.reason),
    });
    showToast("A background action failed unexpectedly — see console for details.", "error");
  });

  // ------------------------------------------------------------- utils
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));

  const fmtDur = (ms) => (ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`);

  const fmtTime = () => {
    const s = state.startedAt ? Math.floor((Date.now() - state.startedAt) / 1000) : 0;
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  };

  const icon = (name) => `<svg class="ic"><use href="#${name}"/></svg>`;

  const scrollDown = (el) => { el.scrollTop = el.scrollHeight; };

  // Minimal safe markdown renderer (escapes first, then applies patterns).
  const markdown = (src) => {
    let text = esc(src);
    const blocks = [];
    text = text.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
      blocks.push(`<pre><code>${code.replace(/\n$/, "")}</code></pre>`);
      return `\u0000B${blocks.length - 1}\u0000`;
    });
    text = text
      .replace(/^###\s+(.+)$/gm, "<h3>$1</h3>")
      .replace(/^##\s+(.+)$/gm, "<h2>$1</h2>")
      .replace(/^#\s+(.+)$/gm, "<h1>$1</h1>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(/^\s*[-*]\s+(.+)$/gm, "<li>$1</li>")
      .replace(/^\s*\d+\.\s+(.+)$/gm, "<li>$1</li>");
    text = text
      .replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g, "<ul>$1</ul>")
      .split(/\n{2,}/)
      .map((p) => (/^\s*<(h\d|ul|ol|pre)/.test(p.trim()) ? p : `<p>${p.replace(/\n/g, "<br>")}</p>`))
      .join("");
    return text.replace(/\u0000B(\d+)\u0000/g, (_, i) => blocks[+i]);
  };

  // ----------------------------------------------------- tool metadata
  // Exact names first; otherwise fall back to the server-provided category
  // (Browser Use CLI 3.0 exposes browser_exec/browser_screenshot/... via MCP).
  const TOOL_UI = {
    browser_use: { cat: "browser", label: "Browsing", ic: "i-globe" },
    browser_exec: { cat: "browser", label: "Browsing", ic: "i-globe" },
    browser_screenshot: { cat: "browser", label: "Capturing screenshot", ic: "i-globe" },
    python_execute: { cat: "terminal", label: "Running code", ic: "i-terminal" },
    str_replace_editor: { cat: "editor", label: "Editing file", ic: "i-file" },
    ask_human: { cat: "chat", label: "Asking you", ic: "i-chat" },
    terminate: { cat: "chat", label: "Wrapping up", ic: "i-check" },
    web_search: { cat: "browser", label: "Searching the web", ic: "i-globe" },
  };

  const CAT_ICON = {
    browser: "i-globe", terminal: "i-terminal", editor: "i-file", chat: "i-chat",
  };

  function toolUI(name, category) {
    if (TOOL_UI[name]) return TOOL_UI[name];
    const cat = category || "tool";
    return {
      cat,
      label: name.startsWith("browser")
        ? "Browsing"
        : name.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()),
      ic: CAT_ICON[cat] || "i-bolt",
    };
  }

  function toolDetail(name, args) {
    const a = args || {};
    switch (name) {
      case "browser_use": {
        const bits = [a.action];
        if (a.url) bits.push(a.url);
        if (a.query) bits.push(`"${a.query}"`);
        if (a.text) bits.push(`"${a.text}"`);
        if (a.index != null) bits.push(`#${a.index}`);
        return bits.filter(Boolean).join(" ");
      }
      case "python_execute": {
        const code = String(a.code || "").split("\n")[0] || "python";
        return code.slice(0, 80);
      }
      case "str_replace_editor":
        return `${a.command || "edit"} ${a.path || a.file_path || ""}`;
      case "web_search": return a.query || "";
      case "terminate": return "";
      default:
        try { return JSON.stringify(a).slice(0, 90); } catch { return ""; }
    }
  }

  // ---------------------------------------------------------- chat items
  function clearWelcome() { $("welcome")?.remove(); }

  // Long prompts render clamped with a toggle so they don't flood the chat.
  function addUserMessage(text) {
    clearWelcome();
    const el = document.createElement("div");
    el.className = "msg-user";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    if (text.length > 400) {
      bubble.classList.add("clamped");
      const toggle = document.createElement("button");
      toggle.className = "bubble-toggle";
      toggle.textContent = "Show more";
      toggle.onclick = () => {
        const clamped = bubble.classList.toggle("clamped");
        toggle.textContent = clamped ? "Show more" : "Show less";
      };
      el.appendChild(bubble);
      el.appendChild(toggle);
    } else {
      el.appendChild(bubble);
    }
    messages.appendChild(el);
    keepPillLast();
    scrollDown(messages);
  }

  function addThought(content) {
    clearWelcome();
    const el = document.createElement("details");
    el.className = "thought";
    el.innerHTML = `<summary>Thought</summary><div class="thought-body">${esc(content)}</div>`;
    messages.appendChild(el);
    keepPillLast();
    scrollDown(messages);
  }

  // Raw/rendered toggle + copy (P2-6: Output/artifact viewer rule).
  function addFinal(content) {
    clearWelcome();
    const el = document.createElement("div");
    el.className = "final";
    el.innerHTML = `
      <div class="final-toolbar">
        <button class="final-toggle" data-mode="rendered">View raw</button>
        ${copyBtnHtml(0, "final answer")}
      </div>
      <div class="final-rendered">${markdown(content)}</div>
      <pre class="final-raw hidden"></pre>`;
    el.querySelector(".final-raw").textContent = content; // textContent: no markdown/HTML re-parsing
    el.querySelector(".final-toggle").onclick = (e) => {
      const btn = e.currentTarget;
      const showingRendered = btn.dataset.mode === "rendered";
      el.querySelector(".final-rendered").classList.toggle("hidden", showingRendered);
      el.querySelector(".final-raw").classList.toggle("hidden", !showingRendered);
      btn.dataset.mode = showingRendered ? "raw" : "rendered";
      btn.textContent = showingRendered ? "View rendered" : "View raw";
    };
    wireCopyButtons(el, [content]);
    messages.appendChild(el);
    keepPillLast();
    scrollDown(messages);
  }

  // Copy-to-clipboard on every error message (P1-9 / §6 mandatory rule).
  function addError(msg) {
    clearWelcome();
    const el = document.createElement("div");
    el.className = "error-banner";
    el.setAttribute("role", "alert"); // assertive: critical failure (§7)
    el.innerHTML = `<span>${esc(msg)}</span>${copyBtnHtml(0, "error message")}`;
    wireCopyButtons(el, [msg]);
    messages.appendChild(el);
    keepPillLast();
    scrollDown(messages);
  }

  function addAskCard(question) {
    clearWelcome();
    const el = document.createElement("div");
    el.className = "ask-card";
    el.innerHTML = `
      <div class="ask-label">OpenManus needs your input</div>
      <div class="ask-question">${esc(question)}</div>
      <div class="ask-form">
        <input type="text" placeholder="Type your answer..." />
        <button>Reply</button>
      </div>`;
    const input = el.querySelector("input");
    const submit = () => {
      const val = input.value.trim();
      if (!val) return;
      send({ type: "human_reply", content: val });
      el.querySelector(".ask-form").outerHTML =
        `<div class="ask-answered">You replied: ${esc(val)}</div>`;
    };
    el.querySelector("button").onclick = submit;
    input.addEventListener("keydown", (e) => e.key === "Enter" && submit());
    messages.appendChild(el);
    keepPillLast();
    input.focus();
    scrollDown(messages);
  }

  // Single compact activity pill in chat (full detail lives in the Live
  // timeline — no per-tool cards in both panes).
  function keepPillLast() {
    pruneContainer(messages, TIMELINE_MAX); // chat stays bounded across many runs
    if (state.activityPill) messages.appendChild(state.activityPill);
  }

  function dropActivityPill() {
    state.activityPill?.remove();
    state.activityPill = null;
  }

  function updateActivityPill(ev) {
    clearWelcome();
    const ui = toolUI(ev.name, ev.category);
    let pill = state.activityPill;
    if (!pill) {
      pill = document.createElement("div");
      pill.className = "activity-pill";
      pill.onclick = () => switchTab("live", true);
      state.activityPill = pill;
    }
    pill.innerHTML = `
      <span class="ap-icon">${icon(ui.ic)}</span>
      <span class="ap-label">${esc(ui.label)}</span>
      <span class="ap-detail">${esc(toolDetail(ev.name, ev.arguments))}</span>
      <span class="ap-count">${state.actionCount || 1} actions</span>
      <span class="ap-status"><span class="spinner"></span></span>`;
    keepPillLast();
    scrollDown(messages);
  }

  function finalizeActivityPill(done) {
    const pill = state.activityPill;
    if (!pill) return;
    pill.classList.add("done");
    pill.querySelector(".ap-status").innerHTML = done ? icon("i-check") : icon("i-stop");
    pill.querySelector(".ap-count").textContent =
      `${state.actionCount} actions · ${fmtTime()} · view details`;
  }

  // -------------------------------------------------------- timeline
  function timelineAdd(ev) {
    const ui = toolUI(ev.name, ev.category);
    const empty = timeline.querySelector(".pane-empty");
    if (empty) empty.remove();
    toolCallData.set(ev.id, { name: ev.name, category: ev.category, arguments: ev.arguments });
    const el = document.createElement("div");
    el.className = "tl-item tl-tool";
    el.dataset.callId = ev.id;
    el.innerHTML = `
      <div class="tl-icon ${ui.cat}">${icon(ui.ic)}</div>
      <div class="tl-body">
        <div class="tl-title">${esc(ui.label)}</div>
        <div class="tl-detail">${esc(toolDetail(ev.name, ev.arguments))}</div>
      </div>
      <div class="tl-time" title="${esc(absoluteTime(Date.now()))}">${fmtTime()}</div>
      <div class="tl-status"><span class="spinner"></span></div>
      <button class="tl-expand" aria-expanded="false" aria-label="Show tool call details">${icon("i-chevron")}</button>`;
    el.querySelector(".tl-expand").onclick = () => toggleToolInspector(el);
    timeline.appendChild(el);
    pruneContainer(timeline, TIMELINE_MAX, (removedEl) => {
      if (removedEl.dataset?.callId) toolCallData.delete(removedEl.dataset.callId);
    });
    scrollDown($("pane-live"));
    return el;
  }

  // Tool-call inspector (P1-8: mandatory §2 requirement — structured fields
  // and a real JSON viewer, not a truncated one-line blob). Built lazily on
  // expand and refreshed in place when tool_end delivers the result.
  function toggleToolInspector(rowEl) {
    const btn = rowEl.querySelector(".tl-expand");
    const existing = rowEl.nextElementSibling;
    if (existing?.classList.contains("tl-inspector")) {
      existing.remove();
      btn.setAttribute("aria-expanded", "false");
      return;
    }
    const panel = buildToolInspector(rowEl.dataset.callId, toolCallData.get(rowEl.dataset.callId) || {});
    rowEl.after(panel);
    btn.setAttribute("aria-expanded", "true");
  }

  function refreshToolInspector(callId) {
    const rowEl = timeline.querySelector(`.tl-tool[data-call-id="${CSS.escape(callId)}"]`);
    const existing = rowEl?.nextElementSibling;
    if (!existing?.classList.contains("tl-inspector")) return; // not expanded — nothing to refresh
    existing.replaceWith(buildToolInspector(callId, toolCallData.get(callId) || {}));
  }

  function buildToolInspector(id, data) {
    const el = document.createElement("div");
    el.className = "tl-inspector";
    const argsText = JSON.stringify(data.arguments ?? {}, null, 2);
    const resultText = data.result != null ? data.result : "(pending — tool is still running)";
    const statusLine = data.ok === undefined
      ? "running"
      : data.ok ? `succeeded in ${fmtDur(data.duration_ms)}` : `failed after ${fmtDur(data.duration_ms)}`;
    el.innerHTML = `
      <div class="ti-row"><span class="ti-label">call id</span><code class="ti-mono">${esc(id)}</code>${copyBtnHtml(0, "call id")}</div>
      <div class="ti-row"><span class="ti-label">tool</span><code class="ti-mono">${esc(data.name || "")}</code></div>
      <div class="ti-row"><span class="ti-label">status</span><span class="ti-status-${data.ok === undefined ? "pending" : data.ok ? "ok" : "err"}">${esc(statusLine)}</span></div>
      <div class="ti-block">
        <div class="ti-block-head"><span>Arguments</span>${copyBtnHtml(1, "arguments JSON")}</div>
        <pre class="ti-json">${esc(argsText)}</pre>
      </div>
      <div class="ti-block">
        <div class="ti-block-head"><span>Result${data.truncated ? ' <span class="ti-note">(truncated to 2000 chars by the server)</span>' : ""}</span>${copyBtnHtml(2, "result")}</div>
        <pre class="ti-json">${esc(resultText)}</pre>
      </div>`;
    wireCopyButtons(el, [id, argsText, resultText]);
    return el;
  }

  // End-of-run report entry in the Live timeline (success / stopped / failed).
  function addRunReport() {
    if (timeline.querySelector(".pane-empty")) timeline.innerHTML = "";
    const outcome = state.runOutcome || "ended";
    const tok = state.runTokens.in + state.runTokens.out > 0
      ? ` · ${(state.runTokens.in + state.runTokens.out).toLocaleString()} tokens` : "";
    const report = {
      completed: { title: "Task completed", detail: `${state.actionCount} actions · ${state.step.current} steps · ${fmtTime()}${tok}`, cls: "ok", ic: "i-check" },
      stopped: { title: "Task stopped by user", detail: `${state.actionCount} actions before stopping · ${fmtTime()}${tok}`, cls: "stopped", ic: "i-stop" },
      failed: { title: "Task failed", detail: `Error during run · ${state.actionCount} actions · ${fmtTime()}`, cls: "err", ic: "i-x" },
      ended: { title: "Run ended", detail: `${state.actionCount} actions · ${fmtTime()}${tok}`, cls: "", ic: "i-bolt" },
    }[outcome];
    const canRetry = (outcome === "failed" || outcome === "stopped") && state.lastPrompt;
    const el = document.createElement("div");
    el.className = `tl-item tl-report ${report.cls}`;
    el.innerHTML = `
      <div class="tl-icon ${report.cls}">
        ${icon(report.ic)}
      </div>
      <div class="tl-body">
        <div class="tl-title">${report.title}</div>
        <div class="tl-detail">${esc(report.detail)}</div>
      </div>
      <div class="tl-time" title="${esc(absoluteTime(Date.now()))}">${fmtTime()}</div>
      ${canRetry ? `<button class="tl-retry">${icon("i-refresh")} Retry</button>` : ""}`;
    if (canRetry) el.querySelector(".tl-retry").onclick = retryLastRun;
    timeline.appendChild(el);
    pruneContainer(timeline, TIMELINE_MAX, (removedEl) => {
      if (removedEl.dataset?.callId) toolCallData.delete(removedEl.dataset.callId);
    });
    scrollDown($("pane-live"));
  }

  // Retry action for the Error-surfacing rule (§2: "retry/resume action if
  // applicable"). Re-submits the exact prompt that failed/was stopped.
  function retryLastRun() {
    const decision = composerDecision(
      state.lastPrompt,
      state.ws?.readyState === WebSocket.OPEN,
      state.running,
      state.submitPending
    );
    if (decision.action !== "send") {
      showToast(`Can't retry right now (${decision.reason.replace(/-/g, " ")}).`, "error");
      return;
    }
    if (!send({ type: "run", prompt: state.lastPrompt })) {
      showToast("Retry failed to send — check the connection.", "error");
      return;
    }
    state.submitPending = true;
    syncSend();
  }

  // -------------------------------------------------------- tabs
  function switchTab(tab, userAction) {
    if (userAction) state.pinnedTab = true;
    state.activeTab = tab;
    document.querySelectorAll(".tab").forEach((t) => {
      const active = t.dataset.tab === tab;
      t.classList.toggle("active", active);
      t.setAttribute("aria-selected", String(active));
      t.tabIndex = active ? 0 : -1;
    });
    document.querySelectorAll(".pane").forEach((p) => p.classList.toggle("active", p.id === `pane-${tab}`));
    if (tab === "live") scrollDown($("pane-live"));
    if (tab === "editor") refreshFiles();   // always reload on switch
    if (tab === "files") loadOutputs();     // always reload on switch
  }

  function autoSwitch(cat) {
    if (state.pinnedTab || state.replaying) return;
    const map = { browser: "browser", terminal: "terminal", editor: "editor" };
    if (map[cat]) switchTab(map[cat], false);
  }

  // -------------------------------------------------------- browser pane
  function setScreenshot(b64) {
    const img = $("browserShot");
    const empty = $("browserEmpty");
    empty?.classList.add("hidden");
    img.classList.remove("hidden");
    img.classList.add("flash");
    // PNG payloads start with iVBOR..., JPEG with /9j/ — match the mime.
    const mime = b64.startsWith("iVBOR") ? "png" : "jpeg";
    img.src = `data:image/${mime};base64,${b64}`;
    setTimeout(() => img.classList.remove("flash"), 250);
    autoSwitch("browser");
  }

  // -------------------------------------------------------- terminal pane
  function addTerminalBlock(code, output) {
    if (terminal.querySelector(".pane-empty")) terminal.innerHTML = "";
    const ok = !/\berror\b|Traceback|Exception/i.test(output || "");
    const el = document.createElement("div");
    el.className = "term-block";
    el.innerHTML = `
      <div class="term-prompt"><span><span class="term-user">agent</span>@openmanus:~$</span><span>${fmtTime()}</span></div>
      <div class="term-code-row"><div class="term-code">${esc(code)}</div>${copyBtnHtml(0, "code")}</div>
      ${output ? `<div class="term-out ${ok ? "" : "err"}">${esc(output)}</div>` : ""}`;
    wireCopyButtons(el, [code]);
    terminal.appendChild(el);
    pruneContainer(terminal, TERMINAL_MAX);
    scrollDown(terminal);
    autoSwitch("terminal");
  }

  // -------------------------------------------------------- editor pane
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const MAX_FETCH_RETRIES = 2; // + the initial attempt = 3 tries total

  // All fetches: 8s timeout, capped exponential-backoff retry on transient
  // failures (network error / timeout / 5xx), explicit error state on
  // exhaustion (no silent staleness — P1-5/P1-7). 4xx is not retried: retrying
  // a client error can't succeed and would just duplicate the request.
  async function fetchJson(url, opts = {}, attempt = 1) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 8000);
    let res;
    try {
      res = await fetch(url, { ...opts, signal: ctrl.signal });
    } catch (err) {
      clearTimeout(t);
      if (attempt <= MAX_FETCH_RETRIES) {
        await sleep(backoffDelay(attempt));
        return fetchJson(url, opts, attempt + 1);
      }
      throw err;
    }
    clearTimeout(t);
    if (res.status === 401) {
      // Session expired/invalidated server-side — reload so "/" renders the
      // sign-in page instead of leaving the UI stuck retrying forever.
      location.reload();
      return new Promise(() => {}); // navigation is imminent; never resolve
    }
    if (res.status >= 500 && attempt <= MAX_FETCH_RETRIES) {
      await sleep(backoffDelay(attempt));
      return fetchJson(url, opts, attempt + 1);
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  }

  const errorRow = (msg, retry) => `
    <div class="fetch-error" role="alert">
      <span>${esc(msg)}</span>
      <button class="btn-ghost sm" data-retry="${esc(String(retry))}">Retry</button>
    </div>`;

  function bindRetry(container, retryFn) {
    container.querySelectorAll("[data-retry]")?.forEach((btn) => {
      btn.onclick = () => retryFn();
    });
  }

  async function refreshFiles() {
    state.filesDirty = false;
    const sidebar = $("editorSidebar");
    try {
      const data = await fetchJson("/api/files");
      const rows = [];
      const walk = (nodes, depth) => {
        for (const n of nodes) {
          const pad = `<span class="depth-pad" style="width:${depth * 12}px"></span>`;
          if (n.type === "dir") {
            rows.push(`<div class="file-row dir-row" data-kind="dir">${pad}${icon("i-folder")}<span>${esc(n.name)}/</span></div>`);
            walk(n.children || [], depth + 1);
          } else {
            const active = n.path === state.activeFile ? " active" : "";
            rows.push(`<div class="file-row${active}" data-kind="file" data-path="${esc(n.path)}">${pad}${icon("i-file")}<span>${esc(n.name)}</span></div>`);
          }
        }
      };
      walk(data.tree || [], 0);
      sidebar.innerHTML = `<div class="file-group-label">${esc(data.root || "workspace")}</div>` + rows.join("");
      sidebar.querySelectorAll('.file-row[data-kind="file"]').forEach((row) => {
        row.onclick = () => openFile(row.dataset.path, false);
      });
    } catch {
      sidebar.innerHTML = errorRow("Could not load the workspace file tree.", "files");
      bindRetry(sidebar, refreshFiles);
    }
  }

  async function openFile(path, follow) {
    state.activeFile = path;
    document.querySelectorAll(".file-row[data-kind=file]").forEach((r) =>
      r.classList.toggle("active", r.dataset.path === path));
    let content = state.files.get(path);
    if (content === undefined) {
      try {
        const res = await fetch(`/api/file?path=${encodeURIComponent(path)}`);
        if (!res.ok) return;
        const data = await res.json();
        content = data.content;
      } catch { return; }
    }
    renderCode(path, content);
    if (follow) autoSwitch("editor");
  }

  function renderCode(path, content) {
    const main = $("editorMain");
    const lines = String(content ?? "").split("\n");
    main.innerHTML = `
      <div class="editor-filebar"><span>${esc(path)}</span><span>${lines.length} lines</span></div>
      <div class="editor-code">${lines
        .map((l, i) => `<div class="code-line"><span class="ln">${i + 1}</span><span class="code">${esc(l) || " "}</span></div>`)
        .join("")}</div>`;
    state.files.set(path, content);
    const codeEl = main.querySelector(".editor-code");
    codeEl.scrollTop = codeEl.scrollHeight; // follow edits at end of file
  }

  // -------------------------------------------------------- output files
  const EXT_CAT = {
    code: new Set(["py", "js", "ts", "html", "css", "sh", "json", "ipynb", "xml", "yml", "yaml", "toml"]),
    image: new Set(["png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp"]),
    data: new Set(["csv", "tsv", "xlsx", "parquet", "db", "sqlite"]),
    doc: new Set(["md", "txt", "pdf", "docx", "pptx", "rtf"]),
  };

  const fmtSize = (b) => {
    if (b < 1024) return `${b} B`;
    if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
    return `${(b / 1024 / 1024).toFixed(1)} MB`;
  };

  const fmtDate = (ts) => new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

  function extCategory(name) {
    const ext = (name.split(".").pop() || "").toLowerCase();
    for (const [cat, set] of Object.entries(EXT_CAT)) if (set.has(ext)) return cat;
    return "other";
  }

  async function loadOutputs() {
    state.outputsDirty = false;
    const list = $("filesList");
    try {
      const data = await fetchJson(`/api/outputs?session=${encodeURIComponent(state.sessionId)}`);
      renderOutputs(data);
    } catch {
      list.innerHTML = errorRow("Could not load output files.", "outputs");
      bindRetry(list, loadOutputs);
    }
  }

  function renderOutputs(data) {
    const list = $("filesList");
    if (!data.files.length) {
      list.innerHTML = `<div class="pane-empty">Files the agent produces will appear here.</div>`;
      return;
    }
    list.innerHTML = "";
    for (const f of data.files) {
      const cat = extCategory(f.name);
      const row = document.createElement("div");
      row.className = `file-row-card cat-${cat}`;
      row.innerHTML = `
        <div class="file-ic">${icon("i-file")}</div>
        <div class="file-body">
          <div class="file-name"><span title="${esc(f.path)}">${esc(f.name)}</span></div>
          <div class="file-meta" title="${esc(absoluteTime(f.modified * 1000))}">${fmtSize(f.size)} · ${fmtDate(f.modified)}</div>
        </div>
        <div class="file-acts">
          <a class="file-act" title="Download" href="/api/download?path=${encodeURIComponent(f.path)}" download>
            ${icon("i-download")}
          </a>
        </div>`;
      list.appendChild(row);
    }
  }

  // -------------------------------------------------------- history drawer
  const historyDrawer = $("historyDrawer");

  const timeAgo = (ts) => {
    if (!ts) return "";
    const s = Math.floor((Date.now() - ts * 1000) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  };

  async function loadHistory() {
    const list = $("historyList");
    let data;
    try {
      data = await fetchJson("/api/sessions");
    } catch {
      list.innerHTML = errorRow("Could not load task history.", "history");
      bindRetry(list, loadHistory);
      return;
    }
    if (!data.sessions.length) {
      list.innerHTML = `<div class="pane-empty">No past tasks yet. Send a message to start one.</div>`;
      return;
    }
    list.innerHTML = "";
    for (const s of data.sessions) {
      const item = document.createElement("div");
      item.className = "history-item" + (s.id === state.sessionId ? " current" : "") + (s.running ? " running" : "");
      item.innerHTML = `
        <span class="h-dot"></span>
        <div class="h-body">
          <div class="h-title" title="${esc(s.title)}">${esc(s.title)}</div>
          <div class="h-meta">${timeAgo(s.created_at)}${s.running ? " · running" : ""}</div>
        </div>
        <button class="h-del" title="Delete" aria-label="Delete this task"><svg class="ic"><use href="#i-trash"/></svg></button>`;
      item.onclick = () => switchSession(s.id);
      item.querySelector(".h-del").onclick = async (e) => {
        e.stopPropagation();
        if (!confirm("Delete this task and its history? This cannot be undone.")) return;
        try {
          await fetchJson(`/api/sessions/${encodeURIComponent(s.id)}`, { method: "DELETE" });
        } catch {
          showToast("Could not delete the task — check your connection and try again.", "error");
          return;
        }
        if (s.id === state.sessionId) { resetToNewSession(); return; }
        loadHistory();
      };
      list.appendChild(item);
    }
    applyHistoryFilter();
  }

  // Client-side filter over what's already loaded (P2-4). This is NOT the
  // mandated global search across runs/logs/outputs — that needs a
  // server-side index this local, in-memory session store doesn't have.
  // See UI_ENGAGEMENT.md §5 for the deferral reasoning.
  function applyHistoryFilter() {
    const q = $("historySearch").value.trim().toLowerCase();
    $("historyList").querySelectorAll(".history-item").forEach((item) => {
      const hay = item.querySelector(".h-title")?.textContent.toLowerCase() || "";
      item.classList.toggle("hidden", q.length > 0 && !hay.includes(q));
    });
  }
  $("historySearch").addEventListener("input", applyHistoryFilter);

  // Live timeline filter (P2-5) — same client-side-only caveat as history
  // search: filters what's already rendered, not a server-side log index.
  $("timelineFilter").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    timeline.querySelectorAll(".tl-item").forEach((it) => {
      it.classList.toggle("hidden", q.length > 0 && !it.textContent.toLowerCase().includes(q));
    });
  });

  function switchSession(sid) {
    if (sid === state.sessionId) { closeHistory(); return; }
    state.intentionalClose = true;
    state.ws?.close();
    state.intentionalClose = false;
    state.sessionId = sid;
    sessionStorage.setItem("openmanus_sid", sid);
    history.replaceState(null, "", `#s=${sid}`);
    setRunning(false);
    resetPanes();
    state.files.clear();
    state.outputsDirty = true;
    state.pinnedTab = false;
    closeHistory();
    connect(); // server replays this session's event log, rebuilding the UI
  }

  function resetToNewSession() {
    state.intentionalClose = true;
    state.ws?.close();
    state.intentionalClose = false;
    state.sessionId = crypto.randomUUID();
    sessionStorage.setItem("openmanus_sid", state.sessionId);
    history.replaceState(null, "", `#s=${state.sessionId}`);
    setRunning(false);
    resetPanes();
    state.files.clear();
    state.outputsDirty = true;
    messages.innerHTML = `
      <div class="welcome" id="welcome">
        <svg class="welcome-logo"><use href="#i-logo"/></svg>
        <h1>Hi, I'm OpenManus.</h1>
        <p>What can I do for you? I can browse the web, run code, and edit files — you'll see every step live in the panel on the right.</p>
      </div>`;
    closeHistory();
    connect();
  }

  function openHistory() {
    historyDrawer.classList.remove("hidden");
    $("historyList").innerHTML = `<div class="pane-empty">Loading...</div>`;
    loadHistory();
  }

  function closeHistory() { historyDrawer.classList.add("hidden"); }

  $("btnHistory").onclick = () =>
    historyDrawer.classList.contains("hidden") ? openHistory() : closeHistory();
  $("historyClose").onclick = closeHistory;

  $("btnLogout").onclick = async () => {
    if (!confirm("Sign out? You'll need your password again to get back in.")) return;
    try {
      await fetch("/api/logout", { method: "POST" });
    } catch {
      // fall through to reload regardless — worst case the cookie is still
      // valid and "/" simply shows the app again, which is safe either way
    }
    location.reload();
  };

  // -------------------------------------------------------- run state UI
  function setRunning(on) {
    state.running = on;
    $("btnSend").classList.toggle("hidden", on);
    $("btnStop").classList.toggle("hidden", !on);
    $("promptInput").disabled = on;
    const pill = $("livePill");
    pill.classList.toggle("running", on);
    pill.classList.remove("stopped");
    $("liveLabel").textContent = on ? "Working" : "Idle";
    if (on) {
      state.startedAt = Date.now();
      state.timer = setInterval(() => ($("elapsedStat").textContent = fmtTime()), 1000);
    } else {
      clearInterval(state.timer);
    }
  }

  // -------------------------------------------------------- events
  const HANDLERS = {
    replay(ev) {
      // Reset UI and re-dispatch the session's event history in order.
      state.replaying = true;
      resetPanes();
      for (const e of ev.events) {
        const h = HANDLERS[e.type];
        if (h) { try { h(e.data); } catch { /* skip malformed entry */ } }
      }
      state.replaying = false;
      scrollDown(messages);
      scrollDown($("pane-live"));
    },
    run_start(ev) {
      setRunning(true);
      state.pinnedTab = false;
      state.toolCards.clear();
      state.actionCount = 0;
      state.submitPending = false; // echo received — composer unlocked
      state.runTokens = { in: 0, out: 0 };
      state.lastPrompt = ev.prompt; // for the Retry action on failure/stop
      syncSend();
      dropActivityPill();
      addUserMessage(ev.prompt);
      if (!historyDrawer.classList.contains("hidden")) loadHistory();
    },
    thought(ev) {
      addThought(ev.content);
      if (timeline.querySelector(".pane-empty")) timeline.innerHTML = "";
      const el = document.createElement("div");
      el.className = "tl-item";
      el.innerHTML = `
        <div class="tl-icon chat">${icon("i-chat")}</div>
        <div class="tl-body"><div class="tl-title">Thinking</div>
        <div class="tl-detail">${esc((ev.content || "").split("\n")[0].slice(0, 90))}</div></div>
        <div class="tl-time">${fmtTime()}</div>`;
      timeline.appendChild(el);
      scrollDown($("pane-live"));
    },
    step(ev) {
      state.step = { current: ev.current, max: ev.max };
      $("stepStat").textContent = `Step ${ev.current}/${ev.max}`;
    },
    tool_start(ev) {
      state.actionCount += 1;
      updateActivityPill(ev);
      const tlEl = timelineAdd(ev);
      state.toolCards.set(ev.id, tlEl);
      autoSwitch(toolUI(ev.name, ev.category).cat);
    },
    tool_end(ev) {
      const tlEl = state.toolCards.get(ev.id);
      if (tlEl) {
        tlEl.classList.add(ev.ok ? "ok" : "err");
        tlEl.querySelector(".tl-status").innerHTML = ev.ok
          ? `${fmtDur(ev.duration_ms)} ${icon("i-check")}`
          : `${icon("i-x")} failed`;
      }
      // Feed the tool-call inspector (P1-8): merge in what tool_start didn't
      // have yet — status, duration, and the raw result/observation.
      const data = toolCallData.get(ev.id) || {};
      Object.assign(data, {
        ok: ev.ok,
        duration_ms: ev.duration_ms,
        result: ev.result,
        truncated: (ev.result || "").length >= 2000,
      });
      toolCallData.set(ev.id, data);
      refreshToolInspector(ev.id);
      const pill = state.activityPill;
      if (pill && !ev.ok) {
        pill.querySelector(".ap-label").textContent = "Last action failed";
        pill.querySelector(".ap-status").innerHTML = icon("i-x");
      }
    },
    terminal(ev) { addTerminalBlock(ev.code, ev.output); },
    browser_screenshot(ev) { setScreenshot(ev.image); },
    browser_meta(ev) {
      $("browserUrl").textContent = ev.title ? `${ev.title} — ${ev.url}` : (ev.url || "");
    },
    file_update(ev) {
      state.filesDirty = true;
      state.outputsDirty = true;
      if (state.activeTab === "files") {
        clearTimeout(state._outputsTimer);
        state._outputsTimer = setTimeout(loadOutputs, 500);
      }
      if (ev.content != null) {
        state.files.set(ev.path, ev.content);
        if (state.activeFile === ev.path) renderCode(ev.path, ev.content);
        if (!state.pinnedTab && !state.replaying) {
          state.activeFile = ev.path;
          renderCode(ev.path, ev.content);
          switchTab("editor", false);
          refreshFiles();
        }
      }
    },
    ask_human(ev) { addAskCard(ev.question); },
    final_result(ev) {
      if (ev.content) addFinal(ev.content);
      const pill = $("livePill");
      pill.classList.remove("running");
      $("liveLabel").textContent = "Done";
      state.runOutcome = "completed";
    },
    status(ev) {
      if (ev.status === "stopped") {
        const pill = $("livePill");
        pill.classList.add("stopped");
        $("liveLabel").textContent = "Stopped";
        finalizeActivityPill(false);
        state.runOutcome = "stopped";
      }
    },
    error(ev) { addError(ev.message); state.runOutcome = "failed"; },
    run_end() {
      setRunning(false);
      state.submitPending = false;
      syncSend();
      finalizeActivityPill(state.runOutcome !== "stopped");
      addRunReport();
      state.runOutcome = "";
    },
    usage(ev) {
      state.tokens.in = ev.total_in;
      state.tokens.out = ev.total_out;
      state.runTokens.in += ev.delta_in || 0;
      state.runTokens.out += ev.delta_out || 0;
      $("tokenStat").textContent =
        `${state.tokens.in.toLocaleString()} in / ${state.tokens.out.toLocaleString()} out`;
    },
    control(ev) {
      // Audit trail: operator actions land in the timeline (and replay log).
      if (timeline.querySelector(".pane-empty")) timeline.innerHTML = "";
      const el = document.createElement("div");
      el.className = "tl-item tl-control";
      const label = ev.action === "stop" ? "Stop requested by operator"
        : ev.action === "model_switch" ? `Model switched to ${ev.model}`
        : `${ev.action} by ${ev.actor || "operator"}`;
      el.innerHTML = `
        <div class="tl-icon chat">${icon("i-chat")}</div>
        <div class="tl-body"><div class="tl-title">${esc(label)}</div></div>
        <div class="tl-time" title="${esc(absoluteTime(Date.now()))}">${fmtTime()}</div>`;
      timeline.appendChild(el);
      scrollDown($("pane-live"));
    },
    pong() {},
  };

  function resetPanes() {
    messages.innerHTML = "";
    timeline.innerHTML = `<div class="pane-empty">Activity from a run will appear here.</div>`;
    terminal.innerHTML = `<div class="pane-empty">Python executions will appear here.</div>`;
    $("browserShot").classList.add("hidden");
    $("browserEmpty")?.classList.remove("hidden");
    $("browserUrl").textContent = "No page loaded";
    $("editorMain").innerHTML = `<div class="pane-empty">Files the agent works on will appear here.</div>`;
    $("stepStat").textContent = "Step 0/0";
    $("elapsedStat").textContent = "0:00";
    state.toolCards.clear();
    state.activityPill = null;
    state.actionCount = 0;
    state.activeFile = null;
    state.zoom = 1;
    toolCallData.clear();
    prunedCounts.clear();
    const timelineFilter = $("timelineFilter");
    if (timelineFilter) timelineFilter.value = "";
  }

  // -------------------------------------------------------- websocket
  const { composerDecision, backoffDelay, pruneCount, absoluteTime } = window.UIUtils;

  // Returns true only when the frame actually went out on an OPEN socket.
  function send(obj) {
    if (state.ws?.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify(obj));
      return true;
    }
    return false;
  }

  const reconnectBanner = $("reconnectBanner");
  let reconnectAttempt = 0;
  let reconnectTimer = null;

  function setReconnecting(on) {
    reconnectBanner.classList.toggle("hidden", !on);
    if (on) {
      $("reconnectLabel").textContent =
        `Connection lost — reconnecting${reconnectAttempt > 1 ? ` (attempt ${reconnectAttempt})` : ""}...`;
      $("liveLabel").textContent = "Reconnecting";
      if (state.running) clearInterval(state.timer); // elapsed pauses at last known truth
    } else if (!state.running) {
      $("liveLabel").textContent = "Idle";
    }
  }

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/${state.sessionId}`);
    state.ws = ws;

    ws.onopen = () => {
      reconnectAttempt = 0;
      setReconnecting(false);
      if (state.running) { // resume elapsed ticking from server-truth events
        $("liveLabel").textContent = "Working";
        state.timer = setInterval(() => ($("elapsedStat").textContent = fmtTime()), 1000);
      }
    };
    ws.onmessage = (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch { return; }
      const h = HANDLERS[msg.type];
      if (h) { try { h(msg.data || {}); } catch (err) { console.error("event handler failed", err); } }
    };
    ws.onclose = (ev) => {
      state.ws = null;
      if (state.intentionalClose) return;
      // Code 1008 = policy violation (auth/session/origin rejected server-side).
      // Retrying with the same dead credential forever just spams the
      // server; a full reload re-runs the "/" auth check and shows the
      // sign-in page if the session is genuinely gone (idle/absolute
      // timeout, logout, or lockout).
      if (ev.code === 1008) {
        showToast("Session ended — reloading to sign in again.", "error", 3000);
        setTimeout(() => location.reload(), 1200);
        return;
      }
      reconnectAttempt += 1;
      setReconnecting(true);
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connect, backoffDelay(reconnectAttempt)); // 1s→15s cap
    };
    ws.onerror = () => ws.close();
  }

  // -------------------------------------------------------- UI wiring
  const input = $("promptInput");

  function submitPrompt() {
    const decision = composerDecision(
      input.value,
      state.ws?.readyState === WebSocket.OPEN,
      state.running,
      state.submitPending
    );
    if (decision.action === "block-empty") return;
    if (decision.action === "block-offline") {
      addError("Not connected — prompt kept. Reconnecting; try again in a moment.");
      return;
    }
    if (decision.action === "block-running" || decision.action === "block-pending") return;
    if (!send({ type: "run", prompt: input.value.trim() })) {
      addError("Send failed — prompt kept. Check the connection banner.");
      return;
    }
    state.submitPending = true;
    syncSend();
    if (decision.clearInput) { input.value = ""; autosize(); }
  }

  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
  }

  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitPrompt(); }
  });
  input.addEventListener("paste", () => setTimeout(autosize, 0));
  $("btnSend").onclick = submitPrompt;
  $("btnStop").onclick = () => send({ type: "stop" });

  document.querySelectorAll(".tab").forEach((tab) =>
    tab.addEventListener("click", () => switchTab(tab.dataset.tab, true)));

  $("btnNew").onclick = () => {
    resetToNewSession();
    if (!historyDrawer.classList.contains("hidden")) loadHistory();
  };

  const applyZoom = () => {
    state.zoom = Math.min(2, Math.max(0.25, state.zoom));
    $("browserShot").style.width = `${state.zoom * 100}%`;
  };
  $("zoomIn").onclick = () => { state.zoom += 0.15; applyZoom(); };
  $("zoomOut").onclick = () => { state.zoom -= 0.15; applyZoom(); };

  // -------------------------------------------------------- model settings
  const modal = $("modelModal");
  let editingId = null;
  let modelsData = { active: null, models: [], presets: [] };

  async function fetchModels() {
    const res = await fetch("/api/models");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  async function updateModelChip() {
    try {
      modelsData = await fetchModels();
      const active = modelsData.models.find((m) => m.id === modelsData.active);
      $("modelChipName").textContent = active ? active.model : "default";
    } catch {
      $("modelChipName").textContent = "default";
    }
  }

  function formMsg(text, isErr) {
    const el = $("formMsg");
    el.textContent = text;
    el.classList.toggle("err", !!isErr);
    if (text) setTimeout(() => { if (el.textContent === text) el.textContent = ""; }, 4000);
  }

  function renderModels() {
    const list = $("modelList");
    list.innerHTML = "";
    if (!modelsData.models.length) {
      list.innerHTML = `<div class="pane-empty">No models configured.</div>`;
      return;
    }
    for (const m of modelsData.models) {
      const isActive = m.id === modelsData.active;
      const card = document.createElement("div");
      card.className = "model-card" + (isActive ? " active" : "");
      card.innerHTML = `
        <span class="model-radio"></span>
        <div class="model-info">
          <div class="model-name">${esc(m.label)}${isActive ? '<span class="model-badge">Active</span>' : ""}</div>
          <div class="model-sub">${esc(m.model)} @ ${esc(m.base_url)}</div>
          <div class="model-key ${m.has_key ? "has-key" : "no-key"}">
            <span class="key-dot"></span>${m.has_key ? `key ${esc(m.key_hint)}` : "no key set"}
          </div>
        </div>
        ${m.source === "user" ? `
        <div class="model-actions">
          <button class="btn-icon" data-act="edit" title="Edit"><svg class="ic"><use href="#i-edit"/></svg></button>
          <button class="btn-icon danger" data-act="del" title="Delete"><svg class="ic"><use href="#i-trash"/></svg></button>
        </div>` : ""}`;
      card.onclick = async (e) => {
        const act = e.target.closest("[data-act]")?.dataset.act;
        if (act === "edit") { startEdit(m); return; }
        try {
          if (act === "del") {
            e.stopPropagation();
            if (!confirm(`Delete model "${m.label}"? This cannot be undone.`)) return;
            const res = await fetch(`/api/models/${encodeURIComponent(m.id)}`, { method: "DELETE" });
            if (res.ok) { await reloadModels(); formMsg("Model deleted"); }
            else formMsg((await res.json()).detail || "Delete failed", true);
            return;
          }
          if (isActive) return;
          const res = await fetch("/api/models/active", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id: m.id }),
          });
          if (res.ok) {
            await reloadModels();
            formMsg(`Switched to ${m.model}`);
          } else formMsg("Switch failed", true);
        } catch {
          // Network error: don't let this become an unhandled rejection (P0-3/P1-7).
          formMsg("Request failed — check your connection and try again.", true);
        }
      };
      list.appendChild(card);
    }
  }

  async function reloadModels() {
    modelsData = await fetchModels();
    renderModels();
    await updateModelChip();
  }

  function fillPresets() {
    const sel = $("fPreset");
    sel.innerHTML = `<option value="">Choose a provider preset (optional)...</option>` +
      modelsData.presets.map((p, i) => `<option value="${i}">${esc(p.label)}</option>`).join("");
    sel.onchange = () => {
      const p = modelsData.presets[+sel.value];
      if (!p) return;
      $("fLabel").value = p.label;
      $("fModel").value = p.model;
      $("fBaseUrl").value = p.base_url;
    };
  }

  function resetForm() {
    editingId = null;
    $("modelForm").reset();
    $("fMaxTokens").value = 8192;
    $("fTemp").value = "0.0";
    $("formCancel").classList.add("hidden");
    $("formHead").textContent = "Add a model";
    $("formSave").textContent = "Save model";
  }

  function startEdit(m) {
    editingId = m.id;
    $("fLabel").value = m.label;
    $("fModel").value = m.model;
    $("fBaseUrl").value = m.base_url;
    $("fKey").value = "";
    $("fKey").placeholder = m.has_key ? `keep current (${m.key_hint})` : "sk-...";
    $("fMaxTokens").value = m.max_tokens;
    $("fTemp").value = m.temperature;
    $("formCancel").classList.remove("hidden");
    $("formHead").textContent = "Edit model";
    $("formSave").textContent = "Update model";
    $("fPreset").value = "";
  }

  $("modelForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {
      id: editingId || undefined,
      label: $("fLabel").value.trim(),
      model: $("fModel").value.trim(),
      base_url: $("fBaseUrl").value.trim(),
      api_key: $("fKey").value.trim() || undefined,
      max_tokens: parseInt($("fMaxTokens").value, 10) || 8192,
      temperature: parseFloat($("fTemp").value) || 0.0,
    };
    if (!body.label || !body.model || !body.base_url) { formMsg("Label, model and base URL are required", true); return; }
    try {
      const res = await fetch("/api/models", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) { formMsg(data.detail || "Save failed", true); return; }
      formMsg(`Model ${data.message}`);
      resetForm();
      await reloadModels();
    } catch { formMsg("Save failed (server unreachable)", true); }
  });

  $("formCancel").onclick = resetForm;

  function openModal() {
    modal.classList.remove("hidden");
    $("modelList").innerHTML = `<div class="pane-empty">Loading models...</div>`;
    reloadModels().then(fillPresets).catch(() => {
      $("modelList").innerHTML = `<div class="pane-empty">Could not load models.</div>`;
    });
  }

  $("btnSettings").onclick = openModal;
  $("modelChip").onclick = openModal;
  $("modalClose").onclick = () => modal.classList.add("hidden");
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.classList.add("hidden"); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.classList.contains("hidden")) modal.classList.add("hidden");
  });

  updateModelChip();

  // Keep the send button enabled only when there is text and agent is idle.
  const syncSend = () => { $("btnSend").disabled = !input.value.trim() || state.running || state.submitPending; };
  input.addEventListener("input", syncSend);
  new MutationObserver(syncSend).observe($("btnStop"), { attributes: true, attributeFilter: ["class"] });

  // Theme: dark is the ops-console default; the operator's explicit choice
  // persists. No prefers-color-scheme override — the spec mandates dark default.
  const applyTheme = (theme) => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("om_theme", theme);
    $("btnTheme").textContent = theme === "dark" ? "Light" : "Dark";
    $("btnTheme").setAttribute("aria-label", `Switch to ${theme === "dark" ? "light" : "dark"} theme`);
  };
  applyTheme(localStorage.getItem("om_theme") || "dark");
  $("btnTheme").onclick = () =>
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");

  // Keyboard support for the tab list (P1-6).
  $("tabs").addEventListener("keydown", (e) => {
    const tabsEl = [...document.querySelectorAll(".tab")];
    const idx = tabsEl.indexOf(document.activeElement);
    if (idx === -1) return;
    const next = e.key === "ArrowRight" ? idx + 1 : e.key === "ArrowLeft" ? idx - 1 : -1;
    if (next >= 0 && next < tabsEl.length) {
      e.preventDefault();
      tabsEl[next].focus();
      tabsEl[next].click();
    } else if (e.key === "Home" && tabsEl[0]) {
      e.preventDefault(); tabsEl[0].focus(); tabsEl[0].click();
    } else if (e.key === "End" && tabsEl.length) {
      e.preventDefault(); tabsEl.at(-1).focus(); tabsEl.at(-1).click();
    }
  });

  // Regression-test hooks (Playwright); inert in normal use.
  window.__om = {
    state, send, submitPrompt, switchTab, setReconnecting,
    // Test-only event dispatcher: drives the same HANDLERS a real WS message
    // would, without needing a live agent run. Inert in normal use.
    dispatch: (type, data) => HANDLERS[type]?.(data || {}),
  };

  connect();
})();
