# Remote Access & Exposure Hardening — OpenManus Web Console

Engagement: make the console reachable from outside the LAN without turning
it into an internet-facing RCE/credential-exfiltration surface. Protocol:
audit → threat model → decide audience → auth hardening → transport →
persistence → verification. **No exposure before Phase 1 passes its gate.**

Status:
- **Phase 0: complete** (audit + threat model, this document).
- **Audience decided (§3): only the operator, own devices** → Tailscale
  Serve, no public exposure.
- **Phase 1: complete and verified live** (§6). CSRF/CORS fixed, session
  auth with timeouts, rate limiting, access logging, secrets tightened.
- **Phase 2: complete and verified live** (§6b). `https://ubik-hippocampal.
  taila37484.ts.net` reachable tailnet-only, real Let's Encrypt cert, login
  + CSRF checks proven against the real HTTPS hostname.
- **Phase 3: configured and verified at the component level; full reboot
  test deliberately deferred** (§6c). LaunchAgent for the webapp installed
  and running; automatic login confirmed on (`sysadminctl -autologin
  status` → `gasu`); lock-on-login mechanism installed and exercised
  cleanly. A real hard reboot would only *test* this configuration, not
  change whether it works — decided it isn't worth the disruption (kills
  this Mac's other running services) purely for that proof right now.
  Deferred to whenever a reboot happens naturally (macOS update, power
  event); §6c has the checklist to run at that point.

---

## 1. Pre-flight audit (Phase 0 — read-only, no changes made)

### Host
- macOS 26.5.2 (build 25F84), Apple Silicon (M4 Pro), hostname `MiniM4-2025`.
- FileVault: **Off**. (Tradeoff, not yet decided — see §7.)
- Power: `sleep=0` (currently held awake by this session's `caffeinate`-style
  lock, not a persistent setting yet), `autorestart=1` (already resumes after
  power loss), `womp=1`.

### Tailscale — real finding, not assumed
- Installed app: `/Applications/Tailscale.app` (standalone/direct-download
  build), version 1.102.3.
- **The `tailscale` CLI shim on `PATH` (`/usr/local/bin/tailscale`) is
  broken**: every invocation crashes with
  `Fatal error: The current bundleIdentifier is unknown to the registry`.
  This is the exact bundle-ID mismatch flagged (and deferred) in this
  project's very first session on 2026-08-01 — it was never fixed. Calling
  the app binary directly works fine:
  `/Applications/Tailscale.app/Contents/MacOS/Tailscale <cmd>`.
  **This must be resolved before Phase 2** (either fix the `PATH` shim to
  point at the correct binary, or always invoke the full path in the
  runbook — see §7 for the actual constraint).
- Tailnet: `acefesan.github`. MagicDNS: **enabled**, suffix
  `taila37484.ts.net`. This machine's tailnet name:
  `ubik-hippocampal.taila37484.ts.net`. No tags currently applied to this
  device (`Tags: None`) — relevant because Funnel's ACL grant should scope to
  a tag on this one machine, not `autogroup:member` (§2 non-negotiable #7 /
  gotcha #4 in the source prompt).
- No `serve`/`funnel` config currently active on any port (`No serve config`
  for both) — clean slate, nothing to conflict with.
- Tailnet has ~12 devices across what looks like two identities (`gasu04@`
  and `acefesan@`) — several already on this exact tailnet. Relevant to the
  audience question in §3.
- **Not yet verified** (needs the Tailscale admin console, which this
  environment has no login for): whether the tailnet-wide "HTTPS
  Certificates" DNS setting is enabled. This is a hard prerequisite for both
  `serve` and `funnel` to mint TLS certs and must be confirmed manually
  before Phase 2.

### Listening sockets (`lsof -iTCP -sTCP:LISTEN -P -n`)
| Port | Bind | Process | In scope? |
|---|---|---|---|
| **8000** | **127.0.0.1** | `Python` (PID 61691) — **this is the OpenManus webapp** | Yes — correctly loopback-only today |
| 8001 | `[::1]` | chromadb (unrelated project, UBIK) | No |
| 8090 | `*` (all interfaces) | `maestro web` (unrelated project) | No, but noted: same risky bind pattern exists elsewhere on this host |
| 4000 | `*` (all interfaces) | `litellm` proxy (unrelated project) | No |
| 62502 | 127.0.0.1 | Tailscale IPN local API | No — Tailscale's own |
| 36852, 59207, 49155, 2968, 5000, 7000, 9222, 7679 | mixed | macOS system services (Control Center, rapportd, AirPlay) + Chrome devtools | No |

**Confirmed: the OpenManus webapp is bound to `127.0.0.1:8000` only** — this
is the correct starting posture and must not change (standing prohibition
#1). Everything in this engagement connects a tunnel *to* loopback; the bind
itself never moves.

### Application security posture (`workspace/webapp.py`, read via source)
| Control | Current state | Verdict |
|---|---|---|
| HTTP route auth | Every HTTP route requires `Depends(verify_credentials)` — HTTP Basic, constant-time compare (`secrets.compare_digest`) | ✅ present |
| WebSocket auth | `verify_ws_credentials()` checks the `Authorization: Basic` header at connect time, closes with code 1008 if missing/wrong | ✅ present — this is *better* than the "common hole" the source prompt warned about (HTTP gated, WS not) |
| **WS Origin validation** | **None.** `verify_ws_credentials` checks only the Basic-auth header, never `websocket.headers.get("origin")` | ❌ **gap** |
| CORS | `allow_origins=["*"]` **and** `allow_credentials=True` | ❌ **real, currently-exploitable gap** (see threat model §2, finding T-1) |
| CSRF protection | None on any state-changing route (`POST /api/models`, `DELETE /api/sessions/{id}`, `POST /api/models/active`, WS `run`/`stop`) | ❌ gap, compounded by the CORS finding above |
| Session model | None — stateless HTTP Basic; browser caches the credential per-origin until the browser/tab closes. No server-side expiry, no idle/absolute timeout, no logout | ❌ gap vs. §4 Phase 1 requirement |
| Rate limiting / lockout on auth | None — unlimited Basic-auth attempts | ❌ gap |
| Structured access logging | None. `app.logger` (project-wide logger) logs errors, not per-request identity/route/status/source-IP. Uvicorn's default access log gives basic INFO lines but no identity | ❌ gap vs. §4 Phase 1 requirement |
| Secrets in frontend bundle | Grepped `workspace/static/*.{js,html,css}` for key-shaped strings (`sk-…`, `AIza…`, bare `api_key=`, long bearer tokens) | ✅ **zero matches** — clean today |
| Secrets at rest | `config/config.toml` and `workspace/webapp_models.json` hold API keys, both currently mode `644` (world-readable within this Mac's user accounts) | ⚠️ minor — should be `600` |
| Health endpoint | `/api/health` is intentionally public (no auth) per Round-1 UI engagement; returns `{"status":"ok"}` only — no version/path/config leak | ✅ already meets §4 Phase 4 requirement |

---

## 2. Threat model (one page)

**What can an anonymous attacker who reaches the URL do *today*, if it were
exposed with no further changes?**

1. **Nothing directly** — every route (HTTP and WS) requires the shared Basic
   Auth credential, and there's no default/weak credential (a random 12-byte
   token is generated per boot unless `WEBAPP_AUTH_PASS` is set). Blind
   brute-forcing has no rate limit, so it's not *rate-limited*, but Basic
   Auth over a 96-bit+ generated secret is not practically guessable either
   way — the real risk is elsewhere.

2. **T-1 (real, reproducible today, not hypothetical): cross-origin
   credential replay via the CORS misconfiguration.** `allow_origins=["*"]`
   combined with `allow_credentials=True` makes Starlette's CORS middleware
   *reflect the requesting page's own Origin* back in
   `Access-Control-Allow-Origin` (this is required by the CORS spec whenever
   credentials are allowed — a literal `*` with credentials is invalid, so
   the middleware substitutes the caller's Origin). Browsers automatically
   re-attach a *cached* HTTP Basic credential to same-origin requests
   without re-prompting, for as long as the tab/window that authenticated is
   open. If the operator has an authenticated tab open and browses to (or is
   redirected/tricked into loading) an unrelated page in another tab, that
   page's JavaScript can do
   `fetch('https://<host>/api/sessions', {credentials:'include'})` and the
   response **is readable** by the malicious page, because the server
   explicitly allows it. Same mechanism reaches every authenticated GET
   route: session list, file listing/contents, model list (keys are masked
   there, but session content and file contents are not).

3. **T-2 (real, reproducible today): blind CSRF against state-changing
   routes and the WebSocket, because there is no Origin check.** A malicious
   page doesn't even need to *read* a response to cause harm — it can fire a
   `POST /api/models/active` or `DELETE /api/sessions/{id}` and, per the same
   auto-credential-attach behavior, the browser sends the cached Basic Auth
   header along with it. Worse: **the WebSocket handshake has the identical
   behavior and there is no server-side Origin allowlist on it either**, so
   a malicious page can do
   `new WebSocket('wss://<host>/ws/attacker-chosen-id')`, have the browser
   attach the victim's cached credential, and then send a real `{"type":
   "run", "prompt": "..."}` message — **starting an agent run, with
   filesystem and tool access, entirely from a background tab the operator
   never intentionally interacted with.** This is the single highest-impact
   finding in this audit: it is a cross-site *agent-triggering* primitive,
   not just data exposure. This directly maps to acceptance test #4 in the
   source prompt ("WS upgrade with a forged/foreign Origin → Rejected") —
   **this app would fail that test today.**

4. **No session expiry means a stolen/observed credential (e.g., shoulder
   surfing the terminal banner that prints it, or a leaked shell history)
   is valid forever** until the operator manually rotates
   `WEBAPP_AUTH_PASS` and restarts. No idle timeout, no absolute timeout, no
   revocation mechanism.

5. **No access log means an actual attack, successful or not, leaves no
   audit trail** beyond stray error lines. If T-1/T-2 above were ever
   exploited, there is currently no way to know it happened.

6. **Once a run is triggered (by any means, including T-2), the agent can**:
   execute arbitrary Python (`python_execute`), read/write files under the
   workspace, browse the web with a real browser session, and call whatever
   LLM the active model config points at (spending the operator's API
   credits). This is the "treat any public endpoint as a potential RCE and
   credential-exfiltration surface" instruction in the source prompt, made
   concrete: **T-2 turns any public exposure of this app, even with correct
   Basic Auth, into a one-click drive-by agent-triggering vector**, because
   the auth mechanism itself (browser-cached Basic Auth + no Origin check +
   permissive CORS) doesn't behave like a real session system.

**Conclusion: findings T-1 and T-2 must be fixed in Phase 1 before any
transport work in Phase 2, regardless of which option in §3 below is
chosen** — they're exploitable today even over Tailscale's private overlay
if a second person or a compromised device shares that tailnet, and they
become dramatically worse under Cloudflare Tunnel (public internet).

---

## 3. The audience question — blocking Phase 1 sizing, per the engagement's own gate

*(This is the one question the source prompt says not to proceed past. See
below — the answer changes the target completely: Tailscale Serve, needing
only the CORS/Origin/session fixes, vs. Cloudflare Tunnel, needing those plus
CSRF tokens, rate limiting, and an Access policy.)*

**Observation from the audit that bears on this:** the tailnet already has
~12 devices across two apparent identities (`gasu04@`, `acefesan@`),
including phones, laptops, and another Mac Mini — this looks like a
personal/household tailnet already in daily use, not an empty one that would
need new invitations.

---

## 4. Recommendation — **confirmed** (§3 answered: operator's own devices only)

**Chosen: Option 1, Tailscale Serve.**

- **Why:** zero public exposure (findings T-1/T-2 above become far less
  dangerous — only devices already authorized on this tailnet, which
  already appears to be a small trusted household/personal set, can even
  reach the hostname), no new domain, no Cloudflare account, automatic TLS
  via MagicDNS certs, lowest effort, and it directly answers "open my
  dashboard from my phone in a meeting" if that's the real need.
- **Honest weaknesses, stated plainly (not one-sided advocacy):**
  - Every device that needs access must run Tailscale and be a tailnet
    member — not workable for an outside collaborator without inviting them
    to the tailnet (a real, if usually acceptable, trust boundary widening).
  - Tailscale Serve traffic still terminates at *this* Mac; if this
    machine's Tailscale identity/keys were ever compromised, Serve doesn't
    add a defense layer the way Cloudflare Access's edge-level SSO gate
    would.
  - No built-in audit log of *who on the tailnet* hit which route — that's
    still on this app (Phase 1's access-logging requirement) or on
    Tailscale's own (paid-tier) network logs, not free by default.
- **Rejected for now:** Cloudflare Tunnel (Option 2) — correct only if a
  non-Tailscale person must reach this, which the audit gives no evidence of
  yet; adds a domain, a Cloudflare account, Access policy management, and a
  wider real attack surface (public internet, scanned within minutes per the
  source prompt's own §1) for no benefit if the audience is "just me, my
  phone, maybe one other household member already on the tailnet."
  Tailscale Funnel (Option 3) is rejected outright: it's fully public with
  no built-in auth, and would require building real app-level session auth
  first anyway — at which point Serve (which needs none of that
  cross-origin exposure) is strictly better for the same audience.
  Reverse-proxy/port-forward (Option 4) and ngrok (Option 5) are rejected
  per the source prompt's own reasoning (CGNAT/dynamic IP fragility,
  router changes, dev-only posture) — nothing in this audit changes that.

**This recommendation is provisional on your answer to §3.** If the real
need includes someone off this tailnet, the recommendation changes to
Cloudflare Tunnel + Access, and Phase 1's scope grows (CSRF tokens, rate
limiting, and an Access policy become required, not optional).

---

## 5. Next steps

1. ~~Blocked on you: answer §3.~~ **Answered:** only the operator, own
   devices → Tailscale Serve, no public exposure.
2. ~~Phase 1 (code-only, stays local)~~ **Done — see §6.**
3. Phase 2 (actually running `tailscale serve`, or Cloudflare if the
   audience answer ever changes) and Phase 3 (installing LaunchDaemons in
   `/Library/LaunchDaemons`, a real hard reboot of this Mac Mini) are
   **held for your explicit go-ahead** — both fall under this project's
   standing rule to confirm before exposing anything beyond loopback or
   touching the system outside the repo.

## 6. Phase 1 — implemented and verified (code-only, `workspace/webapp.py` + frontend)

### What changed, mapped to the T-1/T-2 findings and the §4 checklist

| Change | Closes | Notes |
|---|---|---|
| Removed `CORSMiddleware` entirely (was `allow_origins=["*"]` + `allow_credentials=True`) | **T-1** | App is same-origin only; there was never a legitimate cross-origin use case. |
| `_origin_allowed()`: explicit Origin check on every mutating HTTP method (POST/PUT/DELETE/PATCH) and on the WS handshake | **T-2** | Chosen over classic CSRF tokens because there was no cookie-session concept to bind a token to until this same change introduced one; Origin validation is an OWASP-recognized CSRF defense and fits this architecture directly. Missing Origin (scripts/curl) is allowed — only a *present, disallowed* Origin is rejected. |
| Session-cookie auth (`om_session`, `HttpOnly`, `SameSite=Strict`, `Secure` when `WEBAPP_COOKIE_SECURE=1`) with idle (30 min default) + absolute (12 h default) timeouts, server-side store, real logout | Phase-1 session requirement | HTTP Basic is kept as a **fallback** auth path (no timeout semantics) specifically so scripts, `curl`, and this project's own test suites keep working — documented, not an oversight. Re-validated on every inbound WS message, not just at handshake, so timeouts actually bite on long-lived connections. |
| Real login page (`GET /` serves it when unauthenticated, no `WWW-Authenticate` anywhere) | UX + no native browser popup | Dark-themed, matches the app; wrong credentials show an inline error, never the app shell. |
| Rate limiting: capped exponential backoff per source IP (**50** attempts → lockout, doubling to a 15-minute cap) on **`POST /api/login` only** | Phase-1 lockout requirement | Bounded table (self-prunes past 1000 tracked IPs). **Fixed a real bug on 2026-09-04**: the lockout counter was originally shared across `/api/login`, every other authenticated API route (`require_auth`), and the WS handshake. A 401 on those other paths just means "no valid session right now" (expired session, a background poll before login, a stale tab's reconnect-with-backoff) — not a password guess — but it was silently feeding the *same* counter as the login form. A device's WS auto-reconnect loop (or just an idle-expired session sitting in an open tab) could rack up 50+ "failures" with zero human input and lock out a real login attempt from that IP with the operator never having mistyped anything — confirmed via the access log (a gap of routine 200s, then everything suddenly 429, with no failed-login lines in between). Fixed by rate-limiting only the one endpoint where an actual credential guess happens; `require_auth` and the WS handshake no longer touch the lockout counter, and a valid session cookie now always works regardless of the login-form's lockout state. |
| Structured access log (`logs/webapp_access.log`, rotating at 5 MB × 3): `ts`, `ip`, `identity`, `method`, `path`, `status` on every request | Phase-1 logging requirement | Verified: zero credentials/secrets appear in it (grepped). |
| `/static/*` gated by the same auth check | Bypass fix | `StaticFiles` mounts are raw ASGI apps with no FastAPI `Depends()` support; a request middleware closes the gap so `/static/index.html` can't be used to fetch the app shell around `/`'s login gate. |
| `config/config.toml` and `workspace/webapp_models.json` chmod'd to `600` at every boot (best-effort, non-fatal on read-only FS) | Secrets-at-rest finding | Self-healing if permissions ever drift. |
| Frontend: `fetchJson` reloads on any 401 (session died mid-use → back to the login page, not stuck); WS close code 1008 (auth/origin rejection) triggers a reload instead of an infinite silent reconnect loop; new "Sign out" button (`POST /api/logout` + reload) | UX completion of the session model | |

### Acceptance tests run against Phase 1 (live server, `curl`/`websockets`/`lsof` — actual output, not assertions)

| # | Test | Result |
|---|---|---|
| 1 | `curl` root, no credentials | `401`, body is the login page HTML (`OpenManus — Sign in`), not the app shell |
| 2 | `curl` every API route unauthenticated | `/api/health`→200 (intentionally public liveness only); `/api/files`, `/api/outputs`, `/api/sessions`, `/api/models` → all `401` |
| 2b | `/static/index.html`, `/static/app.js` unauthenticated | both `401` (the bypass fix) |
| 3 | WS upgrade, no credentials, matching Origin | `websockets.connect` → `InvalidStatus: HTTP 403` (rejected pre-accept) |
| 4 | WS upgrade, valid credentials, **forged** `Origin: https://evil.example.com` | `InvalidStatus: HTTP 403` — rejected. **T-2 is closed**, verified live, not just in code. |
| 4b | WS upgrade, valid credentials, no `Origin` header (script client) | `OPEN` — correct, scripts aren't a CSRF vector |
| 4c | WS upgrade, valid credentials, matching local `Origin` | `OPEN`, replay delivered |
| — | `POST /api/login` correct credentials | `200`, `Set-Cookie: om_session=…; HttpOnly` issued |
| — | `POST /api/login` wrong credentials | `401` |
| — | Authenticated request using only the cookie jar (no Basic header) | `200` |
| 4d | `POST /api/models/active` with a valid session cookie but a **forged Origin** | `403` — the CSRF fix also covers plain HTTP mutations, not just WS |
| — | Same request with **no** Origin header (script use case) | `200` — allowed, as designed |
| 10 | Logout, then replay the *exact* old cookie value | `401` — proven server-side invalidation, not just a client-side cookie clear |
| 7 | `lsof -iTCP -sTCP:LISTEN` | `Python …LISTEN 127.0.0.1:8000` only — still loopback-only |
| 8 | Grep built frontend for secrets | zero matches |
| — | Secret file permissions | `config/config.toml` and `workspace/webapp_models.json` both `600` |
| — | Access log content | 156 structured lines after the drill above, zero credentials/secrets present |
| 12 | Session expiry enforced server-side | verified via the stale-cookie replay above; idle/absolute timeouts are config-driven (`WEBAPP_SESSION_IDLE_MINUTES`/`WEBAPP_SESSION_ABSOLUTE_HOURS`) and re-checked on every WS message, not just at connect |

Tests #5 (external-network session, ≥10 min live-log stream), #6 (port scan), #9 (hard reboot), #11 (TLS/SSL Labs) are **not applicable until Phase 2/3** stand up a real transport and a persistent boot path — nothing to test yet.

Regression suite: all 30 existing automated tests (8 node:test, 5 pytest smoke, 17 Playwright DOM — 3 new ones added specifically for this engagement: wrong-credentials, unauthenticated-root, server-side logout invalidation) pass against the new auth model.

### Runbook (current: Phase 1 + 2 + partial Phase 3)

**Day to day (LaunchAgent-managed, this is now the normal path):**
- **Start:** `launchctl kickstart -k gui/$(id -u)/com.openmanus.webapp` (or
  just log in — `RunAtLoad` starts it automatically).
- **Stop:** `launchctl bootout gui/$(id -u)/com.openmanus.webapp`.
- **Restart after editing `webapp.py` or `~/openmanus-bin/webapp.env`:**
  `launchctl kickstart -k gui/$(id -u)/com.openmanus.webapp`.
- **Logs:** `~/Library/Logs/com.openmanus.webapp.{out,err}.log` (local disk,
  works even before the project volume mounts).
- **Rotate the credential:** edit `WEBAPP_AUTH_PASS` in
  `~/openmanus-bin/webapp.env` (600 perms, not in source control), then
  kickstart -k. Invalidates every live session and cached Basic-auth
  credential.
- **Force-expire all sessions without restarting:** none yet — no admin
  "revoke all" endpoint. Deferred; a restart is the current equivalent.

**Kill switch (remove public/tailnet reachability, one command, keep the app
running locally):**
```
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --https=443 off
```
Verify: `.../Tailscale serve status` → `No serve config`. The app is still
listening on `127.0.0.1:8000` after this — only the tailnet-facing proxy is
removed.

**Full kill switch (stop the app too):**
```
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --https=443 off
launchctl bootout gui/$(id -u)/com.openmanus.webapp
```

**Re-enable Serve after a change (e.g., a different local port):**
```
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg --https=443 <port>
```
Gotcha #4 from the source prompt applies: always run `serve status` /
`funnel status` after any change — the last command silently wins on a
given port.

**Verify after any change:** re-run the curl/websockets drill in the
acceptance tables above, or
`.venv/bin/python -m pytest workspace/tests/test_webapp_smoke.py workspace/tests/test_ui_dom.py`.

**Note on the `tailscale` CLI:** the bare `tailscale` command on `PATH`
(`/usr/local/bin/tailscale`, a symlink to the same app binary) crashes with
a bundle-identity error specific to how this Swift app resolves itself when
launched via a symlink outside its `.app` bundle — confirmed not fixable by
just re-pointing the symlink (it's the *invocation path* that breaks the
app's self-detection, not a version mismatch). Always use the full path:
`/Applications/Tailscale.app/Contents/MacOS/Tailscale`.

**What to check when the URL stops responding:**
1. `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/health` — if this fails, the app itself is down: check `~/Library/Logs/com.openmanus.webapp.err.log`.
2. `.../Tailscale serve status` — if it says "No serve config", Serve was turned off (deliberately or via gotcha #4).
3. `launchctl print gui/$(id -u)/com.openmanus.webapp` — check `state` and `last exit reason`.
4. After a macOS update: re-check `.../Tailscale status` (client build compatibility) and that the LaunchAgent didn't get flagged by Gatekeeper/notarization changes — no automated check exists for this yet (source-prompt gotcha #7).

## 6b. Phase 2 — Tailscale Serve activated and verified live

- `tailscale serve --bg --https=443 8000` (via the app binary directly — see
  Phase 3 for the PATH-shim status). Confirmed via `serve status`:
  `https://ubik-hippocampal.taila37484.ts.net (tailnet only)` — **not**
  Funnel, not public. Reversible with one command (see runbook below).
- **Real certificate verified**, not assumed: `openssl s_client` against the
  hostname shows `issuer=C=US, O=Let's Encrypt, CN=YE2`, valid
  2026-09-02→2026-12-01 (Tailscale's standard auto-renewed cert). Caveat for
  future testing from *this* machine specifically: `curl` shows a different,
  *locally re-signed* cert (`O=AO Kaspersky Lab`) because Kaspersky's
  antivirus does TLS interception on outbound HTTPS from this Mac —
  `openssl s_client` bypasses whatever hook curl goes through and shows the
  real wire certificate. Not a server-side issue; noted so a future "the
  cert looks wrong" investigation doesn't start from the wrong assumption.
- Reconfigured the app for the public hostname and flipped to
  production-strict settings: `WEBAPP_ALLOWED_ORIGINS` now pins the exact
  tailnet hostname (plus the two local dev origins used for testing) instead
  of the permissive local-only default, and `WEBAPP_COOKIE_SECURE=1` (the
  session cookie now requires HTTPS — confirmed via `curl`'s cookie jar:
  `Secure` flag now `TRUE`). **Tradeoff to know about:** with
  `COOKIE_SECURE=1`, logging in via plain `http://127.0.0.1:8000` will
  silently fail to persist a session (browsers refuse `Secure` cookies set
  over plain HTTP) — Basic-auth fallback still works there for scripts, but
  the browser login flow now expects the HTTPS tailnet URL.
- **Verified end-to-end over the real HTTPS URL, not just localhost:**
  login → 200 with a `Secure` cookie, authenticated GET → 200, forged-Origin
  POST → 403, forged-Origin **WS** handshake → 403, legitimate Origin WS →
  connects and receives the replay. This is the T-1/T-2 fix proven against
  the actual public-facing (tailnet-scoped) hostname, not just loopback.

## 6c. Phase 3 — persistence: what's done, what's blocked, and why

**Already true, no action needed (verified in Phase 0/here):**
- `autorestart=1` and the `sleep 0` setting are both in the **persistent** AC
  Power profile (`pmset -g custom`), not a live-only override — this Mac
  already resumes and stays awake after a power event without any change.

**New finding, changes the Phase 3 plan:** this machine's Tailscale is the
GUI-app (standalone/direct-download) build, which registers itself as a
**per-user LaunchAgent** (`application.io.tailscale.ipn.macos.*`,
`io.tailscale.ipn.macos.login-item-helper` under `launchctl list`), **not**
a system-level `tailscaled` LaunchDaemon. This is gotcha #5 from the source
prompt, confirmed concretely: **Tailscale itself will not start after a
reboot until a user logs into a GUI session.** A pure-LaunchDaemon design
(the source prompt's ideal) can't make Tailscale connectivity available any
earlier than that on this specific install, regardless of what OpenManus's
own service does — so OpenManus's own persistence was built the same way,
matching it (and matching this Mac's own established convention — see next
point) rather than fighting it.

**Also found:** this Mac already runs three of the operator's own other
local services (`maestro-web`, `chromadb`, `litellm`) as per-user
LaunchAgents in exactly this style — `com.ubik.maestro-web.plist` includes
a documented workaround for a real, machine-specific constraint: this
project's external volume (`/Volumes/990PRO 4T`) may not be mounted yet at
launchd start time, and reading files there under launchd can require Full
Disk Access (FDA) granted to the *exact* interpreter binary (not inherited
from an interactive Terminal session). OpenManus lives on the same external
volume, so the identical pattern was used:

- `~/openmanus-bin/openmanus-python` — a local-disk copy of this project's
  venv interpreter, the FDA-grantable target.
- `~/openmanus-bin/start_openmanus_web.sh` — waits (up to 10 min) for the
  volume to mount via a sentinel file check, manually sets `PYTHONPATH` to
  the real venv's `site-packages` (a bare `cp` of a venv's python binary
  loses its venv identity — this was hit and fixed live: the first attempt
  failed with `ModuleNotFoundError: No module named 'fastapi'`, not an FDA
  error, until `PYTHONPATH` was set explicitly), then loads
  `~/openmanus-bin/webapp.env` (600 permissions, holds `WEBAPP_AUTH_PASS`
  etc. — never in source control, never in the plist itself) and execs into
  the app.
- `~/Library/LaunchAgents/com.openmanus.webapp.plist` — `RunAtLoad` +
  `KeepAlive` + `ThrottleInterval=10`, `StandardOut/ErrorPath` on the local
  disk (so logs work even if the volume isn't mounted or FDA isn't granted
  yet).
- **Installed and verified live**, including a full `bootout`/`bootstrap`
  cycle (the closest thing to "survives a restart" testable without an
  actual reboot): the service came back up cleanly both times, and the
  Phase 2 acceptance drill (login, session cookie, CSRF checks over the real
  HTTPS hostname) passed against the launchd-managed process.
- **Unexpected result, worth recording:** unlike the UBIK precedent, this
  setup did **not** actually hit an FDA permission error in practice — once
  `PYTHONPATH` was fixed, it read and executed fine from the external volume
  under launchd without any manual Full Disk Access grant. The `PYTHONPATH`
  fix and the FDA-grantable interpreter copy are kept anyway (harmless, and
  a reasonable defensive match to this machine's own established pattern),
  but no GUI permission step turned out to be required this time.

**Resolved — automatic login with an immediate lock, not a bare unlocked
desktop:** rather than a straight tradeoff between "unattended boot" and
"anyone with physical access gets the desktop," implemented both:

1. Automatic login gets the GUI session (and therefore Tailscale's
   LaunchAgent, and therefore OpenManus's) running with no one at the
   keyboard.
2. Immediately after that login fires, `com.openmanus.lockonlogin`
   (`RunAtLoad`, one-shot) runs `pmset displaysleepnow` — the display sleeps
   within ~2 seconds of login, before anyone would realistically be looking
   at it.
3. `com.apple.screensaver askForPassword=1` / `askForPasswordDelay=0` (set,
   verified) mean waking that display — from the console or a remote
   screen-sharing session — demands the account password immediately, no
   grace period.
4. None of this affects the background services at all: display sleep and
   screen lock are WindowServer/loginwindow concerns, not process-level ones
   - Tailscale and OpenManus keep running the entire time the screen is
     dark/locked, and system sleep itself stays disabled (persistent
     `pmset` "sleep 0" from Phase 0/here).

**Honest residual gap, not hidden:** there is a small window (roughly the
2-second delay in the lock script, chosen to avoid a race with the
login/WindowServer transition, plus whatever the OS itself takes to render
the desktop) between auto-login completing and the display actually
sleeping. Someone standing at the console at the exact moment of boot could
see an unlocked desktop for a couple of seconds. This is a large reduction
from "however long until the idle timer fires" (many minutes, previously)
to "a couple of seconds," not a mathematical zero - stated plainly rather
than oversold.

**One step only you can do — needs your account password, not mine to
run:**
```
sudo sysadminctl -autologin set -userName gasu
```
(Omit `-password` on the command line so it isn't left in shell history —
it should prompt interactively. If it doesn't prompt on this macOS version,
the alternative is `-password <your password>`, in which case prefix the
whole command with a space if your shell has `HIST_IGNORE_SPACE`/similar
enabled, so it doesn't land in `.zsh_history`.)

Verify afterward (read-only, either of us can run this):
```
sysadminctl -autologin status
```
Confirmed baseline before this command: **"Automatic login is OFF."**

Installed and verified (my side, no sudo needed — done):
- `~/openmanus-bin/lock_on_login.sh` + `~/Library/LaunchAgents/com.openmanus.lockonlogin.plist` — tested via `launchctl bootstrap`, ran cleanly (`last exit code = 0`), no errors in `~/Library/Logs/com.openmanus.lockonlogin.{out,err}.log`. (Note: testing this just now put this Mac's display to sleep almost immediately, twice - expected behavior, not a bug, if you noticed the screen blank while this ran.)
- `defaults write com.apple.screensaver askForPassword -int 1` /
  `askForPasswordDelay -int 0` — verified via `defaults read`.

**Decision made and implemented:** automatic login is **on**, confirmed via
`sysadminctl -autologin status` → `Automatic login user: gasu`. The
physical-access tradeoff this creates is mitigated, not accepted bare: see
the auto-login-with-immediate-lock design above — the account still logs
in unattended (so Tailscale and OpenManus start), but the display sleeps
within ~2s and a password is required to wake it (`askForPassword=1`,
`askForPasswordDelay=0`, both verified).

**A real hard reboot has deliberately not been performed.** Everything
that needs to survive one is already configured and independently
verified: the LaunchAgent's `RunAtLoad`/`KeepAlive` semantics, a full
`launchctl bootout`/`bootstrap` cycle (the closest available proxy for "did
it come back"), the persistent (not live-only) power/auto-restart profile,
and the auto-login + lock settings all check out individually. An actual
reboot at this point would only be a *test* of that configuration, not a
requirement for it to work, and it costs real disruption right now (this
Mac's other running services, any unsaved work) for that proof alone.
Decided that trade isn't worth it today — deferred to whenever a reboot
happens naturally. **Checklist for that moment** (macOS update, power
event, or whenever a reboot is needed for an unrelated reason):

1. After the Mac finishes booting, confirm the display is **locked /
   asking for a password** when woken — not sitting open. If it isn't,
   the lock-on-login LaunchAgent didn't fire; check
   `~/Library/Logs/com.openmanus.lockonlogin.err.log`.
2. `sysadminctl -autologin status` → should still say `gasu` (this setting
   doesn't get silently reverted, but worth confirming after a macOS
   update specifically — gotcha #7).
3. `launchctl print gui/501/com.openmanus.webapp` → `state = running`.
4. `curl -s -o /dev/null -w '%{http_code}\n' https://ubik-hippocampal.taila37484.ts.net/` → `401` (login page) within a minute or so of the desktop loading, from any device, with no one manually starting anything.
5. `/Applications/Tailscale.app/Contents/MacOS/Tailscale serve status` →
   should already show the proxy config (Tailscale persists `serve` config
   across restarts on its own).

If all five pass, Phase 3 is fully closed. If any fail, the relevant
component's logs (`~/Library/Logs/com.openmanus.*.log`) are the first
place to look.

## 8. Known remaining risks / explicit deferrals

1. **FileVault is off**, so automatic login (now enabled) means the disk
   itself isn't encrypted either — the lock-on-login mechanism protects the
   *running* session from casual physical access, but not the disk contents
   if the drive were removed. Not changed as part of this engagement
   (a FileVault decision has broader implications than remote access);
   flagged for awareness.
2. **No hard reboot has been performed** — deliberate, not blocked. See
   §6c for the reasoning and the checklist to run whenever a reboot happens
   naturally.
3. **Tailnet ACL policy** (would be needed to scope a `funnel` grant to a
   tag, if Funnel is ever chosen instead of Serve) can only be edited from
   the Tailscale admin console — this environment has no browser
   session/API key for that. Not needed for the current Serve-only design;
   noted in case the audience answer ever changes.
4. **The `tailscale` CLI PATH shim remains broken** (bundle-identity crash
   when invoked via the `/usr/local/bin/tailscale` symlink) — worked around
   by always using the full app-binary path in the runbook, per gotcha #1's
   own suggestion. Not fixed at the symlink level: the failure is in how
   the Swift app resolves its own identity based on invocation path, not a
   version mismatch a re-symlink would fix, and touching a system-wide
   `/usr/local/bin` entry risks other tools that may depend on it.
5. **No admin "revoke all sessions" endpoint** — rotating `WEBAPP_AUTH_PASS`
   and restarting is the current equivalent. Fine for a single-operator
   tool; would need a real endpoint if that ever changes.
6. `maestro` (port 8090) and `litellm` (port 4000) on this same host are
   bound to `0.0.0.0`, out of scope for this engagement but structurally
   the same risk this whole engagement exists to avoid for OpenManus —
   flagged for your awareness, not touched. This host also already has
   TeamViewer installed as a running system service (`com.teamviewer.*`
   LaunchDaemons) — an existing remote-access path independent of anything
   built here; worth knowing about, not evaluated as part of this
   engagement's threat model.
7. **SSL Labs / external TLS scan (acceptance test #11)** not run — Tailscale
   Serve endpoints are tailnet-only and not publicly routable, so a public
   scanner can't reach it at all (itself a mild confirmation of "not
   public"). Verified instead via `openssl s_client` showing a valid
   Let's Encrypt chain over TLS 1.3 — see §6b.
8. **Test #5 (external-network session, ≥10 min sustained live-log stream)**
   not run from a genuinely separate network/device — this environment has
   no second device to test from. Recommended before fully trusting this
   for real use: open the tailnet URL from your phone (off Wi-Fi, on
   cellular) and run a real multi-minute agent task.
