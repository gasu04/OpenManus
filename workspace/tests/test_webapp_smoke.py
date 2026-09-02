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

_spec = importlib.util.spec_from_file_location("webapp_under_test", PROJECT_ROOT / "workspace" / "webapp.py")
webapp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(webapp)

import uvicorn  # noqa: E402
import websockets  # noqa: E402

AUTH = ("admin", "testpass")


class FakeToolCall:
    def __init__(self, name, arguments):
        self.function = SimpleNamespace(name=name, arguments=arguments)


class FakeManus:
    """Deterministic stand-in for Manus; drives the same instrumentation."""

    slow = False  # per-test knob: True adds latency so `stop` lands mid-run

    def __init__(self):
        self.max_steps = 5
        self.current_step = 0
        self.messages = []
        self.llm = SimpleNamespace(total_input_tokens=0, total_completion_tokens=0)
        self.tool_calls = []

    @classmethod
    async def create(cls):
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

    async def execute_tool(self, command):
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
    assert http("GET", "/api/sessions", auth=None, port=port)[0] == 401
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
