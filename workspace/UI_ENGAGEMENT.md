# UI Engineering Engagement — OpenManus Web Console

Scope: `workspace/webapp.py` + `workspace/static/` (the "current website build").
Protocol: triage → root cause → fix → regression test → changelog. No rewrite.

---

## 1. Component / Data-Flow Map (as-is)

```
Browser (vanilla JS, no framework, no build step)
├── index.html            DOM shell: chat panel + computer panel + model modal
├── style.css             Light theme on CSS custom properties
├── ui_utils.js           Pure helpers (testable via node:test)   [ADDED]
└── app.js                Single IIFE
    ├── state             single mutable store (session, ws, running, tab, caches)
    ├── connect()         WS client, auto-reconnect (fixed 2s)    [FIXED → backoff+banner]
    ├── HANDLERS          server-event → DOM mutation (single source of truth: events)
    ├── tabs / panes      Live, Browser, Terminal, Editor, Files
    ├── model modal       fetch /api/models* (CRUD + activate)
    ├── history drawer    fetch /api/sessions (switch = WS reconnect + replay)
    └── outputs/editor    fetch /api/outputs, /api/files, /api/file, /api/download

Server (FastAPI, single module)
├── HTTP (basic-auth)     /  /static  /api/health  /api/files  /api/file
│                         /api/outputs  /api/download  /api/gdrive/*
│                         /api/models*  /api/sessions*
├── WS /ws/{session_id}  client: run|stop|human_reply|ping
│                         server: replay + 15 event types (see webapp.py)
├── sessions dict         in-memory: AgentSession{agent, ws, event_log≤2000,
│                         touched_files, title, created/last_active}
└── instrumentation       wraps agent.think/step/execute_tool as instance attrs
                          → emits events; no core-code changes

Data flow: server events are authoritative; replay on reconnect rebuilds UI
(idempotent by reset-then-replay). fetch() endpoints are read-only views.
```

## 2. Bug Triage Log

| ID | Sev | Bug (repro) | Root cause | Fix | Test |
|---|---|---|---|---|---|
| P0-1 | P0 | Type prompt while WS disconnected → text destroyed, nothing sent, no error. Also double-Enter races a second `run`. | `submitPrompt()` cleared input before any delivery confirmation; `send()` was a silent no-op when not OPEN; no pending guard | `send()` returns success; input cleared only on success + error banner on failure; pending guard until `run_start` echo | tests/test_ui_helpers.js (decision table), tests/test_ui_dom.py (browser repro) |
| P0-2 | P0 | Stop / human_reply silently dropped while disconnected | same root cause as P0-1 | same fix + reconnect banner (P1-1) makes disconnect visible | covered by P0-1 tests |
| P1-1 | P1 | During WS drop: status pill still says "Working", elapsed timer keeps counting, reconnect retries invisibly at fixed 2s | no connection-state signal to the UI; fixed-interval retry | reconnect banner (aria-live=assertive), backoff 1s→15s capped, elapsed timer paused, live pill shows "Reconnecting" | test_ui_dom.py (banner appears when ws closes) |
| P1-2 | P1 | Live timeline / Terminal / Messages grow unboundedly → long sessions freeze tab | every event appends DOM nodes, nothing prunes | bounded buffers (400 timeline items, 150 terminal blocks) + inline "N earlier entries pruned" notice. Full virtualization deferred (see §5) | test_ui_helpers.js (prune bound) |
| P1-3 | P1 | No cost/usage metrics anywhere | LLM tracks token counts but they were never surfaced | backend emits `usage` event per think() (cumulative in/out tokens); header shows totals; run report shows per-run delta | test_webapp_smoke.py (fake agent → usage events asserted) |
| P1-4 | P1 | Control actions (stop, model switch) leave no trace | no audit events | backend emits `control` events → timeline entries (who: operator, when, what) | test_webapp_smoke.py |
| P1-5 | P1 | Files/Editor/History fetches swallow errors; no timeout; stale data renders as fresh | bare `catch {}` with no failure UI, default fetch has no timeout | 8s AbortController timeout; explicit error rows with Retry in Files/Editor sidebar; banner for history/models | test_webapp_smoke.py (404/timeout paths) |
| P1-6 | P1 | Tabs not keyboard-navigable as tablist; icon buttons unlabeled; live regions not announced; no focus ring on custom buttons | vanilla markup without ARIA | roles tablist/tab/tabpanel + arrow keys + aria-selected; aria-labels; :focus-visible ring; aria-live=polite on status region | test_ui_dom.py (roles + labels asserted) |
| P2-1 | P2 | Ops console is light-only | spec requires dark default + toggle | CSS var dark palette, toggle in header, persisted (localStorage), default dark | test_ui_dom.py (toggle + persistence) |
| P2-2 | P2 | No deep-linking; refresh/share loses session context | session id lived only in sessionStorage | `#s=<id>` hash: adopted on load, updated on switch/new | test_ui_dom.py |
| P2-3 | P2 | Relative timestamps only; no absolute on hover | fmtTime had no title | title attr with full local timestamp on timeline + file rows | manual + a11y spot check |

### Round 2 — closing mandatory-feature and reliability gaps

Round 1 covered data loss, connection truthfulness, bounded rendering, cost
metrics, audit events, fetch error states, a11y, and dark mode. This pass
re-audited against §2 (mandatory feature surface) and §4 (reliability) and
found further concrete, reproducible gaps — not polish.

| ID | Sev | Bug (repro) | Root cause | Fix | Test |
|---|---|---|---|---|---|
| P0-3 | P0 | A broken click handler or rejected promise anywhere in the app fails completely silently — no toast, no log line an operator would ever see, page just "does nothing" | no global `error`/`unhandledrejection` listeners | `window.onerror` + `unhandledrejection` handlers: console-tag with session/route, show a non-blocking toast. No Sentry configured in this deployment — documented as the honest local equivalent | test_ui_dom.py (both handlers proven to toast) |
| P1-7 | P1 | A single transient network blip on `/api/sessions`, `/api/files`, etc. fails immediately with no retry, even though §4 requires "a retry policy (with backoff, capped)" | `fetchJson` only had a timeout, no retry | automatic capped retry (2 extra attempts, `backoffDelay` from ui_utils.js) on network errors/5xx before falling back to the manual-Retry error UI; also found and fixed two *unguarded* fetches (`loadHistory`, session delete, model activate/delete) that could throw an unhandled rejection on the same failure | test_ui_helpers.js (existing backoff tests reused), manual repro (server killed mid-fetch) |
| P1-8 | P1 | Tool calls in the Live timeline show one truncated, one-line string — the mandated "structured view... Collapsible JSON viewer, not a dumped blob of text" (§2) didn't exist, even though the server already sends the full result in `tool_end` | frontend discarded `ev.result`/`ev.arguments` entirely after rendering the one-line summary | expandable per-call inspector: tool name, status+duration, pretty-printed arguments JSON, result (with server-truncation notice), each field copyable; memory-bounded via the same pruning that already bounds the timeline | test_ui_dom.py (expand/collapse, structured content asserted) |
| P1-9 | P1 | No copy-to-clipboard anywhere, despite §6 mandating it "on every ID, error message, and code block" | never implemented | `copyToClipboard()` + reusable copy button wired into: tool-inspector fields (call id/args/result), error banners, terminal code blocks, the final answer | test_ui_dom.py (clipboard content asserted after click) |
| P1-10 | P1 | A failed or stopped run offers no next step — §2 requires "a retry/resume action if applicable" for every failure | no retry path existed (pause/resume/re-run-from-step remain a genuine backend gap, but plain retry doesn't) | run report gets a "Retry" button when the outcome is failed/stopped; resends the exact original prompt through the same guarded `composerDecision` path used by the composer | test_ui_dom.py (click → real round-trip → new chat bubble with the retried prompt) |
| P2-4 | P2 | Task history has no way to find an old run by name once there are more than a handful | no filter | client-side substring filter on the history drawer (title match over what's already loaded) | test_ui_dom.py |
| P2-5 | P2 | Long timelines have no way to jump to a specific step/tool by name | no filter | client-side substring filter on the Live timeline | test_ui_dom.py |
| P2-6 | P2 | Final markdown answers can't be viewed as raw text or copied whole — §2 requires a "raw/rendered toggle" | not implemented | toolbar on every final answer: "View raw" toggle (rendered ⇄ raw `<pre>`) + copy button | covered by P1-9's copy-button test pattern; toggle exercised manually |

**Explicit tradeoff, not a fix (§0 step 5 / "say so explicitly" clause):**
Stop remains a single click with no confirmation dialog, even though §2 lists
"cancel" among destructive control actions requiring confirmation. Rationale:
this is an emergency kill-switch for a runaway agent; adding a confirmation
step to the one action an operator needs *instantly* at 3am is a worse
failure mode than an accidental stop. The new Retry action (P1-10) makes an
accidental stop cheaply recoverable (one click resends the same prompt),
which was judged a better mitigation than friction on the kill switch.
Session delete and model delete already had (and keep) `confirm()` dialogs —
those are genuinely irreversible with no retry path, so the rule applies
there without reservation.

## 3. Changelog (one logical fix at a time)

Filled in as fixes land; see §2 Fix column and git history on feat/manus-web-ui.

## 4. Verification Matrix (per §8)

| Fix | Unit | DOM/e2e | Manual repro | Console clean | A11y spot |
|---|---|---|---|---|---|
| P0-1/2 | test_ui_helpers.js | test_ui_dom.py | done via Playwright | yes | n/a |
| P1-1..6 | smoke/unit | test_ui_dom.py | server restart drill | yes | roles/labels/aria-live |
| P2-1..3 | unit (helpers) | test_ui_dom.py | visual | yes | contrast via palette choice |
| P0-3, P1-7..10, P2-4..6 (Round 2) | reused unit helpers | test_ui_dom.py (7 new cases) | live-server Playwright smoke, zero console errors | yes | aria-label on new controls, aria-live on toasts |

## 5. Known Remaining Issues (explicitly deferred, with constraints)

1. **Virtualized 10k+ run list** — no persistence layer (sessions are in-memory,
   dozens per day on a local tool); true virtualization needs a framework.
   Mitigation: bounded buffers (P1-2). Constraint: no-rewrite mandate.
2. **Pause / resume / retry / re-run-from-step** — *backend contract gap*: the
   OpenManus agent loop supports cancel only (upstream `BaseAgent.run`). Flagged
   per §4 rather than faked client-side.
3. **Diff/compare runs** — requires persisted, versioned artifacts server-side;
   not present. Deferred.
4. **Global search across runs/logs/outputs** — needs a server-side index
   across persisted runs; still deferred. *Partial mitigation shipped:*
   client-side substring filters on the history drawer and the live timeline
   (Round 2, P2-4/P2-5) cover what's already loaded in the browser — not a
   substitute for indexed search across everything ever run.
5. **Monitoring sink (Sentry et al.)** — local deployment has no monitoring
   infrastructure. *Partial mitigation shipped:* a global `window.onerror` /
   `unhandledrejection` handler (Round 2, P0-3) now tags and logs every
   client error with session/route and surfaces a toast, so nothing fails
   purely silently — but this is console + on-screen only, not a real
   ingestion/alerting pipeline.
10. **Pause / resume / re-run-from-step** remain unimplemented (see #2) —
    Round 2 added plain **Retry** (resend the same prompt) as the closest
    honest approximation of "retry/resume" that doesn't require agent-runtime
    changes.
11. **Stop has no confirmation dialog** — deliberate tradeoff, not an
    oversight; see the Round 2 triage table note above for the reasoning
    (kill-switch speed vs. accidental-stop risk, mitigated by Retry).
6. **Raw log-stream viewer with tail-follow** — Terminal pane covers tool
   output; full server-log streaming deferred.
7. **Code-splitting / lazy loading** — no build tooling; total static payload
   ~70 KB. Not applicable.
8. **Step-level deep links** — needs per-step anchors + URL grammar; session-
   level deep links shipped (P2-2), step-level deferred.
9. **WS duplicate/out-of-order delivery** — already safe: replay is a single
   atomic snapshot and handlers reset-then-render; documented here as verified.

## 6. Manual verification notes

- Reconnect drill: `kill -STOP`/`-CONT` or restart server mid-run; banner
  appears, elapsed pauses, resumes on reconnect, replay restores state.
- Keyboard drill: Tab ring reaches all controls; arrow keys move tabs;
  Enter on a history row switches sessions.
- Round 2 drill: run a task that fails (kill the model mid-run) — confirm a
  Retry button appears and resubmits the same prompt; expand a tool call in
  Live and confirm arguments/result render as real JSON, not a truncated
  string; copy each field and paste to confirm; type in both filter boxes and
  confirm non-matching rows hide without losing state.
