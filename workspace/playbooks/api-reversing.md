# API Reversing Playbook (mitmproxy + OpenManus)

**Purpose:** turn an undocumented app's private API into an OpenAPI spec and a
FastAPI mock server, by capturing real traffic and letting the agent do the
analysis. No MCP server is involved — this is a workflow, not a package.

**Prereqs on this machine:** `mitmproxy` 12.2.3 installed via Homebrew cask
(`/opt/homebrew/bin/mitmproxy|mitmdump|mitmweb`). First interactive launch may
need a one-time Gatekeeper approval in System Settings → Privacy & Security.

---

## 1. Capture

**Option A — web app (Playwright-driven):**
1. Start the proxy: `mitmweb --listen-port 8888` (UI on http://127.0.0.1:8081).
2. Point a browser at it: Playwright MCP `browser_navigate` works fine once the
   browser trusts the proxy — launch Chrome with
   `--proxy-server=127.0.0.1:8888 --ignore-certificate-errors`
   (Playwright arg) or install the mitmproxy CA (`~/.mitmproxy`) into the
   macOS keychain for system-wide trust.
3. Drive the target site through its real flows (login, list, detail, export)
   with Playwright MCP tools. Every request/response lands in mitmweb.

**Option B — mobile app (mobile-mcp-driven):**
1. Same proxy. On the Android emulator: `adb shell settings put global http_proxy <mac-ip>:8888`.
   On the iOS simulator: Settings → Wi-Fi → network → Configure Proxy → Manual.
2. Install the mitmproxy CA on the device (visit mitm.it from the device browser).
3. Drive the app with mobile-mcp (`mobile_launch_app`, taps, swipes).
4. Note: certificate-pinned apps won't intercept — see §4.

## 2. Export

- In mitmweb: File → Export → **as HAR** (or `mitmdump -w flows.mitm` from the start).
- Deliverable lands in `workspace/recon/<target>/` (create per-target folder).

## 3. Agent analysis (the OpenManus part)

Prompt pattern after a capture exists:

> "Read `workspace/recon/<target>/capture.har`. Identify the API's base URL,
> auth scheme (headers/cookies/tokens), and every distinct endpoint with
> method, params, and response schema (infer types from samples, note
> pagination). Write: (1) `openapi.yaml` covering the observed surface,
> (2) a FastAPI mock server `mock_<target>.py` implementing those endpoints
> with the captured sample responses, (3) a one-page `API_NOTES.md` with the
> auth flow and rate-limit hints."

- The agent uses `str_replace_editor` for the artifacts and **E2B sandbox**
  (not host `python_execute`) to smoke-test the mock server — per the
  untrusted-code policy (CLAUDE.md).
- Semgrep MCP can scan the generated mock for accidental credential leakage
  from the capture (HAR files often contain live tokens/cookies — **treat
  captures as secrets**: gitignore them, never paste into logs or prompts
  beyond what the analysis needs; purge after the spec is built).

## 4. Limits & notes

- **Certificate pinning** (most banking/medical apps): mitmproxy alone won't
  suffice; the documented next step is a rooted emulator + objection/frida —
  out of scope for this playbook's default path.
- **Legality/ethics:** capture only your own accounts and apps you may test.
  HAR captures contain live credentials — see §3 handling.
- **Regenerating clients:** after `openapi.yaml` exists, the agent can also
  generate a typed Python client (httpx) for the real API in the same style.

## 5. Quick reference

```
mitmweb --listen-port 8888            # proxy + web UI (:8081)
mitmdump -w flows.mitm                # headless capture to file
mitmdump -nr flows.mitm --export-har capture.har   # convert after the fact
```
