"""Regression smoke tests for the OpenManus web app backend.

Boots the real FastAPI app on an ephemeral port with a deterministic fake
agent (no LLM calls) and exercises HTTP + WebSocket contracts:

- auth enforcement on HTTP and WS
- WS event pipeline: run_start -> step -> thought -> tool_start/end ->
  terminal -> usage -> final_result -> run_end (P1-3 regression guard)
- control audit events on stop and model switch (P1-4)
- sessions API lifecycle

Run: .venv/bin/python -m pytest workspace/tests/test_webapp_smoke.py -v
Tier 2: loud failures (asserts + HTTP errors).
"""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("WEBAPP_AUTH_PASS", "testpass")
os.environ.setdefault("WEBAPP_PORT", "0")
# Keep test runs' task journals/exports out of the operator's real vault.
os.environ.setdefault("WEBAPP_OBSIDIAN_TASKS_DIR", tempfile.mkdtemp(prefix="om-test-vault-"))
os.environ.setdefault("WEBAPP_OUTPUT_EXPORT_DIR", tempfile.mkdtemp(prefix="om-test-export-"))
# Keep persisted run history out of the operator's real session store, and make
# the history layer's retry/breaker timings test-fast.
os.environ.setdefault("WEBAPP_SESSION_STORE_DIR", tempfile.mkdtemp(prefix="om-test-sessions-"))
os.environ.setdefault("WEBAPP_HISTORY_BACKOFF_BASE_MS", "5")
os.environ.setdefault("WEBAPP_HISTORY_BACKOFF_MAX_MS", "20")
os.environ.setdefault("WEBAPP_HISTORY_BREAKER_RESET_MS", "50")
os.environ.setdefault("WEBAPP_HISTORY_RETRY_MAX", "500")
os.environ.setdefault("WEBAPP_HISTORY_META_RETRY_MAX", "1")
# Keep model-switch tests off the operator's real model registry (active choice).
os.environ.setdefault(
    "WEBAPP_MODELS_FILE",
    str(Path(tempfile.mkdtemp(prefix="om-test-models-")) / "webapp_models.json"),
)

_spec = importlib.util.spec_from_file_location("webapp_under_test", PROJECT_ROOT / "workspace" / "webapp.py")
webapp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(webapp)

import uvicorn  # noqa: E402
import websockets  # noqa: E402

AUTH = ("admin", "testpass")
FAKE_KEY = "sk" + "-" + "FAKE" * 5 + "1234"  # fragments: not a complete token literal
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-screenshot-bytes" * 4
PNG_B64 = __import__("base64").b64encode(PNG).decode()


class FakeToolCall:
    def __init__(self, name, arguments):
        self.function = SimpleNamespace(name=name, arguments=arguments)


class FakeManus:
    """Deterministic stand-in for Manus; drives the same instrumentation."""

    slow = False  # per-test knob: True adds latency so `stop` lands mid-run
    slow_create = False  # per-test knob: True delays create() (MCP-init-like latency)
    secret = False  # per-test knob: tool output contains fake credentials
    screenshot = None  # per-test knob: base64 image appended as a user message

    def __init__(self):
        self.max_steps = 5
        self.current_step = 0
        self.messages = []
        self.llm = SimpleNamespace(total_input_tokens=0, total_completion_tokens=0)
        self.tool_calls = []

    @classmethod
    async def create(cls):
        if cls.slow_create:
            await asyncio.sleep(3.0)
        return cls()

    @staticmethod
    def _msg(role, content="", tool_calls=None, base64_image=None):
        return SimpleNamespace(
            role=role, content=content, tool_calls=tool_calls, base64_image=base64_image
        )

    async def run(self, prompt):
        self.messages.append(self._msg("user", prompt))
        for _ in range(2):
            self.current_step += 1
            await self.step()
        self.messages.append(self._msg("assistant", "fake final answer"))

    async def step(self):
        await self.think()
        await self.act()

    async def think(self):
        if FakeManus.slow:
            await asyncio.sleep(1.0)
        self.llm.total_input_tokens += 120
        self.llm.total_completion_tokens += 30
        tc = FakeToolCall("python_execute", json.dumps({"code": "print(1)"}))
        self.tool_calls = [tc]  # mirrors ToolCallAgent.think() state update
        self.messages.append(
            self._msg("assistant", "thinking about it", tool_calls=[tc])
        )

    async def act(self):
        for command in self.tool_calls:
            result = await self.execute_tool(command)
            self.messages.append(self._msg("tool", result))
            if FakeManus.screenshot:
                self.messages.append(self._msg("user", "", base64_image=FakeManus.screenshot))

    async def execute_tool(self, command):
        if FakeManus.secret:
            return f"Observed output: key {FAKE_KEY} and api_key=hunter2hunter2"
        return "Observed output of cmd `python_execute` executed:\n1"

    async def cleanup(self):
        pass


@pytest.fixture(scope="module")
def server():
    webapp.Manus = FakeManus  # swap the agent; instrumentation still applies
    webapp.sessions.clear()
    config = uvicorn.Config(webapp.app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.2)
    assert server.started, "server failed to boot"
    yield server.servers[0].sockets[0].getsockname()[1]  # port
    server.should_exit = True


def http(method, path, body=None, auth=AUTH, port=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            **({"Authorization": "Basic " + _b64(auth)} if auth else {}),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _b64(pair):
    import base64

    return base64.b64encode(f"{pair[0]}:{pair[1]}".encode()).decode()


def ws_url(port, sid):
    return f"ws://127.0.0.1:{port}/ws/{sid}"


def ws_headers():
    return {"Authorization": "Basic " + _b64(AUTH)}


async def collect_until_run_end(ws, timeout=20):
    events = []
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        events.append(msg)
        if msg["type"] == "run_end":
            return events


def test_health_and_auth(server):
    port = server
    # Health is the one public liveness endpoint; everything else requires auth.
    status, body = http("GET", "/api/health", auth=None, port=port)
    assert status == 200 and body["status"] == "ok"


def test_static_cache_hardening(server):
    # Guards the "new button is inert in a returning browser" failure mode:
    # the app shell must version its asset URLs, and both shell and assets
    # must force revalidation (heuristic freshness otherwise lets a stale
    # cached app.js run against a brand-new index.html).
    port = server
    auth = {"Authorization": "Basic " + _b64(AUTH)}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/", headers=auth)
    with urllib.request.urlopen(req, timeout=10) as res:
        html = res.read().decode()
        assert "no-cache" in res.headers.get("Cache-Control", "")
    assert 'src="/static/app.js?v=' in html
    assert 'href="/static/style.css?v=' in html

    req = urllib.request.Request(f"http://127.0.0.1:{port}/static/app.js", headers=auth)
    with urllib.request.urlopen(req, timeout=10) as res:
        assert res.status == 200
        assert "no-cache" in res.headers.get("Cache-Control", "")
    assert http("GET", "/api/sessions", auth=None, port=port)[0] == 401
    assert http("GET", "/api/runs", auth=None, port=port)[0] == 401
    assert http("GET", "/api/runs/some-session/some-run", auth=None, port=port)[0] == 401
    assert http("GET", "/api/blobs/some-session/" + "a" * 32 + ".png", auth=None, port=port)[0] == 401
    assert http("GET", "/api/health/detail", auth=None, port=port)[0] == 401
    assert http("POST", "/api/history/prune", {"older_than_days": 30}, auth=None, port=port)[0] == 401
    # the one public liveness route stays minimal (no component detail)
    assert http("GET", "/api/health", auth=None, port=port) == (200, {"status": "ok"})
    assert http("GET", "/api/files", auth=("admin", "wrong"), port=port)[0] == 401
    status, body = http("GET", "/api/health", port=port)
    assert status == 200


@pytest.mark.asyncio
async def test_event_pipeline_with_usage(server):
    port = server
    sid = f"smoke-{uuid.uuid4().hex[:8]}"
    async with websockets.connect(ws_url(port, sid), additional_headers=ws_headers()) as ws:
        await ws.send(json.dumps({"type": "run", "prompt": "do the fake thing"}))
        events = await collect_until_run_end(ws)
        kinds = [e["type"] for e in events]

    assert kinds[0] == "run_start"
    for expected in ("step", "thought", "plan", "tool_start", "tool_end", "terminal", "usage", "final_result", "run_end"):
        assert expected in kinds, f"missing {expected} in {kinds}"

    usage_events = [e["data"] for e in events if e["type"] == "usage"]
    assert len(usage_events) == 2, "one usage event per think()"
    assert all(u["delta_in"] == 120 and u["delta_out"] == 30 for u in usage_events)
    assert usage_events[-1]["total_in"] == 240, "cumulative totals accumulate"
    tool_start = next(e for e in events if e["type"] == "tool_start")["data"]
    assert tool_start["name"] == "python_execute"
    assert tool_start["category"] == "terminal"
    final = next(e for e in events if e["type"] == "final_result")["data"]
    assert final["content"] == "fake final answer"


@pytest.mark.asyncio
async def test_stop_emits_control_and_stopped_status(server):
    port = server
    FakeManus.slow = True
    try:
        sid = f"stop-{uuid.uuid4().hex[:8]}"
        async with websockets.connect(ws_url(port, sid), additional_headers=ws_headers()) as ws:
            await ws.send(json.dumps({"type": "run", "prompt": "slow fake run"}))
            # wait for first step so the run is in flight, then stop
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 20))
                if msg["type"] == "step":
                    break
            await ws.send(json.dumps({"type": "stop"}))
            events = await collect_until_run_end(ws)
            kinds = [e["type"] for e in events]
            assert "control" in kinds, f"audit event missing: {kinds}"
            control = next(e for e in events if e["type"] == "control")["data"]
            assert control["action"] == "stop" and control["actor"] == "operator"
            status = next(e for e in events if e["type"] == "status")["data"]
            assert status["status"] == "stopped"
    finally:
        FakeManus.slow = False


@pytest.mark.asyncio
async def test_model_switch_broadcasts_control_event(server):
    port = server
    sid = f"switch-{uuid.uuid4().hex[:8]}"
    async with websockets.connect(ws_url(port, sid), additional_headers=ws_headers()) as ws:
        await ws.send(json.dumps({"type": "ping"}))
        assert json.loads(await asyncio.wait_for(ws.recv(), 5))["type"] == "pong"
        status, body = http("POST", "/api/models/active", {"id": "builtin:vision"}, port=port)
        assert status == 200
        msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert msg["type"] == "control"
        assert msg["data"]["action"] == "model_switch"
        http("POST", "/api/models/active", {"id": "builtin:default"}, port=port)  # restore


def test_sessions_lifecycle(server):
    port = server
    status, body = http("GET", "/api/sessions", port=port)
    assert status == 200
    our = [s for s in body["sessions"] if s["id"].startswith(("smoke-", "stop-"))]
    assert our, "task sessions should be listed with titles"
    assert all(s["title"] for s in our)
    del_id = our[0]["id"]
    status, _ = http("DELETE", f"/api/sessions/{del_id}", port=port)
    assert status == 200
    status, _ = http("DELETE", f"/api/sessions/{del_id}", port=port)
    assert status == 404


@pytest.mark.asyncio
async def test_persistent_runs_survive_restart_replay_and_delete(server):
    port = server
    sid = f"persist-{uuid.uuid4().hex[:8]}"
    prompts = ("first persisted turn", "second persisted turn")
    for prompt in prompts:
        async with websockets.connect(ws_url(port, sid), additional_headers=ws_headers()) as ws:
            await ws.send(json.dumps({"type": "run", "prompt": prompt}))
            events = await collect_until_run_end(ws)
            assert events[-1]["type"] == "run_end"

    status, body = http("GET", "/api/runs", port=port)
    assert status == 200
    runs = [run for run in body["runs"] if run["session_id"] == sid]
    assert len(runs) == 2
    assert {run["prompt_preview"] for run in runs} == set(prompts)
    assert all(run["status"] == "completed" and run["read_only"] for run in runs)
    assert all(run["steps"] == 2 and run["actions"] == 2 for run in runs)
    assert all(run["input_tokens"] == 240 and run["output_tokens"] == 60 for run in runs)

    first_run = next(run for run in runs if run["prompt_preview"] == prompts[0])
    detail_path = f"/api/runs/{sid}/{first_run['id']}"
    status, detail = http("GET", detail_path, port=port)
    assert status == 200 and detail["read_only"] is True
    detail_events = detail["events"]
    assert next(event for event in detail_events if event["type"] == "run_start")["data"]["prompt"] == prompts[0]
    assert next(event for event in detail_events if event["type"] == "final_result")["data"]["content"] == "fake final answer"

    # Simulate a process restart: RAM is dropped, the disk store remains.
    webapp.sessions.pop(sid, None)
    async with websockets.connect(ws_url(port, sid), additional_headers=ws_headers()) as ws:
        info = json.loads(await asyncio.wait_for(ws.recv(), 5))
        replay = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert info["type"] == "session_info" and info["data"]["read_only"] is True
        assert replay["type"] == "replay"
        replayed_prompts = [
            event["data"]["prompt"]
            for event in replay["data"]["events"]
            if event["type"] == "run_start"
        ]
        assert replayed_prompts == list(prompts)
        await ws.send(json.dumps({"type": "run", "prompt": "must not execute"}))
        rejection = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert rejection["type"] == "error"
        assert "read-only" in rejection["data"]["message"]

    status, body = http("GET", "/api/sessions", port=port)
    restored = next(item for item in body["sessions"] if item["id"] == sid)
    assert restored["historical"] is True and restored["read_only"] is True
    assert http("DELETE", f"/api/sessions/{sid}", port=port)[0] == 200
    assert webapp.SESSION_STORE.get_session(sid) is None
    assert http("GET", detail_path, port=port)[0] == 404


@pytest.mark.asyncio
async def test_disk_event_history_outlives_ram_replay_cap(server):
    port = server
    sid = f"cap-{uuid.uuid4().hex[:8]}"
    previous_cap = webapp.MAX_EVENT_LOG
    webapp.MAX_EVENT_LOG = 3
    try:
        async with websockets.connect(ws_url(port, sid), additional_headers=ws_headers()) as ws:
            await ws.send(json.dumps({"type": "run", "prompt": "disk keeps every event"}))
            events = await collect_until_run_end(ws)
    finally:
        webapp.MAX_EVENT_LOG = previous_cap

    assert len(events) > 3
    assert len(webapp.sessions[sid].event_log) == 3
    status, body = http("GET", "/api/runs", port=port)
    run = next(run for run in body["runs"] if run["session_id"] == sid)
    status, detail = http("GET", f"/api/runs/{sid}/{run['id']}", port=port)
    assert status == 200
    assert len(detail["events"]) == len(events)
    assert detail["events"][0]["type"] == "run_start"


@pytest.mark.asyncio
async def test_deleted_live_session_cannot_rerun_or_recreate_history(server):
    port = server
    sid = f"deleted-{uuid.uuid4().hex[:8]}"
    async with websockets.connect(ws_url(port, sid), additional_headers=ws_headers()) as ws:
        await ws.send(json.dumps({"type": "run", "prompt": "create then delete"}))
        await collect_until_run_end(ws)
        assert http("DELETE", f"/api/sessions/{sid}", port=port)[0] == 200
        await ws.send(json.dumps({"type": "run", "prompt": "must never recreate history"}))
        rejection = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert rejection["type"] == "error"
        assert "deleted" in rejection["data"]["message"]
        assert webapp.SESSION_STORE.get_session(sid) is None


@pytest.mark.asyncio
async def test_repeated_stop_requests_do_not_cancel_run_finalization(server):
    port = server
    sid = f"double-stop-{uuid.uuid4().hex[:8]}"
    FakeManus.slow = True
    try:
        async with websockets.connect(ws_url(port, sid), additional_headers=ws_headers()) as ws:
            await ws.send(json.dumps({"type": "run", "prompt": "stop twice safely"}))
            while True:
                message = json.loads(await asyncio.wait_for(ws.recv(), 20))
                if message["type"] == "step":
                    break
            await ws.send(json.dumps({"type": "stop"}))
            await ws.send(json.dumps({"type": "stop"}))
            events = await collect_until_run_end(ws)
            assert events[-1]["type"] == "run_end"
    finally:
        FakeManus.slow = False

    session = webapp.sessions[sid]
    assert session._current_run_id is None
    _, body = http("GET", "/api/runs", port=port)
    run = next(run for run in body["runs"] if run["session_id"] == sid)
    assert run["status"] == "stopped"
    assert run["ended_at"] is not None


# ------------------------------------------------------- run history (P1-3) --
def on_server_loop(coro, timeout=10):
    """Run a coroutine on the webapp's own event loop (the store is loop-bound)."""
    return asyncio.run_coroutine_threadsafe(coro, webapp.app.state.loop).result(timeout)


def raw_get(path, port, auth=AUTH):
    headers = {"Authorization": "Basic " + _b64(auth)} if auth else {}
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, {k.lower(): v for k, v in res.headers.items()}, res.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


async def run_one(port, sid, prompt):
    """Run one fake task on a session; return every event received."""
    async with websockets.connect(ws_url(port, sid), additional_headers=ws_headers()) as ws:
        await ws.send(json.dumps({"type": "run", "prompt": prompt}))
        return await collect_until_run_end(ws)


def find_run(port, sid, **query):
    qs = "&".join(f"{k}={v}" for k, v in {"limit": 200, **query}.items())
    status, body = http("GET", f"/api/runs?{qs}", port=port)
    assert status == 200
    return [run for run in body["runs"] if run["session_id"] == sid]


@pytest.mark.asyncio
async def test_runs_api_paginates_filters_sorts_and_searches(server):
    port = server
    tag = uuid.uuid4().hex[:8]
    sids = [f"page-{tag}-{i}" for i in range(3)]
    for index, sid in enumerate(sids):
        await run_one(port, sid, f"pagination probe {tag} number {index}")

    status, first = http("GET", f"/api/runs?limit=2&q={tag}", port=port)
    assert status == 200 and len(first["runs"]) == 2 and first["next_cursor"]
    assert first["summary"]["total"] == 3 and first["summary"]["success_rate"] == 1.0
    status, second = http("GET", f"/api/runs?limit=2&q={tag}&cursor={first['next_cursor']}", port=port)
    assert len(second["runs"]) == 1 and second["next_cursor"] is None
    seen = [run["session_id"] for run in first["runs"] + second["runs"]]
    assert sorted(seen) == sorted(sids), "pages must not drop or repeat runs"
    assert all("prompt" not in run and "prompt_preview" in run for run in first["runs"]), (
        "list entries carry previews only; full text comes from the detail endpoint"
    )
    assert http("GET", f"/api/runs?q={tag}&status=failed", port=port)[1]["runs"] == []
    assert http("GET", "/api/runs?sort=sideways", port=port)[0] == 400
    assert http("GET", "/api/runs?cursor=not-a-cursor", port=port)[0] == 400
    assert http("GET", "/api/runs?limit=0", port=port)[0] == 422
    capped = http("GET", "/api/runs?limit=999999", port=port)
    assert capped[0] == 200 and len(capped[1]["runs"]) <= webapp.RUNS_PAGE_MAX
    oldest = http("GET", f"/api/runs?q={tag}&sort=oldest&limit=1", port=port)[1]["runs"][0]
    assert oldest["prompt_preview"].endswith("number 0")


@pytest.mark.asyncio
async def test_run_detail_pages_and_filters_events(server):
    port = server
    sid = f"detail-{uuid.uuid4().hex[:8]}"
    events = await run_one(port, sid, "detail paging probe")
    run = find_run(port, sid)[0]
    status, page = http("GET", f"/api/runs/{sid}/{run['id']}?offset=1&limit=3", port=port)
    assert status == 200 and page["total_events"] == len(events)
    assert len(page["events"]) == 3 and page["limit"] == 3
    assert [e["type"] for e in page["events"]] == [e["type"] for e in events[1:4]]
    status, thoughts = http("GET", f"/api/runs/{sid}/{run['id']}?types=thought", port=port)
    assert {e["type"] for e in thoughts["events"]} == {"thought"}
    assert thoughts["total_events"] == len([e for e in events if e["type"] == "thought"])
    assert http("GET", f"/api/runs/{sid}/{run['id']}?offset=-1", port=port)[0] == 422
    assert http("GET", f"/api/runs/{sid}/nope", port=port)[0] == 404
    assert http("GET", f"/api/runs/bad%20id/{run['id']}", port=port)[0] == 404


@pytest.mark.asyncio
async def test_secrets_are_redacted_on_disk_but_not_in_the_live_stream(server):
    port = server
    sid = f"secret-{uuid.uuid4().hex[:8]}"
    FakeManus.secret = True
    try:
        live = await run_one(port, sid, f"use credential {FAKE_KEY} please")
    finally:
        FakeManus.secret = False
    live_results = [e["data"]["result"] for e in live if e["type"] == "tool_end"]
    assert any(FAKE_KEY in result for result in live_results), "live stream must be unmodified"

    run = find_run(port, sid)[0]
    assert run["status"] == "completed"
    _, detail = http("GET", f"/api/runs/{sid}/{run['id']}", port=port)
    flat = json.dumps(detail)
    assert FAKE_KEY not in flat and "hunter2hunter2" not in flat and "[REDACTED]" in flat
    on_server_loop(webapp.SESSION_STORE.flush(sid))
    for path in Path(webapp.SESSION_STORE_DIR).rglob("*"):
        if path.is_file() and sid in str(path):
            assert FAKE_KEY.encode() not in path.read_bytes(), f"secret leaked into {path.name}"


@pytest.mark.asyncio
async def test_screenshots_are_served_from_authenticated_blobs_not_inlined(server):
    port = server
    sid = f"shot-{uuid.uuid4().hex[:8]}"
    FakeManus.screenshot = PNG_B64
    try:
        live = await run_one(port, sid, "take a screenshot")
    finally:
        FakeManus.screenshot = None
    assert any(e["type"] == "browser_screenshot" and e["data"].get("image") for e in live)

    run = find_run(port, sid)[0]
    status, detail = http("GET", f"/api/runs/{sid}/{run['id']}", port=port)
    shots = [e for e in detail["events"] if e["type"] == "browser_screenshot"]
    assert shots and PNG_B64 not in json.dumps(detail)
    reference = shots[0]["data"]["image_ref"]
    status, headers, body = raw_get(f"/api/blobs/{sid}/{reference}", port)
    assert status == 200 and body == PNG and headers["content-type"] == "image/png"
    assert raw_get(f"/api/blobs/{sid}/{reference}", port, auth=None)[0] == 401
    assert raw_get(f"/api/blobs/{sid}/not-a-real-blob.png", port)[0] == 404
    assert raw_get(f"/api/blobs/bad%20id/{reference}", port)[0] == 404
    assert raw_get(f"/api/blobs/{sid}/..%2F..%2Fsession.json", port)[0] == 404


def test_health_detail_reports_store_component_and_degrades_without_a_store(server, monkeypatch):
    port = server
    status, body = http("GET", "/api/health/detail", port=port)
    assert status == 200 and body["status"] == "healthy" and body["timestamp"] > 0
    component = body["components"]["session_store"]
    assert component["initialised"] is True and component["writable"] is True
    assert component["breaker_state"] == "closed" and component["dropped_events"] == 0

    monkeypatch.setattr(webapp, "SESSION_STORE", None)
    status, body = http("GET", "/api/health/detail", port=port)
    assert status == 200 and body["status"] == "unhealthy"
    assert http("GET", "/api/runs", port=port)[0] == 503, "history is unavailable, not crashing"
    assert http("GET", "/api/health", auth=None, port=port)[0] == 200, "the portal itself still serves"


@pytest.mark.asyncio
async def test_prune_endpoint_deletes_old_sessions_and_validates_input(server):
    port = server
    old_sid = f"old-{uuid.uuid4().hex[:8]}"
    started = int(time.time()) - 10 * 86_400

    async def make_old():
        store = webapp.SESSION_STORE
        await store.create_run(old_sid, "oldrun", "Old task", "old prompt", started)
        await store.finish_run(old_sid, "oldrun", "completed", started + 5, "", [], 1, 1, 1, 1)

    on_server_loop(make_old())
    assert http("POST", "/api/history/prune", {"older_than_days": 0}, port=port)[0] == 422
    assert http("POST", "/api/history/prune", {}, port=port)[0] == 422
    status, result = http("POST", "/api/history/prune", {"older_than_days": 5}, port=port)
    assert status == 200 and result["deleted_sessions"] >= 1 and "freed_bytes" in result
    status, body = http("GET", "/api/sessions", port=port)
    assert old_sid not in [s["id"] for s in body["sessions"]]
    assert not (Path(webapp.SESSION_STORE_DIR) / old_sid).exists()


@pytest.mark.asyncio
async def test_idle_agents_are_evicted_to_read_only_but_running_ones_are_not(server):
    port = server
    sid = f"idle-{uuid.uuid4().hex[:8]}"
    await run_one(port, sid, "make an agent")
    session = webapp.sessions[sid]
    assert session.agent is not None
    for _ in range(100):  # the server notices the closed socket asynchronously
        if session.websocket is None and not session.is_running:
            break
        await asyncio.sleep(0.02)
    assert on_server_loop(webapp._evict_idle_sessions(0, now=time.monotonic() + 100)) >= 1
    assert session.agent is None and session.read_only and session.evicted

    async with websockets.connect(ws_url(port, sid), additional_headers=ws_headers()) as ws:
        info = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert info["type"] == "session_info" and info["data"]["read_only"] is True
        assert info["data"]["evicted"] is True
        await ws.send(json.dumps({"type": "run", "prompt": "should be refused"}))
        replayed = [json.loads(await asyncio.wait_for(ws.recv(), 5)) for _ in range(2)]
        assert any(m["type"] == "error" and "read-only" in m["data"]["message"] for m in replayed)

    # a session with a run in flight is never evicted, even when "idle"
    busy_sid = f"busy-{uuid.uuid4().hex[:8]}"
    FakeManus.slow = True
    try:
        async with websockets.connect(ws_url(port, busy_sid), additional_headers=ws_headers()) as ws:
            await ws.send(json.dumps({"type": "run", "prompt": "long running"}))
            while json.loads(await asyncio.wait_for(ws.recv(), 20))["type"] != "step":
                pass
        busy = webapp.sessions[busy_sid]  # websocket closed, run still going
        evicted = on_server_loop(webapp._evict_idle_sessions(0, now=time.monotonic() + 100))
        assert busy.agent is not None and busy.is_running and not busy.read_only
        assert evicted == 0
        busy._task.cancel()
    finally:
        FakeManus.slow = False


@pytest.mark.asyncio
async def test_disk_failure_never_fails_the_run_and_history_recovers(server, monkeypatch):
    import workspace.session_store as session_store_module

    port = server
    sid = f"degrade-{uuid.uuid4().hex[:8]}"
    store = webapp.SESSION_STORE
    original_fs_write, original_atomic = store._fs_write, session_store_module._atomic_write
    failing = {"on": True}

    def broken_fs_write(descriptor, data):
        if failing["on"]:
            raise OSError(28, "No space left on device")
        original_fs_write(descriptor, data)

    def broken_atomic(path, payload):
        if failing["on"]:
            raise OSError(28, "No space left on device")
        original_atomic(path, payload)

    monkeypatch.setattr(store, "_fs_write", broken_fs_write)
    monkeypatch.setattr(session_store_module, "_atomic_write", broken_atomic)

    seen = []
    async with websockets.connect(ws_url(port, sid), additional_headers=ws_headers()) as ws:
        await ws.send(json.dumps({"type": "run", "prompt": "survive a full disk"}))
        while True:  # the run itself must still complete normally
            message = json.loads(await asyncio.wait_for(ws.recv(), 20))
            seen.append(message["type"])
            if message["type"] == "run_end":
                break
        assert "final_result" in seen and "error" not in seen, "the agent run must stay healthy"
        assert "history_degraded" in seen, "the operator must be told history is not saving"
        assert http("GET", "/api/health/detail", port=port)[1]["status"] == "degraded"

        failing["on"] = False  # the disk comes back
        recovered = False
        for _ in range(100):
            message = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if message["type"] == "history_recovered":
                recovered = True
                break
        assert recovered

    run = None
    for _ in range(100):  # the remembered completion is saved once the disk recovers
        runs = find_run(port, sid)
        if runs and runs[0]["status"] == "completed":
            run = runs[0]
            break
        await asyncio.sleep(0.05)
    assert run is not None, "the run's completion must be persisted after recovery"
    _, detail = http("GET", f"/api/runs/{sid}/{run['id']}", port=port)
    kinds = [event["type"] for event in detail["events"]]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end" and "final_result" in kinds
    assert http("GET", "/api/health/detail", port=port)[1]["status"] == "healthy"


@pytest.mark.asyncio
async def test_restored_long_session_replays_whole_runs_and_reports_the_window(server, monkeypatch):
    port = server
    sid = f"window-{uuid.uuid4().hex[:8]}"
    for index in range(3):
        await run_one(port, sid, f"window turn {index}")
    monkeypatch.setattr(webapp, "MAX_EVENT_LOG", 30)  # fits one whole run, not two
    webapp.sessions.pop(sid, None)  # simulate a restart: RAM gone, disk remains
    async with websockets.connect(ws_url(port, sid), additional_headers=ws_headers()) as ws:
        info = json.loads(await asyncio.wait_for(ws.recv(), 5))
        replay = json.loads(await asyncio.wait_for(ws.recv(), 5))
    assert info["data"]["runs_total"] == 3 and info["data"]["runs_shown"] == 1
    events = replay["data"]["events"]
    assert events[0]["type"] == "run_start" and events[0]["data"]["prompt"] == "window turn 2"
    assert events[-1]["type"] == "run_end" and len(events) <= 30, "whole runs only, never a mid-run cut"

    monkeypatch.setattr(webapp, "MAX_EVENT_LOG", 40)
    webapp.sessions.pop(sid, None)
    async with websockets.connect(ws_url(port, sid), additional_headers=ws_headers()) as ws:
        info = json.loads(await asyncio.wait_for(ws.recv(), 5))
        replay = json.loads(await asyncio.wait_for(ws.recv(), 5))
    assert info["data"]["runs_shown"] == 2
    prompts = [e["data"]["prompt"] for e in replay["data"]["events"] if e["type"] == "run_start"]
    assert prompts == ["window turn 1", "window turn 2"], "runs replay oldest-first within the window"


def test_importing_the_webapp_module_does_not_touch_the_history_store(tmp_path, monkeypatch):
    target = tmp_path / "never-created"
    monkeypatch.setenv("WEBAPP_SESSION_STORE_DIR", str(target))
    spec = importlib.util.spec_from_file_location("webapp_import_probe", PROJECT_ROOT / "workspace" / "webapp.py")
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    assert probe.SESSION_STORE is None
    assert not target.exists(), "importing must not create the store directory"


# ---------------------------------------------------------------- MCP API --
_MCP_STUB = {
    "mcpServers": {
        "alpha": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "alpha-pkg"],
            "env": {"ALPHA_API_KEY": "stub-secret"},
            "description": "Alpha test server",
        },
        "beta": {"type": "sse", "url": "http://127.0.0.1:9/sse", "enabled": False},
    }
}


@pytest.fixture
def mcp_stub(tmp_path):
    """Point the webapp's MCP registry at a throwaway mcp.json.

    Two layers are redirected: the file the toggle endpoint reads/writes
    (webapp.MCP_CONFIG_PATH) and the loader used to refresh the live config
    singleton (webapp.MCPSettings.load_server_config). The real
    config/mcp.json is never touched, and the global servers dict is
    restored afterwards because app.config is shared across test modules.
    """
    from app.config import MCPServerConfig

    temp = tmp_path / "mcp.json"
    temp.write_text(json.dumps(_MCP_STUB))

    class _FakeMCPSettings:
        @classmethod
        def load_server_config(cls):
            data = json.loads(temp.read_text())
            return {
                sid: MCPServerConfig(
                    type=sc["type"],
                    url=sc.get("url"),
                    command=sc.get("command"),
                    args=sc.get("args", []),
                    enabled=sc.get("enabled", True),
                    env=sc.get("env"),
                    description=sc.get("description"),
                )
                for sid, sc in data.get("mcpServers", {}).items()
            }

    orig_path = webapp.MCP_CONFIG_PATH
    orig_settings = webapp.MCPSettings
    orig_servers = dict(webapp.config.mcp_config.servers)
    webapp.MCP_CONFIG_PATH = temp
    webapp.MCPSettings = _FakeMCPSettings
    webapp._reload_mcp_servers()
    try:
        yield temp
    finally:
        webapp.MCP_CONFIG_PATH = orig_path
        webapp.MCPSettings = orig_settings
        webapp.config.mcp_config.servers.clear()
        webapp.config.mcp_config.servers.update(orig_servers)


def _mcp_ids(body):
    return {s["id"]: s for s in body["servers"]}


def test_mcp_servers_list(server, mcp_stub):
    port = server
    status, body = http("GET", "/api/mcp/servers", auth=None, port=port)
    assert status == 401  # auth enforced like every other API
    status, body = http("GET", "/api/mcp/servers", port=port)
    assert status == 200
    by_id = _mcp_ids(body)
    assert set(by_id) == {"browser_use", "alpha", "beta"}
    assert by_id["browser_use"]["enabled"] is True and by_id["browser_use"]["builtin"] is True
    assert by_id["alpha"]["enabled"] is True
    assert by_id["beta"]["enabled"] is False
    assert by_id["alpha"]["summary"] == "npx -y alpha-pkg"
    assert by_id["beta"]["summary"] == "http://127.0.0.1:9/sse"
    # descriptions surface; env values must never leak into the API (§2.4)
    assert by_id["alpha"]["description"] == "Alpha test server"
    assert by_id["beta"]["description"] == ""
    assert by_id["browser_use"]["description"]
    assert all("env" not in s for s in body["servers"])
    assert "stub-secret" not in json.dumps(body)


def test_mcp_toggle_persists_and_unknown_server_404(server, mcp_stub):
    port = server
    status, body = http("POST", "/api/mcp/servers/alpha/toggle", {"enabled": False}, port=port)
    assert status == 200 and body["enabled"] is False and body["applied"] == 0
    on_disk = json.loads(mcp_stub.read_text())
    assert on_disk["mcpServers"]["alpha"]["enabled"] is False
    status, body = http("GET", "/api/mcp/servers", port=port)
    assert _mcp_ids(body)["alpha"]["enabled"] is False

    status, body = http("POST", "/api/mcp/servers/alpha/toggle", {"enabled": True}, port=port)
    assert status == 200 and body["enabled"] is True
    assert json.loads(mcp_stub.read_text())["mcpServers"]["alpha"]["enabled"] is True

    status, _ = http("POST", "/api/mcp/servers/nope/toggle", {"enabled": True}, port=port)
    assert status == 404


def test_mcp_toggle_browser_use_roundtrip(server, mcp_stub):
    port = server
    status, body = http("POST", "/api/mcp/servers/browser_use/toggle", {"enabled": False}, port=port)
    assert status == 200 and body["enabled"] is False
    on_disk = json.loads(mcp_stub.read_text())
    assert on_disk["mcpServers"]["browser_use"]["enabled"] is False
    assert _mcp_ids(http("GET", "/api/mcp/servers", port=port)[1])["browser_use"]["enabled"] is False

    status, body = http("POST", "/api/mcp/servers/browser_use/toggle", {"enabled": True}, port=port)
    assert status == 200 and body["enabled"] is True
    # Re-enabling removes the explicit entry so the built-in auto-connect resumes.
    assert "browser_use" not in json.loads(mcp_stub.read_text())["mcpServers"]


def test_mcp_toggle_unknown_server_reloads_registry(server, mcp_stub):
    """A server appended to mcp.json while the app runs must be toggleable
    without a restart (the endpoint refreshes the registry before 404ing)."""
    port = server
    data = json.loads(mcp_stub.read_text())
    data["mcpServers"]["gamma"] = {"type": "stdio", "command": "npx", "args": ["-y", "gamma"]}
    mcp_stub.write_text(json.dumps(data))
    # No _reload_mcp_servers() call here: the app has not seen gamma yet.
    status, body = http("POST", "/api/mcp/servers/gamma/toggle", {"enabled": False}, port=port)
    assert status == 200 and body["server_id"] == "gamma"
    assert json.loads(mcp_stub.read_text())["mcpServers"]["gamma"]["enabled"] is False


def test_mcp_toggle_hot_applies_to_live_session(server, mcp_stub):
    port = server

    class LiveStubAgent:
        def __init__(self):
            self.connected_servers = {"alpha": "npx"}
            self.mcp_clients = SimpleNamespace(tools=[])
            self.calls = []

        async def disconnect_mcp_server(self, server_id=""):
            self.calls.append(("disconnect", server_id))
            self.connected_servers.pop(server_id, None)

        async def connect_mcp_server(self, server_url, server_id="", **kwargs):
            self.calls.append(("connect", server_id, server_url))
            self.connected_servers[server_id] = server_url

    stub = LiveStubAgent()
    sid = f"mcpstub-{uuid.uuid4().hex[:8]}"
    session = webapp.AgentSession()
    session.agent = stub
    webapp.sessions[sid] = session
    try:
        status, body = http("POST", "/api/mcp/servers/alpha/toggle", {"enabled": False}, port=port)
        assert status == 200 and body["applied"] == 1
        assert ("disconnect", "alpha") in stub.calls
        assert "alpha" not in stub.connected_servers

        status, body = http("POST", "/api/mcp/servers/beta/toggle", {"enabled": True}, port=port)
        assert status == 200 and body["applied"] == 1
        assert ("connect", "beta", "http://127.0.0.1:9/sse") in stub.calls
    finally:
        webapp.sessions.pop(sid, None)


def test_mcp_env_merged_over_sdk_default():
    # app.tool.mcp._merged_env: config env must EXTEND the SDK default env
    # (PATH et al.), not replace it - otherwise spawned npx/uvx lose PATH.
    from app.tool.mcp import _merged_env

    assert _merged_env(None) is None
    assert _merged_env({}) is None
    merged = _merged_env({"MY_KEY": "secret"})
    assert merged["MY_KEY"] == "secret"
    assert "PATH" in merged and "HOME" in merged
    overridden = _merged_env({"PATH": "/custom/bin"})
    assert overridden["PATH"] == "/custom/bin"


def test_models_available_endpoint(server, monkeypatch):
    port = server
    calls = []

    async def fake_fetch(base_url, api_key):
        calls.append((base_url, api_key))
        if "fail" in base_url:
            raise LookupError("provider returned HTTP 401")
        return ["model-a", "model-b"]  # stub pre-sorted; sorting is _fetch's job

    monkeypatch.setattr(webapp, "_fetch_provider_models", fake_fetch)

    status, _ = http("POST", "/api/models/available", {"base_url": "https://api.example.com/v1"}, auth=None, port=port)
    assert status == 401  # auth enforced

    status, body = http("POST", "/api/models/available", {"base_url": "https://api.example.com/v1", "api_key": "k"}, port=port)
    assert status == 200 and body["models"] == ["model-a", "model-b"] and body["count"] == 2
    assert calls[-1] == ("https://api.example.com/v1", "k")

    # SSRF guard: plain http is only allowed for loopback providers
    status, _ = http("POST", "/api/models/available", {"base_url": "http://169.254.169.254/latest"}, port=port)
    assert status == 400
    status, body = http("POST", "/api/models/available", {"base_url": "http://localhost:11434/v1"}, port=port)
    assert status == 200

    # provider error maps to 502 without leaking internals
    status, body = http("POST", "/api/models/available", {"base_url": "https://fail.example.com"}, port=port)
    assert status == 502 and "401" in body["detail"]


def _multipart(field, filename, content, ctype="text/plain"):
    boundary = "----omtestboundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return body, boundary


def test_upload_endpoint(server, tmp_path, monkeypatch):
    port = server
    # Both must move together: the endpoint reports paths relative to WORKSPACE_ROOT.
    monkeypatch.setattr(webapp, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(webapp, "UPLOAD_DIR", tmp_path / "uploads")

    def post(body, boundary, auth=AUTH):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/upload",
            method="POST",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                **({"Authorization": "Basic " + _b64(auth)} if auth else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    body, boundary = _multipart("files", "../../etc/evil.txt", b"attack")
    status, _ = post(body, boundary, auth=None)
    assert status == 401  # auth enforced

    status, data = post(body, boundary)
    assert status == 200
    # traversal stripped: lands as a bare sanitized name inside the upload dir
    assert data["files"][0]["name"] == "evil.txt"
    assert (tmp_path / "uploads" / "evil.txt").read_bytes() == b"attack"

    # same name again -> not overwritten, timestamp-prefixed copy
    body, boundary = _multipart("files", "evil.txt", b"second")
    status, data = post(body, boundary)
    assert status == 200
    assert data["files"][0]["name"].endswith("-evil.txt")
    assert (tmp_path / "uploads" / "evil.txt").read_bytes() == b"attack"


def test_task_journal_and_export(tmp_path, monkeypatch):
    obs, icloud, ws = tmp_path / "vault", tmp_path / "icloud", tmp_path / "ws"
    obs.mkdir(), icloud.mkdir()
    (ws / "reports").mkdir(parents=True)
    (ws / "reports" / "summary.md").write_text("# Summary")
    monkeypatch.setattr(webapp, "OBSIDIAN_TASKS_DIR", obs)
    monkeypatch.setattr(webapp, "OUTPUT_EXPORT_DIR", icloud)
    monkeypatch.setattr(webapp, "WORKSPACE_ROOT", ws)

    session = webapp.AgentSession()
    session.created_at = 1_700_000_000
    session.title = "Research AeroDR"
    session._first_prompt = "Research AeroDR API surface"
    session.touched_files = {"reports/summary.md"}

    webapp._write_task_journal(session, "running")
    journal = session.journal_path()
    assert journal.parent == obs
    text = journal.read_text()
    assert "status: running" in text and "Research AeroDR API surface" in text

    copied = webapp._export_task_outputs(session, ("reports/summary.md",))
    assert copied == ["reports/summary.md"]
    exported = icloud / journal.stem / "reports" / "summary.md"
    assert exported.read_text() == "# Summary"

    webapp._write_task_journal(session, "completed", "Done: 3 endpoints found.", tuple(copied))
    text = journal.read_text()
    assert "status: completed" in text
    assert "## Output" in text and "3 endpoints found" in text
    assert "## Files produced" in text and "reports/summary.md" in text

    # traversal attempts in touched_files never escape the workspace
    session.touched_files = {"../../etc/passwd"}
    assert webapp._export_task_outputs(session, ("../../etc/passwd",)) == []


def test_extract_auth_url():
    # Authorization handoffs must surface a clean URL, and only then.
    google = (
        "**ACTION REQUIRED: Google Authentication Needed**\n"
        "Authorization URL: https://accounts.google.com/o/oauth2/auth?client_id=x&scope=y.\n"
        "2. After successful authorization, retry."
    )
    assert (
        webapp._extract_auth_url(google)
        == "https://accounts.google.com/o/oauth2/auth?client_id=x&scope=y"
    )
    assert webapp._extract_auth_url("authentication needed: visit https://auth.example.com/consent now") == "https://auth.example.com/consent"
    # no marker -> no card (ordinary URLs in results must not trigger it)
    assert webapp._extract_auth_url("fetched https://example.com fine") is None
    # marker but no URL -> nothing to link
    assert webapp._extract_auth_url("ACTION REQUIRED but no link here") is None
    assert webapp._extract_auth_url("") is None


@pytest.mark.asyncio
async def test_run_start_precedes_agent_initialization(server):
    """UX-001 regression guard: run_start must reach the client immediately,
    even while agent/MCP initialization is still in flight (pre-fix it was
    only emitted after _ensure_agent, leaving a dead-silent UI window)."""
    port = server
    FakeManus.slow_create = True
    try:
        sid = f"smoke-{uuid.uuid4().hex[:8]}"
        async with websockets.connect(ws_url(port, sid), additional_headers=ws_headers()) as ws:
            await ws.send(json.dumps({"type": "run", "prompt": "slow init ordering probe"}))
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert first["type"] == "run_start", (
                f"run_start must arrive before agent init completes; got {first['type']}"
            )
            assert first["data"]["max_steps"] == webapp.MAX_STEPS
            # UX-002: every event carries a server ts for replay-safe durations
            assert isinstance(first["data"].get("ts"), int)
            # Drain so the session settles before the next test shares the server.
            await collect_until_run_end(ws)
    finally:
        FakeManus.slow_create = False


def test_security_headers_and_health_payload(server):
    """SEC-001: hardening headers on shell and API responses.
    SEC-002: the one public route returns a minimal, non-fingerprinting body."""
    port = server
    auth = {"Authorization": "Basic " + _b64(AUTH)}
    for path in ("/", "/api/health"):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=auth)
        with urllib.request.urlopen(req, timeout=10) as res:
            assert res.headers.get("X-Content-Type-Options") == "nosniff"
            assert res.headers.get("X-Frame-Options") == "DENY"
            assert res.headers.get("Referrer-Policy") == "same-origin"
            csp = res.headers.get("Content-Security-Policy", "")
            assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    status, body = http("GET", "/api/health", auth=None, port=port)
    assert status == 200 and body == {"status": "ok"}
