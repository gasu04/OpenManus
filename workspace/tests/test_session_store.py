"""Unit tests for the portal's disk-backed run history store.

Covers persistence and restart recovery, the rebuildable index, pagination and
search, the per-session writer queue, degraded-disk behavior (retry, circuit
breaker, recovery), redaction, screenshot offloading, size caps, retention,
health, schema versioning, and cancellation races. All data lives in pytest
temporary directories; no webapp server or network is required.

Run: .venv/bin/python -m pytest workspace/tests/test_session_store.py -v
Dependencies: pytest and pytest-asyncio.
Tier 1 (the store): silent history loss or secret leakage is unrecoverable.
"""

import asyncio
import base64
import errno
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from workspace.history_config import HistoryConfig
from workspace.session_store import (
    SessionStore,
    SessionStoreError,
    StoreIOError,
    StoreVersionError,
)

FAST = {
    "retry_max": 3,
    "meta_retry_max": 1,
    "backoff_base_s": 0.005,
    "backoff_max_s": 0.02,
    "breaker_threshold": 2,
    "breaker_reset_s": 0.05,
    "flush_timeout_s": 3.0,
    "writer_idle_s": 5.0,
}
FAKE_KEY = "sk" + "-" + "FAKE" * 5 + "1234"  # fragments: not a complete token literal
FAKE_GOOGLE = "GOC" + "SPX-" + "fake" * 3 + "1234"
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes" * 4
PNG_B64 = base64.b64encode(PNG).decode()


def make_config(**overrides) -> HistoryConfig:
    return HistoryConfig(**{**FAST, **overrides})


@asynccontextmanager
async def open_store(root: Path, states: list | None = None, **overrides):
    callback = states.append if states is not None else None
    store = SessionStore(root, make_config(**overrides), on_state_change=callback)
    try:
        yield store
    finally:
        await store.close()


def _event(kind: str, marker: str, **extra) -> dict:
    return {"type": kind, "data": {"marker": marker, "ts": 1_700_000_000_000, **extra}}


async def seed(
    store,
    sid,
    rid,
    started_at,
    status="completed",
    prompt="prompt",
    final="",
    tokens=(0, 0),
    seconds=5,
    steps=1,
    actions=1,
    files=(),
):
    await store.create_run(sid, rid, prompt.split("\n")[0][:80], prompt, started_at)
    if status != "running":
        await store.finish_run(
            sid,
            rid,
            status,
            started_at + seconds,
            final,
            list(files),
            steps,
            actions,
            *tokens,
        )


async def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def markers(events: list) -> list:
    return [event["data"].get("marker") for event in events]


# ----------------------------------------------------------- persistence --
@pytest.mark.asyncio
async def test_store_multiple_runs_reopen_preserves_metadata_and_events(tmp_path):
    async with open_store(tmp_path) as store:
        for index, started_at in enumerate((100, 200), start=1):
            sid, rid = "session-one", f"run-{index}"
            await store.create_run(
                sid, rid, f"Run {index}", f"Prompt {index}", started_at
            )
            for kind in ("run_start", "final_result", "run_end"):
                store.enqueue_event(sid, rid, _event(kind, f"{kind}-{index}"))
            await store.finish_run(
                sid,
                rid,
                "completed",
                started_at + 5,
                f"Answer {index}",
                [f"report-{index}.md"],
                index,
                index + 1,
                100 * index,
                25 * index,
            )

    async with open_store(tmp_path) as reopened:
        summaries = reopened.list_session_summaries()
        runs = reopened.query_runs(limit=None)["runs"]
        assert len(summaries) == 1 and summaries[0]["run_count"] == 2
        assert [run["id"] for run in runs] == ["run-2", "run-1"]
        first = reopened.get_run("session-one", "run-1")
        assert first is not None
        assert first["prompt"] == "Prompt 1" and first["duration_ms"] == 5_000
        assert (first["steps"], first["actions"]) == (1, 2)
        assert (first["input_tokens"], first["output_tokens"]) == (100, 25)
        assert first["files"] == ["report-1.md"]
        events = await reopened.read_events("session-one", "run-1")
        assert markers(events) == ["run_start-1", "final_result-1", "run_end-1"]
        assert len(await reopened.read_events("session-one")) == 6


@pytest.mark.asyncio
async def test_store_running_run_becomes_interrupted_using_last_event_time(tmp_path):
    started_at = int(time.time()) - 10
    async with open_store(tmp_path) as store:
        await store.create_run("session-live", "run-live", "Live", "Prompt", started_at)
        last = _event("run_start", "start")
        last["data"]["ts"] = (started_at + 5) * 1000
        store.enqueue_event("session-live", "run-live", last)
        await store.flush("session-live")

    async with open_store(tmp_path) as reopened:
        run = reopened.get_run("session-live", "run-live")
        assert run is not None
        assert run["status"] == "interrupted"
        assert run["ended_at"] == started_at + 5 and run["duration_ms"] == 5_000
        summary = reopened.query_runs(limit=None)["runs"][0]
        assert summary["status"] == "interrupted"


@pytest.mark.asyncio
async def test_store_corrupt_records_are_skipped_without_losing_valid_history(tmp_path):
    async with open_store(tmp_path) as store:
        await store.create_run("good-session", "good-run", "Good", "Prompt", 100)
        store.enqueue_event(
            "good-session", "good-run", _event("run_start", "valid-one")
        )
        await store.flush("good-session")
        with (tmp_path / "good-session" / "events.jsonl").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write("not-json\n")
        store.enqueue_event("good-session", "good-run", _event("run_end", "valid-two"))

    (tmp_path / "corrupt-session").mkdir()
    (tmp_path / "corrupt-session" / "session.json").write_text(
        "{broken", encoding="utf-8"
    )
    (tmp_path / "semantic-session").mkdir()
    (tmp_path / "semantic-session" / "session.json").write_text(
        json.dumps(
            {"id": "semantic-session", "runs": [{"id": "run", "status": "running"}]}
        ),
        encoding="utf-8",
    )
    (tmp_path / "index.json").write_text("{also broken", encoding="utf-8")

    async with open_store(tmp_path) as reopened:
        assert [item["id"] for item in reopened.list_session_summaries()] == [
            "good-session"
        ]
        events = await reopened.read_events("good-session", "good-run")
        assert markers(events) == ["valid-one", "valid-two"]


@pytest.mark.asyncio
async def test_store_delete_removes_history_and_rejects_path_escape(tmp_path):
    async with open_store(tmp_path) as store:
        await store.create_run("delete-me", "run-delete", "Delete", "Prompt", 100)
        store.enqueue_event("delete-me", "run-delete", _event("run_start", "start"))
        await store.flush("delete-me")
        with pytest.raises(SessionStoreError):
            await store.delete_session("../outside")
        assert await store.delete_session("delete-me") is True
        assert await store.delete_session("delete-me") is False
        assert store.get_session("delete-me") is None
        assert not (tmp_path / "delete-me").exists()
        assert store.query_runs(limit=None)["runs"] == []
        # a deleted session can never be silently recreated by a late event
        assert (
            store.enqueue_event("delete-me", "run-delete", _event("x", "late")) is False
        )
        assert not (tmp_path / "delete-me").exists()


@pytest.mark.asyncio
async def test_store_events_for_unknown_sessions_are_not_queued(tmp_path):
    async with open_store(tmp_path) as store:
        assert (
            store.enqueue_event("empty-session", None, _event("pong", "ping")) is False
        )
        assert await store.read_events("empty-session") == []
        assert not (tmp_path / "empty-session").exists()


@pytest.mark.asyncio
async def test_store_cancelled_write_finishes_before_delete(tmp_path, monkeypatch):
    async with open_store(tmp_path) as store:
        original_write = store._write_record
        started, release = threading.Event(), threading.Event()

        def blocked_write(record):
            started.set()
            release.wait(timeout=5)
            original_write(record)

        monkeypatch.setattr(store, "_write_record", blocked_write)
        create_task = asyncio.create_task(
            store.create_run("race-session", "race-run", "Race", "Prompt", 100)
        )
        assert await asyncio.to_thread(started.wait, 2)
        create_task.cancel()
        delete_task = asyncio.create_task(store.delete_session("race-session"))
        try:
            await asyncio.sleep(0.05)
            assert not delete_task.done(), "deletion must wait for the in-flight write"
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await create_task
        assert await asyncio.wait_for(delete_task, timeout=2) is True
        assert not (tmp_path / "race-session").exists()


@pytest.mark.asyncio
async def test_store_lock_table_does_not_grow(tmp_path):
    async with open_store(tmp_path) as store:
        for index in range(5):
            await seed(store, f"s{index}", f"r{index}", 100 + index)
        await store.delete_session("s0")
        assert store._locks == {} and store._lock_refs == {}


# --------------------------------------------------------------- schema --
@pytest.mark.asyncio
async def test_store_future_schema_is_skipped_and_never_overwritten(tmp_path):
    future = {
        "version": 99,
        "id": "future-session",
        "title": "From the future",
        "created_at": 1,
        "last_active": 1,
        "runs": [],
    }
    (tmp_path / "future-session").mkdir()
    path = tmp_path / "future-session" / "session.json"
    path.write_text(json.dumps(future), encoding="utf-8")
    before = path.read_bytes()
    async with open_store(tmp_path) as store:
        assert store.list_session_summaries() == []
        with pytest.raises(StoreVersionError):
            await store.create_run("future-session", "new-run", "T", "P", 5)
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_store_unversioned_record_is_migrated_to_current_schema(tmp_path):
    legacy = {
        "id": "legacy-session",
        "title": "Legacy",
        "created_at": 1,
        "last_active": 2,
        "runs": [
            {
                "id": "legacy-run",
                "status": "completed",
                "started_at": 1,
                "prompt": "old",
            }
        ],
    }
    (tmp_path / "legacy-session").mkdir()
    (tmp_path / "legacy-session" / "session.json").write_text(
        json.dumps(legacy), encoding="utf-8"
    )
    async with open_store(tmp_path) as store:
        assert store.get_session("legacy-session")["version"] == 1
        assert store.get_run("legacy-session", "legacy-run")["prompt"] == "old"


# ---------------------------------------------------------------- index --
@pytest.mark.asyncio
async def test_index_rebuild_matches_original_and_detects_stale_or_corrupt_index(
    tmp_path,
):
    async with open_store(tmp_path) as store:
        await seed(
            store, "s1", "r1", 100, prompt="alpha", final="zebra answer", tokens=(5, 5)
        )
        await seed(store, "s1", "r2", 200, status="failed", prompt="beta")
        await seed(store, "s2", "r3", 300, prompt="gamma", files=["a.md"])
        original = store.query_runs(limit=None)
        assert (tmp_path / "index.json").is_file()

    (tmp_path / "index.json").unlink()
    async with open_store(tmp_path) as rebuilt:
        assert rebuilt.query_runs(limit=None) == original
    assert (tmp_path / "index.json").is_file(), "the rebuilt index is persisted"

    (tmp_path / "index.json").write_text("{corrupt", encoding="utf-8")
    async with open_store(tmp_path) as recovered:
        assert recovered.query_runs(limit=None) == original

    record_path = tmp_path / "s1" / "session.json"
    record = json.loads(record_path.read_text())
    record["runs"][0]["title"] = "externally edited title"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    async with open_store(tmp_path) as stale:
        titles = [run["title"] for run in stale.query_runs(limit=None)["runs"]]
        assert "externally edited title" in titles


@pytest.mark.asyncio
async def test_index_updates_on_run_start_and_finish_without_reopening(tmp_path):
    async with open_store(tmp_path) as store:
        await store.create_run("s1", "r1", "Title", "Prompt text", 100)
        running = store.query_runs()["runs"][0]
        assert (
            running["status"] == "running"
            and running["prompt_preview"] == "Prompt text"
        )
        persisted = json.loads((tmp_path / "index.json").read_text())
        assert persisted["runs"]["s1"][0]["status"] == "running"
        await store.finish_run("s1", "r1", "completed", 104, "All done", [], 2, 3, 7, 8)
        finished = store.query_runs()["runs"][0]
        assert (
            finished["status"] == "completed"
            and finished["final_preview"] == "All done"
        )
        assert finished["actions"] == 3 and finished["duration_ms"] == 4_000
        assert "search" not in finished and "seq" not in finished


@pytest.mark.asyncio
async def test_query_runs_pagination_filters_sorting_and_search(tmp_path):
    async with open_store(tmp_path) as store:
        await seed(
            store,
            "s1",
            "r1",
            100,
            prompt="alpha task",
            final="answer about ZEBRA",
            tokens=(10, 5),
            seconds=5,
        )
        await seed(
            store,
            "s1",
            "r2",
            200,
            status="failed",
            prompt="beta task",
            tokens=(100, 50),
            seconds=10,
        )
        await seed(store, "s2", "r3", 300, prompt="gamma", tokens=(1, 1), seconds=1)
        await seed(
            store,
            "s2",
            "r4",
            400,
            status="stopped",
            prompt="delta",
            tokens=(3, 3),
            seconds=50,
        )
        await seed(store, "s3", "r5", 500, status="interrupted", prompt="epsilon")

        def ids(result):
            return [run["id"] for run in result["runs"]]

        first = store.query_runs(limit=2)
        assert ids(first) == ["r5", "r4"] and first["next_cursor"]
        second = store.query_runs(limit=2, cursor=first["next_cursor"])
        assert ids(second) == ["r3", "r2"] and second["next_cursor"]
        third = store.query_runs(limit=2, cursor=second["next_cursor"])
        assert ids(third) == ["r1"] and third["next_cursor"] is None

        assert ids(store.query_runs(status="failed")) == ["r2"]
        assert ids(store.query_runs(q="zebra")) == ["r1"]
        assert ids(store.query_runs(q="  ALPHA ")) == ["r1"]
        assert ids(store.query_runs(since=300, until=400)) == ["r4", "r3"]
        assert ids(store.query_runs(sort="oldest", limit=2)) == ["r1", "r2"]
        assert ids(store.query_runs(sort="duration", limit=1)) == ["r4"]
        assert ids(store.query_runs(sort="tokens", limit=1)) == ["r2"]

        summary = store.query_runs(limit=1)["summary"]
        assert summary["total"] == 5 and summary["completed"] == 2
        assert summary["success_rate"] == pytest.approx(2 / 5)
        assert summary["total_tokens"] == 15 + 150 + 2 + 6
        assert store.query_runs(status="failed", limit=1)["total_all"] == 5

        for bad in ({"cursor": "garbage"}, {"sort": "sideways"}, {"status": "weird"}):
            with pytest.raises(SessionStoreError):
                store.query_runs(**bad)


@pytest.mark.asyncio
async def test_same_second_runs_sort_by_creation_order(tmp_path):
    async with open_store(tmp_path) as store:
        for rid in ("first", "second", "third"):
            await store.create_run("s1", rid, rid, rid, 100)
        ordered = [run["id"] for run in store.query_runs()["runs"]]
        assert ordered == ["third", "second", "first"]


# --------------------------------------------------------------- events --
@pytest.mark.asyncio
async def test_event_pages_types_filter_and_totals(tmp_path):
    async with open_store(tmp_path) as store:
        await store.create_run("s1", "r1", "T", "P", 100)
        await store.create_run("s1", "r2", "T", "P", 200)
        for index in range(10):
            kind = "thought" if index % 2 == 0 else "tool_start"
            store.enqueue_event("s1", "r1", _event(kind, f"a{index}"))
        store.enqueue_event("s1", "r2", _event("thought", "other-run"))

        page, total = await store.read_event_page("s1", "r1", offset=2, limit=3)
        assert markers(page) == ["a2", "a3", "a4"] and total == 10
        thoughts, total = await store.read_event_page("s1", "r1", types=["thought"])
        assert markers(thoughts) == ["a0", "a2", "a4", "a6", "a8"] and total == 5
        tail, total = await store.read_event_page("s1", "r1", offset=9, limit=50)
        assert markers(tail) == ["a9"] and total == 10
        everything, total = await store.read_event_page("s1")
        assert total == 11 and everything[-1]["data"]["marker"] == "other-run"


@pytest.mark.asyncio
async def test_recent_run_events_returns_whole_runs_newest_first_window(tmp_path):
    async with open_store(tmp_path) as store:
        for number, started in ((1, 100), (2, 200), (3, 300)):
            rid = f"r{number}"
            await store.create_run("s1", rid, rid, rid, started)
            for index in range(4):
                store.enqueue_event("s1", rid, _event("thought", f"{rid}-{index}"))

        events, total, shown = await store.recent_run_events("s1", 7)
        assert (total, shown) == (3, 1) and len(events) == 4
        assert {item["data"]["marker"].split("-")[0] for item in events} == {"r3"}
        events, total, shown = await store.recent_run_events("s1", 8)
        assert (total, shown) == (3, 2)
        assert [item["data"]["marker"] for item in events][::4] == ["r2-0", "r3-0"]
        events, total, shown = await store.recent_run_events("s1", 100)
        assert (total, shown) == (3, 3) and len(events) == 12
        assert await store.recent_run_events("missing", 10) == ([], 0, 0)


# --------------------------------------------------------------- writer --
@pytest.mark.asyncio
async def test_writer_preserves_order_for_many_events(tmp_path):
    async with open_store(tmp_path, batch_max=7) as store:
        await store.create_run("s1", "r1", "T", "P", 100)
        for index in range(300):
            assert store.enqueue_event("s1", "r1", _event("thought", f"e{index:03d}"))
        events = await store.read_events("s1", "r1")
        assert markers(events) == [f"e{index:03d}" for index in range(300)]


@pytest.mark.asyncio
async def test_writer_queue_is_bounded_and_counts_dropped_events(tmp_path, monkeypatch):
    async with open_store(tmp_path, queue_max=5, retry_max=1000) as store:
        await store.create_run("s1", "r1", "T", "P", 100)
        original = store._fs_write
        failing = {"on": True}

        def flaky(descriptor, data):
            if failing["on"]:
                raise OSError(errno.ENOSPC, "No space left on device")
            original(descriptor, data)

        monkeypatch.setattr(store, "_fs_write", flaky)
        accepted = [
            store.enqueue_event("s1", "r1", _event("thought", f"e{i}"))
            for i in range(30)
        ]
        assert accepted.count(True) == 5 and accepted.count(False) == 25
        health = store.health()
        assert health["queue_depth"] == 5 and health["dropped_events"] == 25
        failing["on"] = False
        assert await wait_for(lambda: not store.degraded)
        events = await store.read_events("s1", "r1")
        assert markers(events) == [f"e{i}" for i in range(5)]


@pytest.mark.asyncio
async def test_writer_releases_handle_on_delete_and_when_idle(tmp_path):
    async with open_store(tmp_path, writer_idle_s=0.1) as store:
        await seed(store, "s1", "r1", 100, status="running")
        store.enqueue_event("s1", "r1", _event("thought", "before-idle"))
        await store.flush("s1")
        assert await wait_for(lambda: "s1" not in store._writers, timeout=2.0)
        store.enqueue_event("s1", "r1", _event("thought", "after-idle"))
        assert markers(await store.read_events("s1", "r1")) == [
            "before-idle",
            "after-idle",
        ]

    async with open_store(tmp_path / "second", writer_idle_s=60) as store:
        await seed(store, "s9", "r9", 100, status="running")
        store.enqueue_event("s9", "r9", _event("thought", "x"))
        await store.flush("s9")
        writer = store._writers["s9"]
        assert writer._fd is not None
        await store.delete_session("s9")
        assert writer._fd is None and "s9" not in store._writers


@pytest.mark.asyncio
async def test_terminal_events_are_fsynced_and_ordinary_events_are_not(
    tmp_path, monkeypatch
):
    calls: list[int] = []
    real_fsync = os.fsync

    def spy(descriptor):
        calls.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", spy)
    async with open_store(tmp_path, writer_idle_s=60) as store:
        await store.create_run("s1", "r1", "T", "P", 100)
        after_metadata = len(
            calls
        )  # metadata writes fsync too; count only event fsyncs
        for index in range(3):
            store.enqueue_event("s1", "r1", _event("thought", f"e{index}"))
        await store.flush("s1")
        descriptor = store._writers["s1"]._fd
        assert descriptor is not None and descriptor not in calls[after_metadata:]
        before_terminal = len(calls)
        store.enqueue_event("s1", "r1", _event("run_end", "end"))
        await store.flush("s1")
        assert descriptor in calls[before_terminal:]


@pytest.mark.asyncio
async def test_writer_recovers_from_a_torn_partial_write_without_losing_the_next_event(
    tmp_path, monkeypatch
):
    async with open_store(tmp_path, retry_max=5) as store:
        await store.create_run("s1", "r1", "T", "P", 100)
        original = store._fs_write
        calls = {"n": 0}

        def tearing(descriptor, data):
            calls["n"] += 1
            if calls["n"] == 1:
                os.write(descriptor, data[: len(data) // 2])
                raise OSError(errno.EIO, "torn write")
            original(descriptor, data)

        monkeypatch.setattr(store, "_fs_write", tearing)
        store.enqueue_event("s1", "r1", _event("thought", "e0"))
        assert await wait_for(lambda: calls["n"] >= 2)
        store.enqueue_event("s1", "r1", _event("thought", "e1"))
        assert markers(await store.read_events("s1", "r1")) == ["e0", "e1"]


# ----------------------------------------------------------- degradation --
@pytest.mark.asyncio
async def test_degraded_disk_retries_signals_state_and_recovers_in_order(
    tmp_path, monkeypatch
):
    states: list[str] = []
    async with open_store(tmp_path, states, retry_max=500) as store:
        await store.create_run("s1", "r1", "T", "P", 100)
        original = store._fs_write
        flag = {"fail": True}

        def flaky(descriptor, data):
            if flag["fail"]:
                raise OSError(errno.ENOSPC, "No space left on device")
            original(descriptor, data)

        monkeypatch.setattr(store, "_fs_write", flaky)
        for index in range(5):
            store.enqueue_event("s1", "r1", _event("thought", f"e{index}"))
        assert await wait_for(lambda: "degraded" in states)
        health = store.health()
        assert (
            health["status"] == "degraded" and health["last_error_class"] == "OSError"
        )
        assert "No space" not in json.dumps(health), "error details must not be exposed"

        flag["fail"] = False
        assert await wait_for(lambda: "recovered" in states)
        assert markers(await store.read_events("s1", "r1")) == [
            f"e{i}" for i in range(5)
        ]
        assert states == ["degraded", "recovered"]
        assert store.health()["status"] == "healthy"


@pytest.mark.asyncio
async def test_metadata_failure_raises_io_error_then_finish_recovers_the_lost_run(
    tmp_path, monkeypatch
):
    states: list[str] = []
    async with open_store(tmp_path, states) as store:
        original = store._write_record

        def failing(record):
            raise StoreIOError("simulated disk failure")

        monkeypatch.setattr(store, "_write_record", failing)
        with pytest.raises(StoreIOError):
            await store.create_run("s1", "r1", "Title", "Prompt", 100)
        assert store.degraded and states == ["degraded"]

        monkeypatch.setattr(store, "_write_record", original)
        await asyncio.sleep(0.12)  # let the breaker's reset window elapse
        run = await store.finish_run(
            "s1",
            "r1",
            "completed",
            105,
            "done",
            [],
            1,
            1,
            1,
            1,
            title="Title",
            prompt="Prompt",
            started_at=100,
        )
        assert run["status"] == "completed"
        assert store.get_run("s1", "r1")["prompt"] == "Prompt"
        assert states == ["degraded", "recovered"]


@pytest.mark.asyncio
async def test_metadata_writes_fail_fast_while_the_breaker_is_open(
    tmp_path, monkeypatch
):
    async with open_store(tmp_path, breaker_reset_s=30.0, meta_retry_max=0) as store:
        monkeypatch.setattr(
            store,
            "_write_record",
            lambda record: (_ for _ in ()).throw(StoreIOError("down")),
        )
        for _ in range(2):  # reach the breaker threshold
            with pytest.raises(StoreIOError):
                await store.create_run("s1", "r1", "T", "P", 100)
        started = time.monotonic()
        with pytest.raises(StoreIOError):
            await store.create_run("s1", "r2", "T", "P", 100)
        assert (
            time.monotonic() - started < 0.5
        ), "an open circuit must not block the caller"


# ------------------------------------------------------------- retention --
@pytest.mark.asyncio
async def test_prune_by_age_keeps_recent_running_and_protected_sessions(tmp_path):
    now = int(time.time())
    day = 86_400
    async with open_store(tmp_path) as store:
        await seed(store, "old-done", "r1", now - 10 * day)
        await seed(store, "old-protected", "r2", now - 10 * day)
        await seed(store, "old-running", "r3", now - 10 * day, status="running")
        await seed(store, "recent", "r4", now - day)

        result = await store.prune(older_than_days=5, protected={"old-protected"})
        assert result["deleted_sessions"] == 1
        remaining = {item["id"] for item in store.list_session_summaries()}
        assert remaining == {"old-protected", "old-running", "recent"}
        assert not (tmp_path / "old-done").exists()


@pytest.mark.asyncio
async def test_prune_by_size_removes_oldest_first_until_under_the_limit(tmp_path):
    now = int(time.time())
    async with open_store(tmp_path) as store:
        for index, sid in enumerate(("oldest", "middle", "newest")):
            await seed(
                store, sid, f"r{index}", now - (3 - index) * 100, status="running"
            )
            for event_index in range(20):
                store.enqueue_event(
                    sid, f"r{index}", _event("thought", "x" * 200, n=event_index)
                )
            await store.finish_run(
                sid,
                f"r{index}",
                "completed",
                now - (3 - index) * 100 + 1,
                "",
                [],
                0,
                0,
                0,
                0,
            )
        await store.flush("newest")
        sizes = await asyncio.to_thread(store._session_sizes)
        limit = sizes["middle"] + sizes["newest"] + 10
        result = await store.prune(max_bytes=limit)
        assert (
            result["deleted_sessions"] == 1 and result["freed_bytes"] == sizes["oldest"]
        )
        assert {item["id"] for item in store.list_session_summaries()} == {
            "middle",
            "newest",
        }


# ---------------------------------------------------------------- health --
@pytest.mark.asyncio
async def test_health_reports_healthy_and_unhealthy_on_low_disk_space(tmp_path):
    async with open_store(tmp_path) as store:
        healthy = store.health()
        assert healthy["status"] == "healthy" and healthy["writable"] is True
        assert healthy["queue_depth"] == 0 and healthy["breaker_state"] == "closed"
    async with open_store(tmp_path, min_free_bytes=10**18) as starved:
        assert starved.health()["status"] == "unhealthy"


# ----------------------------------------------- redaction, blobs, limits --
@pytest.mark.asyncio
async def test_secrets_never_reach_disk_but_live_events_are_unchanged(tmp_path):
    secrets_in = [FAKE_KEY, FAKE_GOOGLE, "hunter2hunter2", "Bearer abcdefgh12345"]
    live_event = {
        "type": "tool_end",
        "data": {
            "name": "http",
            "arguments": {"api_key": "hunter2hunter2"},
            "result": f"saw {FAKE_KEY} and {FAKE_GOOGLE} and Authorization: Bearer abcdefgh12345",
            "ts": 1,
        },
    }
    async with open_store(tmp_path) as store:
        await store.create_run(
            "s1", "r1", f"Use {FAKE_KEY}", f"Prompt with {FAKE_GOOGLE}", 100
        )
        store.enqueue_event("s1", "r1", live_event)
        await store.finish_run(
            "s1",
            "r1",
            "completed",
            105,
            f"Final mentions {FAKE_KEY}",
            [f"notes-{FAKE_KEY}.md"],
            1,
            1,
            1,
            1,
        )
        await store.flush("s1")

    for path in tmp_path.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            for secret in secrets_in:
                assert (
                    secret.encode() not in content
                ), f"{secret!r} leaked into {path.name}"
    assert (
        FAKE_KEY in live_event["data"]["result"]
    ), "the live stream's event is never mutated"
    async with open_store(tmp_path) as reopened:
        stored = (await reopened.read_events("s1", "r1"))[0]["data"]
        assert stored["name"] == "http" and "[REDACTED]" in stored["result"]


@pytest.mark.asyncio
async def test_screenshots_are_offloaded_to_blobs_not_inlined(tmp_path):
    async with open_store(tmp_path) as store:
        await store.create_run("s1", "r1", "T", "P", 100)
        for _ in range(2):  # identical images are stored once
            store.enqueue_event(
                "s1",
                "r1",
                {"type": "browser_screenshot", "data": {"image": PNG_B64, "ts": 1}},
            )
        store.enqueue_event(
            "s1",
            "r1",
            {
                "type": "browser_screenshot",
                "data": {
                    "image": base64.b64encode(b"definitely not an image").decode(),
                    "ts": 2,
                },
            },
        )
        await store.flush("s1")

        raw = (tmp_path / "s1" / "events.jsonl").read_text()
        assert PNG_B64 not in raw and "iVBOR" not in raw
        events = await store.read_events("s1", "r1")
        reference = events[0]["data"]["image_ref"]
        assert reference.endswith(".png") and "image" not in events[0]["data"]
        assert events[1]["data"]["image_ref"] == reference
        assert events[2]["data"]["image_omitted"] is True
        blob = store.blob_path("s1", reference)
        assert blob is not None and blob.read_bytes() == PNG
        assert (blob.stat().st_mode & 0o777) == 0o600
        assert len(list((tmp_path / "s1" / "blobs").iterdir())) == 1
        for hostile in (
            "../../etc/passwd",
            "../session.json",
            "nothex.png",
            "a" * 32 + ".exe",
        ):
            assert store.blob_path("s1", hostile) is None
        with pytest.raises(SessionStoreError):
            store.blob_path("../s1", reference)


@pytest.mark.asyncio
async def test_size_caps_truncate_events_and_records_with_markers(tmp_path):
    async with open_store(tmp_path, max_event_chars=100, max_record_chars=200) as store:
        await store.create_run("s1", "r1", "T", "P" * 1000, 100)
        store.enqueue_event("s1", "r1", _event("terminal", "m", output="y" * 500))
        await store.finish_run("s1", "r1", "completed", 101, "F" * 1000, [], 0, 0, 0, 0)
        stored = (await store.read_events("s1", "r1"))[0]["data"]
        assert (
            stored["output"].startswith("y" * 100)
            and "truncated 400 chars" in stored["output"]
        )
        assert stored["_truncated"] == ["output"]
        run = store.get_run("s1", "r1")
        assert (
            "truncated 800 chars" in run["prompt"]
            and "truncated 800 chars" in run["final_response"]
        )
