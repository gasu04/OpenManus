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
- P2-4/5: filters on the Previous Runs list (now server-side) + live timeline

Run (requires the smoke-test server from test_webapp_smoke to be importable;
boots its own): .venv/bin/python -m pytest workspace/tests/test_ui_dom.py -v
"""

import asyncio
import importlib.util
import json
import os
import re
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("WEBAPP_AUTH_PASS", "testpass")
# Keep test runs' task journals/exports out of the operator's real vault.
os.environ.setdefault("WEBAPP_OBSIDIAN_TASKS_DIR", tempfile.mkdtemp(prefix="om-test-vault-"))
os.environ.setdefault("WEBAPP_OUTPUT_EXPORT_DIR", tempfile.mkdtemp(prefix="om-test-export-"))
# Keep persisted run history out of the operator's real session store.
os.environ.setdefault("WEBAPP_SESSION_STORE_DIR", tempfile.mkdtemp(prefix="om-test-sessions-"))
# Keep model-switch tests off the operator's real model registry (active choice).
os.environ.setdefault(
    "WEBAPP_MODELS_FILE",
    str(Path(tempfile.mkdtemp(prefix="om-test-models-")) / "webapp_models.json"),
)

_spec = importlib.util.spec_from_file_location("webapp_dom_under_test", PROJECT_ROOT / "workspace" / "webapp.py")
webapp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(webapp)

import uvicorn  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

AUTH = ("admin", "testpass")
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-screenshot-bytes" * 4
PNG_B64 = __import__("base64").b64encode(PNG).decode()


@pytest.fixture(scope="module")
def server():
    class NoopAgent:
        @classmethod
        async def create(cls):
            return cls()

        async def run(self, prompt):
            pass

        def refresh_skills_prompt(self, loader):
            self.system_prompt = loader.prompt_index()

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
    assert tabs.count() == 7
    expect(tabs.first).to_have_attribute("aria-selected", "true")
    assert page.locator("[role=tabpanel]").count() == 7
    for label in ["btnHistory", "btnMcp", "btnSettings", "btnNew", "btnTheme", "btnSend", "btnStop", "modelChip", "zoomIn", "zoomOut"]:
        assert page.locator(f"#{label}").get_attribute("aria-label"), f"#{label} missing aria-label"


def test_tab_arrow_keys_wrap_without_changing_active_tab(page):
    page, _ = page
    active = page.evaluate("() => window.__om.state.activeTab")
    page.locator("#tab-runs").focus()
    page.keyboard.press("ArrowRight")
    expect(page.locator("#tab-live")).to_be_focused()
    assert page.evaluate("() => window.__om.state.activeTab") == active
    page.keyboard.press("ArrowLeft")
    expect(page.locator("#tab-runs")).to_be_focused()
    assert page.evaluate("() => window.__om.state.activeTab") == active


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


def on_server_loop(coro, timeout=15):
    """Run a coroutine on the webapp's own event loop (the store is loop-bound)."""
    return asyncio.run_coroutine_threadsafe(coro, webapp.app.state.loop).result(timeout)


def seed_run(sid, rid, started_at, *, title, prompt, final="", status="completed", seconds=5,
             tokens=(0, 0), files=(), events=()):
    """Persist one finished run (with optional events) through the real store."""

    async def go():
        store = webapp.SESSION_STORE
        await store.create_run(sid, rid, title, prompt, started_at)
        for kind, data in events:
            store.enqueue_event(sid, rid, {"type": kind, "data": {"ts": started_at * 1000, **data}})
        if status != "running":
            await store.finish_run(sid, rid, status, started_at + seconds, final, list(files), 1, 1, *tokens)
        await store.flush(sid)

    on_server_loop(go())


def open_runs_tab(page, query=""):
    page.evaluate("() => window.__om.switchTab('runs', true)")
    page.evaluate("() => document.querySelector('#runBack')?.click()")  # leave any open detail
    page.fill("#runsSearch", query)
    if query:  # search is debounced and server-side: wait for the new results to land
        expect(page.locator("#runsList")).to_contain_text(query, timeout=10000)


def cards(page):
    return page.locator("#runsList .run-card")


def reset_runs_ui(page):
    """Return the shared page to a neutral state between history tests."""
    page.evaluate("() => { document.querySelector('#runBack')?.click(); }")
    page.click("#btnNew")
    page.evaluate("() => window.__om.switchTab('live', true)")


def test_btn_history_opens_previous_runs_tab_and_the_old_drawer_is_gone(page):
    page, _ = page
    page.click("#btnHistory")
    expect(page.locator("#tab-runs")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#pane-runs")).to_be_visible()
    assert page.locator("#historyDrawer").count() == 0
    page.evaluate("() => window.__om.switchTab('live', true)")


def test_previous_runs_filters_sorts_groups_and_searches_on_the_server(page):
    page, _ = page
    tag, now = uuid.uuid4().hex[:8], int(time.time())
    sid = f"dom-list-{tag}"
    seed_run(sid, "ra", now, title=f"alpha {tag}", prompt=f"alpha prompt {tag}",
             final=f"needle-{tag} in the answer", tokens=(10, 10), seconds=5)
    seed_run(sid, "rb", now, title=f"beta {tag}", prompt=f"beta prompt {tag}", status="failed")
    seed_run(sid, "rc", now - 86_400, title=f"gamma {tag}", prompt=f"gamma prompt {tag}",
             status="stopped", seconds=50)
    seed_run(sid, "rd", now - 5 * 86_400, title=f"delta {tag}", prompt=f"delta prompt {tag}",
             status="interrupted")

    open_runs_tab(page, tag)
    expect(cards(page)).to_have_count(4)
    groups = page.locator("#runsList .run-group")
    expect(groups.nth(0)).to_have_text("Today")
    expect(groups.nth(1)).to_have_text("Yesterday")
    assert groups.count() == 3, "older runs get a dated heading"
    expect(page.locator("#runsSummary")).to_contain_text("4 runs")
    expect(page.locator("#runsSummary")).to_contain_text("success")

    page.click('.run-chip[data-status="failed"]')
    expect(page.locator('.run-chip[data-status="failed"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator('.run-chip[data-status=""]')).to_have_attribute("aria-pressed", "false")
    expect(cards(page)).to_have_count(1)
    expect(cards(page).first).to_contain_text("✕ failed")  # status is not conveyed by colour alone
    expect(page.locator("#runsSummary")).to_contain_text("1 run")
    page.click('.run-chip[data-status=""]')
    expect(cards(page)).to_have_count(4)

    page.select_option("#runsSort", "duration")
    expect(cards(page).first).to_contain_text(f"gamma {tag}")
    assert page.locator("#runsList .run-group").count() == 0, "grouping applies to date sorts only"
    page.select_option("#runsSort", "newest")

    page.fill("#runsSearch", f"needle-{tag}")  # matches the final answer, server-side
    expect(cards(page)).to_have_count(1)
    expect(cards(page).first).to_contain_text(f"alpha {tag}")
    page.fill("#runsSearch", "zzz-no-such-run-ever")
    expect(page.locator(".runs-empty")).to_contain_text("No runs match")
    page.fill("#runsSearch", "")
    page.evaluate("() => window.__om.switchTab('live', true)")


def test_previous_runs_load_more_pages_through_the_list(page):
    page, _ = page
    tag, now = uuid.uuid4().hex[:8], int(time.time())
    for index in range(30):
        seed_run(f"dom-page-{tag}", f"r{index:02d}", now - index, title=f"paged {tag} {index}",
                 prompt=f"paged prompt {tag} {index}")
    open_runs_tab(page, tag)
    expect(cards(page)).to_have_count(25)
    expect(page.locator("#runsMore")).to_be_visible()
    page.click("#runsMore")
    expect(cards(page)).to_have_count(30)
    expect(page.locator("#runsMore")).to_be_hidden()
    expect(page.locator("#runsSummary")).to_contain_text("30 runs")
    page.fill("#runsSearch", "")
    page.evaluate("() => window.__om.switchTab('live', true)")


def _seed_detail_run(tag, *, status="completed"):
    sid, rid, now = f"dom-detail-{tag}", f"run{tag}", int(time.time())
    seed_run(
        sid, rid, now, title=f"detail {tag}", prompt=f"detail prompt {tag}",
        final=f"**final** answer {tag}", status=status, tokens=(120, 30), files=["notes/out.md"],
        events=[
            ("run_start", {"prompt": f"detail prompt {tag}"}),
            ("thought", {"content": "thinking it over"}),
            ("tool_start", {"id": "t1", "name": "python_execute", "category": "terminal",
                            "arguments": {"code": "print(1)"}}),
            ("tool_end", {"id": "t1", "name": "python_execute", "ok": True, "duration_ms": 12,
                          "result": "1"}),
            ("terminal", {"code": "print(1)", "output": "1"}),
            ("browser_screenshot", {"image": PNG_B64}),
            ("final_result", {"content": f"**final** answer {tag}"}),
            ("run_end", {}),
        ],
    )
    return sid, rid


def test_run_detail_shows_everything_and_replay_renders_the_panes_read_only(page):
    page, _ = page
    tag = uuid.uuid4().hex[:8]
    sid, rid = _seed_detail_run(tag)
    open_runs_tab(page, tag)
    cards(page).first.click()
    detail = page.locator("#runDetail")
    expect(detail).to_contain_text("Read-only historical run")
    expect(detail.locator(".run-prompt")).to_have_text(f"detail prompt {tag}")
    expect(detail.locator(".run-final")).to_contain_text(f"final answer {tag}")
    expect(detail.locator(".run-files a")).to_have_text("notes/out.md")
    expect(detail.locator("#runEventsCount")).to_have_text("8")
    page.click("#runEventsBox summary")
    expect(detail.locator(".run-event")).to_have_count(8)
    expect(detail.locator(".re-shot")).to_have_attribute("src", re.compile(rf"/api/blobs/{sid}/[a-f0-9]{{32}}\.png"))
    assert page.evaluate("() => location.hash") == f"#run={sid}/{rid}"

    page.select_option("#runEventType", "tool_start,tool_end")
    expect(detail.locator(".run-event")).to_have_count(2)
    page.select_option("#runEventType", "")

    page.click("#runReplay")
    expect(page.locator("#replayBanner")).to_be_visible()
    expect(page.locator("#replayLabel")).to_contain_text("Replaying")
    expect(page.locator("#replayLabel")).to_contain_text("read-only")
    expect(page.locator('.tl-tool[data-call-id="t1"]')).to_be_visible()
    expect(page.locator(".msg-user .bubble").last).to_contain_text(f"detail prompt {tag}")
    expect(page.locator("#browserShot")).to_have_attribute("src", re.compile(r"/api/blobs/"))
    expect(page.locator("#promptInput")).to_be_disabled()
    expect(page.locator("#btnSend")).to_be_disabled()
    page.evaluate("() => window.__om.switchTab('terminal', true)")
    expect(page.locator("#terminal")).to_contain_text("print(1)")

    # live traffic must not leak into a replayed run
    page.evaluate("() => window.__om.state.ws?.onmessage({data: JSON.stringify({type:'thought', data:{content:'LIVE-INTRUSION'}})})")
    assert "LIVE-INTRUSION" not in page.locator("#messages").inner_text()

    page.click("#replayExit")
    expect(page.locator("#replayBanner")).to_be_hidden()
    expect(page.locator("#promptInput")).to_be_enabled()
    page.wait_for_function("() => window.__om.state.ws?.readyState === 1", timeout=15000)
    assert page.evaluate("() => window.__om.state.replayMode") is None
    reset_runs_ui(page)


def test_run_actions_rerun_continue_copy_export_and_delete(page):
    page, _ = page
    tag = uuid.uuid4().hex[:8]
    sid, rid = _seed_detail_run(tag)
    open_runs_tab(page, tag)
    cards(page).first.click()
    expect(page.locator("#runDetail")).to_contain_text("Read-only historical run")
    original_sid = page.evaluate("() => window.__om.state.sessionId")

    page.locator("#runDetail .run-section").first.locator(".copy-btn").click()
    assert page.evaluate("() => navigator.clipboard.readText()") == f"detail prompt {tag}"

    with page.expect_download() as download_info:
        page.click("#runExportJson")
    exported = json.loads(Path(download_info.value.path()).read_text())
    assert exported["run"]["id"] == rid and len(exported["events"]) == 8
    assert "sk-" not in json.dumps(exported)
    with page.expect_download() as download_info:
        page.click("#runExportMd")
    markdown_text = Path(download_info.value.path()).read_text()
    assert f"# detail {tag}" in markdown_text and "## Final response" in markdown_text
    assert "notes/out.md" in markdown_text and "## Timeline" in markdown_text

    page.click("#runRerun")  # new session; prompt prefilled but never auto-submitted
    expect(page.locator("#promptInput")).to_have_value(f"detail prompt {tag}")
    assert page.evaluate("() => window.__om.state.sessionId") != original_sid
    expect(page.locator(".msg-user .bubble")).to_have_count(0)
    expect(page.locator(".toast", has_text="Prompt copied")).to_be_visible()

    open_runs_tab(page, tag)
    cards(page).first.click()
    page.click("#runContinue")
    seeded = page.input_value("#promptInput")
    assert seeded.startswith("Context from an earlier run")
    assert "not restored" in seeded
    assert f"detail prompt {tag}" in seeded and f"final** answer {tag}" in seeded
    assert seeded.rstrip().endswith("New request:")
    expect(page.locator(".toast", has_text="agent's state is not restored")).to_be_visible()

    open_runs_tab(page, tag)
    cards(page).first.click()
    page.click("#runDelete")
    dialog = page.locator(".confirm-modal")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("ALL of its runs")
    dialog.locator('[data-x="cancel"]').click()
    assert webapp.SESSION_STORE.get_session(sid) is not None, "cancel must not delete anything"
    page.click("#runDelete")
    page.locator(".confirm-modal").locator('[data-x="ok"]').click()
    expect(page.locator("#runDetail")).to_be_hidden()
    expect(cards(page)).to_have_count(0)
    assert webapp.SESSION_STORE.get_session(sid) is None
    reset_runs_ui(page)


def test_run_detail_deep_link_back_navigation_scroll_and_focus(page):
    page, _ = page
    tag, now = uuid.uuid4().hex[:8], int(time.time())
    for index in range(14):
        seed_run(f"dom-nav-{tag}", f"n{index:02d}", now - index, title=f"nav {tag} {index}",
                 prompt=f"nav prompt {tag} {index}", final="x" * 200)
    open_runs_tab(page, tag)
    expect(cards(page)).to_have_count(14)
    page.set_viewport_size({"width": 1100, "height": 420})
    page.evaluate("() => { document.querySelector('#pane-runs').scrollTop = 260; }")
    scrolled = page.evaluate("() => document.querySelector('#pane-runs').scrollTop")
    assert scrolled > 0
    target = page.locator('.run-card[data-run-id="n09"]')
    target.evaluate("el => el.click()")
    expect(page.locator("#runDetail")).to_contain_text("Read-only historical run")
    assert page.evaluate("() => document.activeElement.id") == "runDetailTitle"
    page.click("#runBack")
    expect(page.locator("#runsListView")).to_be_visible()
    assert abs(page.evaluate("() => document.querySelector('#pane-runs').scrollTop") - scrolled) <= 4
    assert page.evaluate("() => document.activeElement.dataset.runId") == "n09"
    assert page.evaluate("() => location.hash").startswith("#s="), "back restores the session hash"
    page.set_viewport_size({"width": 1280, "height": 720})

    # deep link: pasting #run=<session>/<run> opens the detail, and survives a reload
    link = f"#run=dom-nav-{tag}/n03"
    page.evaluate(f"() => {{ location.hash = '{link}'; }}")
    expect(page.locator("#runDetail")).to_contain_text(f"nav prompt {tag} 3")
    page.reload(wait_until="networkidle")
    page.wait_for_function("() => window.__om && window.__om.state.ws?.readyState === 1")
    expect(page.locator("#tab-runs")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#runDetail")).to_contain_text(f"nav prompt {tag} 3")
    assert page.evaluate("() => window.__om.state.sessionId") != f"dom-nav-{tag}", (
        "a run link must not adopt the historical session"
    )
    page.click("#runBack")
    page.fill("#runsSearch", "")
    page.evaluate("() => window.__om.switchTab('live', true)")


def test_previous_runs_are_keyboard_operable(page):
    page, _ = page
    tag, now = uuid.uuid4().hex[:8], int(time.time())
    seed_run(f"dom-kb-{tag}", "ok1", now, title=f"kb ok {tag}", prompt=f"kb prompt {tag}")
    seed_run(f"dom-kb-{tag}", "bad1", now, title=f"kb bad {tag}", prompt=f"kb prompt {tag}",
             status="failed")
    open_runs_tab(page, tag)
    expect(cards(page)).to_have_count(2)
    chip = page.locator('.run-chip[data-status="failed"]')
    chip.focus()
    page.keyboard.press("Enter")
    expect(chip).to_have_attribute("aria-pressed", "true")
    expect(cards(page)).to_have_count(1)
    cards(page).first.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#runDetail")).to_contain_text("Read-only historical run")
    assert page.evaluate("() => document.activeElement.id") == "runDetailTitle"
    page.locator("#runBack").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#runsListView")).to_be_visible()
    assert page.evaluate("() => document.activeElement.dataset.runId") == "bad1"
    page.click('.run-chip[data-status=""]')
    page.fill("#runsSearch", "")
    page.evaluate("() => window.__om.switchTab('live', true)")


def test_opening_a_historical_session_from_a_run_is_a_read_only_chat(page):
    page, _ = page
    tag = uuid.uuid4().hex[:8]
    _seed_detail_run(tag)
    open_runs_tab(page, tag)
    cards(page).first.click()
    page.click("#runOpenSession")
    page.wait_for_function("() => window.__om.state.ws?.readyState === 1", timeout=15000)
    expect(page.locator("#promptInput")).to_be_disabled()
    expect(page.locator("#promptInput")).to_have_attribute("placeholder", re.compile("read-only"))
    expect(page.locator(".msg-user .bubble").first).to_contain_text(f"detail prompt {tag}")
    page.evaluate("() => window.__om.submitPrompt()")
    expect(page.locator(".toast", has_text="read-only")).to_be_visible()
    reset_runs_ui(page)


def test_session_window_note_history_banner_tab_badge_and_live_dot(page):
    page, _ = page
    page.evaluate("() => window.__om.dispatch('session_info', {read_only: true, runs_total: 5, runs_shown: 2})")
    expect(page.locator(".toast", has_text="latest 2 of 5 runs")).to_be_visible()
    page.evaluate("() => window.__om.dispatch('session_info', {read_only: false})")

    page.evaluate("() => window.__om.dispatch('history_degraded', {message: 'Run history is not being saved right now.'})")
    banner = page.locator("#historyBanner")
    expect(banner).to_be_visible()
    expect(banner).to_have_attribute("role", "alert")
    expect(banner).to_contain_text("not being saved")
    page.evaluate("() => window.__om.dispatch('history_recovered', {message: 'Run history saving has recovered.'})")
    expect(banner).to_be_hidden()
    expect(page.locator(".toast", has_text="recovered")).to_be_visible()

    page.evaluate("() => window.__om.dispatch('run_start', {prompt: 'live dot probe', ts: Date.now()})")
    expect(page.locator("#runsLiveDot")).to_be_visible()
    page.evaluate("() => window.__om.dispatch('run_end', {ts: Date.now()})")
    expect(page.locator("#runsLiveDot")).to_be_hidden()

    seed_run(f"dom-badge-{uuid.uuid4().hex[:8]}", "b1", int(time.time()), title="badge", prompt="badge")
    page.reload(wait_until="networkidle")
    page.wait_for_function("() => window.__om && window.__om.state.ws?.readyState === 1")
    badge = page.locator("#runsBadge")
    expect(badge).to_be_visible()
    assert int(badge.inner_text()) >= 1


# --------------------------------------------------------- Connectors tab --
_MCP_STUB = {
    "mcpServers": {
        "alpha": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "alpha-pkg"],
            "description": "Alpha test server",
        },
        "beta": {"type": "sse", "url": "http://127.0.0.1:9/sse", "enabled": False},
        "rea": {
            "type": "stdio",
            "command": "node",
            "args": ["rea.mjs", "mcp"],
            "enabled": False,
            "description": "Reverse engineering test server",
        },
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
    skills_root = temp.parent / "skills"
    skill = skills_root / "reverse-engineer-anything"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: reverse-engineer-anything\n"
        "description: Test reverse engineering\n---\n# Instructions\n",
        encoding="utf-8",
    )
    links = skills_root / "connectors.json"
    links.write_text('{"reverse-engineer-anything": ["rea"]}', encoding="utf-8")
    state = temp.parent / "skills_state.json"

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
    orig_skill_paths = (webapp.SKILLS_ROOT, webapp.SKILLS_STATE_FILE, webapp.SKILLS_LINKS_FILE)
    webapp.MCP_CONFIG_PATH = temp
    webapp.MCPSettings = _FakeMCPSettings
    webapp.SKILLS_ROOT = skills_root
    webapp.SKILLS_STATE_FILE = state
    webapp.SKILLS_LINKS_FILE = links
    webapp._reload_mcp_servers()
    try:
        yield temp
    finally:
        webapp.MCP_CONFIG_PATH = orig_path
        webapp.MCPSettings = orig_settings
        (
            webapp.SKILLS_ROOT,
            webapp.SKILLS_STATE_FILE,
            webapp.SKILLS_LINKS_FILE,
        ) = orig_skill_paths
        webapp.config.mcp_config.servers.clear()
        webapp.config.mcp_config.servers.update(orig_servers)


def test_connectors_tab_lists_and_toggles_without_losing_focus(page, mcp_stub):
    page, errors = page
    errors_before = len(errors)  # earlier tests leave known-benign console noise
    page.click("#btnMcp")
    expect(page.locator("#pane-connectors")).to_be_visible()
    cards = page.locator("#connMcpList .mcp-card")
    expect(cards).to_have_count(4)  # browser_use + alpha + beta + rea
    expect(page.locator("#connSkillList .mcp-card")).to_have_count(1)

    builtin = page.locator('#connMcpList .mcp-card[data-id="browser_use"]')
    expect(builtin.locator(".mcp-badge")).to_have_text("built-in")
    expect(builtin.locator("input")).to_be_checked()
    expect(page.locator('#connMcpList .mcp-card[data-id="beta"] input')).not_to_be_checked()
    expect(page.locator('#connMcpList .mcp-card[data-id="beta"]')).to_have_class(re.compile(r"\bdisabled\b"))
    expect(page.locator('#connMcpList .mcp-card[data-id="rea"] .mcp-badge.risk')).to_have_text("Runs on host")
    expect(page.locator('#connMcpList .mcp-card[data-id="alpha"] .mcp-desc')).to_have_text("Alpha test server")

    alpha = page.locator('#connMcpList .mcp-card[data-id="alpha"]')
    alpha.locator(".slider").click()
    expect(page.locator(".toast").last).to_contain_text("alpha deactivated", timeout=5000)
    expect(alpha).to_have_class(re.compile(r"\bdisabled\b"))
    expect(alpha.locator(".mcp-status")).to_have_text("disabled")
    expect(alpha.locator("input")).to_be_focused()
    assert json.loads(mcp_stub.read_text())["mcpServers"]["alpha"]["enabled"] is False

    alpha.locator(".slider").click()
    expect(alpha).not_to_have_class(re.compile(r"\bdisabled\b"), timeout=5000)
    expect(alpha.locator("input")).to_be_focused()
    assert json.loads(mcp_stub.read_text())["mcpServers"]["alpha"]["enabled"] is True
    assert errors[errors_before:] == []


def test_connectors_search_and_status_filters(page, mcp_stub):
    page, _ = page
    page.click("#btnMcp")
    expect(page.locator("#connMcpList .mcp-card")).to_have_count(4)
    page.fill("#connSearch", "Alpha")
    expect(page.locator("#connMcpList .mcp-card")).to_have_count(1)
    expect(page.locator("#connMcpList .mcp-card")).to_have_attribute("data-id", "alpha")
    page.get_by_role("button", name="Clear filter").first.click()
    expect(page.locator("#connMcpList .mcp-card")).to_have_count(4)

    page.locator('[data-conn-filter="off"]').click()
    expect(page.locator("#connMcpList .mcp-card")).to_have_count(2)
    page.locator('[data-conn-filter="risky"]').click()
    expect(page.locator("#connMcpList .mcp-card")).to_have_count(1)
    expect(page.locator("#connSkillList .mcp-card")).to_have_count(1)
    page.locator('[data-conn-filter=""]').click()


def test_connectors_load_failure_clears_skeleton_and_retries(page, mcp_stub):
    page, _ = page
    page.route(
        "**/api/skills",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body="not json"
        ),
    )
    page.reload(wait_until="networkidle")
    page.wait_for_function("() => window.__om && window.__om.state.ws?.readyState === 1")
    page.click("#btnMcp")
    expect(page.locator("#connError .fetch-error")).to_be_visible()
    expect(page.locator("#pane-connectors .conn-skeleton")).to_have_count(0)

    page.unroute("**/api/skills")
    page.click("#connRetry")
    expect(page.locator("#connSkillList .mcp-card")).to_have_count(1)
    expect(page.locator("#connError .fetch-error")).to_have_count(0)


def test_connectors_delayed_toggle_blocks_refresh_and_reconciles(page, mcp_stub):
    page, _ = page
    page.click("#btnMcp")
    alpha = page.locator('#connMcpList .mcp-card[data-id="alpha"]')
    expect(alpha).to_be_visible()
    page.evaluate(
        """() => {
          const original = window.fetch.bind(window);
          window.__raceOriginalFetch = original;
          window.__raceRefreshCount = 0;
          window.__raceRelease = null;
          window.fetch = (input, init) => {
            const url = String(input);
            if (url.includes('/api/mcp/servers/alpha/toggle')) {
              return new Promise((resolve, reject) => {
                window.__raceRelease = () => original(input, init).then(resolve, reject);
              });
            }
            if (url.endsWith('/api/mcp/servers')) window.__raceRefreshCount += 1;
            return original(input, init);
          };
        }"""
    )
    alpha.locator(".slider").click()
    page.wait_for_function("() => Boolean(window.__raceRelease)")
    expect(page.locator("#connRefresh")).to_be_disabled()
    page.evaluate("() => window.__om.loadConnectors()")
    assert page.evaluate("() => window.__raceRefreshCount") == 0

    page.evaluate("() => window.__raceRelease()")
    expect(alpha.locator(".mcp-status")).to_have_text("disabled", timeout=5000)
    page.evaluate(
        """() => {
          window.fetch = window.__raceOriginalFetch;
          delete window.__raceOriginalFetch;
          delete window.__raceRelease;
        }"""
    )
    expect(page.locator("#connRefresh")).to_be_enabled()
    alpha.locator(".slider").click()
    expect(alpha.locator(".mcp-status")).to_contain_text("Applied to", timeout=5000)


def test_rea_enable_requires_confirmation_and_cancel_reverts(page, mcp_stub):
    page, _ = page
    page.click("#btnMcp")
    rea = page.locator('#connMcpList .mcp-card[data-id="rea"]')
    expect(rea).to_be_visible()
    rea.locator(".slider").click()
    dialog = page.get_by_role("alertdialog", name="Enable REA connector")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("70k tokens")
    expect(rea.locator("input")).not_to_be_checked()
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_be_hidden()
    expect(rea.locator("input")).not_to_be_checked()
    assert json.loads(mcp_stub.read_text())["mcpServers"]["rea"]["enabled"] is False


def test_skill_toggle_and_related_rea_activation(page, mcp_stub):
    page, _ = page
    page.click("#btnMcp")
    skill = page.locator('#connSkillList .mcp-card[data-id="reverse-engineer-anything"]')
    expect(skill).to_be_visible()
    expect(skill.locator(".conn-needs")).to_have_text("Needs rea (off)")

    skill.locator(".slider").click()
    expect(skill.locator(".mcp-status")).to_have_text("disabled", timeout=5000)
    state = mcp_stub.parent / "skills_state.json"
    assert json.loads(state.read_text())["disabled"] == ["reverse-engineer-anything"]
    skill.locator(".slider").click()
    expect(skill.locator(".mcp-status")).to_contain_text("Applied to", timeout=5000)
    expect(skill.locator("input")).to_be_checked()
    assert json.loads(state.read_text())["disabled"] == []

    skill.get_by_role("button", name="Turn on rea").click()
    dialog = page.get_by_role("alertdialog", name="Enable REA connector")
    dialog.get_by_role("button", name="Enable REA").click()
    rea = page.locator('#connMcpList .mcp-card[data-id="rea"]')
    expect(rea.locator("input")).to_be_checked(timeout=5000)
    assert json.loads(mcp_stub.read_text())["mcpServers"]["rea"]["enabled"] is True

    rea.locator(".slider").click()  # restore the risky connector's off state
    expect(rea.locator("input")).not_to_be_checked(timeout=5000)
    page.evaluate("() => window.__om.switchTab('live', true)")


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


def test_new_task_clears_all_panes(page):
    """'New' must clear the previous task's artifacts from EVERY pane."""
    page, errors = page
    errors_before = len(errors)
    page.evaluate(
        """() => {
          const om = window.__om;
          om.dispatch('final_result', {content: 'old task result'});
          om.dispatch('terminal', {code: 'print(1)', output: '1'});
          document.querySelector('#editorSidebar').innerHTML = '<div class="file-group-label">stale-tree</div>';
          document.querySelector('#filesList').innerHTML = '<div>stale-file</div>';
          document.querySelector('#tokenStat').textContent = '9,999 in / 9,999 out';
          document.querySelector('#liveLabel').textContent = 'Done';
          document.querySelector('#promptInput').value = 'stale draft';
        }"""
    )
    page.click("#btnNew")
    expect(page.locator("#welcome")).to_be_visible()
    expect(page.locator("#timeline .pane-empty")).to_have_count(1)
    expect(page.locator("#terminal .pane-empty")).to_have_count(1)
    expect(page.locator("#editorSidebar")).to_be_empty()
    expect(page.locator("#filesList .pane-empty")).to_have_count(1)
    expect(page.locator("#tokenStat")).to_have_text("0 in / 0 out")
    expect(page.locator("#liveLabel")).to_have_text("Idle")
    expect(page.locator("#promptInput")).to_have_value("")
    assert errors[errors_before:] == []


def test_upload_chips_flow(page, tmp_path, monkeypatch):
    page, errors = page
    errors_before = len(errors)
    monkeypatch.setattr(webapp, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(webapp, "UPLOAD_DIR", tmp_path / "uploads")
    f = tmp_path / "notes.txt"
    f.write_text("hello agent")
    page.set_input_files("#fileInput", str(f))
    chip = page.locator(".upload-chip")
    expect(chip).to_have_count(1, timeout=5000)
    expect(chip.locator(".upload-chip-name")).to_have_text("notes.txt")
    assert (tmp_path / "uploads" / "notes.txt").read_text() == "hello agent"
    chip.locator(".upload-chip-x").click()
    expect(page.locator(".upload-chip")).to_have_count(0)
    assert errors[errors_before:] == []


# ------------------------------------------- audit-fix regression tests --
# Guards for the 2026-09-27 website-audit.md findings (see audit report for
# evidence/acceptance criteria). These must stay above the logout test —
# that one ends the shared page's session by design.


def test_bug_header_actions_never_overflow_chat_panel(page):
    """BUG-001/002: header actions must stay inside the chat panel on desktop
    and remain on-screen at mobile width (previously overlapped the computer
    panel title / were clipped off-viewport entirely)."""
    page, _ = page
    page.set_viewport_size({"width": 1440, "height": 900})
    m = page.evaluate(
        """() => {
          const a = document.querySelector('.header-actions').getBoundingClientRect();
          const p = document.getElementById('chatPanel').getBoundingClientRect();
          return { actionsRight: a.right, panelRight: p.right };
        }"""
    )
    assert m["actionsRight"] <= m["panelRight"] + 1, (
        f"header actions overflow the chat panel by {m['actionsRight'] - m['panelRight']:.0f}px"
    )
    page.set_viewport_size({"width": 390, "height": 844})
    on_screen = page.evaluate(
        """() => ['btnTheme','btnLogout','btnHistory','btnMcp','btnSettings','btnNew'].every(id => {
          const r = document.getElementById(id).getBoundingClientRect();
          return r.width > 0 && r.left >= 0 && r.right <= window.innerWidth;
        })"""
    )
    assert on_screen, "all header buttons must be reachable at 390px"
    page.set_viewport_size({"width": 1280, "height": 720})


def test_bug_tabs_scrollable_at_mobile_width(page):
    """BUG-003: the tab bar scrolls horizontally instead of clipping the
    Files tab off-screen at 390px."""
    page, _ = page
    page.set_viewport_size({"width": 390, "height": 844})
    overflow = page.evaluate("() => getComputedStyle(document.getElementById('tabs')).overflowX")
    assert overflow == "auto"
    page.locator("#tab-files").scroll_into_view_if_needed()
    page.click("#tab-files")
    expect(page.locator("#pane-files")).to_be_visible()
    page.locator("#tab-connectors").scroll_into_view_if_needed()
    page.click("#tab-connectors")
    expect(page.locator("#pane-connectors")).to_be_visible()
    page.set_viewport_size({"width": 1280, "height": 720})
    page.evaluate("() => window.__om.switchTab('live', true)")


def test_a11y_dialog_semantics_focus_trap_and_return(page):
    """A11Y-003: modals are named modal dialogs; focus moves in on open, Tab
    is trapped, Escape closes, focus returns to the trigger."""
    page, _ = page
    page.click("#btnSettings")
    dlg = page.locator("#modelModal .modal")
    expect(dlg).to_have_attribute("role", "dialog")
    expect(dlg).to_have_attribute("aria-modal", "true")
    expect(dlg).to_have_attribute("aria-labelledby", "modelModalTitle")
    inside = page.evaluate(
        "() => document.getElementById('modelModal').contains(document.activeElement)"
    )
    assert inside, "focus must move into the dialog on open"
    page.evaluate(
        """() => {
          const els = [...document.querySelectorAll('#modelModal button, #modelModal input, #modelModal select')]
            .filter(e => e.offsetParent !== null);
          els[els.length - 1].focus();
        }"""
    )
    page.keyboard.press("Tab")
    wrapped = page.evaluate(
        "() => document.getElementById('modelModal').contains(document.activeElement)"
    )
    assert wrapped, "Tab past the last control must wrap inside the dialog"
    page.keyboard.press("Escape")
    expect(page.locator("#modelModal")).to_be_hidden()
    assert page.evaluate("() => document.activeElement.id") == "btnSettings"


def test_a11y_model_cards_are_keyboard_operable_radios(page):
    """A11Y-005: model cards form a real radiogroup — roving tabindex, arrow
    keys move the tab stop, aria-checked reflects the active model."""
    page, _ = page
    page.click("#btnSettings")
    cards = page.locator("#modelList [role=radio]")
    expect(cards.first).to_be_visible(timeout=5000)
    assert cards.count() >= 1
    assert page.locator('#modelList [role=radio][aria-checked="true"]').count() == 1
    stops = page.evaluate(
        "() => [...document.querySelectorAll('#modelList [role=radio]')].filter(c => c.tabIndex === 0).length"
    )
    assert stops == 1, "exactly one card is the tab stop (roving tabindex)"
    page.evaluate("() => document.querySelector('#modelList [role=radio]').focus()")
    page.keyboard.press("ArrowDown")
    moved = page.evaluate(
        """() => {
          const cards = [...document.querySelectorAll('#modelList [role=radio]')];
          if (cards.length < 2) return true; // single-card groups have nothing to move to
          return document.activeElement === cards[1] && cards[1].tabIndex === 0 && cards[0].tabIndex === -1;
        }"""
    )
    assert moved
    page.keyboard.press("Escape")


def test_a11y_composer_and_preset_have_accessible_names(page):
    """A11Y-004: composer textarea and preset select expose accessible names."""
    page, _ = page
    assert page.locator("#promptInput").get_attribute("aria-label")
    page.click("#btnSettings")
    expect(page.locator("#fPreset")).to_have_attribute("aria-label", "Provider preset")
    page.keyboard.press("Escape")


def test_a11y_primary_button_contrast_both_themes(page):
    """A11Y-001: .btn-primary text clears WCAG AA 4.5:1 in dark AND light."""
    page, _ = page
    ratio_js = """() => {
        const lum = (rgb) => {
          const c = rgb.match(/[\\d.]+/g).map(Number);
          const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
          return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
        };
        const cs = getComputedStyle(document.getElementById('formSave'));
        const l1 = lum(cs.color), l2 = lum(cs.backgroundColor);
        return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    }"""
    page.click("#btnSettings")
    expect(page.locator("#formSave")).to_be_visible()
    dark_ratio = page.evaluate(ratio_js)
    assert dark_ratio >= 4.5, f"dark theme .btn-primary contrast {dark_ratio:.2f}:1 < 4.5:1"
    page.keyboard.press("Escape")
    page.click("#btnTheme")
    page.click("#btnSettings")
    light_ratio = page.evaluate(ratio_js)
    assert light_ratio >= 4.5, f"light theme .btn-primary contrast {light_ratio:.2f}:1 < 4.5:1"
    page.keyboard.press("Escape")
    page.click("#btnTheme")  # restore dark for subsequent tests


def test_ux_signout_confirm_is_inline_and_cancellable(page):
    """UX-006: sign-out gates on the styled inline confirm; Cancel keeps the
    session alive (a native confirm() would auto-dismiss and fail this test)."""
    page, _ = page
    page.click("#btnLogout")
    dlg = page.locator(".confirm-modal")
    expect(dlg).to_be_visible(timeout=3000)
    expect(dlg).to_have_attribute("role", "alertdialog")
    expect(dlg).to_contain_text("Sign out?")
    dlg.locator('[data-x="cancel"]').click()
    expect(page.locator(".confirm-modal")).to_have_count(0)
    expect(page.locator("#promptInput")).to_be_visible()  # still signed in


def test_ux_run_report_uses_event_timestamps_on_replay(page):
    """UX-002: replayed run renders durations from event ts, not 0:00."""
    page, _ = page
    page.evaluate(
        """() => {
          const t0 = Date.now() - 65000; // run started 65s ago
          window.__om.dispatch('run_start', { prompt: 'ts-replay-probe', ts: t0 });
          window.__om.dispatch('thought', { content: 'thinking', ts: t0 + 42000 });
          window.__om.dispatch('run_end', { ts: t0 + 65000 });
        }"""
    )
    expect(page.locator(".tl-report").last).to_contain_text("1:05")
    expect(page.locator("#elapsedStat")).to_have_text("1:05")


def test_logout_invalidates_the_session_server_side(page):
    page, _ = page
    old_cookie = next(c for c in page.context.cookies() if c["name"] == "om_session")
    page.click("#btnLogout")
    # UX-006: inline styled confirm (role=alertdialog), not a native confirm().
    confirm = page.locator(".confirm-modal")
    expect(confirm).to_be_visible(timeout=3000)
    expect(confirm).to_have_attribute("role", "alertdialog")
    confirm.locator('[data-x="ok"]').click()
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
