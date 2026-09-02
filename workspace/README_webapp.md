# OpenManus Web App

A Manus-style web interface for the OpenManus agent: a left chat panel and a
right "OpenManus's Computer" panel that streams everything the agent does in
real time.

## Features

- **Chat panel** — user messages, agent thoughts (collapsible), tool activity
  cards with live status and duration, markdown-rendered final answers.
- **Live timeline** — every step of the run as it happens, newest at bottom.
- **Browser pane** — live screenshots of the agent's browser session with URL
  bar and zoom controls.
- **Terminal pane** — each `python_execute` call rendered as a terminal block
  (code + output, errors in red).
- **Editor pane** — workspace file tree plus a code viewer with line numbers
  that auto-opens files as the agent edits them.
- **Files pane** — every output file the agent produces (webapp runtime files
  excluded), with type icons, size/date, a "New" badge for files touched in
  the current session, one-click **download**, and optional **Save to
  Google Drive**.
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
- **Basic auth** — HTTP and WebSocket are protected; see configuration below.

## Quick start

```bash
cd <project root>
.venv/bin/python workspace/webapp.py
```

Then open **http://localhost:8000** (username `admin`; the password is
printed to the console unless you set it, see below).

## Configuration (environment variables)

| Variable             | Default            | Purpose                                  |
| -------------------- | ------------------ | ---------------------------------------- |
| `WEBAPP_AUTH_USER`   | `admin`            | Basic-auth username                      |
| `WEBAPP_AUTH_PASS`   | random, printed    | Basic-auth password                      |
| `WEBAPP_HOST`        | `127.0.0.1`        | Bind host                                |
| `WEBAPP_PORT`        | `8000`             | Bind port                                |
| `WEBAPP_MAX_STEPS`   | `30`               | Agent step limit per run                 |

Model APIs (all basic-auth protected): `GET /api/models`, `PUT /api/models`,
`DELETE /api/models/{id}`, `POST /api/models/active`.

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
editing. The app binds to localhost by default and enforces basic auth on
every route (including the WebSocket handshake). Set real credentials before
exposing it through any tunnel.
