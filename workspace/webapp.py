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
    WEBAPP_AUTH_USER / WEBAPP_AUTH_PASS   Basic-auth credentials. If the
                                          password is unset a random one is
                                          generated and printed to console.
    WEBAPP_HOST / WEBAPP_PORT             Bind address (default 127.0.0.1:8000).
    WEBAPP_MAX_STEPS                      Agent step limit (default 30).

Usage:
    .venv/bin/python workspace/webapp.py

Dependencies: fastapi, uvicorn (already in project requirements).
Tier 2 (failure is loud: HTTP/WS errors surface in the browser and logs).
"""

import asyncio
import base64
import json
import mimetypes
import os
import secrets
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent.manus import Manus
from app.config import LLMSettings, config
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

app = FastAPI(title="OpenManus Web")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBasic()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """Basic-auth dependency protecting every HTTP route.

    Args:
        credentials: Parsed Authorization header.

    Returns:
        Authenticated username.

    Raises:
        HTTPException: 401 when username or password mismatches.
    """
    ok_user = secrets.compare_digest(credentials.username, AUTH_USERNAME)
    ok_pass = secrets.compare_digest(credentials.password, AUTH_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=401, detail="Invalid credentials", headers={"WWW-Authenticate": "Basic"}
        )
    return credentials.username


def verify_ws_credentials(websocket: WebSocket) -> bool:
    """Apply the same basic-auth check to the WebSocket handshake.

    Browsers resend cached Basic-auth headers on same-origin upgrades, but a
    client connecting directly must be checked here too.

    Args:
        websocket: Incoming WebSocket connection.

    Returns:
        True when the Authorization header carries valid credentials.
    """
    auth_header = websocket.headers.get("authorization", "")
    if not auth_header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[len("Basic "):]).decode("utf-8")
        username, _, password = decoded.partition(":")
    except Exception:
        return False
    return secrets.compare_digest(username, AUTH_USERNAME) and secrets.compare_digest(
        password, AUTH_PASSWORD
    )


# ---------------------------------------------------------------------------
# Static + read-only file APIs
# ---------------------------------------------------------------------------
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root(username: str = Depends(verify_credentials)) -> FileResponse:
    """Serve the main HTML page."""
    return FileResponse(str(STATIC_DIR / "index.html"))


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
async def list_files(username: str = Depends(verify_credentials)) -> JSONResponse:
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
    path: str = Query(...), username: str = Depends(verify_credentials)
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
    session: str = Query(""), username: str = Depends(verify_credentials)
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
    path: str = Query(...), username: str = Depends(verify_credentials)
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
async def gdrive_status(username: str = Depends(verify_credentials)) -> JSONResponse:
    """Report whether Google Drive saving is configured, with setup help."""
    return JSONResponse({"enabled": _gdrive_enabled(), "hint": GDRIVE_SETUP_HINT})


@app.post("/api/gdrive/upload")
async def gdrive_upload(
    payload: Dict[str, str], username: str = Depends(verify_credentials)
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
async def list_sessions(username: str = Depends(verify_credentials)) -> JSONResponse:
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
async def delete_session(session_id: str, username: str = Depends(verify_credentials)) -> JSONResponse:
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
async def list_models(username: str = Depends(verify_credentials)) -> JSONResponse:
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
async def save_model(payload: ModelPayload, username: str = Depends(verify_credentials)) -> JSONResponse:
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
    return JSONResponse({"status": "ok", "message": message, "id": entry["id"]})


@app.delete("/api/models/{model_id}")
async def delete_model(model_id: str, username: str = Depends(verify_credentials)) -> JSONResponse:
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
async def set_active_model(payload: Dict[str, str], username: str = Depends(verify_credentials)) -> JSONResponse:
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
    return JSONResponse({"status": "ok", "active": model_id, "model": llm.model, "agents_updated": applied})



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

    def _instrument(self, agent: Manus) -> None:
        """Wrap think/step/execute_tool as instance attributes to emit events."""
        orig_think = agent.think
        orig_step = agent.step
        orig_execute = agent.execute_tool

        async def wrapped_think() -> bool:
            prev_count = len(agent.messages)
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
    """WebSocket endpoint: client sends run/stop/human_reply, server pushes events."""
    if not verify_ws_credentials(websocket):
        await websocket.close(code=1008)
        return

    session = sessions.setdefault(session_id, AgentSession())
    await session.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
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
