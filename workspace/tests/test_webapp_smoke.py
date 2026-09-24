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


# ---------------------------------------------------------------- MCP API --
_MCP_STUB = {
    "mcpServers": {
        "alpha": {"type": "stdio", "command": "npx", "args": ["-y", "alpha-pkg"]},
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
