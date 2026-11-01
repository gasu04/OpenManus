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
    readOnly: false,
    replayMode: null,     // {sessionId, runId} while a historical run is replayed
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
    uploaded: [],         // files attached this turn ({name, path, size})
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

  // UX-002: render a timeline timestamp from the event's own server-side
  // `ts` (epoch ms, embedded by the backend since the duration fix) relative
  // to the run's start, so replayed runs show real durations instead of the
  // client-side live timer's 0:00. Falls back to the live timer for events
  // that predate ts instrumentation.
  const fmtEventTime = (ev) => {
    if (ev && ev.ts && state.startedAt) {
      const s = Math.max(0, Math.floor((ev.ts - state.startedAt) / 1000));
      return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    }
    return fmtTime();
  };

  const icon = (name) => `<svg class="ic"><use href="#${name}"/></svg>`;

  const scrollDown = (el) => { el.scrollTop = el.scrollHeight; };

  // Minimal safe markdown renderer (escapes first, then applies patterns).
  const markdown = (src) => {
    let text = esc(src);
    const blocks = [];
    const stash = (html) => ` B${blocks.push(html) - 1} `;
    text = text.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) =>
      stash(`<pre><code>${code.replace(/\n$/, "")}</code></pre>`));
    // Markdown links are stashed BEFORE bare-URL linkification so their URLs
    // are never matched twice.
    text = text.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, (_, label, url) =>
      stash(`<a href="${url}" target="_blank" rel="noopener">${label}</a>`));
    text = text
      .replace(/^###\s+(.+)$/gm, "<h3>$1</h3>")
      .replace(/^##\s+(.+)$/gm, "<h2>$1</h2>")
      .replace(/^#\s+(.+)$/gm, "<h1>$1</h1>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      // Bare URLs become real links (e.g. consent URLs in tool results); the
      // trailing character class keeps sentence punctuation out of the href.
      .replace(
        /(?<![/"'>=])(https?:\/\/[^\s<>"')]+[A-Za-z0-9/#~=_%&+-])/g,
        '<a href="$1" target="_blank" rel="noopener">$1</a>'
      )
      .replace(/^\s*[-*]\s+(.+)$/gm, "<li>$1</li>")
      .replace(/^\s*\d+\.\s+(.+)$/gm, "<li>$1</li>");
    text = text
      // Group each run of consecutive items into ONE list (was: last item got
      // its own <ul>, leaving siblings orphaned with a visible gap).
      .replace(/((?:<li>[\s\S]*?<\/li>\s*)+)/g, "<ul>$1</ul>")
      .split(/\n{2,}/)
      .map((p) => (/^\s*<(h\d|ul|ol|pre)/.test(p.trim()) ? p : `<p>${p.replace(/\n/g, "<br>")}</p>`))
      .join("");
    return text.replace(/ B(\d+) /g, (_, i) => blocks[+i]);
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
      <div class="final-head">
        <svg class="ic" aria-hidden="true"><use href="#i-check"/></svg><span>Final answer</span>
        <div class="final-toolbar">
          <button class="final-toggle" data-mode="rendered">View raw</button>
          ${copyBtnHtml(0, "final answer")}
        </div>
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

  function finalizeActivityPill(done, ev) {
    const pill = state.activityPill;
    if (!pill) return;
    pill.classList.add("done");
    pill.querySelector(".ap-status").innerHTML = done ? icon("i-check") : icon("i-stop");
    pill.querySelector(".ap-count").textContent =
      `${state.actionCount} actions · ${fmtEventTime(ev)} · view details`;
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
      <div class="tl-time" title="${esc(absoluteTime(ev.ts || Date.now()))}">${fmtEventTime(ev)}</div>
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
  // `ev` is the run_end event — its server ts makes the duration replay-safe.
  function addRunReport(ev) {
    if (timeline.querySelector(".pane-empty")) timeline.innerHTML = "";
    const outcome = state.runOutcome || "ended";
    const dur = fmtEventTime(ev);
    const tok = state.runTokens.in + state.runTokens.out > 0
      ? ` · ${(state.runTokens.in + state.runTokens.out).toLocaleString()} tokens` : "";
    const report = {
      completed: { title: "Task completed", detail: `${state.actionCount} actions · ${state.step.current} steps · ${dur}${tok}`, cls: "ok", ic: "i-check" },
      stopped: { title: "Task stopped by user", detail: `${state.actionCount} actions before stopping · ${dur}${tok}`, cls: "stopped", ic: "i-stop" },
      failed: { title: "Task failed", detail: `Error during run · ${state.actionCount} actions · ${dur}`, cls: "err", ic: "i-x" },
      ended: { title: "Run ended", detail: `${state.actionCount} actions · ${dur}${tok}`, cls: "", ic: "i-bolt" },
    }[outcome];
    const canRetry = !state.readOnly && (outcome === "failed" || outcome === "stopped") && state.lastPrompt;
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
      <div class="tl-time" title="${esc(absoluteTime((ev && ev.ts) || Date.now()))}">${dur}</div>
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
    if (tab === "runs") loadRuns(false);   // disk-backed history on every visit
    if (tab === "connectors") loadConnectors();
    const activeTab = document.querySelector(`#tabs .tab[data-tab="${tab}"]`);
    activeTab?.scrollIntoView({ block: "nearest", inline: "nearest" });
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

  function setScreenshotUrl(url) {
    const img = $("browserShot");
    $("browserEmpty")?.classList.add("hidden");
    img.classList.remove("hidden");
    img.src = url;
    autoSwitch("browser");
  }

  // -------------------------------------------------------- terminal pane
  function addTerminalBlock(code, output, ev) {
    if (terminal.querySelector(".pane-empty")) terminal.innerHTML = "";
    const ok = !/\berror\b|Traceback|Exception/i.test(output || "");
    const el = document.createElement("div");
    el.className = "term-block";
    el.innerHTML = `
      <div class="term-prompt"><span><span class="term-user">agent</span>@openmanus:~$</span><span>${fmtEventTime(ev)}</span></div>
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

  // Live timeline filter (P2-5) — same client-side-only caveat as history
  // search: filters what's already rendered, not a server-side log index.
  $("timelineFilter").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    timeline.querySelectorAll(".tl-item").forEach((it) => {
      it.classList.toggle("hidden", q.length > 0 && !it.textContent.toLowerCase().includes(q));
    });
  });

  function switchSession(sid, readOnly = false) {
    if (sid === state.sessionId) return;
    clearReplayMode();
    state.intentionalClose = true;
    state.ws?.close();
    state.intentionalClose = false;
    state.sessionId = sid;
    sessionStorage.setItem("openmanus_sid", sid);
    history.replaceState(null, "", `#s=${sid}`);
    setReadOnly(readOnly);
    setRunning(false);
    resetPanes();
    state.files.clear();
    state.outputsDirty = true;
    state.pinnedTab = false;
    switchTab("live", true);
    connect(); // server replays this session's event log, rebuilding the UI
  }

  function resetToNewSession() {
    clearReplayMode();
    state.intentionalClose = true;
    state.ws?.close();
    state.intentionalClose = false;
    state.sessionId = crypto.randomUUID();
    sessionStorage.setItem("openmanus_sid", state.sessionId);
    history.replaceState(null, "", `#s=${state.sessionId}`);
    setReadOnly(false);
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
    connect();
  }

  // ---------------------------------------------- persistent previous runs
  // Disk-backed history (survives restarts). The list comes from a server-side
  // index (filters, sort, search and pagination run on the server); detail
  // pages stream events so large runs never load whole into the browser.
  const RUN_STATUSES = ["completed", "failed", "stopped", "interrupted", "running"];
  const STATUS_GLYPH = { completed: "✓", failed: "✕", stopped: "■", interrupted: "⚠", running: "●" };
  const RUNS_PAGE = 25;
  const EVENTS_PAGE = 200;
  const REPLAY_PAGE = 2000;
  const REPLAY_EVENT_CAP = 20000;
  const RUN_HASH_RE = /^#run=([\w-]+)\/([\w-]+)$/;
  const enc = encodeURIComponent;
  const runFilters = { status: "", sort: "newest", q: "" };
  const runsView = { items: [], cursor: null, summary: null, token: 0, scroll: 0, open: null, lastCard: null };
  let runsSearchTimer = null;

  const statusOf = (run) => (run.running ? "running" : RUN_STATUSES.includes(run.status) ? run.status : "ended");
  const statusBadge = (run) => {
    const s = statusOf(run);
    return `<span class="run-status ${s}"><span aria-hidden="true">${STATUS_GLYPH[s] || "•"}</span> ${esc(s)}</span>`;
  };
  const runTokenTotal = (run) => (run.input_tokens || 0) + (run.output_tokens || 0);
  const runMeta = (run) =>
    `${fmtDate(run.started_at || 0)} · ${fmtDur(run.duration_ms || 0)} · ${run.actions || 0} actions · ${run.steps || 0} steps` +
    (runTokenTotal(run) ? ` · ${runTokenTotal(run).toLocaleString()} tokens` : "");

  function groupLabel(ts) {
    const day = new Date(ts * 1000);
    const today = new Date();
    const yesterday = new Date();
    yesterday.setDate(today.getDate() - 1);
    if (day.toDateString() === today.toDateString()) return "Today";
    if (day.toDateString() === yesterday.toDateString()) return "Yesterday";
    return day.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  }

  function updateRunsBadge(total) {
    const badge = $("runsBadge");
    if (!badge) return;
    badge.textContent = String(total);
    badge.classList.toggle("hidden", !total);
  }

  async function refreshRunsBadge() {
    try {
      const data = await fetchJson("/api/runs?limit=1");
      updateRunsBadge(data.total_all || 0);
    } catch { /* badge is cosmetic; the list reports its own errors */ }
  }

  function runsQueryUrl(cursor) {
    const params = new URLSearchParams({ limit: String(RUNS_PAGE), sort: runFilters.sort });
    if (runFilters.status) params.set("status", runFilters.status);
    if (runFilters.q) params.set("q", runFilters.q);
    if (cursor) params.set("cursor", cursor);
    return `/api/runs?${params}`;
  }

  async function loadRuns(append = false) {
    const list = $("runsList");
    const token = ++runsView.token; // a newer request supersedes this one
    let data;
    try {
      data = await fetchJson(runsQueryUrl(append ? runsView.cursor : null));
    } catch {
      if (token !== runsView.token) return;
      list.innerHTML = errorRow("Could not load previous runs.", "runs");
      bindRetry(list, () => loadRuns(false));
      return;
    }
    if (token !== runsView.token) return;
    runsView.items = append ? runsView.items.concat(data.runs) : data.runs;
    runsView.cursor = data.next_cursor;
    runsView.summary = data.summary;
    updateRunsBadge(data.total_all || 0);
    renderRuns();
  }

  function renderRunsSummary() {
    const s = runsView.summary;
    const el = $("runsSummary");
    if (!s) { el.textContent = ""; return; }
    const rate = s.success_rate == null ? "—" : `${Math.round(s.success_rate * 100)}%`;
    el.textContent =
      `${s.total.toLocaleString()} run${s.total === 1 ? "" : "s"} · ${rate} success · ${s.total_tokens.toLocaleString()} tokens`;
  }

  function renderRuns() {
    const list = $("runsList");
    renderRunsSummary();
    $("runsMore").classList.toggle("hidden", !runsView.cursor);
    if (!runsView.items.length) {
      const filtered = runFilters.status || runFilters.q;
      list.innerHTML = `<div class="pane-empty runs-empty">${filtered
        ? "No runs match these filters."
        : "No previous runs yet. Start a task and it will appear here."}</div>`;
      return;
    }
    list.innerHTML = "";
    const grouped = runFilters.sort === "newest" || runFilters.sort === "oldest";
    let lastGroup = "";
    for (const run of runsView.items) {
      if (grouped) {
        const label = groupLabel(run.started_at || 0);
        if (label !== lastGroup) {
          const heading = document.createElement("h3");
          heading.className = "run-group";
          heading.textContent = label;
          list.appendChild(heading);
          lastGroup = label;
        }
      }
      const card = document.createElement("button");
      card.type = "button";
      card.className = "run-card";
      card.dataset.sessionId = run.session_id;
      card.dataset.runId = run.id;
      const preview = run.final_preview || "";
      card.innerHTML = `
        <div class="run-card-top">
          <span class="run-title">${esc(run.title || run.prompt_preview || "Task")}</span>
          ${statusBadge(run)}
        </div>
        <div class="run-meta">${esc(runMeta(run))}</div>
        ${preview ? `<div class="run-preview">${esc(preview)}</div>` : ""}
        <div class="run-card-foot">${run.file_count ? `${run.file_count} produced file${run.file_count === 1 ? "" : "s"} · ` : ""}${run.read_only ? "Read-only history" : "Live run"}</div>`;
      card.onclick = () => openRun(run.session_id, run.id);
      list.appendChild(card);
    }
  }

  function showRunsList() {
    $("runDetail").classList.add("hidden");
    $("runDetail").innerHTML = "";
    $("runsListView").classList.remove("hidden");
    runsView.open = null;
    history.replaceState(null, "", `#s=${state.sessionId}`);
    $("pane-runs").scrollTop = runsView.scroll;
    const last = runsView.lastCard;
    if (last) {
      const card = document.querySelector(`.run-card[data-run-id="${CSS.escape(last.runId)}"]`);
      (card || $("runsSearch")).focus({ preventScroll: true }); // keep the restored scroll position
    }
  }

  // Screenshots are stored as blob references, never inline.
  const blobUrl = (sessionId, ref) => `/api/blobs/${enc(sessionId)}/${enc(ref)}`;

  const EVENT_LABEL = {
    run_start: "Prompt", thought: "Thought", step: "Step", plan: "Plan", tool_start: "Tool started",
    tool_end: "Tool finished", terminal: "Terminal", final_result: "Final answer", run_end: "Run ended",
    error: "Error", status: "Status", usage: "Token usage", file_update: "File update",
    browser_screenshot: "Screenshot", control: "Operator action", auth_required: "Authorization",
    ask_human: "Question for you",
  };

  function eventLine(event) {
    const d = event.data || {};
    const first = (text) => String(text || "").split("\n")[0].slice(0, 140);
    switch (event.type) {
      case "run_start": return first(d.prompt);
      case "thought": case "final_result": return first(d.content);
      case "tool_start": return d.name || "";
      case "tool_end": return `${d.name || ""} ${d.ok === false ? "failed" : "ok"}`;
      case "error": return first(d.message);
      case "status": return first(d.message || d.status);
      case "file_update": return d.path || "";
      default: return "";
    }
  }

  function renderEventRows(sessionId, events, container) {
    for (const event of events) {
      const payload = event.data || {};
      const json = JSON.stringify(payload, null, 2);
      const row = document.createElement("details");
      row.className = "run-event";
      const shot = payload.image_ref
        ? `<img class="re-shot" loading="lazy" alt="Browser screenshot" src="${esc(blobUrl(sessionId, payload.image_ref))}" />`
        : "";
      row.innerHTML = `
        <summary><span class="re-type">${esc(EVENT_LABEL[event.type] || event.type)}</span><code>${esc(event.type)}</code>
          <span class="re-line">${esc(eventLine(event))}</span>
          <time>${esc(payload.ts ? new Date(payload.ts).toLocaleTimeString() : "")}</time></summary>
        <div class="re-body">${shot}<div class="re-tools">${copyBtnHtml(0, "event JSON")}</div><pre>${esc(json)}</pre></div>`;
      wireCopyButtons(row, [json]);
      container.appendChild(row);
    }
  }

  async function loadRunEvents(append) {
    const open = runsView.open;
    if (!open) return;
    const { run } = open;
    const params = new URLSearchParams({ offset: String(append ? open.events : 0), limit: String(EVENTS_PAGE) });
    if (open.types) params.set("types", open.types);
    const data = await fetchJson(`/api/runs/${enc(run.session_id)}/${enc(run.id)}?${params}`);
    if (runsView.open !== open) return;
    const list = $("runEventList");
    if (!append) { list.innerHTML = ""; open.events = 0; }
    renderEventRows(run.session_id, data.events, list);
    open.events += data.events.length;
    open.total = data.total_events;
    $("runEventsCount").textContent = String(open.total);
    $("runEventsMore").classList.toggle("hidden", open.events >= open.total);
    if (!open.total) list.innerHTML = `<div class="pane-empty">No events recorded for this selection.</div>`;
  }

  async function fetchAllRunEvents(sessionId, runId) {
    const out = [];
    let total = Infinity;
    while (out.length < total && out.length < REPLAY_EVENT_CAP) {
      const data = await fetchJson(
        `/api/runs/${enc(sessionId)}/${enc(runId)}?offset=${out.length}&limit=${REPLAY_PAGE}`);
      total = data.total_events;
      if (!data.events.length) break;
      out.push(...data.events);
    }
    return { events: out, total: Number.isFinite(total) ? total : out.length };
  }

  async function openRun(sessionId, runId) {
    const detail = $("runDetail");
    runsView.scroll = $("pane-runs").scrollTop;
    runsView.lastCard = { sessionId, runId };
    $("runsListView").classList.add("hidden");
    detail.classList.remove("hidden");
    $("pane-runs").scrollTop = 0; // the detail opens at its top
    detail.innerHTML = `<div class="pane-empty">Loading run history…</div>`;
    history.replaceState(null, "", `#run=${sessionId}/${runId}`);
    let data;
    try {
      data = await fetchJson(`/api/runs/${enc(sessionId)}/${enc(runId)}?offset=0&limit=${EVENTS_PAGE}`);
    } catch {
      detail.innerHTML = `<button class="btn-ghost sm" id="runBack" type="button">← All runs</button>${errorRow("Could not load this run.", "run")}`;
      $("runBack").onclick = showRunsList;
      bindRetry(detail, () => openRun(sessionId, runId));
      return;
    }
    const run = data.run;
    runsView.open = { run, events: 0, total: data.total_events, types: "" };
    const files = (run.files || []).map((path) =>
      `<li><a href="/api/download?path=${enc(path)}" download>${esc(path)}</a></li>`).join("");
    detail.innerHTML = `
      <div class="run-detail-head">
        <button class="btn-ghost sm" id="runBack" type="button">← All runs</button>
        <span class="run-readonly">${data.read_only ? "Read-only historical run" : "Live run snapshot"}</span>
      </div>
      <div class="run-detail-title"><h3 id="runDetailTitle" tabindex="-1">${esc(run.title || run.prompt || "Task")}</h3>${statusBadge(run)}</div>
      <div class="run-meta">${esc(runMeta(run))}</div>
      <div class="run-actions" role="group" aria-label="Run actions">
        <button class="btn-ghost sm" id="runReplay" type="button">Open replay</button>
        <button class="btn-ghost sm" id="runOpenSession" type="button">Open session</button>
        <button class="btn-ghost sm" id="runRerun" type="button">Re-run this prompt</button>
        <button class="btn-ghost sm" id="runContinue" type="button">Continue from this run</button>
        <button class="btn-ghost sm" id="runExportMd" type="button">Export Markdown</button>
        <button class="btn-ghost sm" id="runExportJson" type="button">Export JSON</button>
        <button class="btn-ghost sm danger" id="runDelete" type="button">Delete session</button>
      </div>
      <section class="run-section"><div class="run-section-head"><h4>Prompt</h4>${copyBtnHtml(0, "prompt")}</div><div class="run-prompt">${esc(run.prompt || "")}</div></section>
      <section class="run-section"><div class="run-section-head"><h4>Final response</h4>${copyBtnHtml(1, "final response")}</div><div class="run-final">${run.final_response ? markdown(run.final_response) : "No final response recorded."}</div></section>
      ${files ? `<section class="run-section"><h4>Produced files</h4><ul class="run-files">${files}</ul></section>` : ""}
      <section class="run-section">
        <details id="runEventsBox">
          <summary class="run-events-summary">Event inspector (<span id="runEventsCount">${data.total_events}</span> events)</summary>
          <div class="run-events-tools">
            <label>Show <select id="runEventType" aria-label="Filter events by type">
              <option value="">All events</option>
              <option value="thought">Thoughts</option>
              <option value="tool_start,tool_end">Tool calls</option>
              <option value="terminal">Terminal</option>
              <option value="file_update">File updates</option>
              <option value="browser_screenshot">Screenshots</option>
              <option value="error,status">Errors and status</option>
            </select></label>
          </div>
          <div id="runEventList"></div>
          <button class="btn-ghost sm hidden" id="runEventsMore" type="button">Load more events</button>
        </details>
      </section>`;
    wireCopyButtons(detail, [run.prompt || "", run.final_response || ""]);
    renderEventRows(run.session_id, data.events, $("runEventList"));
    runsView.open.events = data.events.length;
    $("runEventsMore").classList.toggle("hidden", runsView.open.events >= runsView.open.total);
    $("runBack").onclick = showRunsList;
    $("runReplay").onclick = () => startReplay(run);
    $("runOpenSession").onclick = () => switchSession(run.session_id, false);
    $("runRerun").onclick = () => startNewTaskWith(run.prompt || "", "Prompt copied from a previous run — review it, then send.");
    $("runContinue").onclick = () => startNewTaskWith(continuationText(run),
      "Seeded from a previous run. The agent's state is not restored — only this text is carried over.");
    $("runExportMd").onclick = () => exportRun(run, "md");
    $("runExportJson").onclick = () => exportRun(run, "json");
    $("runDelete").onclick = () => deleteRunSession(run);
    $("runEventsMore").onclick = () => loadRunEvents(true);
    $("runEventType").onchange = (e) => { runsView.open.types = e.target.value; loadRunEvents(false); };
    $("runDetailTitle").focus();
  }

  function continuationText(run) {
    const clip = (text, n) => (text.length > n ? `${text.slice(0, n)}…` : text);
    return "Context from an earlier run (the agent's state is not restored; only this text is carried over):\n\n" +
      `Previous task:\n${clip(run.prompt || "", 2000)}\n\n` +
      `Previous result:\n${clip(run.final_response || "(no final response recorded)", 4000)}\n\n` +
      "New request:\n";
  }

  function startNewTaskWith(text, notice) {
    resetToNewSession();
    input.value = text;
    autosize();
    syncSend();
    switchTab("live", true);
    input.focus();
    showToast(notice, "info", 6000);
  }

  async function deleteRunSession(run) {
    const ok = await confirmDialog(
      "Delete this session and ALL of its runs and events from disk? This cannot be undone.",
      { danger: true, confirmLabel: "Delete" });
    if (!ok) return;
    try {
      await fetchJson(`/api/sessions/${enc(run.session_id)}`, { method: "DELETE" });
    } catch {
      showToast("Could not delete the session — check your connection and try again.", "error");
      return;
    }
    if (run.session_id === state.sessionId) resetToNewSession();
    showRunsList();
    loadRuns(false);
  }

  function downloadText(filename, text, type) {
    const url = URL.createObjectURL(new Blob([text], { type }));
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function exportRun(run, kind) {
    let all;
    try {
      all = await fetchAllRunEvents(run.session_id, run.id);
    } catch {
      showToast("Could not load the run for export.", "error");
      return;
    }
    const slug = (run.title || "run").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40) || "run";
    const base = `openmanus-${slug}-${run.id.slice(0, 8)}`;
    if (kind === "json") {
      downloadText(`${base}.json`, JSON.stringify({ run, events: all.events }, null, 2), "application/json");
      return;
    }
    const lines = [
      `# ${run.title || "Run"}`, "",
      `- Status: ${statusOf(run)}`, `- Started: ${absoluteTime(run.started_at * 1000)}`,
      `- ${runMeta(run)}`, "", "## Prompt", "", run.prompt || "", "",
      "## Final response", "", run.final_response || "(none recorded)", "",
    ];
    if ((run.files || []).length) lines.push("## Produced files", "", ...run.files.map((f) => `- \`${f}\``), "");
    lines.push("## Timeline", "");
    for (const e of all.events) {
      if (["usage", "step", "plan"].includes(e.type)) continue;
      const when = e.data?.ts ? new Date(e.data.ts).toLocaleTimeString() : "";
      lines.push(`- ${when} **${EVENT_LABEL[e.type] || e.type}** ${eventLine(e)}`.trimEnd());
    }
    downloadText(`${base}.md`, `${lines.join("\n")}\n`, "text/markdown");
  }

  // --- replay: render one historical run through the existing panes (read-only)
  async function startReplay(run) {
    let all;
    try {
      all = await fetchAllRunEvents(run.session_id, run.id);
    } catch {
      showToast("Could not load the run for replay.", "error");
      return;
    }
    state.replayMode = { sessionId: run.session_id, runId: run.id };
    setReadOnly(true);
    setRunning(false);
    resetPanes();
    state.files.clear();
    state.outputsDirty = true;
    state.pinnedTab = false;
    state.replaying = true;
    for (const event of all.events) {
      try { HANDLERS[event.type]?.(event.data || {}); } catch (err) { console.error("replay handler failed", err); }
    }
    state.replaying = false;
    const capped = all.total > all.events.length ? ` (first ${all.events.length.toLocaleString()} of ${all.total.toLocaleString()} events)` : "";
    $("replayLabel").textContent = `Replaying "${run.title || "run"}" — read-only${capped}`;
    $("replayBanner").classList.remove("hidden");
    switchTab("live", true);
    scrollDown(messages);
    scrollDown($("pane-live"));
  }

  // Detach the old socket's handlers so closing it can't trigger its
  // reconnect logic, then rebuild the UI from the server's event log.
  function reconnectFresh() {
    const old = state.ws;
    state.ws = null;
    if (old) { old.onclose = null; old.onmessage = null; old.close(); }
    clearTimeout(reconnectTimer);
    connect();
  }

  function clearReplayMode() {
    state.replayMode = null;
    $("replayBanner").classList.add("hidden");
  }

  function exitReplay() {
    clearReplayMode();
    setReadOnly(false); // the server re-asserts read-only via session_info if needed
    resetPanes();
    state.files.clear();
    state.outputsDirty = true;
    reconnectFresh();
  }

  function openRunFromHash() {
    const match = location.hash.match(RUN_HASH_RE);
    if (!match) return false;
    switchTab("runs", true);
    openRun(match[1], match[2]);
    return true;
  }

  $("btnHistory").onclick = () => switchTab("runs", true);
  $("replayExit").onclick = exitReplay;
  $("runsRefresh").onclick = () => loadRuns(false);
  $("runsMore").onclick = () => loadRuns(true);
  $("runsSort").addEventListener("change", (e) => { runFilters.sort = e.target.value; loadRuns(false); });
  $("runsSearch").addEventListener("input", () => {
    clearTimeout(runsSearchTimer);
    runsSearchTimer = setTimeout(() => { runFilters.q = $("runsSearch").value.trim(); loadRuns(false); }, 250);
  });
  document.querySelectorAll(".run-chip").forEach((chip) => chip.addEventListener("click", () => {
    runFilters.status = chip.dataset.status;
    document.querySelectorAll(".run-chip").forEach((other) => {
      const on = other.dataset.status === runFilters.status;
      other.classList.toggle("active", on);
      other.setAttribute("aria-pressed", String(on));
    });
    loadRuns(false);
  }));
  window.addEventListener("hashchange", () => {
    if (!openRunFromHash() && runsView.open) showRunsList();
  });

  $("btnLogout").onclick = async () => {
    if (!(await confirmDialog("Sign out? You'll need your password again to get back in.", { confirmLabel: "Sign out" }))) return;
    try {
      await fetch("/api/logout", { method: "POST" });
    } catch {
      // fall through to reload regardless — worst case the cookie is still
      // valid and "/" simply shows the app again, which is safe either way
    }
    location.reload();
  };

  // -------------------------------------------------------- run state UI
  function setReadOnly(on) {
    state.readOnly = Boolean(on);
    $("promptInput").disabled = state.readOnly || state.running;
    $("promptInput").placeholder = state.readOnly
      ? "Historical session — read-only. Click New to start a task."
      : "Give OpenManus a task...";
    syncSend();
  }

  function setRunning(on) {
    state.running = on;
    $("btnSend").classList.toggle("hidden", on);
    $("btnStop").classList.toggle("hidden", !on);
    $("promptInput").disabled = on || state.readOnly;
    $("runsLiveDot")?.classList.toggle("hidden", !on);
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
    session_info(ev) {
      setReadOnly(Boolean(ev.read_only));
      if (ev.runs_total != null && ev.runs_shown < ev.runs_total) {
        showToast(`Showing the latest ${ev.runs_shown} of ${ev.runs_total} runs of this session. Open Previous Runs for the rest.`, "info", 7000);
      }
    },
    history_degraded(ev) {
      $("historyBannerText").textContent = ev.message || "Run history is not being saved right now.";
      $("historyBanner").classList.remove("hidden");
    },
    history_recovered(ev) {
      $("historyBanner").classList.add("hidden");
      showToast(ev.message || "Run history saving has recovered.", "info");
    },
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
      setRunning(!state.readOnly);
      // UX-002: anchor the elapsed clock to the server's run_start timestamp
      // (survives reload/replay; setRunning() just used client receive time).
      if (ev.ts) state.startedAt = ev.ts;
      state.pinnedTab = false;
      state.toolCards.clear();
      state.actionCount = 0;
      state.submitPending = false; // echo received — composer unlocked
      state.runTokens = { in: 0, out: 0 };
      state.lastPrompt = ev.prompt; // for the Retry action on failure/stop
      syncSend();
      dropActivityPill();
      addUserMessage(ev.prompt);
      if (!state.replaying) { if (state.activeTab === "runs") loadRuns(false); else refreshRunsBadge(); }
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
        <div class="tl-time">${fmtEventTime(ev)}</div>`;
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
    terminal(ev) { addTerminalBlock(ev.code, ev.output, ev); },
    browser_screenshot(ev) {
      if (ev.image) setScreenshot(ev.image);
      else if (ev.image_ref) setScreenshotUrl(blobUrl(state.replayMode?.sessionId || state.sessionId, ev.image_ref));
    },
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
    auth_required(ev) {
      // Authorization handoff (e.g. Google consent): a real card with a real
      // link - never an agent-taken screenshot of the consent page.
      if (!ev.url) return;
      clearWelcome();
      const el = document.createElement("div");
      el.className = "auth-card";
      el.setAttribute("role", "alert");
      el.innerHTML = `
        <div class="auth-icon">${icon("i-plug")}</div>
        <div class="auth-body">
          <div class="auth-title">Authorization needed${ev.tool ? ` — ${esc(ev.tool)}` : ""}</div>
          <div class="auth-text">This step needs your sign-in to continue. Complete the consent in the browser, then ask me to retry.</div>
          <a class="auth-open" href="${esc(ev.url)}" target="_blank" rel="noopener">Open authorization page</a>
          <div class="auth-note">Also opened automatically in this Mac's default browser.</div>
        </div>`;
      messages.appendChild(el);
      keepPillLast();
      scrollDown(messages);
    },
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
        finalizeActivityPill(false, ev);
        state.runOutcome = "stopped";
      }
    },
    error(ev) { addError(ev.message); state.runOutcome = "failed"; },
    run_end(ev) {
      setRunning(false);
      state.submitPending = false;
      syncSend();
      finalizeActivityPill(state.runOutcome !== "stopped", ev);
      addRunReport(ev);
      // UX-002: freeze the header clock at the true run duration (replay-safe).
      if (ev && ev.ts) $("elapsedStat").textContent = fmtEventTime(ev);
      state.runOutcome = "";
      if (!state.replaying) { if (state.activeTab === "runs") loadRuns(false); else refreshRunsBadge(); }
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
        <div class="tl-time" title="${esc(absoluteTime(ev.ts || Date.now()))}">${fmtEventTime(ev)}</div>`;
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
    $("editorSidebar").innerHTML = "";
    $("filesList").innerHTML = `<div class="pane-empty">Files the agent produces will appear here.</div>`;
    $("stepStat").textContent = "Step 0/0";
    $("elapsedStat").textContent = "0:00";
    $("tokenStat").textContent = "0 in / 0 out";
    $("liveLabel").textContent = "Idle";
    input.value = "";
    autosize();
    clearUploads();
    state.toolCards.clear();
    state.activityPill = null;
    state.actionCount = 0;
    state.activeFile = null;
    state.zoom = 1;
    state.tokens = { in: 0, out: 0 };
    state.runTokens = { in: 0, out: 0 };
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
      if (state.replayMode) return; // a historical run is on screen; the live view rebuilds on exit
      const h = HANDLERS[msg.type];
      if (h) { try { h(msg.data || {}); } catch (err) { console.error("event handler failed", err); } }
    };
    ws.onclose = (ev) => {
      if (state.ws && state.ws !== ws) return; // a newer socket already replaced this one
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
    if (state.readOnly) {
      showToast("Historical sessions are read-only. Click New to start a task.", "info");
      return;
    }
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
    if (decision.action === "block-pending") {
      // UX-001: the pending window used to swallow re-submits silently — say why.
      showToast("Run request already sent — the agent is starting…", "info", 2500);
      return;
    }
    if (decision.action === "block-running") return;
    let prompt = input.value.trim();
    if (state.uploaded.length) {
      // Point the agent at the files it just received (they live in the workspace).
      const listing = state.uploaded.map((f) => `- ${f.path} (${f.name})`).join("\n");
      prompt = `${prompt}\n\nAttached files available in the workspace:\n${listing}`;
    }
    if (!send({ type: "run", prompt })) {
      addError("Send failed — prompt kept. Check the connection banner.");
      return;
    }
    state.submitPending = true;
    syncSend();
    if (decision.clearInput) { input.value = ""; autosize(); }
    clearUploads();
  }

  // ---------------------------------------------------------- file uploads
  function renderUploadChips() {
    const wrap = $("uploadChips");
    wrap.classList.toggle("hidden", !state.uploaded.length);
    wrap.innerHTML = state.uploaded
      .map(
        (f, i) => `
        <span class="upload-chip" title="${esc(f.path)}">
          <svg class="ic" aria-hidden="true"><use href="#i-paperclip"/></svg>
          <span class="upload-chip-name">${esc(f.name)}</span>
          <button class="upload-chip-x" data-i="${i}" aria-label="Remove ${esc(f.name)}">&times;</button>
        </span>`
      )
      .join("");
    wrap.querySelectorAll(".upload-chip-x").forEach(
      (btn) => (btn.onclick = () => { state.uploaded.splice(+btn.dataset.i, 1); renderUploadChips(); })
    );
  }

  function clearUploads() {
    state.uploaded = [];
    $("fileInput").value = "";
    renderUploadChips();
  }

  $("btnUpload").onclick = () => $("fileInput").click();
  $("fileInput").onchange = async (e) => {
    const files = [...e.target.files];
    if (!files.length) return;
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f, f.name));
    try {
      const res = await fetch("/api/upload", { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok) { showToast(data.detail || "Upload failed", "error"); return; }
      state.uploaded.push(...data.files);
      renderUploadChips();
      showToast(`${data.files.length} file${data.files.length === 1 ? "" : "s"} attached — they'll be available to the agent in the workspace`, "info", 2500);
    } catch {
      showToast("Upload failed (server unreachable)", "error");
    } finally {
      e.target.value = "";
    }
  };

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

  $("btnNew").onclick = () => resetToNewSession();

  // -------------------------------------------------------- dialogs (A11Y-003)
  // Shared modal behavior: focus moves into the dialog on open, Tab and
  // Shift+Tab cycle within it, Escape closes, and focus returns to the
  // trigger element on close. Overlays keep their role="dialog" semantics in
  // the markup; this supplies the focus half of the contract.
  function dialogFocusables(overlay) {
    const box = overlay.querySelector(".modal") || overlay;
    return [...box.querySelectorAll(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )].filter((el) => el.offsetParent !== null);
  }

  function openDialog(overlay, trigger) {
    overlay._trigger = trigger || document.activeElement;
    overlay.classList.remove("hidden");
    const first = dialogFocusables(overlay)[0];
    if (first) first.focus();
    if (!overlay._trapBound) {
      overlay._trapBound = true;
      overlay.addEventListener("keydown", (e) => {
        if (e.key !== "Tab") return;
        const els = dialogFocusables(overlay);
        if (!els.length) return;
        const firstEl = els[0], lastEl = els[els.length - 1];
        if (e.shiftKey && document.activeElement === firstEl) { e.preventDefault(); lastEl.focus(); }
        else if (!e.shiftKey && document.activeElement === lastEl) { e.preventDefault(); firstEl.focus(); }
      });
    }
  }

  function closeDialog(overlay) {
    overlay.classList.add("hidden");
    overlay.dispatchEvent(new CustomEvent("dialog:closed"));
    const trigger = overlay._trigger;
    overlay._trigger = null;
    if (trigger && document.contains(trigger)) trigger.focus();
  }

  // Escape closes the topmost open dialog, regardless of where focus sits
  // (the per-overlay Tab trap only fires while focus is inside it).
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const open = [...document.querySelectorAll(".modal-overlay:not(.hidden)")].pop();
    if (open) closeDialog(open);
  });

  // UX-006: styled in-app confirmation replacing native confirm() — resolves
  // true/false, traps focus (via openDialog), Escape/Cancel = false.
  function confirmDialog(message, { danger = false, confirmLabel = "Confirm", ariaLabel = "Confirmation" } = {}) {
    return new Promise((resolve) => {
      const overlay = document.createElement("div");
      const descriptionId = `confirm-text-${crypto.randomUUID()}`;
      overlay.className = "modal-overlay confirm-overlay";
      overlay.innerHTML = `
        <div class="modal confirm-modal" role="alertdialog" aria-modal="true" aria-label="${esc(ariaLabel)}" aria-describedby="${descriptionId}">
          <div class="confirm-text" id="${descriptionId}">${esc(message)}</div>
          <div class="form-actions">
            <button type="button" class="btn-ghost" data-x="cancel">Cancel</button>
            <button type="button" class="btn-primary${danger ? " danger" : ""}" data-x="ok">${esc(confirmLabel)}</button>
          </div>
        </div>`;
      document.body.appendChild(overlay);
      let settled = false;
      const done = (val) => {
        if (settled) return;
        settled = true;
        // Escape arrives via the global handler (already closed us); button
        // and backdrop paths close here (fires dialog:closed, guarded).
        if (!overlay.classList.contains("hidden")) closeDialog(overlay);
        overlay.remove();
        resolve(val);
      };
      overlay.addEventListener("dialog:closed", () => done(false)); // Escape/backdrop path
      overlay.querySelector('[data-x="cancel"]').onclick = () => done(false);
      overlay.querySelector('[data-x="ok"]').onclick = () => done(true);
      overlay.addEventListener("click", (e) => { if (e.target === overlay) done(false); });
      openDialog(overlay, document.activeElement);
    });
  }

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
    // A11Y-005: cards present as a radio group (visual radios already), so
    // keyboard/AT users can review and switch the active model — previously
    // mouse-only. Roving tabindex: the active card (else the first) is the
    // tab stop; arrows move between cards; Enter/Space selects.
    const cards = [];
    for (const m of modelsData.models) {
      const isActive = m.id === modelsData.active;
      const card = document.createElement("div");
      card.className = "model-card" + (isActive ? " active" : "");
      card.setAttribute("role", "radio");
      card.setAttribute("aria-checked", isActive ? "true" : "false");
      cards.push(card);
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
      const selectModel = async () => {
        if (isActive) return;
        try {
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
      card.onclick = async (e) => {
        const act = e.target.closest("[data-act]")?.dataset.act;
        if (act === "edit") { startEdit(m); return; }
        if (act === "del") {
          e.stopPropagation();
          if (!(await confirmDialog(`Delete model "${m.label}"? This cannot be undone.`, { danger: true, confirmLabel: "Delete" }))) return;
          try {
            const res = await fetch(`/api/models/${encodeURIComponent(m.id)}`, { method: "DELETE" });
            if (res.ok) { await reloadModels(); formMsg("Model deleted"); }
            else formMsg((await res.json()).detail || "Delete failed", true);
          } catch {
            formMsg("Request failed — check your connection and try again.", true);
          }
          return;
        }
        await selectModel();
      };
      list.appendChild(card);
    }
    // Roving tabindex + keyboard operation for the radio group (A11Y-005).
    const activeIdx = Math.max(0, cards.findIndex((c) => c.getAttribute("aria-checked") === "true"));
    cards.forEach((c, i) => (c.tabIndex = i === activeIdx ? 0 : -1));
    list.onkeydown = (e) => {
      const idx = cards.indexOf(e.target.closest(".model-card"));
      if (idx === -1) return;
      const move = (next) => {
        cards.forEach((c) => (c.tabIndex = -1));
        next.tabIndex = 0;
        next.focus();
      };
      if (e.key === "ArrowDown" || e.key === "ArrowRight") {
        e.preventDefault();
        move(cards[(idx + 1) % cards.length]);
      } else if (e.key === "ArrowUp" || e.key === "ArrowLeft") {
        e.preventDefault();
        move(cards[(idx - 1 + cards.length) % cards.length]);
      } else if ((e.key === "Enter" || e.key === " ") && !e.target.closest("[data-act]")) {
        e.preventDefault();
        cards[idx].click();
      }
    };
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
    $("fModelChoices").innerHTML = "";
    $("formCancel").classList.add("hidden");
    $("formHead").textContent = "Add a model";
    $("formSave").textContent = "Save model";
  }

  // Pull the provider's full model list into the Model ID datalist (the field
  // stays free-text; the list is just every model the key can see).
  $("fFetchModels").onclick = async () => {
    const btn = $("fFetchModels");
    btn.disabled = true;
    try {
      const res = await fetch("/api/models/available", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_url: $("fBaseUrl").value.trim(),
          api_key: $("fKey").value.trim(),
          id: editingId || undefined,
        }),
      });
      const data = await res.json();
      if (!res.ok) { formMsg(data.detail || "Fetch failed", true); return; }
      $("fModelChoices").innerHTML = data.models.map((m) => `<option value="${esc(m)}">`).join("");
      formMsg(data.count ? `${data.count} models available — pick from the list or type any ID` : "Provider returned no models");
      if (data.count) $("fModel").focus();
    } catch {
      formMsg("Fetch failed (server unreachable)", true);
    } finally {
      btn.disabled = false;
    }
  };

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

  function openModal(trigger) {
    openDialog(modal, trigger);
    $("modelList").innerHTML = `<div class="pane-empty">Loading models...</div>`;
    reloadModels().then(fillPresets).catch(() => {
      $("modelList").innerHTML = `<div class="pane-empty">Could not load models.</div>`;
    });
  }

  $("btnSettings").onclick = (e) => openModal(e.currentTarget);
  $("modelChip").onclick = (e) => openModal(e.currentTarget);
  $("modalClose").onclick = () => closeDialog(modal);
  modal.addEventListener("click", (e) => { if (e.target === modal) closeDialog(modal); });

  // -------------------------------------------------------- connectors + skills
  let mcpServers = [];
  let agentSkills = [];
  let connectorsLoading = false;
  let connectorsLoaded = false;
  const connFilters = { q: "", status: "" };
  const connectorPending = new Set();

  function connectorsBusy() {
    return connectorsLoading || connectorPending.size > 0;
  }

  function syncConnectorControls() {
    const busy = connectorsBusy();
    $("connRefresh").disabled = busy;
    document.querySelectorAll("#pane-connectors .mcp-card input, #pane-connectors .conn-link").forEach((control) => {
      control.disabled = busy;
    });
  }

  async function fetchMcpServers() {
    const res = await fetch("/api/mcp/servers");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return (await res.json()).servers || [];
  }

  async function fetchSkills() {
    const res = await fetch("/api/skills");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return (await res.json()).skills || [];
  }

  function mcpStatusText(s) {
    if (!s.enabled) return "disabled";
    if (s.connected_sessions > 0) {
      const tools = s.tools == null ? "?" : s.tools;
      return `${tools} tool${tools === 1 ? "" : "s"} · live in ${s.connected_sessions} session${s.connected_sessions === 1 ? "" : "s"}`;
    }
    return "enabled · connects on next run";
  }

  function connectorStatusText(kind, item) {
    if (kind === "mcp") return mcpStatusText(item);
    return item.enabled ? "enabled · applies on the next agent step" : "disabled";
  }

  function connectorDotState(kind, item) {
    if (!item.enabled) return "off";
    return kind === "mcp" && item.connected_sessions > 0 ? "on" : "idle";
  }

  function connectorMatches(item) {
    const q = connFilters.q.trim().toLowerCase();
    const haystack = [item.id, item.label, item.name, item.description, item.summary, ...(item.requires_mcp || [])]
      .filter(Boolean).join(" ").toLowerCase();
    if (q && !haystack.includes(q)) return false;
    if (connFilters.status === "on") return item.enabled;
    if (connFilters.status === "off") return !item.enabled;
    if (connFilters.status === "risky") return item.risk === "high";
    return true;
  }

  function sortedConnectorItems(items) {
    return [...items].sort((a, b) =>
      Number(b.risk === "high") - Number(a.risk === "high")
      || Number(b.enabled) - Number(a.enabled)
      || String(a.label || a.name).localeCompare(String(b.label || b.name))
    );
  }

  function connectorCard(kind, id) {
    const list = $(kind === "mcp" ? "connMcpList" : "connSkillList");
    return [...list.querySelectorAll(".mcp-card")].find((card) => card.dataset.id === id);
  }

  function updateConnectorSummary(note = "") {
    const serversOn = mcpServers.filter((item) => item.enabled).length;
    const skillsOn = agentSkills.filter((item) => item.enabled).length;
    $("connStatus").textContent = note || `${serversOn} of ${mcpServers.length} servers on · ${skillsOn} of ${agentSkills.length} skills on`;
    const badge = $("connBadge");
    badge.textContent = String(serversOn + skillsOn);
    badge.classList.toggle("hidden", !connectorsLoaded);
  }

  function clearConnectorFilters() {
    connFilters.q = "";
    connFilters.status = "";
    $("connSearch").value = "";
    document.querySelectorAll("[data-conn-filter]").forEach((chip) => {
      const active = chip.dataset.connFilter === "";
      chip.classList.toggle("active", active);
      chip.setAttribute("aria-pressed", String(active));
    });
    renderConnectors();
  }

  function scrollToConnector(kind, id) {
    let card = connectorCard(kind, id);
    if (!card) {
      clearConnectorFilters();
      card = connectorCard(kind, id);
    }
    if (!card) return;
    card.scrollIntoView({ block: "nearest", inline: "nearest" });
    card.classList.add("highlight");
    card.focus({ preventScroll: true });
    setTimeout(() => card.classList.remove("highlight"), 1600);
  }

  function setConnectorRowStatus(card, text, isError = false, retry = null) {
    const status = card?.querySelector(".mcp-status");
    if (!status) return;
    status.textContent = text;
    status.classList.toggle("error", isError);
    if (retry) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn-ghost sm conn-retry";
      button.textContent = "Retry";
      button.onclick = retry;
      status.appendChild(button);
    }
  }

  function createConnectorCard(item, kind) {
    const label = item.label || item.name;
    const risky = item.risk === "high";
    const state = connectorDotState(kind, item);
    const description = item.description || (item.id === "rea" ? "Reverse-engineering toolkit · large tool catalog · approval required" : "");
    const card = document.createElement("div");
    card.className = `mcp-card${item.enabled ? "" : " disabled"}${risky ? " risk" : ""}`;
    card.dataset.id = item.id;
    card.dataset.kind = kind;
    card.tabIndex = -1;
    card.innerHTML = `
      <span class="mcp-dot ${state}" role="img" aria-label="${item.enabled ? "enabled" : "disabled"}"></span>
      <div class="mcp-info">
        <div class="mcp-name">${esc(label)}${item.builtin ? '<span class="mcp-badge">built-in</span>' : ""}${risky ? `<span class="mcp-badge risk" title="Large tool catalog; runtime actions require approval">${kind === "mcp" ? "Runs on host" : "Host tools"}</span>` : ""}</div>
        ${description ? `<div class="mcp-desc" title="${esc(description)}">${esc(description)}</div>` : ""}
        ${item.summary ? `<div class="mcp-sub" title="${esc(item.summary)}">${esc(item.summary)}</div>` : ""}
        <div class="mcp-status">${esc(connectorStatusText(kind, item))}</div>
      </div>
      <label class="switch" title="${item.enabled ? "Deactivate" : "Activate"} ${esc(item.id)}">
        <input type="checkbox" role="switch" aria-label="Toggle ${esc(item.id)}" ${item.enabled ? "checked" : ""} />
        <span class="slider"></span>
      </label>`;
    const toggle = card.querySelector("input");
    toggle.disabled = connectorsBusy();
    toggle.onchange = () => toggleConnector(kind, item, toggle);

    const links = document.createElement("div");
    links.className = "conn-links";
    if (kind === "skill") {
      for (const serverId of item.requires_mcp || []) {
        const link = document.createElement("button");
        link.type = "button";
        link.className = "conn-link";
        link.disabled = connectorsBusy();
        link.textContent = `Uses: ${serverId}`;
        link.onclick = () => scrollToConnector("mcp", serverId);
        links.appendChild(link);
        const server = mcpServers.find((entry) => entry.id === serverId);
        if (server && !server.enabled) {
          const note = document.createElement("span");
          note.className = "conn-needs";
          note.textContent = `Needs ${serverId} (off)`;
          links.appendChild(note);
          const enable = document.createElement("button");
          enable.type = "button";
          enable.className = "conn-link enable";
          enable.disabled = connectorsBusy();
          enable.dataset.serverId = serverId;
          enable.textContent = `Turn on ${serverId}`;
          enable.onclick = async () => {
            if (await toggleConnector("mcp", server, null, true)) scrollToConnector("mcp", serverId);
          };
          links.appendChild(enable);
        }
      }
    } else {
      for (const skillId of item.skills || []) {
        const link = document.createElement("button");
        link.type = "button";
        link.className = "conn-link";
        link.disabled = connectorsBusy();
        link.textContent = `Skill: ${skillId}`;
        link.onclick = () => scrollToConnector("skill", skillId);
        links.appendChild(link);
      }
    }
    if (links.childElementCount) card.querySelector(".mcp-info").appendChild(links);
    return card;
  }

  function renderConnectorList(kind) {
    const isMcp = kind === "mcp";
    const list = $(isMcp ? "connMcpList" : "connSkillList");
    const items = sortedConnectorItems((isMcp ? mcpServers : agentSkills).filter(connectorMatches));
    list.innerHTML = "";
    list.setAttribute("aria-busy", "false");
    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "conn-empty";
      empty.textContent = `No matching ${isMcp ? "servers" : "skills"}.`;
      const clear = document.createElement("button");
      clear.type = "button";
      clear.className = "btn-ghost sm";
      clear.textContent = "Clear filter";
      clear.onclick = clearConnectorFilters;
      empty.appendChild(clear);
      list.appendChild(empty);
      return;
    }
    for (const item of items) list.appendChild(createConnectorCard(item, kind));
  }

  function renderConnectors() {
    renderConnectorList("mcp");
    renderConnectorList("skill");
    updateConnectorSummary();
    syncConnectorControls();
  }

  function updateConnectorCard(kind, item, result) {
    const card = connectorCard(kind, item.id);
    if (card) {
      card.classList.toggle("disabled", !item.enabled);
      const dot = card.querySelector(".mcp-dot");
      dot.className = `mcp-dot ${connectorDotState(kind, item)}`;
      dot.setAttribute("aria-label", item.enabled ? "enabled" : "disabled");
      const toggle = card.querySelector("input");
      toggle.checked = item.enabled;
      toggle.disabled = connectorsBusy();
      const errors = result.errors || [];
      const applied = result.applied || 0;
      const text = item.enabled
        ? `Applied to ${applied} live session${applied === 1 ? "" : "s"}`
        : "disabled";
      setConnectorRowStatus(
        card,
        `${text}${errors.length ? ` · ${errors.length} session error${errors.length === 1 ? "" : "s"}` : ""}`,
        errors.length > 0,
        errors.length ? () => toggleConnector(kind, item, connectorCard(kind, item.id)?.querySelector("input"), item.enabled) : null
      );
    }
    if (kind === "mcp") renderConnectorList("skill");
    updateConnectorSummary(`${item.id} ${item.enabled ? "activated" : "deactivated"} · applied to ${result.applied || 0} live sessions`);
  }

  async function toggleConnector(kind, item, toggle, desired = null) {
    const want = desired == null ? toggle.checked : desired;
    const key = `${kind}:${item.id}`;
    const restoreFocus = Boolean(toggle && document.activeElement === toggle);
    if (connectorsBusy()) {
      if (toggle) toggle.checked = item.enabled;
      return false;
    }
    if (kind === "mcp" && item.risk === "high" && want) {
      if (toggle) toggle.checked = false;
      const approved = await confirmDialog(
        "REA tools can execute targets on this host, add roughly 70k tokens per step, and require your approval for runtime operations. Enable REA only for reverse-engineering tasks.",
        { danger: true, confirmLabel: "Enable REA", ariaLabel: "Enable REA connector" }
      );
      if (!approved) {
        if (toggle) toggle.checked = item.enabled;
        return false;
      }
    }
    if (connectorsBusy()) {
      if (toggle) toggle.checked = item.enabled;
      return false;
    }
    connectorPending.add(key);
    syncConnectorControls();
    if (toggle) {
      toggle.disabled = true;
      toggle.checked = want;
    }
    const endpoint = kind === "mcp" ? `/api/mcp/servers/${encodeURIComponent(item.id)}/toggle` : `/api/skills/${encodeURIComponent(item.id)}/toggle`;
    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: want }),
      });
      const data = await res.json();
      if (!res.ok) {
        if (toggle) toggle.checked = item.enabled;
        const message = data.detail || `Could not ${want ? "activate" : "deactivate"} ${item.id}`;
        setConnectorRowStatus(connectorCard(kind, item.id), message, true,
          () => toggleConnector(kind, item, connectorCard(kind, item.id)?.querySelector("input"), want));
        showToast(message, "error");
        return false;
      }
      item.enabled = want;
      updateConnectorCard(kind, item, data);
      const warn = data.errors?.length ? ` (${data.errors.length} live-session error${data.errors.length === 1 ? "" : "s"})` : "";
      showToast(`${item.id} ${want ? "activated" : "deactivated"}${kind === "mcp" && !want ? " — tools removed from live sessions" : ""}${warn}`, data.errors?.length ? "error" : "info");
      return true;
    } catch {
      if (toggle) toggle.checked = item.enabled;
      setConnectorRowStatus(connectorCard(kind, item.id), "Toggle failed · server unreachable", true,
        () => toggleConnector(kind, item, connectorCard(kind, item.id)?.querySelector("input"), want));
      showToast("Toggle failed (server unreachable)", "error");
      return false;
    } finally {
      connectorPending.delete(key);
      syncConnectorControls();
      if (toggle) {
        toggle.disabled = false;
        if (restoreFocus && document.contains(toggle)) toggle.focus({ preventScroll: true });
      }
    }
  }

  async function loadConnectors() {
    if (connectorsLoading || connectorPending.size) return;
    connectorsLoading = true;
    syncConnectorControls();
    $("connError").innerHTML = "";
    for (const id of ["connMcpList", "connSkillList"]) {
      const list = $(id);
      list.setAttribute("aria-busy", "true");
      if (!connectorsLoaded) list.innerHTML = '<div class="mcp-card conn-skeleton" aria-hidden="true"></div><div class="mcp-card conn-skeleton" aria-hidden="true"></div>';
    }
    if (!connectorsLoaded) $("connStatus").textContent = "Loading connectors…";
    try {
      [mcpServers, agentSkills] = await Promise.all([fetchMcpServers(), fetchSkills()]);
      connectorsLoaded = true;
      renderConnectors();
    } catch {
      $("connStatus").textContent = "Could not load connectors.";
      $("connError").innerHTML = '<div class="fetch-error"><span>Could not load connectors.</span><button type="button" class="btn-ghost sm" id="connRetry">Retry</button></div>';
      $("connRetry").onclick = loadConnectors;
      for (const id of ["connMcpList", "connSkillList"]) {
        const list = $(id);
        list.setAttribute("aria-busy", "false");
        if (!connectorsLoaded) list.innerHTML = "";
      }
    } finally {
      connectorsLoading = false;
      syncConnectorControls();
    }
  }

  $("connSearch").addEventListener("input", (event) => {
    connFilters.q = event.target.value;
    renderConnectors();
  });
  document.querySelectorAll("[data-conn-filter]").forEach((chip) => {
    chip.onclick = () => {
      connFilters.status = chip.dataset.connFilter;
      document.querySelectorAll("[data-conn-filter]").forEach((other) => {
        const active = other === chip;
        other.classList.toggle("active", active);
        other.setAttribute("aria-pressed", String(active));
      });
      renderConnectors();
    };
  });
  $("connRefresh").onclick = loadConnectors;
  $("btnMcp").onclick = () => switchTab("connectors", true);

  updateModelChip();

  // Keep the send button enabled only when there is text and agent is idle.
  const syncSend = () => { $("btnSend").disabled = state.readOnly || !input.value.trim() || state.running || state.submitPending; };
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

  // Manual tab activation: arrows move focus with wrap-around; Enter/Space
  // activates the focused native button without fetching data on every move.
  $("tabs").addEventListener("keydown", (e) => {
    const tabsEl = [...document.querySelectorAll("#tabs .tab[role=tab]")];
    const idx = tabsEl.indexOf(document.activeElement);
    if (idx === -1) return;
    let next = -1;
    if (e.key === "ArrowRight") next = (idx + 1) % tabsEl.length;
    if (e.key === "ArrowLeft") next = (idx - 1 + tabsEl.length) % tabsEl.length;
    if (e.key === "Home") next = 0;
    if (e.key === "End") next = tabsEl.length - 1;
    if (next >= 0) {
      e.preventDefault();
      tabsEl[next].focus();
      tabsEl[next].scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  });

  // Regression-test hooks (Playwright); inert in normal use.
  window.__om = {
    state, send, submitPrompt, switchTab, setReconnecting, loadRuns, loadConnectors, openRun, exitReplay,
    // Test-only event dispatcher: drives the same HANDLERS a real WS message
    // would, without needing a live agent run. Inert in normal use.
    dispatch: (type, data) => HANDLERS[type]?.(data || {}),
  };

  connect();
  refreshRunsBadge();
  openRunFromHash();
})();
