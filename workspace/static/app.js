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
    toolCards: new Map(), // event id -> {chatEl, tlEl}
    files: new Map(),     // path -> content cache
    activeFile: null,
    filesDirty: true,
    outputsDirty: true,
    intentionalClose: false,
    zoom: 1,
  };
  sessionStorage.setItem("openmanus_sid", state.sessionId);

  const $ = (id) => document.getElementById(id);
  const messages = $("messages");
  const timeline = $("timeline");
  const terminal = $("terminal");

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

  function addUserMessage(text) {
    clearWelcome();
    const el = document.createElement("div");
    el.className = "msg-user";
    el.innerHTML = `<div class="bubble">${esc(text)}</div>`;
    messages.appendChild(el);
    scrollDown(messages);
  }

  function addThought(content) {
    clearWelcome();
    const el = document.createElement("details");
    el.className = "thought";
    el.innerHTML = `<summary>Thought</summary><div class="thought-body">${esc(content)}</div>`;
    messages.appendChild(el);
    scrollDown(messages);
  }

  function addFinal(content) {
    clearWelcome();
    const el = document.createElement("div");
    el.className = "final";
    el.innerHTML = markdown(content);
    messages.appendChild(el);
    scrollDown(messages);
  }

  function addError(msg) {
    clearWelcome();
    const el = document.createElement("div");
    el.className = "error-banner";
    el.textContent = msg;
    messages.appendChild(el);
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
    input.focus();
    scrollDown(messages);
  }

  function addToolCard(ev) {
    clearWelcome();
    const ui = toolUI(ev.name, ev.category);
    const el = document.createElement("div");
    el.className = "tool-card";
    el.dataset.cat = ui.cat;
    el.innerHTML = `
      <div class="tool-icon">${icon(ui.ic)}</div>
      <div class="tool-body">
        <div class="tool-title">${esc(ui.label)}</div>
        <div class="tool-detail">${esc(toolDetail(ev.name, ev.arguments))}</div>
      </div>
      <div class="tool-status"><span class="spinner"></span></div>`;
    el.onclick = () => switchTab(ui.cat === "browser" ? "browser"
      : ui.cat === "terminal" ? "terminal"
      : ui.cat === "editor" ? "editor" : "live", true);
    messages.appendChild(el);
    scrollDown(messages);
    return el;
  }

  // -------------------------------------------------------- timeline
  function timelineAdd(ev) {
    const ui = toolUI(ev.name, ev.category);
    const empty = timeline.querySelector(".pane-empty");
    if (empty) empty.remove();
    const el = document.createElement("div");
    el.className = "tl-item";
    el.innerHTML = `
      <div class="tl-icon ${ui.cat}">${icon(ui.ic)}</div>
      <div class="tl-body">
        <div class="tl-title">${esc(ui.label)}</div>
        <div class="tl-detail">${esc(toolDetail(ev.name, ev.arguments))}</div>
      </div>
      <div class="tl-time">${fmtTime()}</div>
      <div class="tl-status"><span class="spinner"></span></div>`;
    timeline.appendChild(el);
    scrollDown($("pane-live"));
    return el;
  }

  // -------------------------------------------------------- tabs
  function switchTab(tab, userAction) {
    if (userAction) state.pinnedTab = true;
    state.activeTab = tab;
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
    document.querySelectorAll(".pane").forEach((p) => p.classList.toggle("active", p.id === `pane-${tab}`));
    if (tab === "editor" && state.filesDirty) refreshFiles();
    if (tab === "files" && state.outputsDirty) loadOutputs();
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
      <div class="term-code">${esc(code)}</div>
      ${output ? `<div class="term-out ${ok ? "" : "err"}">${esc(output)}</div>` : ""}`;
    terminal.appendChild(el);
    scrollDown(terminal);
    autoSwitch("terminal");
  }

  // -------------------------------------------------------- editor pane
  async function refreshFiles() {
    state.filesDirty = false;
    try {
      const res = await fetch("/api/files");
      const data = await res.json();
      const sidebar = $("editorSidebar");
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
    } catch { /* server unreachable; keep old listing */ }
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
    try {
      const res = await fetch(`/api/outputs?session=${encodeURIComponent(state.sessionId)}`);
      const data = await res.json();
      renderOutputs(data);
    } catch { /* keep previous listing on transient errors */ }
  }

  function renderOutputs(data) {
    const list = $("filesList");
    $("gdriveBanner").classList.toggle("hidden", !!data.gdrive?.enabled);
    $("filesCount").textContent = data.files.length || "";
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
          <div class="file-name"><span title="${esc(f.path)}">${esc(f.name)}</span>${f.touched ? '<span class="new-badge">New</span>' : ""}</div>
          <div class="file-meta">${fmtSize(f.size)} · ${fmtDate(f.modified)} · ${esc(f.path)}</div>
        </div>
        <div class="file-acts">
          <span class="file-status-msg"></span>
          <a class="file-act" title="Download" href="/api/download?path=${encodeURIComponent(f.path)}" download>
            ${icon("i-download")}
          </a>
          ${data.gdrive?.enabled ? `<button class="file-act gdrive" title="Save to Google Drive">${icon("i-cloud")}</button>` : ""}
        </div>`;
      const gdriveBtn = row.querySelector(".gdrive");
      if (gdriveBtn) gdriveBtn.onclick = () => uploadToGdrive(f, gdriveBtn, row.querySelector(".file-status-msg"));
      list.appendChild(row);
    }
  }

  async function uploadToGdrive(file, btn, msgEl) {
    btn.classList.add("busy");
    msgEl.textContent = "Uploading to Drive...";
    try {
      const res = await fetch("/api/gdrive/upload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: file.path }),
      });
      const data = await res.json();
      if (res.ok && data.link) {
        btn.classList.remove("busy");
        btn.classList.add("ok");
        msgEl.innerHTML = `Saved — <a href="${esc(data.link)}" target="_blank" rel="noopener">open in Drive</a>`;
      } else {
        btn.classList.remove("busy");
        btn.classList.add("err");
        msgEl.textContent = (data.detail || "Upload failed").slice(0, 120);
        setTimeout(() => { btn.classList.remove("err"); msgEl.textContent = ""; }, 6000);
      }
    } catch {
      btn.classList.remove("busy");
      btn.classList.add("err");
      msgEl.textContent = "Upload failed (server unreachable)";
    }
  }

  $("btnRefreshOutputs").onclick = loadOutputs;
  $("gdriveHow").onclick = async () => {
    const span = $("gdriveBanner").querySelector("span");
    if (span.dataset.expanded) {
      span.textContent = "Google Drive saving is not configured.";
      delete span.dataset.expanded;
      return;
    }
    try {
      const res = await fetch("/api/gdrive/status");
      const data = await res.json();
      span.textContent = data.hint;
      span.dataset.expanded = "1";
    } catch { /* ignore */ }
  };

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
    try {
      const res = await fetch("/api/sessions");
      const data = await res.json();
      const list = $("historyList");
      if (!data.sessions.length) {
        list.innerHTML = `<div class="pane-empty">No past tasks yet.</div>`;
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
          <button class="h-del" title="Delete"><svg class="ic"><use href="#i-trash"/></svg></button>`;
        item.onclick = () => switchSession(s.id);
        item.querySelector(".h-del").onclick = async (e) => {
          e.stopPropagation();
          if (!confirm("Delete this task and its history?")) return;
          await fetch(`/api/sessions/${encodeURIComponent(s.id)}`, { method: "DELETE" });
          if (s.id === state.sessionId) { resetToNewSession(); return; }
          loadHistory();
        };
        list.appendChild(item);
      }
    } catch { /* server unreachable; keep old list */ }
  }

  function switchSession(sid) {
    if (sid === state.sessionId) { closeHistory(); return; }
    state.intentionalClose = true;
    state.ws?.close();
    state.intentionalClose = false;
    state.sessionId = sid;
    sessionStorage.setItem("openmanus_sid", sid);
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
      const chatEl = addToolCard(ev);
      const tlEl = timelineAdd(ev);
      state.toolCards.set(ev.id, { chatEl, tlEl });
      autoSwitch(toolUI(ev.name, ev.category).cat);
    },
    tool_end(ev) {
      const card = state.toolCards.get(ev.id);
      if (!card) return;
      const status = ev.ok ? `${fmtDur(ev.duration_ms)} ${icon("i-check")}` : `${icon("i-x")} failed`;
      for (const el of [card.chatEl, card.tlEl]) {
        el.classList.add(ev.ok ? "ok" : "err");
        el.querySelector(".tool-status, .tl-status").innerHTML = status;
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
    },
    status(ev) {
      if (ev.status === "stopped") {
        const pill = $("livePill");
        pill.classList.add("stopped");
        $("liveLabel").textContent = "Stopped";
      }
    },
    error(ev) { addError(ev.message); },
    run_end() { setRunning(false); },
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
    state.activeFile = null;
    state.zoom = 1;
  }

  // -------------------------------------------------------- websocket
  function send(obj) {
    if (state.ws?.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify(obj));
  }

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/${state.sessionId}`);
    state.ws = ws;

    ws.onmessage = (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch { return; }
      const h = HANDLERS[msg.type];
      if (h) { try { h(msg.data || {}); } catch (err) { console.error("event handler failed", err); } }
    };
    ws.onclose = () => {
      state.ws = null;
      if (!state.intentionalClose) setTimeout(connect, 2000);
    };
    ws.onerror = () => ws.close();
  }

  // -------------------------------------------------------- UI wiring
  const input = $("promptInput");

  function submitPrompt() {
    const text = input.value.trim();
    if (!text || state.running) return;
    send({ type: "run", prompt: text });
    input.value = "";
    autosize();
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
        if (act === "del") {
          e.stopPropagation();
          if (!confirm(`Delete model "${m.label}"?`)) return;
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
  const syncSend = () => { $("btnSend").disabled = !input.value.trim() || state.running; };
  input.addEventListener("input", syncSend);
  new MutationObserver(syncSend).observe($("btnStop"), { attributes: true, attributeFilter: ["class"] });

  connect();
})();
