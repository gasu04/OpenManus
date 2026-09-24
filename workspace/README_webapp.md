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
- **Model management** — gear icon (or the model chip in the header) opens a
  Models dialog: switch between the profiles in `config/config.toml`, add
  custom OpenAI-compatible models with your own API keys (provider presets
  included), edit or delete them. The active model hot-swaps into live
  agents and applies to new sessions. Keys are stored in
  `workspace/webapp_models.json` (gitignored), only ever shown as a hint,
  and never logged.
- **Task history** — the clock icon in the chat header opens a drawer listing
  past tasks (first prompt, age, running indicator). Click to switch back
  (the full conversation is replayed from the server's event log); hover to
  delete. History lives in memory, so it clears on server restart.
- **Multi-turn** — the agent (and its memory) persists across turns in a
  session; follow-up prompts continue the same conversation.
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
| `WEBAPP_ALLOWED_ORIGINS`         | localhost/127.0.0.1 (any port) | CSRF/WS-Origin allowlist — set explicitly to the public hostname once a tunnel is in front |
| `WEBAPP_SESSION_IDLE_MINUTES`    | `30`               | Idle session timeout                            |
| `WEBAPP_SESSION_ABSOLUTE_HOURS`  | `12`               | Absolute session lifetime                       |
| `WEBAPP_COOKIE_SECURE`           | `0`                | Set to `1` once TLS is live in front (Tailscale Serve, etc.) |

Model APIs (all auth-protected): `GET /api/models`, `PUT /api/models`,
`DELETE /api/models/{id}`, `POST /api/models/active`.

### MCP servers panel

The plug icon (header) opens the **MCP servers** modal: every server from
`config/mcp.json` plus the built-in Browser Use entry, each with a live
status line (disabled / connects on next run / N tools live in M sessions)
and a toggle switch. Toggling persists to `config/mcp.json` (`enabled` flag)
and hot-applies to every live agent session — disabling removes the server's
tools immediately, enabling reconnects without a new run or restart.
MCP APIs (all auth-protected): `GET /api/mcp/servers`,
`POST /api/mcp/servers/{id}/toggle` with body `{"enabled": true|false}`.

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
