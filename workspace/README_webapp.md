# OpenManus Web App

A Manus-style web interface for the OpenManus agent: a left chat panel and a
right "OpenManus's Computer" panel that streams everything the agent does in
real time.

## Features

- **Chat panel** — user messages, agent thoughts (collapsible), tool activity
  cards with live status and duration, markdown-rendered final answers.
- **Live timeline** — every step of the run as it happens, newest at bottom,
  ending with an end-of-run report (completed / stopped / failed with action,
  step, and duration stats). A **Retry** button appears on failed/stopped
  runs to resend the same prompt. Each tool call expands into a **tool-call
  inspector** (chevron button): status, duration, and the full arguments and
  result as pretty-printed, copyable JSON — not a truncated one-liner. A
  filter box narrows the timeline to matching text.
- **Browser pane** — live screenshots of the agent's browser session with URL
  bar and zoom controls.
- **Terminal pane** — each `python_execute` call rendered as a terminal block
  (code + output, errors in red).
- **Editor pane** — workspace file tree plus a code viewer with line numbers
  that auto-opens files as the agent edits them.
- **Files pane** — a clean list of every output file the agent produces
  (webapp runtime files excluded), with type icons, size/date, and one-click
  **download**. The list refreshes automatically whenever you switch to the
  tab. (Drive-upload APIs exist server-side but are not surfaced in the UI.)
- **Copy-to-clipboard** on every error message, tool-call field, terminal
  code block, and final answer. Errors and background failures always surface
  as a visible toast (never silent — see `UI_ENGAGEMENT.md`).
- **Filters** — a search box on the task-history drawer and one on the Live
  timeline narrow long lists to matching text (client-side over what's
  already loaded; not a full server-side search index).
- **Ask-human bridge** — when the agent calls `ask_human`, the question
  appears in chat with an inline reply box instead of blocking the server.
- **Authorization cards** — when a tool result is an authorization handoff
  (e.g. Google consent "ACTION REQUIRED" + URL), the run shows an attention
  card with a real link *and* the server opens the URL in the host Mac's
  browser — never an agent-taken screenshot of the consent page. Bare URLs
  in any final answer are linkified automatically.
- **Visible final answers** — the run's result renders as a labeled,
  accent-bordered "Final answer" block (raw/rendered toggle + copy), not
  just another timeline entry.
- **File uploads** — the paperclip in the composer attaches files for the
  agent: they land in `workspace/uploads/` (sanitized names, 50MB cap,
  never silently overwritten) and the prompt is annotated with their
  workspace paths so the agent can read them with its normal file tools.
- **Clean "New"** — starting a new task clears every pane (chat, timeline,
  terminal, editor tree, files, browser), all stats, the composer draft,
  and any attachments.
- **Task archiving** — every task writes a journal `.md` (description,
  status, final output, produced-files list) into the Obsidian vault at
  `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/openmanus/`
  (`WEBAPP_OBSIDIAN_TASKS_DIR` overrides), and files the agent produces
  are exported to `~/Library/Mobile Documents/com~apple~CloudDocs/openmanus_output/<task>/`
  (`WEBAPP_OUTPUT_EXPORT_DIR` overrides). Both sync via iCloud.
- **Model management** — gear icon (or the model chip in the header) opens a
  Models dialog: switch between the profiles in `config/config.toml`, add
  custom OpenAI-compatible models with your own API keys (provider presets
  included: OpenAI, Moonshot/Kimi, DeepSeek, Anthropic, Gemini, Z.AI, PPIO,
  Jiekou.AI, Ollama), edit or delete them. The **Fetch** button next to the
  Model ID field probes the provider's `/models` endpoint (server-side, via
  `POST /api/models/available`) and fills a picker with every model the key
  can see — the field stays free-text, so any model ID works. The active
  model hot-swaps into live agents and applies to new sessions. Keys are
  stored in `workspace/webapp_models.json` (gitignored), only ever shown as
  a hint, and never logged.
- **Previous Runs** — a dedicated tab lists every persisted run (each turn
  of a session is its own run) with status, date, duration, action/step counts,
  token usage, a final-answer preview, and produced files. Filter by status
  chips, sort (newest, oldest, longest, most tokens), group by day, search
  titles/prompts/answers on the server, and "Load more" through long lists.
  A summary bar shows the match count, success rate, and token total; a badge
  on the tab counts all runs and a dot shows when one is in progress.
- **Run detail & replay** — select a run for its prompt, final answer, files,
  and a paged **event inspector** (type filter, per-event copy, screenshots).
  **Open replay** renders the run through the real Live / Terminal / Editor /
  Browser panes, read-only, with an *Exit replay* banner. **Open session**
  shows the whole session as a read-only chat.
- **Run actions** — *Re-run this prompt* (new session, prompt prefilled, never
  auto-sent), *Continue from this run* (new session seeded with the prompt and
  answer as plain text — the agent's state is **not** restored), copy prompt /
  answer, export as Markdown or JSON, and delete the session (confirmed).
  Runs are deep-linkable as `#run=<session>/<run>`; going back restores scroll
  position and focus. The clock icon in the header opens this tab.
- **Disk-backed history** — sessions, run metadata, and events persist under
  `session_store/` inside the project and survive server restarts and crashes.
  Historical sessions reopen read-only; agent execution state is not restored.
- **Multi-turn** — the live agent (and its memory) persists across turns in a
  session while the server is running; follow-up prompts continue the same
  conversation. A restored historical session cannot accept new turns.
- **Refresh-safe** — events are replayed on reconnect, so reloading the page
  mid-run loses nothing.
- **Session-based sign-in** — visiting the app shows a real login page (not
  a native browser popup); sessions have idle (30 min) and absolute (12 h)
  timeouts enforced server-side, plus a Sign-out button. HTTP Basic auth is
  still accepted as a fallback for scripts/`curl`/tests. See
  `workspace/REMOTE_ACCESS_ENGAGEMENT.md` for the full auth-hardening
  writeup (CSRF/Origin checks, rate limiting, access logging).

## Quick start

```bash
cd <project root>
.venv/bin/python workspace/webapp.py
```

Then open **http://localhost:8000** (username `admin`; the password is
printed to the console unless you set it, see below).

## Configuration (environment variables)

| Variable                        | Default            | Purpose                                        |
| ------------------------------- | ------------------ | ----------------------------------------------- |
| `WEBAPP_AUTH_USER`               | `admin`            | Login username (also accepted as HTTP Basic)    |
| `WEBAPP_AUTH_PASS`               | random, printed    | Login password                                  |
| `WEBAPP_HOST`                    | `127.0.0.1`        | Bind host — never change to `0.0.0.0`; put a tunnel in front instead |
| `WEBAPP_PORT`                    | `8000`             | Bind port                                       |
| `WEBAPP_MAX_STEPS`               | `30`               | Agent step limit per run                        |
| `WEBAPP_SESSION_STORE_DIR`        | `session_store/`   | Disk-backed session/run/event history directory |
| `WEBAPP_AGENT_IDLE_MINUTES`      | `0` (off)          | Release an idle session's agent (and its MCP processes) after this long; the session stays as read-only history |
| `WEBAPP_AGENT_IDLE_CHECK_SECONDS`| `60`               | How often idle agents are swept                 |
| `WEBAPP_RUNS_PAGE_MAX` / `WEBAPP_EVENTS_PAGE_MAX` | `200` / `2000` | Server caps for `limit` on `/api/runs` and run-detail event pages |
| `WEBAPP_ALLOWED_ORIGINS`         | localhost/127.0.0.1 (any port) | CSRF/WS-Origin allowlist — set explicitly to the public hostname once a tunnel is in front |
| `WEBAPP_SESSION_IDLE_MINUTES`    | `30`               | Idle session timeout                            |
| `WEBAPP_SESSION_ABSOLUTE_HOURS`  | `12`               | Absolute session lifetime                       |
| `WEBAPP_COOKIE_SECURE`           | `0`                | Set to `1` once TLS is live in front (Tailscale Serve, etc.) |
| `WEBAPP_MODELS_FILE`             | `workspace/webapp_models.json` | UI model-registry persistence path — tests redirect this to a temp file |

Model APIs (all auth-protected): `GET /api/models`, `PUT /api/models`,
`DELETE /api/models/{id}`, `POST /api/models/active`.

### Persistent run history

The default store is `PROJECT_ROOT/session_store/` (mode 700, gitignored); set
`WEBAPP_SESSION_STORE_DIR` to relocate it. Plain files are the source of truth:

```
session_store/
├── index.json             # derived, rebuildable summary used by list/search
└── <session_id>/
    ├── session.json       # session metadata + one record per run (atomic writes)
    ├── events.jsonl       # append-only, chronological event envelopes
    └── blobs/<hash>.png   # offloaded screenshots (mode 600)
```

**APIs** (all authenticated):

| Endpoint | Purpose |
| --- | --- |
| `GET /api/runs?limit&cursor&status&q&since&until&sort` | Paginated run list from the in-memory index (previews only) plus a `summary` over every match and `total_all` |
| `GET /api/runs/{session}/{run}?offset&limit&types` | One full run and a page of its events (`total_events` for the filter) |
| `GET /api/blobs/{session}/{name}` | A stored screenshot (path-confined) |
| `GET /api/sessions` / `DELETE /api/sessions/{id}` | Session summaries / delete a session and all its files |
| `POST /api/history/prune` `{"older_than_days": N}` | Delete old sessions; sessions with a run in flight are never deleted; returns counts only |
| `GET /api/health/detail` | Per-component health with timestamp (store writable, free space, last error *class*, queue depth, dropped events, breaker state). The public `GET /api/health` stays minimal |

**Persistence behavior**

- Events are queued to a bounded per-session writer (batched appends, kept-open
  descriptor, released when idle). Terminal events (`final_result`, `status`,
  `error`, `run_end`) are fsynced; ordinary events are flushed to the OS after
  each batch. Anything already handed to the OS survives a process crash
  (SIGKILL); events still waiting in the in-memory queue at that instant
  (typically milliseconds' worth) are lost, and a power cut can additionally
  lose recent un-fsynced ordinary events. Only events inside a run are stored.
- The 2,000-event in-memory replay cap never prunes disk history. A restored
  session replays whole runs, newest first, up to that cap, and tells the client
  how many runs are shown (`session_info.runs_shown/runs_total`); every run is
  always reachable from Previous Runs.
- On startup, runs left `running` become `interrupted`, with the duration taken
  from the run's last event (not the restart time). A missing, corrupt, or stale
  `index.json` is rebuilt from the session files (stat-only check per session).
- Unknown future schema versions are skipped and never overwritten; unversioned
  records are migrated in memory to the current schema.

**Degraded disk (full / unmounted volume)** — a persistence failure never fails
or cancels an agent run. Writes retry with exponential backoff plus jitter behind
a circuit breaker (one probe when half-open). The portal shows a
*history is not being saved* banner (`history_degraded` event), `health/detail`
reports `degraded`, queued events wait in the bounded spill queue (overflow is
counted in `dropped_events`), and run completions are remembered and saved on
recovery (`history_recovered`). If the store cannot start at all, the portal
still serves and `/api/runs` answers 503.

**Privacy** — everything is redacted *before* it is written (the live stream is
never altered): `sk-…`, `GOCSPX-…`, Google/GitHub/Slack/AWS keys, JWTs, private
key blocks, bearer/authorization headers, and `api_key`/`token`/`password`/
`secret` values (also by JSON key). Add patterns with
`WEBAPP_HISTORY_REDACTION_PATTERNS='["corp-[a-z0-9]{12}"]'`. Strings over
`WEBAPP_HISTORY_MAX_EVENT_CHARS` are truncated with a marker; screenshots are
stored as blobs, never inline. The directory still holds your prompts and
answers: protect it, and note the volume may not be encrypted.

**Retention and tuning** (`WEBAPP_HISTORY_*`, all optional):

| Variable | Default | Meaning |
| --- | --- | --- |
| `MAX_AGE_DAYS` / `MAX_BYTES` | off | Prune sessions by inactivity / total size (oldest first) |
| `PRUNE_INTERVAL_MINUTES` | `60` | Background retention pass period (only if a limit is set) |
| `MIN_FREE_MB` | `100` | Free space below which health is `unhealthy` |
| `MAX_EVENT_CHARS` / `MAX_RECORD_CHARS` | `20000` / `200000` | Per-string caps for events / prompts+answers |
| `MAX_BLOB_MB` | `10` | Largest stored screenshot |
| `QUEUE_MAX` / `BATCH_MAX` | `5000` / `200` | Spill queue length / events per write |
| `RETRY_MAX` / `META_RETRY_MAX` | `5` / `2` | Attempts for event batches / foreground metadata writes |
| `BACKOFF_BASE_MS` / `BACKOFF_MAX_MS` | `200` / `5000` | Retry backoff |
| `BREAKER_THRESHOLD` / `BREAKER_RESET_MS` | `3` / `2000` | Failures to open the circuit / wait before one probe |
| `FLUSH_TIMEOUT_MS` / `WRITER_IDLE_SECONDS` | `3000` / `30` | Reader wait for queued events / idle writer shutdown |

Restored sessions are always read-only and reject new runs; deleting a session
stops its run, closes its writer, and removes its whole directory.

### Connectors tab (MCP servers and skills)

The plug icon (header) opens the **Connectors** tab. It lists every server from
`config/mcp.json` plus the built-in Browser Use entry, followed by installed
agent skills. Search and All / On / Off / Risky filters work across both lists.
Each row has a description, live status and toggle. Toggling a server persists
to `config/mcp.json` (`enabled` flag) and hot-applies to every live agent
session — disabling removes the server's tools immediately, enabling reconnects
without a new run or restart. Enabling the risky `rea` server requires explicit
confirmation; cancelling leaves it off.

Skill toggles persist to the gitignored `config/skills_state.json` and rebuild
the compact prompt index on every live agent. Disabled skills are hidden from
`skills list` and the prompt, and `skills read` rejects them. New agents read
the same state when created. A missing file means all skills are enabled;
an unreadable or malformed existing file raises an error rather than silently
re-enabling skills. `workspace/skills/connectors.json` maps skills to
related MCP server IDs; the UI offers a link and an optional enable action, but
never silently couples their states. Set `OPENMANUS_SKILLS_STATE_FILE` to move
the state file (the webapp also accepts `WEBAPP_SKILLS_STATE_FILE`, primarily
for isolated test deployments).

APIs (all auth-protected, with mutating Origin/CSRF checks):
`GET /api/mcp/servers`, `POST /api/mcp/servers/{id}/toggle`,
`GET /api/skills`, and `POST /api/skills/{name}/toggle`. Toggle bodies are
`{"enabled": true|false}`.

`config/mcp.json` entries support these optional fields beyond
`type`/`command`/`args`/`url`:

- `enabled` (bool, default `true`) — the toggle's persisted state.
- `description` (string) — the one-liner shown under the server's name.
- `env` (object) — environment variables for stdio servers, **merged over**
  the MCP SDK default environment (`PATH`, `HOME`, … are preserved). This is
  how API keys reach MCP servers; the file is gitignored, and env values are
  never echoed back through the API.

#### REA reverse-engineering connector

An optional `rea` entry registers [`morluto/rea`](https://github.com/morluto/rea)
(`rea-agents` 6.8.0, 229 tools) **disabled by default**. Toggle it on in the MCP
tab only for reverse-engineering tasks; the full catalog has significant
context cost. Its skill is vendored under
`workspace/skills/reverse-engineer-anything/` and loads on demand through the
generic `skills` tool. Installation, Node-version requirements, and the
mandatory host/runtime-approval rules are documented in
`workspace/skills/README.md` and that skill's `OPENMANUS.md`. Runtime tools can
execute targets on the host and require explicit `ask_human` approval; do not
run `rea setup` or let the agent install/repair client registrations. Native
analysis uses Ghidra 12.1.4 + JDK 21 (including a locally built macOS arm64
decompiler); GNU objdump and Rizin are also configured. Hopper, IDA, and JADX
remain uninstalled.

#### Google Workspace connector (Gmail, Chat, Calendar, Drive)

Ships pre-configured but **disabled** in `config/mcp.json` (server id
`google`, the community [`workspace-mcp`](https://github.com/taylorwilsdon/google_workspace_mcp)
package). One-time setup (~10 min):

1. Google Cloud Console → create/select a project → enable the **Gmail API**,
   **Google Calendar API**, **Google Drive API**, and **Google Chat API**.
2. OAuth consent screen → External → fill app info → add yourself as a test
   user. **Publish it to "In production"** — a screen left in "Testing"
   issues refresh tokens that expire after 7 days (this exact expiry killed
   the first token here on 2026-09).
3. Create an **OAuth client → Desktop application**; copy the client ID/secret.
4. Chat only: Google Chat API → **Configuration** tab → set app name/avatar/
   description (and note Chat needs a Workspace account, not @gmail.com).
5. Add the credentials to the `google` entry's `env` in `config/mcp.json`:
   `"GOOGLE_OAUTH_CLIENT_ID"`, `"GOOGLE_OAUTH_CLIENT_SECRET"`
   (or `"GOOGLE_CLIENT_SECRET_PATH"` pointing at the downloaded JSON).
   *(Already done on this machine — recovered from the previous setup's
   stored token file.)*
6. Consent flow: simply run any Google task in the webapp (e.g. "list my
   calendars") — the first tool call auto-opens the browser consent page
   while the agent's server process waits. Complete it **while that run is
   active** (Google shows "unverified app" for Testing screens: Advanced →
   Continue). Tokens cache to `~/.google_workspace_mcp/credentials/` and
   refresh headlessly forever after. Terminal alternative:
   `uvx workspace-mcp --single-user --tools gmail calendar drive chat --tool-tier core`
7. The connector is already toggled **on** in the Connectors tab (`enabled` in
   `config/mcp.json`).

### Google Drive saving (optional)

Enabled by dropping a service-account key at
`workspace/gdrive_service_account.json`:

1. Google Cloud Console → create a project → enable the **Google Drive API**.
2. Create a **Service Account**, download its JSON key, and save it as
   `workspace/gdrive_service_account.json` (gitignored; no restart needed).
3. Optional: to save into your own Drive, share the target folder with the
   service account's email and start the app with
   `GDRIVE_FOLDER_ID=<folder id>`.

Requires `google-api-python-client` and `google-auth`
(`uv pip install --python .venv/bin/python google-api-python-client google-auth`).
Other endpoints: `GET /api/outputs`, `GET /api/download?path=`,
`GET /api/gdrive/status`, `POST /api/gdrive/upload`.

## Architecture

```
workspace/
├── webapp.py           FastAPI backend: auth, static, file APIs, WS sessions
├── session_store.py    Run-history store: index, writer queue, retention, health
├── history_config.py   Typed WEBAPP_HISTORY_* settings
├── history_redaction.py Secret redaction, size caps, screenshot offloading
├── history_resilience.py Circuit breaker + backoff for the store
└── static/
    ├── index.html      Two-pane layout (chat + computer)
    ├── style.css       Manus-style light theme
    └── app.js          WebSocket client, event rendering, tab logic
```

- `GET /` serves the UI; `GET /api/files` and `GET /api/file?path=` expose
  workspace files (path-confined, size-capped) for the editor pane.
- `WS /ws/{session_id}` carries client messages (`run`, `stop`,
  `human_reply`) and server events (`thought`, `tool_start`/`tool_end`,
  `terminal`, `browser_screenshot`, `browser_meta`, `file_update`,
  `ask_human`, `final_result`, ...).
- The backend wraps the agent's `think()` / `step()` / `execute_tool()` as
  instance attributes to emit events — no changes to OpenManus core code.
- Browser screenshots are harvested from the screenshot user-messages that
  `BrowserContextHelper` appends during browser-driven steps.

## Security notes

The agent has unrestricted Python execution, browser control, and file
editing. The app binds to `127.0.0.1` by default and enforces authentication
on every route including the WebSocket handshake and `/static/*`. Set real
credentials (`WEBAPP_AUTH_PASS`) before exposing it through any tunnel, and
set `WEBAPP_ALLOWED_ORIGINS` + `WEBAPP_COOKIE_SECURE=1` once that tunnel
terminates TLS. See `workspace/REMOTE_ACCESS_ENGAGEMENT.md` for the full
threat model, the remote-access plan, and why the app is never bound to
`0.0.0.0`.
