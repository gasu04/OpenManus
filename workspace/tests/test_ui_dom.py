"""Browser-level regression tests (Playwright) for the OpenManus web UI.

Guards the highest-risk frontend fixes against the original repro steps:

- P0-1: prompt text must survive a submit attempt while disconnected
- P0-1: double-submit guard while a run request is pending
- P1-1: reconnect banner appears when the socket drops
- P1-6: tablist/tab/tabpanel ARIA contract
- P2-1: theme toggle persists across reloads (dark is the default)
- P2-2: #s=<sessionId> deep link adopts the session

Round 2 (§2 mandatory-feature gaps + §4 reliability):
- P0-3: window error / unhandledrejection -> visible toast (no silent JS failures)
- P1-8: tool-call inspector expands with structured args/result/status
- P1-9: copy-to-clipboard on error messages
- P1-10: Retry action on a failed run resends the original prompt
- P2-4/5: client-side filters on history drawer + live timeline

Run (requires the smoke-test server from test_webapp_smoke to be importable;
boots its own): .venv/bin/python -m pytest workspace/tests/test_ui_dom.py -v
"""

import importlib.util
import json
import os
import re
import threading
import time
import urllib.request
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("WEBAPP_AUTH_PASS", "testpass")

_spec = importlib.util.spec_from_file_location("webapp_dom_under_test", PROJECT_ROOT / "workspace" / "webapp.py")
webapp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(webapp)

import uvicorn  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

AUTH = ("admin", "testpass")


@pytest.fixture(scope="module")
def server():
    class NoopAgent:
        @classmethod
        async def create(cls):
            return cls()

        async def run(self, prompt):
            pass

        async def cleanup(self):
            pass

    webapp.Manus = NoopAgent
    webapp.sessions.clear()
    config = uvicorn.Config(webapp.app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.2)
    assert server.started
    yield server.servers[0].sockets[0].getsockname()[1]
    server.should_exit = True


@pytest.fixture(scope="module")
def page(server):
    # Auth is now session-cookie based (Remote Access engagement, Phase 1):
    # "/" no longer sends a WWW-Authenticate challenge (by design - real
    # users get a proper login page, not a native browser popup), so
    # Playwright's http_credentials (which waits for that challenge) no
    # longer applies here. Log in through the real form instead, exactly as
    # an operator would - this also exercises the login page itself.
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        context.grant_permissions(["clipboard-read", "clipboard-write"])
        page = context.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(f"http://127.0.0.1:{server}/", wait_until="networkidle")
        page.fill("#u", AUTH[0])
        page.fill("#p", AUTH[1])
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")
        page.wait_for_function("() => window.__om && window.__om.state.ws?.readyState === 1")
        yield page, errors
        print("console errors:", errors)
        browser.close()


def test_login_wrong_credentials_shows_error_and_keeps_login_page(page):
    page, _ = page
    # Fresh, unauthenticated context off the same browser instance (a nested
    # sync_playwright() call here would conflict with the module fixture's).
    ctx = page.context.browser.new_context()
    pg = ctx.new_page()
    pg.goto(f"http://{page.url.split('/')[2]}/", wait_until="networkidle")
    pg.fill("#u", AUTH[0])
    pg.fill("#p", "definitely-wrong-password")
    pg.click("button[type=submit]")
    expect(pg.locator("#e")).to_have_text("Invalid credentials", timeout=5000)
    # Must still be the login form, not the app shell (acceptance test #1).
    assert pg.locator("#promptInput").count() == 0
    ctx.close()


def test_unauthenticated_root_serves_login_not_app_shell(page):
    page, _ = page
    ctx = page.context.browser.new_context()
    pg = ctx.new_page()
    resp = pg.goto(f"http://{page.url.split('/')[2]}/", wait_until="networkidle")
    assert resp.status == 401
    assert pg.locator("#promptInput").count() == 0
    assert pg.locator("#f input#u").count() == 1  # the login form, and nothing else
    ctx.close()


def test_p0_prompt_survives_disconnected_send(page):
    page, errors = page
    page.fill("#promptInput", "critical prompt that must not be lost")
    # Repro original bug: socket not OPEN at submit time
    page.evaluate("() => window.__om.state.ws.close()")
    page.wait_for_selector("#reconnectBanner:not(.hidden)", timeout=5000)
    page.evaluate("() => window.__om.submitPrompt()")
    expect(page.locator("#promptInput")).to_have_value("critical prompt that must not be lost")
    expect(page.locator(".error-banner").last).to_be_visible()
    page.wait_for_function("() => window.__om.state.ws?.readyState === 1", timeout=20000)  # reconnects


def test_p0_double_submit_guard_keeps_text(page):
    page, errors = page
    page.wait_for_function("() => window.__om.state.ws?.readyState === 1")
    page.fill("#promptInput", "queued task")
    page.evaluate("() => { window.__om.state.submitPending = true; }")
    page.evaluate("() => window.__om.submitPrompt()")
    expect(page.locator("#promptInput")).to_have_value("queued task")
    page.evaluate("() => { window.__om.state.submitPending = false; }")
    page.fill("#promptInput", "")


def test_p1_tablist_aria_contract(page):
    page, _ = page
    expect(page.locator("#tabs")).to_have_attribute("role", "tablist")
    tabs = page.locator("#tabs [role=tab]")
    assert tabs.count() == 5
    expect(tabs.first).to_have_attribute("aria-selected", "true")
    assert page.locator("[role=tabpanel]").count() == 5
    for label in ["btnHistory", "btnSettings", "btnNew", "btnTheme", "zoomIn", "zoomOut"]:
        assert page.locator(f"#{label}").get_attribute("aria-label"), f"#{label} missing aria-label"


def test_p1_reconnect_banner_appears_on_drop(page):
    page, _ = page
    page.evaluate("() => window.__om.state.ws.close()")
    expect(page.locator("#reconnectBanner")).to_be_visible()
    expect(page.locator("#liveLabel")).to_have_text("Reconnecting")
    page.wait_for_function("() => window.__om.state.ws?.readyState === 1", timeout=20000)
    expect(page.locator("#reconnectBanner")).to_be_hidden()


def test_p2_theme_toggle_persists_and_dark_is_default(page):
    page, _ = page
    default_theme = page.evaluate("() => document.documentElement.dataset.theme")
    assert default_theme == "dark", "ops console must default to dark"
    page.click("#btnTheme")
    assert page.evaluate("() => document.documentElement.dataset.theme") == "light"
    assert page.evaluate("() => localStorage.getItem('om_theme')") == "light"
    page.reload(wait_until="networkidle")
    page.wait_for_function("() => window.__om && window.__om.state.ws?.readyState === 1")
    assert page.evaluate("() => document.documentElement.dataset.theme") == "light"
    page.click("#btnTheme")  # restore dark for the next test
    assert page.evaluate("() => localStorage.getItem('om_theme')") == "dark"


def test_p2_session_deep_link(page):
    page, _ = page
    sid = "deeplink-test-session"
    page.evaluate(f"() => location.hash = '#s={sid}'")
    page.reload(wait_until="networkidle")
    page.wait_for_function("() => window.__om && window.__om.state.ws?.readyState === 1")
    assert page.evaluate("() => window.__om.state.sessionId") == sid


def test_p0_global_error_handler_shows_toast(page):
    page, _ = page
    # A thrown error in a macrotask can't be caught locally — it must surface
    # via window.onerror, not vanish into devtools only (§4/P0-3).
    page.evaluate("() => setTimeout(() => { throw new Error('round2-boom') }, 0)")
    expect(page.locator(".toast.error").last).to_be_visible(timeout=3000)
    expect(page.locator(".toast.error").last).to_contain_text("Something went wrong")


def test_p0_unhandled_rejection_shows_toast(page):
    page, _ = page
    page.evaluate("() => { Promise.reject(new Error('round2-rejection')); }")
    expect(page.locator(".toast.error").last).to_be_visible(timeout=3000)
    expect(page.locator(".toast.error").last).to_contain_text("background action failed")


def test_p1_tool_call_inspector_expands_with_structured_data(page):
    page, _ = page
    # A python_execute event auto-switches to the Terminal tab by design
    # (autoSwitch) unless the operator has pinned a tab — pin Live first, the
    # same way a real click on the Live tab would.
    page.evaluate("() => window.__om.switchTab('live', true)")
    page.evaluate("""() => {
        window.__om.dispatch('tool_start', {
            id: 'itest-1', name: 'python_execute', category: 'terminal',
            arguments: { code: 'print(1+1)' },
        });
    }""")
    row = page.locator('.tl-tool[data-call-id="itest-1"]')
    expect(row).to_be_visible()
    # Result arrives slightly later, same as a real tool_end event.
    page.evaluate("""() => {
        window.__om.dispatch('tool_end', {
            id: 'itest-1', ok: true, duration_ms: 42, result: '2',
        });
    }""")
    row.locator(".tl-expand").click()
    panel = row.locator("xpath=following-sibling::div[1][contains(@class,'tl-inspector')]")
    expect(panel).to_be_visible()
    expect(panel).to_contain_text("python_execute")
    expect(panel).to_contain_text("succeeded in 42ms")
    expect(panel).to_contain_text('"code": "print(1+1)"')  # pretty-printed JSON, not a blob
    expect(panel).to_contain_text("2")  # the result
    # Collapse toggles it away (not a second panel stacking up).
    row.locator(".tl-expand").click()
    expect(row.locator("xpath=following-sibling::div[1][contains(@class,'tl-inspector')]")).to_have_count(0)


def test_p1_copy_button_on_error_banner_copies_text(page):
    page, _ = page
    page.evaluate("() => window.__om.dispatch('error', { message: 'round2 copy-test error' })")
    banner = page.locator(".error-banner", has_text="round2 copy-test error").last
    banner.locator(".copy-btn").click()
    expect(page.locator(".toast", has_text="Copied to clipboard").last).to_be_visible(timeout=2000)
    clipboard_text = page.evaluate("() => navigator.clipboard.readText()")
    assert clipboard_text == "round2 copy-test error"


def test_p1_retry_button_resends_the_original_prompt(page):
    page, _ = page
    marker = "retry-me-round2-marker"
    page.evaluate(f"""() => {{
        window.__om.dispatch('run_start', {{ prompt: '{marker}' }});
        window.__om.dispatch('error', {{ message: 'synthetic failure for retry test' }});
        window.__om.dispatch('run_end', {{}});
    }}""")
    retry_btn = page.locator(".tl-retry").last
    expect(retry_btn).to_be_visible()
    retry_btn.click()
    # A real `run` message went out and the (no-op) fake agent echoed a real
    # run_start back — the marker prompt must show up as a new chat bubble.
    expect(page.locator(".msg-user .bubble", has_text=marker).last).to_be_visible(timeout=10000)


def test_p2_timeline_filter_hides_non_matching_rows(page):
    page, _ = page
    page.evaluate("""() => {
        window.__om.dispatch('tool_start', {
            id: 'filter-test-1', name: 'web_search', category: 'browser',
            arguments: { query: 'unique-filter-marker-xyz' },
        });
    }""")
    row = page.locator('.tl-tool[data-call-id="filter-test-1"]')
    expect(row).to_be_visible()
    page.fill("#timelineFilter", "text-that-matches-nothing-at-all")
    expect(row).to_have_class(re.compile(r"\bhidden\b"))
    page.fill("#timelineFilter", "")
    expect(row).not_to_have_class(re.compile(r"\bhidden\b"))


def test_p2_history_filter_hides_non_matching_rows(page):
    page, _ = page
    page.click("#btnHistory")
    expect(page.locator("#historyDrawer")).to_be_visible()
    page.fill("#historySearch", "text-that-matches-nothing-at-all")
    visible = page.locator(".history-item:not(.hidden)")
    assert visible.count() == 0
    page.fill("#historySearch", "")
    page.click("#historyClose")


# ------------------------------------------------------------- MCP modal --
_MCP_STUB = {
    "mcpServers": {
        "alpha": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "alpha-pkg"],
            "description": "Alpha test server",
        },
        "beta": {"type": "sse", "url": "http://127.0.0.1:9/sse", "enabled": False},
    }
}


@pytest.fixture(scope="module")
def mcp_stub(tmp_path_factory):
    """Redirect the DOM-test webapp's MCP registry to a throwaway mcp.json.

    Same redirect as test_webapp_smoke's mcp_stub (file path + loader), kept
    local because this module boots its own webapp module instance. The real
    config/mcp.json is never read or written.
    """
    from app.config import MCPServerConfig

    temp = tmp_path_factory.mktemp("mcpdom") / "mcp.json"
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


def test_mcp_modal_lists_and_toggles(page, mcp_stub):
    page, errors = page
    errors_before = len(errors)  # earlier tests leave known-benign console noise
    page.click("#btnMcp")
    expect(page.locator("#mcpModal")).to_be_visible()
    cards = page.locator(".mcp-card")
    expect(cards).to_have_count(3)  # browser_use (built-in) + alpha + beta

    builtin = page.locator('.mcp-card[data-id="browser_use"]')
    expect(builtin.locator(".mcp-badge")).to_have_text("built-in")
    expect(builtin.locator("input")).to_be_checked()
    expect(page.locator('.mcp-card[data-id="beta"] input')).not_to_be_checked()
    expect(page.locator('.mcp-card[data-id="beta"]')).to_have_class(re.compile(r"\bdisabled\b"))
    # brief description renders under the server name
    expect(page.locator('.mcp-card[data-id="alpha"] .mcp-desc')).to_have_text("Alpha test server")

    # Toggle alpha off: card re-renders disabled, toast confirms, file persists.
    page.locator('.mcp-card[data-id="alpha"] .slider').click()
    expect(page.locator(".toast").last).to_contain_text("alpha deactivated", timeout=5000)
    expect(page.locator('.mcp-card[data-id="alpha"]')).to_have_class(re.compile(r"\bdisabled\b"))
    expect(page.locator('.mcp-card[data-id="alpha"] .mcp-status')).to_have_text("disabled")
    assert json.loads(mcp_stub.read_text())["mcpServers"]["alpha"]["enabled"] is False

    # Toggle back on (leave the stub clean), then close via Escape.
    page.locator('.mcp-card[data-id="alpha"] .slider').click()
    expect(page.locator('.mcp-card[data-id="alpha"]')).not_to_have_class(re.compile(r"\bdisabled\b"), timeout=5000)
    assert json.loads(mcp_stub.read_text())["mcpServers"]["alpha"]["enabled"] is True
    page.keyboard.press("Escape")
    expect(page.locator("#mcpModal")).to_be_hidden()
    assert errors[errors_before:] == []  # this test added no console errors


def test_model_form_fetch_populates_datalist(page, monkeypatch):
    """The Fetch button fills the Model ID datalist from the provider probe
    (endpoint stubbed here; its own contract is covered in the smoke suite)."""
    page, errors = page
    errors_before = len(errors)

    async def fake_fetch(base_url, api_key):
        return ["kimi-k3", "kimi-k2.6", "kimi-k2.7-code"]

    monkeypatch.setattr(webapp, "_fetch_provider_models", fake_fetch)

    page.click("#btnSettings")
    expect(page.locator("#modelModal")).to_be_visible()
    page.select_option("#fPreset", label="Moonshot AI (Kimi)")
    assert page.input_value("#fBaseUrl") == "https://api.moonshot.ai/v1"
    page.click("#fFetchModels")
    expect(page.locator("#formMsg")).to_contain_text("3 models available", timeout=5000)
    options = page.locator("#fModelChoices option")
    assert options.count() == 3
    assert options.nth(0).get_attribute("value") == "kimi-k3"
    page.keyboard.press("Escape")
    expect(page.locator("#modelModal")).to_be_hidden()
    assert errors[errors_before:] == []


def test_auth_card_and_bare_url_linkify(page):
    """Regression for 'auth URL arrived as a screenshot/unclickable blob':
    an auth_required event renders a card with a real link, and bare URLs in
    final answers become anchors."""
    page, errors = page
    errors_before = len(errors)
    page.evaluate(
        """() => {
          window.__om.dispatch('auth_required', {url: 'https://accounts.google.com/o/oauth2/auth?x=1', tool: 'mcp_google_list_calendars'});
          window.__om.dispatch('final_result', {content: 'Consent: https://accounts.google.com/o/oauth2/auth?x=1. Then retry.'});
        }"""
    )
    card = page.locator(".auth-card")
    expect(card).to_be_visible()
    expect(card.locator(".auth-title")).to_contain_text("Authorization needed")
    expect(card.locator("a.auth-open")).to_have_attribute(
        "href", "https://accounts.google.com/o/oauth2/auth?x=1"
    )
    final_link = page.locator(".final .final-rendered a").last
    expect(final_link).to_have_attribute("href", "https://accounts.google.com/o/oauth2/auth?x=1")
    expect(page.locator(".final .final-head")).to_contain_text("Final answer")
    assert errors[errors_before:] == []


def test_logout_invalidates_the_session_server_side(page):
    page, _ = page
    old_cookie = next(c for c in page.context.cookies() if c["name"] == "om_session")
    page.on("dialog", lambda d: d.accept())  # confirm() on the logout button
    page.click("#btnLogout")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#f input#u")).to_be_visible(timeout=5000)  # back at the login form
    # Prove *server-side* invalidation, not just the client forgetting the
    # cookie: replay the exact old token and confirm the server itself
    # rejects it.
    page.context.add_cookies([old_cookie])
    resp = page.request.get(page.url)
    assert resp.status == 401


def test_no_console_errors_after_suite(page):
    _, errors = page
    # The two synthetic global-error tests are expected to log to
    # console.error by design (that's the point of the safety net); the
    # logout test's final reload deliberately hits "/" post-logout and gets
    # a real 401 (login page), which Chromium logs for the top-level
    # navigation itself — also expected, not a defect.
    unexpected = [
        e for e in errors
        if "favicon" not in e
        and "round2-boom" not in e
        and "round2-rejection" not in e
        and "401 (Unauthorized)" not in e
    ]
    assert not unexpected, f"console errors: {unexpected}"
