"""
OpenManus Web App - a Manus-style web interface for the OpenManus agent.

Provides a two-pane UI: a left chat panel (thoughts, tool activity cards,
final answers, ask-human prompts) and a right "computer" panel (live event
timeline, browser screenshots, python terminal, file editor) streamed over
WebSocket.

Architecture:
    - FastAPI serves the static frontend and a WebSocket at /ws/{session_id}.
    - Each session owns one Manus agent, kept alive across turns so follow-up
      prompts continue the same conversation.
    - The agent's think()/step()/execute_tool() are wrapped as instance
      attributes to stream fine-grained events without touching app code.
    - Events are logged per session and replayed to reconnecting clients, so
      a page refresh mid-run loses nothing.

Configuration (environment variables):
    WEBAPP_AUTH_USER / WEBAPP_AUTH_PASS   Login credentials. If the password
                                          is unset a random one is generated
                                          and printed to console. Also
                                          accepted as HTTP Basic auth for
                                          scripts/tests (no session/timeout
                                          semantics on that path - see
                                          workspace/REMOTE_ACCESS_ENGAGEMENT.md).
    WEBAPP_HOST / WEBAPP_PORT             Bind address (default 127.0.0.1:8000).
    WEBAPP_MAX_STEPS                      Agent step limit (default 30).
    WEBAPP_ALLOWED_ORIGINS                Comma-separated Origin allowlist for
                                          CSRF/WS-Origin checks (default:
                                          http(s)://127.0.0.1 and localhost at
                                          WEBAPP_PORT). Add the public hostname
                                          here once a tunnel is in front.
    WEBAPP_SESSION_IDLE_MINUTES           Idle session timeout (default 30).
    WEBAPP_SESSION_ABSOLUTE_HOURS         Absolute session lifetime (default 12).
    WEBAPP_COOKIE_SECURE                  "1" to mark the session cookie
                                          Secure (requires HTTPS in front -
                                          set this once a tunnel terminates
                                          TLS). Default "0" for local HTTP.

Usage:
    .venv/bin/python workspace/webapp.py

Dependencies: fastapi, uvicorn (already in project requirements).
Tier 2 (failure is loud: HTTP/WS errors surface in the browser and logs).
"""

import asyncio
import base64
import json
import logging
import mimetypes
import os
import re
import secrets
import sys
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent.manus import (
    _BROWSER_USE_ARGS,
    _BROWSER_USE_COMMAND,
    _BROWSER_USE_SERVER_ID,
    Manus,
    _browser_use_env,
)
from app.config import LLMSettings, MCPSettings, config
from app.llm import LLM
from app.logger import logger
from app.schema import ToolCall

# ---------------------------------------------------------------------------
# Configuration (all overridable via environment; no hardcoded runtime values)
# ---------------------------------------------------------------------------
AUTH_USERNAME = os.environ.get("WEBAPP_AUTH_USER", "admin")
AUTH_PASSWORD = os.environ.get("WEBAPP_AUTH_PASS") or secrets.token_urlsafe(12)
HOST = os.environ.get("WEBAPP_HOST", "127.0.0.1")
PORT = int(os.environ.get("WEBAPP_PORT", "8000"))
MAX_STEPS = int(os.environ.get("WEBAPP_MAX_STEPS", "30"))
MAX_FILE_BYTES = 1_000_000       # refuse to serve files larger than this
MAX_EVENT_LOG = 2000             # per-session replay buffer
HUMAN_REPLY_TIMEOUT = 600        # seconds to wait for ask_human answer
SNAPSHOT_BYTES = 200_000         # max file content pushed in file_update events

# Origin allowlist: used for CSRF defense on state-changing HTTP routes and
# for the WebSocket handshake. This app is same-origin only (frontend and
# API share one FastAPI process) so there is no legitimate cross-origin use
# case - CORSMiddleware was previously configured as allow_origins=["*"] +
# allow_credentials=True, which is a real vulnerability (a browser
# auto-reattaches cached Basic-auth/session cookies on same-origin requests,
# and Starlette reflects the caller's Origin back when credentials are
# allowed, so *any* page could read/mutate this API - see
# workspace/REMOTE_ACCESS_ENGAGEMENT.md finding T-1/T-2). Fixed by removing
# CORS entirely and validating Origin explicitly instead.
#
# WEBAPP_ALLOWED_ORIGINS unset (local/dev default): any port on
# 127.0.0.1/localhost/::1 is accepted, regardless of which port this process
# actually bound to. This does not weaken the threat model the check exists
# for: a *remote* attacker's page can never make a browser send an Origin of
# 127.0.0.1/localhost, no matter what it does - only a page actually served
# from this machine can, whatever port it happens to be bound to. Once a
# tunnel is in front (Phase 2), set WEBAPP_ALLOWED_ORIGINS explicitly to the
# public hostname(s) and this leniency no longer applies - matching becomes
# an exact allowlist with no fallback.
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("WEBAPP_ALLOWED_ORIGINS", "").split(",")
    if o.strip()
]
_LOCAL_ORIGIN_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _origin_allowed(origin: Optional[str]) -> bool:
    """True when `origin` may talk to this app (see ALLOWED_ORIGINS comment).

    A missing Origin header (non-browser clients: curl, scripts, tests) is
    always allowed - it's not a vector for browser-mediated CSRF.
    """
    if origin is None:
        return True
    if ALLOWED_ORIGINS:
        return origin in ALLOWED_ORIGINS
    try:
        from urllib.parse import urlparse

        host = urlparse(origin).hostname
    except ValueError:
        return False
    return host in _LOCAL_ORIGIN_HOSTS

SESSION_COOKIE_NAME = "om_session"
SESSION_IDLE_SECONDS = int(os.environ.get("WEBAPP_SESSION_IDLE_MINUTES", "30")) * 60
SESSION_ABSOLUTE_SECONDS = int(os.environ.get("WEBAPP_SESSION_ABSOLUTE_HOURS", "12")) * 3600
COOKIE_SECURE = os.environ.get("WEBAPP_COOKIE_SECURE", "0") == "1"

RATE_LIMIT_MAX_ATTEMPTS = 50     # failures before lockout kicks in - high on
                                 # purpose: single-operator deployment with a
                                 # high-entropy generated password, so this
                                 # exists only as a backstop against a truly
                                 # automated/scripted attack, not to catch
                                 # normal mistyped-password retries.
RATE_LIMIT_BASE_LOCKOUT = 2.0    # seconds; doubles per additional failure
RATE_LIMIT_MAX_LOCKOUT = 900.0   # 15 minutes, hard cap

if "WEBAPP_AUTH_PASS" not in os.environ:
    print(
        f"\n{'=' * 60}\n"
        f"  Generated web app credentials (set WEBAPP_AUTH_USER /\n"
        f"  WEBAPP_AUTH_PASS env vars to override):\n"
        f"    username: {AUTH_USERNAME}\n"
        f"    password: {AUTH_PASSWORD}\n"
        f"{'=' * 60}\n",
        flush=True,
    )

WORKSPACE_ROOT = config.workspace_root.resolve()

# Secrets at rest should not be world/group-readable. Best-effort: a
# read-only filesystem or a file that doesn't exist yet must not crash boot.
for _secret_path in (PROJECT_ROOT / "config" / "config.toml", Path(__file__).parent / "webapp_models.json"):
    try:
        if _secret_path.exists():
            os.chmod(_secret_path, 0o600)
    except OSError:
        pass

# ---------------------------------------------------------------------------
# Structured access log (timestamp, source IP, identity, route, status) -
# bounded via rotation so it can never grow unbounded (Tier 2: loud on read,
# but the file itself must not be able to fill the disk).
# ---------------------------------------------------------------------------
_LOG_DIR = PROJECT_ROOT / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
access_logger = logging.getLogger("openmanus.webapp.access")
access_logger.setLevel(logging.INFO)
access_logger.propagate = False
if not access_logger.handlers:  # avoid duplicate handlers on module reload (tests import this module directly)
    _access_handler = RotatingFileHandler(_LOG_DIR / "webapp_access.log", maxBytes=5_000_000, backupCount=3)
    _access_handler.setFormatter(logging.Formatter("%(message)s"))
    access_logger.addHandler(_access_handler)


def _client_ip(conn: Any) -> str:
    """Best-effort source IP for a Request or WebSocket (never raises)."""
    client = getattr(conn, "client", None)
    return client.host if client else "unknown"


def _log_access(conn: Any, identity: str, status: int) -> None:
    """One structured line per request: never logs credentials or payloads."""
    path = conn.url.path if hasattr(conn, "url") else "-"
    method = getattr(conn, "method", "-")
    access_logger.info(
        f"ts={time.time():.0f} ip={_client_ip(conn)} identity={identity} "
        f"method={method} path={path} status={status}"
    )


# ---------------------------------------------------------------------------
# Rate limiting: brute-force protection on the login path (§4 Phase 1).
# Capped exponential backoff per source IP, bounded table size.
# ---------------------------------------------------------------------------
_failed_attempts: Dict[str, Dict[str, float]] = {}


def _is_locked_out(ip: str) -> float:
    """Seconds remaining in lockout for this IP, or 0.0 if not locked out."""
    entry = _failed_attempts.get(ip)
    if not entry:
        return 0.0
    return max(0.0, entry["locked_until"] - time.monotonic())


def _record_auth_failure(ip: str) -> None:
    entry = _failed_attempts.setdefault(ip, {"count": 0, "locked_until": 0.0})
    entry["count"] += 1
    if entry["count"] >= RATE_LIMIT_MAX_ATTEMPTS:
        step = entry["count"] - RATE_LIMIT_MAX_ATTEMPTS
        delay = min(RATE_LIMIT_MAX_LOCKOUT, RATE_LIMIT_BASE_LOCKOUT * (2**step))
        entry["locked_until"] = time.monotonic() + delay
    if len(_failed_attempts) > 1000:  # bounded table: prune stale, non-locked entries
        now = time.monotonic()
        for stale_ip in [
            k for k, v in _failed_attempts.items()
            if v["locked_until"] < now and v["count"] < RATE_LIMIT_MAX_ATTEMPTS
        ]:
            _failed_attempts.pop(stale_ip, None)


def _record_auth_success(ip: str) -> None:
    _failed_attempts.pop(ip, None)


# ---------------------------------------------------------------------------
# Session store: opaque server-side tokens (not JWTs - nothing client-side
# needs to verify, and there's no signing-key rotation complexity this way).
# In-memory by design: sessions do not need to survive a server restart for
# a single-operator local tool, and this keeps the whole auth layer
# dependency-free.
# ---------------------------------------------------------------------------
_sessions: Dict[str, Dict[str, Any]] = {}


def _prune_expired_sessions() -> None:
    now = time.monotonic()
    expired = [
        t for t, e in _sessions.items()
        if now - e["created"] > SESSION_ABSOLUTE_SECONDS or now - e["last_seen"] > SESSION_IDLE_SECONDS
    ]
    for t in expired:
        _sessions.pop(t, None)


def _create_session(username: str) -> str:
    _prune_expired_sessions()  # opportunistic: catches sessions nobody ever revisits
    token = secrets.token_urlsafe(32)
    now = time.monotonic()
    _sessions[token] = {"username": username, "created": now, "last_seen": now}
    return token


def _validate_session(token: Optional[str]) -> Optional[str]:
    """Returns the username for a live session token, else None.

    Enforces both idle and absolute timeouts; touches last_seen (sliding
    idle window) on success. Expired/unknown tokens are dropped from the
    store so they can't be reused.
    """
    if not token:
        return None
    entry = _sessions.get(token)
    if not entry:
        return None
    now = time.monotonic()
    if now - entry["created"] > SESSION_ABSOLUTE_SECONDS or now - entry["last_seen"] > SESSION_IDLE_SECONDS:
        _sessions.pop(token, None)
        return None
    entry["last_seen"] = now
    return entry["username"]


def _destroy_session(token: Optional[str]) -> None:
    if token:
        _sessions.pop(token, None)


app = FastAPI(title="OpenManus Web")


# ---------------------------------------------------------------------------
# Auth: session cookie (primary, has idle/absolute timeouts + logout) with
# HTTP Basic accepted as a back-compat fallback for scripts/curl/tests (that
# path has no timeout semantics - it's the same shared credential, just
# without a revocable session; see workspace/REMOTE_ACCESS_ENGAGEMENT.md).
# ---------------------------------------------------------------------------
def _check_basic_header(auth_header: str) -> Optional[str]:
    """Parse and verify a `Basic ...` Authorization header value.

    Returns:
        The username on success, else None. Never raises.
    """
    if not auth_header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(auth_header[len("Basic "):]).decode("utf-8")
        username, _, password = decoded.partition(":")
    except Exception:
        return None
    if secrets.compare_digest(username, AUTH_USERNAME) and secrets.compare_digest(password, AUTH_PASSWORD):
        return username
    return None


def _resolve_auth(request: Request) -> Optional[str]:
    """Resolve the caller's identity without raising (session cookie, then Basic).

    Not rate-limited: a valid session cookie must always work regardless of
    the login-form lockout state (see require_auth's docstring - the two
    used to be conflated and a lockout could block an already-logged-in
    session, which makes no sense: the cookie already proves identity).
    """
    username = _validate_session(request.cookies.get(SESSION_COOKIE_NAME))
    if username:
        return username
    username = _check_basic_header(request.headers.get("authorization", ""))
    if username:
        _record_auth_success(_client_ip(request))
        return username
    return None


def require_auth(request: Request) -> str:
    """FastAPI dependency: 401 when not authenticated.

    Deliberately does NOT touch the login rate limiter: a 401 here just
    means "no valid session/credential on this request" (an expired
    session, a background poll before login, a stray WS-adjacent fetch) -
    it is not evidence of a password guess. Bug fixed 2026-09-04: this used
    to call _record_auth_failure() on every such 401, so routine
    not-yet-authenticated traffic (page loads, model-chip polling, a stale
    tab's background requests) silently shared the same lockout counter as
    the login form and could lock out a real login attempt from the same
    IP with the operator never having mistyped anything. Rate limiting now
    lives only around POST /api/login, the one place an actual guess
    happens.

    Raises:
        HTTPException: 401 when there's no valid session or credential.
    """
    username = _resolve_auth(request)
    if username:
        request.state.identity = username
        return username
    raise HTTPException(status_code=401, detail="Authentication required")


def _resolve_ws_auth(websocket: WebSocket) -> Optional[str]:
    """Same resolution as `_resolve_auth`, adapted for the WebSocket handshake.

    Re-run on every inbound client message (not just at connect) so idle and
    absolute session timeouts actually take effect on long-lived connections.
    """
    cookie_header = websocket.headers.get("cookie", "")
    token = None
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == SESSION_COOKIE_NAME:
            token = value
            break
    username = _validate_session(token)
    if username:
        return username
    return _check_basic_header(websocket.headers.get("authorization", ""))


def verify_ws_origin(websocket: WebSocket) -> bool:
    """Reject cross-origin WebSocket handshakes (the CSRF vector in T-2).

    Non-browser clients (scripts) send no Origin header at all and are not
    subject to browser-mediated CSRF, so a missing Origin is allowed; a
    *present but disallowed* Origin is always rejected.
    """
    return _origin_allowed(websocket.headers.get("origin"))


LOGIN_PAGE_HTML = """<!doctype html>
<html data-theme="dark"><head><meta charset="utf-8"/><title>OpenManus — Sign in</title>
<style>
  :root{--bg:#161619;--panel:#1e1e22;--text:#e9e9e4;--text-dim:#a8a69f;--accent:#8b8bf7;--border:#2c2c31;--err:#f26d6d;}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg);color:var(--text);font:14px/1.4 -apple-system,BlinkMacSystemFont,sans-serif;}
  form{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:32px 28px;width:280px;}
  h1{font-size:15px;margin:0 0 18px;font-weight:650;}
  label{display:block;font-size:12px;color:var(--text-dim);margin:12px 0 4px;}
  input{width:100%;box-sizing:border-box;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:8px 10px;color:var(--text);font-size:13px;}
  input:focus{outline:2px solid var(--accent);outline-offset:1px;}
  button{width:100%;margin-top:18px;padding:9px;border:none;border-radius:8px;background:var(--accent);color:#fff;font-weight:600;cursor:pointer;font-size:13px;}
  .err{color:var(--err);font-size:12px;margin-top:10px;min-height:14px;}
</style></head>
<body>
  <form id="f">
    <h1>OpenManus</h1>
    <label for="u">Username</label><input id="u" name="username" autocomplete="username" autofocus/>
    <label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password"/>
    <button type="submit">Sign in</button>
    <div class="err" id="e" role="alert"></div>
  </form>
  <script>
    document.getElementById("f").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const errEl = document.getElementById("e");
      errEl.textContent = "";
      try {
        const res = await fetch("/api/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            username: document.getElementById("u").value,
            password: document.getElementById("p").value,
          }),
        });
        if (res.ok) { location.href = "/"; return; }
        const data = await res.json().catch(() => ({}));
        errEl.textContent = data.detail || "Sign in failed";
      } catch {
        errEl.textContent = "Sign in failed — server unreachable";
      }
    });
  </script>
</body></html>"""


class LoginPayload(BaseModel):
    username: str
    password: str


@app.post("/api/login")
async def login(payload: LoginPayload, request: Request) -> JSONResponse:
    """Verify credentials and issue a session cookie.

    Args:
        payload: username/password from the login form.
        request: used for source-IP rate limiting and access logging.

    Returns:
        200 with a Set-Cookie session token on success; 401 on bad
        credentials; 429 while the source IP is locked out from repeated
        failures.
    """
    ip = _client_ip(request)
    remaining = _is_locked_out(ip)
    if remaining > 0:
        return JSONResponse({"detail": f"Too many attempts; retry in {int(remaining)}s"}, status_code=429)
    ok = secrets.compare_digest(payload.username, AUTH_USERNAME) and secrets.compare_digest(
        payload.password, AUTH_PASSWORD
    )
    if not ok:
        _record_auth_failure(ip)
        # No explicit _log_access here: the security_middleware logs every
        # request generically (identity defaults to "-" since nothing below
        # sets request.state.identity on this failing path) - one line, not two.
        return JSONResponse({"detail": "Invalid credentials"}, status_code=401)
    _record_auth_success(ip)
    token = _create_session(payload.username)
    request.state.identity = payload.username  # so the generic access-log line is accurate
    response = JSONResponse({"status": "ok"})
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_ABSOLUTE_SECONDS,
        httponly=True,
        samesite="strict",
        secure=COOKIE_SECURE,
        path="/",
    )
    return response


@app.post("/api/logout")
async def logout(request: Request) -> JSONResponse:
    """Invalidate the current session (server-side) and clear its cookie."""
    _destroy_session(request.cookies.get(SESSION_COOKIE_NAME))
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """Origin/CSRF check on mutating routes, static-asset auth gate, and
    the structured access-log line for every request (§4 Phase 1)."""
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        if not _origin_allowed(request.headers.get("origin")):
            _log_access(request, "-", 403)
            return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
    if request.url.path.startswith("/static/") and not _resolve_auth(request):
        # StaticFiles is a raw ASGI sub-app with no FastAPI Depends() support,
        # so the mount itself can't require auth - gate it here instead.
        # Otherwise /static/index.html would serve the full app shell to an
        # unauthenticated caller, bypassing "/"'s login gate entirely.
        _log_access(request, "-", 401)
        return JSONResponse({"detail": "Authentication required"}, status_code=401)
    response = await call_next(request)
    identity = getattr(request.state, "identity", None) or "-"
    _log_access(request, identity, response.status_code)
    return response


# ---------------------------------------------------------------------------
# Static + read-only file APIs
# ---------------------------------------------------------------------------
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root(request: Request) -> Response:
    """Serve the app shell when authenticated, the login page otherwise.

    Deliberately does not raise via `Depends(require_auth)`: an anonymous
    visitor should see a real sign-in form, not a bare 401. No
    `WWW-Authenticate` header is sent anywhere in this app (root or API) so
    browsers never summon their native Basic-auth popup - `curl -u`/scripts
    can still authenticate proactively without waiting for a challenge, and
    the frontend redirects to this page itself on a 401 from any fetch.
    """
    username = _resolve_auth(request)
    if username:
        request.state.identity = username
        return FileResponse(str(STATIC_DIR / "index.html"))
    return HTMLResponse(LOGIN_PAGE_HTML, status_code=401)


@app.get("/api/health")
async def health() -> Dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "service": "OpenManus Web"}


def _safe_workspace_path(raw: str) -> Optional[Path]:
    """Resolve a request path inside WORKSPACE_ROOT, rejecting escapes.

    Args:
        raw: Client-supplied path (absolute, or relative to workspace root).

    Returns:
        Resolved absolute Path inside the workspace, or None when the path
        escapes the root or does not exist as a regular file.
    """
    try:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = WORKSPACE_ROOT / candidate
        resolved = candidate.resolve()
        resolved.relative_to(WORKSPACE_ROOT)  # raises ValueError on escape
    except (ValueError, OSError):
        return None
    if not resolved.is_file():
        return None
    return resolved


@app.get("/api/files")
async def list_files(username: str = Depends(require_auth)) -> JSONResponse:
    """Return a shallow tree of files the agent has produced in the workspace.

    Returns:
        JSONResponse with ``{"root": name, "tree": [node...]}`` where each
        node is ``{"name", "path", "type", "children"}``. Hidden dirs, caches
        and virtualenvs are skipped; listing is capped to keep payloads small.
    """
    SKIP_DIRS = {"__pycache__", "node_modules", ".venv", ".git", ".DS_Store", "static"}
    MAX_ENTRIES = 800

    def build(directory: Path, depth: int) -> List[Dict[str, Any]]:
        nodes: List[Dict[str, Any]] = []
        try:
            entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return nodes
        for entry in entries:
            if len(nodes) >= MAX_ENTRIES:
                break
            if entry.name.startswith(".") or entry.name in SKIP_DIRS:
                continue
            rel = str(entry.relative_to(WORKSPACE_ROOT))
            if entry.is_dir():
                children = build(entry, depth + 1) if depth < 5 else []
                nodes.append({"name": entry.name, "path": rel, "type": "dir", "children": children})
            elif entry.is_file() and entry.stat().st_size <= MAX_FILE_BYTES:
                nodes.append({"name": entry.name, "path": rel, "type": "file", "children": []})
        return nodes

    return JSONResponse({"root": WORKSPACE_ROOT.name, "tree": build(WORKSPACE_ROOT, 0)})


@app.get("/api/file")
async def read_file(
    path: str = Query(...), username: str = Depends(require_auth)
) -> JSONResponse:
    """Serve one workspace file's text content for the editor pane.

    Args:
        path: Absolute path or workspace-relative path.

    Returns:
        JSONResponse ``{"path", "content", "size"}`` or 404/413.
    """
    resolved = _safe_workspace_path(path)
    if resolved is None:
        raise HTTPException(status_code=404, detail="File not found in workspace")
    size = resolved.stat().st_size
    if size > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File too large")
    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Unable to read file") from exc
    return JSONResponse(
        {"path": str(resolved.relative_to(WORKSPACE_ROOT)), "content": content, "size": size}
    )


# ---------------------------------------------------------------------------
# Output files: downloadable artifacts + optional Google Drive upload
#
# "Output files" are workspace files the agent produced, excluding the web
# app's own runtime files. Drive uploads use a service-account key placed at
# workspace/gdrive_service_account.json (gitignored); see /api/gdrive/status.
# ---------------------------------------------------------------------------
WEBAPP_OWN_FILES = {
    "webapp.py",
    "run_webapp.sh",
    "README_webapp.md",
    "webapp_models.json",
    "gdrive_service_account.json",
    "example.txt",
    ".DS_Store",
}
WEBAPP_OWN_DIRS = {"static"}

GDRIVE_CREDS_FILE = Path(__file__).parent / "gdrive_service_account.json"
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "")
GDRIVE_SETUP_HINT = (
    "1) In Google Cloud Console create a project and enable the Google Drive API. "
    "2) Create a Service Account, download its JSON key and save it as "
    "workspace/gdrive_service_account.json (restart not needed). "
    "3) Optional: to save into your own Drive folder, share that folder with the "
    "service account's email and start the app with GDRIVE_FOLDER_ID=<folder id>."
)

_gdrive_service: Any = None
_gdrive_checked: bool = False


def _gdrive_enabled() -> bool:
    """True when a service-account key file exists and client libs import."""
    global _gdrive_checked
    if not GDRIVE_CREDS_FILE.is_file():
        return False
    if not _gdrive_checked:
        try:
            from googleapiclient.discovery import build  # noqa: F401
            from google.oauth2 import service_account  # noqa: F401
        except ImportError:
            return False
        _gdrive_checked = True
    return True


def _get_gdrive_service() -> Any:
    """Build (and cache) an authorized Drive v3 service from the SA key.

    Raises:
        Exception: propagation of credential/build failures, surfaced by the
            upload endpoint as a 4xx/5xx with a readable message.
    """
    global _gdrive_service
    if _gdrive_service is None:
        from googleapiclient.discovery import build
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(
            str(GDRIVE_CREDS_FILE),
            scopes=["https://www.googleapis.com/auth/drive.file"],
        )
        _gdrive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _gdrive_service


def _output_files(touched: set) -> List[Dict[str, Any]]:
    """List agent-produced files in the workspace for the Files tab.

    Args:
        touched: Session-scoped set of paths the agent touched this session.

    Returns:
        File descriptors (path, name, size, modified, touched) sorted newest
        first. The web app's own runtime files are excluded.
    """
    files: List[Dict[str, Any]] = []
    for path in WORKSPACE_ROOT.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(WORKSPACE_ROOT)
        if rel.parts[0] in WEBAPP_OWN_DIRS:
            continue
        if path.name in WEBAPP_OWN_FILES or path.name == ".DS_Store":
            continue
        if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append(
            {
                "name": path.name,
                "path": str(rel),
                "size": stat.st_size,
                "modified": int(stat.st_mtime),
                "touched": str(rel) in touched,
            }
        )
    files.sort(key=lambda f: f["modified"], reverse=True)
    return files


@app.get("/api/outputs")
async def list_outputs(
    session: str = Query(""), username: str = Depends(require_auth)
) -> JSONResponse:
    """List downloadable output files for the Files tab.

    Args:
        session: Optional WebSocket session id, used to flag files the agent
            touched during the current session.
    """
    touched = sessions.get(session).touched_files if session in sessions else set()
    return JSONResponse(
        {"files": _output_files(touched), "gdrive": {"enabled": _gdrive_enabled()}}
    )


@app.get("/api/download")
async def download_file(
    path: str = Query(...), username: str = Depends(require_auth)
) -> FileResponse:
    """Stream one workspace file as an attachment (browser download)."""
    resolved = _safe_workspace_path(path)
    if resolved is None:
        raise HTTPException(status_code=404, detail="File not found in workspace")
    media_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    return FileResponse(
        str(resolved), filename=resolved.name, media_type=media_type
    )


@app.get("/api/gdrive/status")
async def gdrive_status(username: str = Depends(require_auth)) -> JSONResponse:
    """Report whether Google Drive saving is configured, with setup help."""
    return JSONResponse({"enabled": _gdrive_enabled(), "hint": GDRIVE_SETUP_HINT})


@app.post("/api/gdrive/upload")
async def gdrive_upload(
    payload: Dict[str, str], username: str = Depends(require_auth)
) -> JSONResponse:
    """Upload one workspace output file to Google Drive.

    Args:
        payload: ``{"path": "report.md"}`` (workspace-relative or absolute).

    Returns:
        ``{"link": <webViewLink>}`` on success.
    """
    if not _gdrive_enabled():
        raise HTTPException(status_code=409, detail=GDRIVE_SETUP_HINT)
    resolved = _safe_workspace_path(payload.get("path", ""))
    if resolved is None:
        raise HTTPException(status_code=404, detail="File not found in workspace")

    from googleapiclient.http import MediaFileUpload

    def _upload() -> str:
        service = _get_gdrive_service()
        media = MediaFileUpload(str(resolved), resumable=False)
        meta: Dict[str, Any] = {"name": resolved.name}
        if GDRIVE_FOLDER_ID:
            meta["parents"] = [GDRIVE_FOLDER_ID]
        result = (
            service.files()
            .create(body=meta, media_body=media, fields="webViewLink")
            .execute(num_retries=2)
        )
        return result.get("webViewLink", "")

    try:
        # googleapiclient is sync; run it off the event loop with a hard cap.
        link = await asyncio.wait_for(asyncio.to_thread(_upload), timeout=120)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Google Drive upload timed out")
    except Exception as exc:
        _gdrive_reset()
        raise HTTPException(
            status_code=502,
            detail=f"Drive upload failed ({type(exc).__name__}): {str(exc)[:300]}",
        )
    if not link:
        raise HTTPException(status_code=502, detail="Drive upload returned no link")
    return JSONResponse({"link": link})


def _gdrive_reset() -> None:
    """Drop the cached Drive client so the next attempt rebuilds it cleanly."""
    global _gdrive_service
    _gdrive_service = None


# ---------------------------------------------------------------------------
# Session history: list and delete past conversations (in-memory, per process)
# ---------------------------------------------------------------------------
@app.get("/api/sessions")
async def list_sessions(username: str = Depends(require_auth)) -> JSONResponse:
    """List chat sessions that ran at least one task.

    Bare reconnects (page loads without a run) also create session objects;
    they are skipped so the history shows only real conversations.
    """
    items = [
        {
            "id": sid,
            "title": s.title,
            "created_at": s.created_at,
            "last_active": s.last_active,
            "running": s.is_running,
        }
        for sid, s in sessions.items()
        if s.title
    ]
    items.sort(key=lambda x: x["last_active"], reverse=True)
    return JSONResponse({"sessions": items})


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, username: str = Depends(require_auth)) -> JSONResponse:
    """Delete a session: stop a running agent, free its resources, drop history."""
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session._task and not session._task.done():
        session._task.cancel()
    agent = session.agent
    if agent is not None:
        async def _cleanup() -> None:
            try:
                await agent.cleanup()
            except Exception:
                pass

        asyncio.create_task(_cleanup())
    del sessions[session_id]
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Model registry: pick the LLM and manage custom provider keys from the UI
#
# Built-in profiles come from config/config.toml ([llm] sections). Custom
# models added in the UI are persisted to workspace/webapp_models.json, which
# is inside the gitignored workspace dir, so API keys never enter source
# control. Keys are never returned in full over the API and never logged.
# ---------------------------------------------------------------------------
MODELS_FILE = Path(__file__).parent / "webapp_models.json"

PROVIDER_PRESETS: List[Dict[str, str]] = [
    {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-4o"},
    {"label": "DeepSeek", "base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    {"label": "Anthropic", "base_url": "https://api.anthropic.com/v1/", "model": "claude-sonnet-4-5-20250929"},
    {"label": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "model": "gemini-2.0-flash"},
    {"label": "Z.AI", "base_url": "https://api.z.ai/api/paas/v4/", "model": "glm-4.5"},
    {"label": "PPIO", "base_url": "https://api.ppinfra.com/v3/openai", "model": "deepseek/deepseek-v3-0324"},
    {"label": "Jiekou.AI", "base_url": "https://api.jiekou.ai/openai", "model": "claude-sonnet-4-5-20250929"},
    {"label": "Ollama (local)", "base_url": "http://localhost:11434/v1", "model": "qwen3:8b"},
]


def _load_registry() -> Dict[str, Any]:
    """Load the persisted UI model registry (tolerates a missing/corrupt file)."""
    try:
        data = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"active": "builtin:default", "revision": 0, "custom": []}


def _save_registry(registry: Dict[str, Any]) -> None:
    """Persist the registry atomically enough for a single-process app."""
    MODELS_FILE.write_text(json.dumps(registry, indent=2), encoding="utf-8")


def _mask_key(key: str) -> str:
    """Return a non-reversible display hint for an API key."""
    if not key:
        return ""
    if len(key) <= 10:
        return "*" * len(key)
    return f"{key[:5]}...{key[-4:]}"


def _builtin_entries() -> List[Dict[str, Any]]:
    """Model profiles defined in config/config.toml, safe for UI display."""
    entries = []
    for name, settings in (config.llm or {}).items():
        entries.append(
            {
                "id": f"builtin:{name}",
                "label": f"{name} (config.toml)",
                "model": settings.model,
                "base_url": settings.base_url,
                "source": "config",
                "has_key": bool((settings.api_key or "").strip()),
                "key_hint": _mask_key(settings.api_key or ""),
                "max_tokens": settings.max_tokens,
                "temperature": settings.temperature,
            }
        )
    return entries


def _custom_entries(registry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """User-added models, with keys masked for UI display."""
    entries = []
    for entry in registry.get("custom", []):
        entries.append(
            {
                "id": entry["id"],
                "label": entry.get("label") or entry.get("model", "custom model"),
                "model": entry.get("model", ""),
                "base_url": entry.get("base_url", ""),
                "source": "user",
                "has_key": bool((entry.get("api_key") or "").strip()),
                "key_hint": _mask_key(entry.get("api_key") or ""),
                "max_tokens": entry.get("max_tokens", 8192),
                "temperature": entry.get("temperature", 0.0),
            }
        )
    return entries


def _find_entry(active_id: str) -> Optional[Dict[str, Any]]:
    """Return the raw registry entry (with key) for a custom model id."""
    for entry in _load_registry().get("custom", []):
        if entry["id"] == active_id:
            return entry
    return None


def _build_llm(entry: Dict[str, Any], revision: int) -> LLM:
    """Construct an LLM client for a custom model registry entry.

    The revision counter is baked into the LLM singleton's config_name so an
    edited entry (e.g. rotated key) always produces a fresh client instead of
    the cached previous one.

    Args:
        entry: Raw registry entry (model, base_url, api_key, ...).
        revision: Registry revision at save time.

    Returns:
        A configured LLM instance.
    """
    settings = LLMSettings(
        model=entry["model"],
        base_url=entry["base_url"],
        api_key=(entry.get("api_key") or "").strip() or "local",
        max_tokens=int(entry.get("max_tokens") or 8192),
        temperature=float(entry.get("temperature", 0.0)),
        api_type="openai",
        api_version="",
    )
    name = f"webui:{entry['id']}:{revision}"
    return LLM(config_name=name, llm_config={"default": settings, name: settings})


def _active_llm() -> LLM:
    """Resolve the currently active LLM (registry choice or config default)."""
    registry = _load_registry()
    active_id = registry.get("active") or "builtin:default"
    if active_id.startswith("builtin:"):
        profile = active_id.split(":", 1)[1]
        try:
            return LLM(config_name=profile)
        except Exception:
            return LLM(config_name="default")
    entry = _find_entry(active_id)
    if entry:
        return _build_llm(entry, int(registry.get("revision", 0)))
    return LLM(config_name="default")


def _apply_llm_to_agent(agent: Manus) -> None:
    """Point an agent at the active LLM chosen in the UI."""
    try:
        agent.llm = _active_llm()
    except Exception as exc:
        logger.error(f"Could not apply active LLM, using config default: {type(exc).__name__}")


class ModelPayload(BaseModel):
    """Validated request body for adding/updating a custom model."""

    id: Optional[str] = Field(None, description="Existing entry id when editing")
    label: str = Field(..., min_length=1, max_length=60, description="Display name")
    model: str = Field(..., min_length=1, max_length=120, description="Model id")
    base_url: str = Field(..., min_length=1, max_length=300, description="OpenAI-compatible base URL")
    api_key: Optional[str] = Field(None, description="API key; omitted/empty keeps existing on edit")
    max_tokens: int = Field(8192, ge=256, le=131072, description="Max tokens per request")
    temperature: float = Field(0.0, ge=0.0, le=2.0, description="Sampling temperature")


@app.get("/api/models")
async def list_models(username: str = Depends(require_auth)) -> JSONResponse:
    """List selectable models (config profiles + user-added) and the active one."""
    registry = _load_registry()
    models = _builtin_entries() + _custom_entries(registry)
    return JSONResponse(
        {
            "active": registry.get("active") or "builtin:default",
            "models": models,
            "presets": PROVIDER_PRESETS,
        }
    )


@app.put("/api/models")
async def save_model(payload: ModelPayload, username: str = Depends(require_auth)) -> JSONResponse:
    """Add a custom model, or update one when payload.id matches an entry.

    Keys are stored in workspace/webapp_models.json (gitignored) and are
    never echoed back or logged.
    """
    registry = _load_registry()
    custom: List[Dict[str, Any]] = registry.setdefault("custom", [])
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")

    if payload.id:
        entry = next((e for e in custom if e["id"] == payload.id), None)
        if entry is None:
            raise HTTPException(status_code=404, detail="Model entry not found")
        entry.update(
            label=payload.label.strip(),
            model=payload.model.strip(),
            base_url=payload.base_url.strip().rstrip("/"),
            max_tokens=payload.max_tokens,
            temperature=payload.temperature,
            updated_at=now_iso,
        )
        if payload.api_key and payload.api_key.strip():
            entry["api_key"] = payload.api_key.strip()
        message = "updated"
    else:
        entry = {
            "id": f"user:{secrets.token_hex(4)}",
            "label": payload.label.strip(),
            "model": payload.model.strip(),
            "base_url": payload.base_url.strip().rstrip("/"),
            "api_key": (payload.api_key or "").strip(),
            "max_tokens": payload.max_tokens,
            "temperature": payload.temperature,
            "created_at": now_iso,
        }
        custom.append(entry)
        message = "added"

    registry["revision"] = int(registry.get("revision", 0)) + 1
    _save_registry(registry)

    # Bug fixed 2026-09-04: editing the currently-active model (e.g.
    # rotating its API key) used to only update the file on disk.
    # set_active_model already hot-swaps live agents when the *choice* of
    # model changes; an in-place edit of the model that's already active
    # needs the exact same refresh, or an already-running session keeps
    # using its original (possibly now-invalid) cached LLM client until
    # the process restarts or the user explicitly re-selects a model -
    # which is exactly what caused a live run to fail with "Invalid
    # Anthropic API Key" right after the key was corrected in the UI.
    applied = 0
    if registry.get("active") == entry["id"]:
        for session in sessions.values():
            if session.agent is not None:
                _apply_llm_to_agent(session.agent)
                applied += 1

    return JSONResponse({"status": "ok", "message": message, "id": entry["id"], "agents_updated": applied})


@app.delete("/api/models/{model_id}")
async def delete_model(model_id: str, username: str = Depends(require_auth)) -> JSONResponse:
    """Delete a user-added model. Config profiles and the active model cannot be deleted."""
    registry = _load_registry()
    custom = registry.get("custom", [])
    remaining = [e for e in custom if e["id"] != model_id]
    if len(remaining) == len(custom):
        raise HTTPException(status_code=404, detail="Model entry not found")
    if registry.get("active") == model_id:
        raise HTTPException(status_code=409, detail="Cannot delete the active model")
    registry["custom"] = remaining
    registry["revision"] = int(registry.get("revision", 0)) + 1
    _save_registry(registry)
    return JSONResponse({"status": "ok"})


@app.post("/api/models/active")
async def set_active_model(payload: Dict[str, str], username: str = Depends(require_auth)) -> JSONResponse:
    """Switch the active model, persist the choice, and hot-swap live agents.

    Args:
        payload: ``{"id": "builtin:<profile>" | "user:<id>"}``.

    Returns:
        Confirmation with the resolved model name.
    """
    model_id = (payload or {}).get("id", "")
    valid_ids = [m["id"] for m in _builtin_entries() + _custom_entries(_load_registry())]
    if model_id not in valid_ids:
        raise HTTPException(status_code=400, detail="Unknown model id")

    registry = _load_registry()
    registry["active"] = model_id
    _save_registry(registry)

    applied = 0
    for session in sessions.values():
        if session.agent is not None:
            _apply_llm_to_agent(session.agent)
            applied += 1

    llm = _active_llm()
    for session in sessions.values():
        if session.websocket is not None:
            await session.send_event(
                "control",
                {"action": "model_switch", "actor": "operator", "model": llm.model},
            )
    return JSONResponse({"status": "ok", "active": model_id, "model": llm.model, "agents_updated": applied})


# ---------------------------------------------------------------------------
# MCP server registry: list + toggle (persisted in config/mcp.json, hot-applied
# to live agent sessions - same pattern as the model hot-swap above)
# ---------------------------------------------------------------------------
MCP_CONFIG_PATH = PROJECT_ROOT / "config" / "mcp.json"
_mcp_toggle_lock = asyncio.Lock()

# Redact anything that looks like an inline credential before echoing a
# server's command line to the UI (§2.4: mcp.json args may carry KEY=value).
_SECRET_ARG_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password)=\S+")


def _load_mcp_file() -> Dict[str, Any]:
    """Read config/mcp.json, tolerating a missing or malformed file."""
    try:
        return json.loads(MCP_CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"mcpServers": {}}


def _save_mcp_file(data: Dict[str, Any]) -> None:
    MCP_CONFIG_PATH.write_text(json.dumps(data, indent=4) + "\n")


def _reload_mcp_servers() -> Dict[str, Any]:
    """Re-read mcp.json into the live config singleton (mutated in place)."""
    servers = MCPSettings.load_server_config()
    config.mcp_config.servers.clear()
    config.mcp_config.servers.update(servers)
    return servers


def _server_summary(server_config: Any) -> str:
    """One-line human summary of a server's transport, credentials redacted."""
    if server_config.type == "sse":
        return _SECRET_ARG_RE.sub(r"\1=***", server_config.url or "")
    cmdline = " ".join([server_config.command or "", *server_config.args]).strip()
    return _SECRET_ARG_RE.sub(r"\1=***", cmdline)


def _mcp_server_entries() -> List[Dict[str, Any]]:
    """All known servers: the built-in browser_use plus mcp.json entries."""
    servers = config.mcp_config.servers
    browser_entry = servers.get(_BROWSER_USE_SERVER_ID)
    entries = [
        {
            "id": _BROWSER_USE_SERVER_ID,
            "label": "Browser Use (built-in)",
            "type": browser_entry.type if browser_entry else "stdio",
            "summary": (
                _server_summary(browser_entry)
                if browser_entry
                else f"{_BROWSER_USE_COMMAND} {' '.join(_BROWSER_USE_ARGS)}"
            ),
            "enabled": browser_entry.enabled if browser_entry else True,
            "builtin": True,
        }
    ]
    for server_id, server_config in servers.items():
        if server_id == _BROWSER_USE_SERVER_ID:
            continue
        entries.append(
            {
                "id": server_id,
                "label": server_id,
                "type": server_config.type,
                "summary": _server_summary(server_config),
                "enabled": server_config.enabled,
                "builtin": False,
            }
        )
    return entries


def _mcp_live_status() -> Dict[str, Dict[str, Any]]:
    """Per-server live state across agent sessions: connection count + tools."""
    status: Dict[str, Dict[str, Any]] = {}
    for session in sessions.values():
        agent = session.agent
        if agent is None:
            continue
        for server_id in getattr(agent, "connected_servers", {}):
            st = status.setdefault(server_id, {"sessions": 0, "tools": None})
            st["sessions"] += 1
            if st["tools"] is None:
                st["tools"] = sum(
                    1
                    for t in getattr(agent.mcp_clients, "tools", [])
                    if t.server_id == server_id
                )
    return status


async def _connect_mcp_on_agent(agent: Manus, server_id: str, servers: Dict[str, Any]) -> None:
    """Connect one server on a live agent, mirroring Manus.initialize_mcp_servers."""
    browser_entry = servers.get(_BROWSER_USE_SERVER_ID)
    if server_id == _BROWSER_USE_SERVER_ID and browser_entry is None:
        await agent.connect_mcp_server(
            _BROWSER_USE_COMMAND,
            server_id,
            use_stdio=True,
            stdio_args=list(_BROWSER_USE_ARGS),
            tool_name_prefix=False,
            stdio_env=_browser_use_env(),
        )
        return
    server_config = servers.get(server_id)
    if server_config is None or not server_config.enabled:
        raise ValueError(f"server {server_id} is not enabled in config")
    if server_config.type == "sse" and server_config.url:
        await agent.connect_mcp_server(server_config.url, server_id)
    elif server_config.type == "stdio" and server_config.command:
        await agent.connect_mcp_server(
            server_config.command,
            server_id,
            use_stdio=True,
            stdio_args=server_config.args,
            tool_name_prefix=server_id != _BROWSER_USE_SERVER_ID,
            stdio_env=(
                _browser_use_env() if server_id == _BROWSER_USE_SERVER_ID else None
            ),
        )


@app.get("/api/mcp/servers")
async def list_mcp_servers(username: str = Depends(require_auth)) -> JSONResponse:
    """List all MCP servers with enabled state and live connection status."""
    live = _mcp_live_status()
    out = []
    for entry in _mcp_server_entries():
        st = live.get(entry["id"], {"sessions": 0, "tools": None})
        out.append({**entry, "connected_sessions": st["sessions"], "tools": st["tools"]})
    return JSONResponse({"servers": out})


class MCPTogglePayload(BaseModel):
    enabled: bool


@app.post("/api/mcp/servers/{server_id}/toggle")
async def toggle_mcp_server(
    server_id: str, payload: MCPTogglePayload, username: str = Depends(require_auth)
) -> JSONResponse:
    """Enable/disable an MCP server: persist to mcp.json, then hot-apply to
    every live agent session (connect on enable, disconnect on disable).

    browser_use is special-cased: its persisted "off" state is an explicit
    mcp.json entry with enabled=false; enabling it removes that entry so the
    built-in auto-connect resumes (a custom-configured entry is just flipped).
    """
    async with _mcp_toggle_lock:
        if server_id != _BROWSER_USE_SERVER_ID and server_id not in config.mcp_config.servers:
            raise HTTPException(status_code=404, detail="Unknown MCP server")

        data = _load_mcp_file()
        servers_json = data.setdefault("mcpServers", {})
        if server_id == _BROWSER_USE_SERVER_ID:
            existing = servers_json.get(_BROWSER_USE_SERVER_ID)
            if payload.enabled:
                custom = existing and (
                    existing.get("command") not in (None, _BROWSER_USE_COMMAND)
                    or existing.get("args", list(_BROWSER_USE_ARGS)) != list(_BROWSER_USE_ARGS)
                )
                if custom:
                    existing["enabled"] = True
                else:
                    servers_json.pop(_BROWSER_USE_SERVER_ID, None)
            else:
                servers_json[_BROWSER_USE_SERVER_ID] = existing or {
                    "type": "stdio",
                    "command": _BROWSER_USE_COMMAND,
                    "args": list(_BROWSER_USE_ARGS),
                }
                servers_json[_BROWSER_USE_SERVER_ID]["enabled"] = False
        else:
            servers_json[server_id]["enabled"] = payload.enabled
        _save_mcp_file(data)
        servers = _reload_mcp_servers()

        applied, errors = 0, []
        for session in sessions.values():
            agent = session.agent
            if agent is None:
                continue
            try:
                if payload.enabled:
                    if server_id not in getattr(agent, "connected_servers", {}):
                        await _connect_mcp_on_agent(agent, server_id, servers)
                        applied += 1
                else:
                    if server_id in getattr(agent, "connected_servers", {}):
                        await agent.disconnect_mcp_server(server_id)
                        applied += 1
            except Exception as e:
                errors.append(type(e).__name__)
                logger.warning(
                    f"MCP toggle {server_id} failed on a live session: {type(e).__name__}"
                )
        logger.info(
            f"MCP server {server_id} {'enabled' if payload.enabled else 'disabled'} "
            f"by {username}; applied to {applied} live session(s)"
        )
        return JSONResponse(
            {
                "status": "ok",
                "server_id": server_id,
                "enabled": payload.enabled,
                "applied": applied,
                "errors": errors,
            }
        )



# ---------------------------------------------------------------------------
# Agent session with streaming instrumentation
# ---------------------------------------------------------------------------
def _tool_category(name: str) -> str:
    """Map a tool name to a UI pane category.

    Browser Use CLI 3.0 exposes MCP tools (browser_exec, browser_screenshot,
    ...), so browser tools are matched by prefix as well as exact name.
    """
    if name.startswith("browser") or name == "web_search":
        return "browser"
    if name == "python_execute":
        return "terminal"
    if name == "str_replace_editor":
        return "editor"
    if name in ("ask_human", "terminate"):
        return "chat"
    return "tool"


class AgentSession:
    """One chat session: a persistent Manus agent plus a WebSocket client.

    The agent is created lazily on first prompt and reused for subsequent
    prompts, so the conversation (memory) survives across turns. think(),
    step() and execute_tool() are wrapped to emit UI events.
    """

    def __init__(self) -> None:
        self.agent: Optional[Manus] = None
        self.websocket: Optional[WebSocket] = None
        self.is_running = False
        self._task: Optional[asyncio.Task] = None
        self._human_future: Optional[asyncio.Future] = None
        self.event_log: List[Dict[str, Any]] = []
        self.touched_files: set = set()  # workspace-relative paths touched this session
        self.title: str = ""             # first prompt, for the history list
        self.created_at: int = 0
        self.last_active: int = 0

    # -- websocket plumbing -------------------------------------------------
    async def connect(self, websocket: WebSocket) -> None:
        """Accept the client socket and replay this session's event history."""
        await websocket.accept()
        self.websocket = websocket
        if self.event_log:
            await self._send_raw({"type": "replay", "data": {"events": self.event_log}})

    async def disconnect(self) -> None:
        """Drop the current client socket without stopping a running agent."""
        if self.websocket:
            try:
                await self.websocket.close()
            except Exception:
                pass
        self.websocket = None

    async def _send_raw(self, payload: Dict[str, Any]) -> None:
        if self.websocket is None:
            return
        try:
            await self.websocket.send_json(payload)
        except Exception:
            self.websocket = None

    async def send_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Push an event to the client and append it to the replay log.

        Args:
            event_type: Event discriminator (e.g. "tool_start", "thought").
            data: Event payload consumed by the frontend.
        """
        event = {"type": event_type, "data": data}
        self.event_log.append(event)
        if len(self.event_log) > MAX_EVENT_LOG:
            del self.event_log[: len(self.event_log) - MAX_EVENT_LOG]
        await self._send_raw(event)

    # -- agent lifecycle ----------------------------------------------------
    async def _ensure_agent(self) -> Manus:
        """Create and instrument the agent on first use."""
        if self.agent is None:
            self.agent = await Manus.create()
            self.agent.max_steps = MAX_STEPS
            _apply_llm_to_agent(self.agent)
            self._instrument(self.agent)
        return self.agent

    @staticmethod
    def _read_token_counters(agent: Manus) -> tuple:
        """Best-effort read of the agent LLM's cumulative token counters.

        Returns:
            (input_tokens, output_tokens), or (None, None) if unavailable.
        """
        try:
            llm = agent.llm
            return int(llm.total_input_tokens), int(llm.total_completion_tokens)
        except Exception:
            return None, None

    def _instrument(self, agent: Manus) -> None:
        """Wrap think/step/execute_tool as instance attributes to emit events."""
        orig_think = agent.think
        orig_step = agent.step
        orig_execute = agent.execute_tool

        async def wrapped_think() -> bool:
            prev_count = len(agent.messages)
            prev_in, prev_out = self._read_token_counters(agent)
            result = await orig_think()
            for msg in agent.messages[prev_count:]:
                if msg.role == "assistant":
                    if msg.content:
                        await self.send_event("thought", {"content": msg.content})
                    for tc in msg.tool_calls or []:
                        await self.send_event(
                            "plan",
                            {"tool": tc.function.name},
                        )
            # Cost/usage metrics: token deltas for this think() call. LLM
            # singletons accumulate counters; guard for counter swaps when the
            # active model is switched mid-run.
            cur_in, cur_out = self._read_token_counters(agent)
            if cur_in is not None:
                await self.send_event(
                    "usage",
                    {
                        "delta_in": max(0, cur_in - (prev_in or 0)),
                        "delta_out": max(0, cur_out - (prev_out or 0)),
                        "total_in": cur_in,
                        "total_out": cur_out,
                    },
                )
            return result

        async def wrapped_step() -> str:
            # Screenshots arrive as user messages carrying base64_image —
            # appended during act() by ToolCallAgent (MCP tool images) — so
            # harvest them across the whole step, not just think().
            prev_count = len(agent.messages)
            await self.send_event(
                "step",
                {"current": agent.current_step, "max": agent.max_steps},
            )
            result = await orig_step()
            for msg in agent.messages[prev_count:]:
                if msg.role == "user" and getattr(msg, "base64_image", None):
                    await self.send_event(
                        "browser_screenshot", {"image": msg.base64_image}
                    )
            return result

        async def wrapped_execute_tool(command: ToolCall) -> str:
            name = command.function.name if command.function else ""
            try:
                args = json.loads(command.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": command.function.arguments}

            if name == "ask_human":
                return await self._handle_ask_human(args)

            call_id = secrets.token_hex(4)
            started = time.monotonic()
            await self.send_event(
                "tool_start",
                {
                    "id": call_id,
                    "name": name,
                    "category": _tool_category(name),
                    "arguments": args,
                },
            )
            try:
                result = await orig_execute(command)
                error = False
            except Exception:
                result = "Tool execution raised an exception"
                error = True
            duration_ms = int((time.monotonic() - started) * 1000)
            preview = (result or "")[:2000]
            await self.send_event(
                "tool_end",
                {"id": call_id, "name": name, "ok": not error, "duration_ms": duration_ms, "result": preview},
            )

            if name == "python_execute":
                await self.send_event(
                    "terminal",
                    {"code": args.get("code", ""), "output": preview},
                )
            elif name == "str_replace_editor":
                await self._emit_file_update(args)
            return result

        agent.think = wrapped_think  # type: ignore[method-assign]
        agent.step = wrapped_step  # type: ignore[method-assign]
        agent.execute_tool = wrapped_execute_tool  # type: ignore[method-assign]

    async def _handle_ask_human(self, args: Dict[str, Any]) -> str:
        """Bridge the ask_human tool to the web UI instead of blocking input().

        Args:
            args: Tool arguments; expects an "inquire" question string.

        Returns:
            Observation string with the user's reply (or a timeout notice).
        """
        question = str(args.get("inquire", "The agent needs your input."))
        loop = asyncio.get_running_loop()
        self._human_future = loop.create_future()
        await self.send_event("ask_human", {"question": question})
        try:
            reply = await asyncio.wait_for(self._human_future, timeout=HUMAN_REPLY_TIMEOUT)
            answer = str(reply).strip() or "(empty reply)"
        except asyncio.TimeoutError:
            answer = "(no reply: timed out waiting for user input)"
        finally:
            self._human_future = None
        return f"Observed output of cmd `ask_human` executed:\nUser's reply: {answer}"

    async def resolve_human_reply(self, content: str) -> None:
        """Resolve a pending ask_human future with the client's answer."""
        if self._human_future and not self._human_future.done():
            self._human_future.set_result(content)

    async def _emit_file_update(self, args: Dict[str, Any]) -> None:
        """Push file content to the editor pane after a str_replace_editor call."""
        raw_path = str(args.get("path") or args.get("file_path") or "")
        resolved = _safe_workspace_path(raw_path)
        payload: Dict[str, Any] = {
            "path": raw_path,
            "command": args.get("command", ""),
            "content": None,
        }
        if resolved is not None:
            payload["path"] = str(resolved.relative_to(WORKSPACE_ROOT))
            self.touched_files.add(payload["path"])
            try:
                if resolved.stat().st_size <= SNAPSHOT_BYTES:
                    payload["content"] = resolved.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        await self.send_event("file_update", payload)

    # -- run loop -----------------------------------------------------------
    async def run_agent(self, prompt: str) -> None:
        """Run one turn of the agent, streaming progress to the client.

        Args:
            prompt: The user's request for this turn.
        """
        self.is_running = True
        self._started_at = time.monotonic()
        now = int(time.time())
        self.last_active = now
        if not self.created_at:
            self.created_at = now
            self.title = prompt.strip().split("\n")[0][:80]
        try:
            agent = await self._ensure_agent()
            await self.send_event("run_start", {"prompt": prompt, "max_steps": agent.max_steps})
            await agent.run(prompt)
            final = next(
                (
                    m.content
                    for m in reversed(agent.messages)
                    if m.role == "assistant" and m.content
                ),
                "",
            )
            await self.send_event("final_result", {"content": final})
        except asyncio.CancelledError:
            await self.send_event("status", {"status": "stopped", "message": "Stopped by user"})
        except Exception as exc:
            logger.error(f"Agent run failed: {type(exc).__name__}")
            logger.error(traceback.format_exc())
            await self.send_event(
                "error", {"message": f"{type(exc).__name__}: {exc}"}
            )
        finally:
            if self._human_future and not self._human_future.done():
                self._human_future.set_result("(interrupted)")
            self.is_running = False
            self.last_active = int(time.time())
            await self.send_event("run_end", {})

    async def stop(self) -> None:
        """Cancel the in-flight run; agent memory is kept for the next turn."""
        if self._task and not self._task.done():
            self._task.cancel()


sessions: Dict[str, AgentSession] = {}


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str) -> None:
    """WebSocket endpoint: client sends run/stop/human_reply, server pushes events.

    Not rate-limited (see require_auth's docstring for the full reasoning):
    a WS handshake without a valid session is routine (expired session,
    reconnect-with-backoff after a network blip) and used to silently share
    the login-form's failure counter, which could lock out a real login
    attempt from the same IP with no password ever having been guessed.
    """
    if not verify_ws_origin(websocket):
        # No accept() yet - close is the correct rejection for a disallowed
        # cross-origin handshake (T-2: this used to be checked nowhere).
        await websocket.close(code=1008)
        return
    if not _resolve_ws_auth(websocket):
        await websocket.close(code=1008)
        return

    session = sessions.setdefault(session_id, AgentSession())
    await session.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # Re-validate on every inbound message so idle/absolute session
            # timeouts take effect on long-lived connections, not just at
            # the initial handshake.
            if not _resolve_ws_auth(websocket):
                await websocket.close(code=1008)
                await session.disconnect()
                break
            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                continue

            kind = message.get("type")
            if kind == "run":
                prompt = (message.get("prompt") or "").strip()
                if not prompt:
                    await session.send_event("error", {"message": "Prompt cannot be empty"})
                elif session.is_running or (session._task and not session._task.done()):
                    await session.send_event("error", {"message": "Agent is already running"})
                else:
                    session._task = asyncio.create_task(session.run_agent(prompt))
            elif kind == "stop":
                await session.send_event(
                    "control", {"action": "stop", "actor": "operator"}
                )
                await session.stop()
            elif kind == "human_reply":
                await session.resolve_human_reply(message.get("content", ""))
            elif kind == "ping":
                await session.send_event("pong", {})
    except WebSocketDisconnect:
        await session.disconnect()
    except Exception as exc:
        logger.error(f"WebSocket error: {type(exc).__name__}")
        await session.disconnect()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
