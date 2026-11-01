"""Disk-backed run history for the OpenManus web portal.

Stores one metadata document and one append-only JSONL event log per session so
previous runs remain inspectable after a webapp restart. ``session.json`` and
``events.jsonl`` are the source of truth; ``index.json`` is a derived,
rebuildable summary that keeps list and search requests off the disk.

Highlights:
    * Events go through a bounded per-session writer queue with batched,
      kept-open appends; terminal events are fsynced.
    * Disk failures degrade instead of failing runs: a circuit breaker,
      retry with backoff and jitter, and a degraded/recovered state callback.
    * Secrets are redacted, long strings capped, and screenshots offloaded to
      ``blobs/`` before anything is written.
    * Startup reconciles runs left ``running`` into ``interrupted`` using each
      run's last event time, and rebuilds a stale or corrupt index.

Key classes:
    SessionStore: persist and query sessions, runs, events, and blobs.
    SessionStoreError / StoreIOError / StoreVersionError: failure types.

Usage:
    store = SessionStore(Path("session_store"), HistoryConfig.from_env())
    await store.create_run("session-id", "run-id", "Title", "Prompt", 1_700_000_000)
    store.enqueue_event("session-id", "run-id", {"type": "run_end", "data": {}})

Dependencies: Python 3.11+ standard library, ``app.exceptions``, and the
``workspace.history_*`` helper modules.
Tier 1: if history writes fail silently (full disk, dropped events) the
operator loses the only record of past runs and cannot recreate it, so every
failure path surfaces through ``health()`` and the state-change callback.
"""

import asyncio
import base64
import binascii
import contextlib
import copy
import hashlib
import json
import logging
import os
import re
import shutil
import time
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

from app.exceptions import OpenManusError
from workspace.history_config import HistoryConfig
from workspace.history_redaction import Redactor
from workspace.history_resilience import CircuitBreaker, CircuitOpenError, backoff_delay

logger = logging.getLogger(__name__)

T = TypeVar("T")

SCHEMA_VERSION = 1
INDEX_VERSION = 1
TERMINAL_EVENT_TYPES = frozenset({"final_result", "status", "error", "run_end"})

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_BLOB_NAME_RE = re.compile(r"^[a-f0-9]{32}\.(png|jpg|gif|webp)$")
_VALID_RUN_STATUSES = {"running", "completed", "stopped", "failed", "interrupted"}
_SORTS = {"newest", "oldest", "duration", "tokens"}
_PREVIEW_CHARS = 300
_SEARCH_CHARS = 1_000
_INDEX_FILE_LIMIT = 20


class SessionStoreError(OpenManusError):
    """Raise when a session-store operation cannot be completed safely."""


class StoreIOError(SessionStoreError):
    """Raise for transient filesystem failures that are safe to retry."""


class StoreVersionError(SessionStoreError):
    """Raise when stored data was written by a newer schema version."""


def _migrate_v0_to_v1(record: dict[str, Any]) -> dict[str, Any]:
    """Stamp pre-versioning records as schema 1 (no structural change)."""
    record["version"] = 1
    return record


_MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {
    0: _migrate_v0_to_v1
}


def _valid_timestamp(value: Any) -> bool:
    """Return whether a value is a non-negative numeric Unix timestamp."""
    return isinstance(value, int | float) and not isinstance(value, bool) and value >= 0


def _is_transient(exc: BaseException) -> bool:
    """Whether an exception is a retryable filesystem failure."""
    return isinstance(exc, OSError | StoreIOError)


def _sniff_extension(data: bytes) -> str | None:
    """Return a blob file extension for known image signatures, else None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"GIF8"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write a mode-600 file atomically using a temp file and replace."""
    temp = path.with_name(path.name + ".tmp")
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except OSError:
        temp.unlink(missing_ok=True)
        raise


class _SessionWriter:
    """Single-consumer event writer for one session.

    Drains a bounded queue in batches and appends them through one kept-open
    file descriptor. Idle writers release their descriptor and remove
    themselves so many sessions never hold many open files.
    """

    def __init__(self, store: "SessionStore", session_id: str) -> None:
        self.store = store
        self.session_id = session_id
        self.queue: asyncio.Queue[tuple[str | None, dict[str, Any], bool]] = (
            asyncio.Queue(maxsize=store.config.queue_max)
        )
        self._fd: int | None = None
        self._torn_tail = False
        self.task = asyncio.get_running_loop().create_task(
            self._run(), name=f"history-writer-{session_id[:16]}"
        )

    def _close_fd(self) -> None:
        """Close the descriptor, ignoring errors (it may already be invalid)."""
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None

    def _ensure_fd(self) -> int:
        """Open the append descriptor lazily."""
        if self._fd is None:
            directory = self.store._session_dir(self.session_id)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._fd = os.open(
                directory / "events.jsonl",
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
        return self._fd

    def _write_sync(self, batch: list[tuple[str | None, dict[str, Any], bool]]) -> None:
        """Sanitize and append one batch (runs in a worker thread)."""
        store = self.store
        lines: list[str] = []
        durable = False
        for run_id, event, wants_fsync in batch:
            try:
                clean = store._redactor.sanitize_event(
                    event,
                    max_chars=store.config.max_event_chars,
                    offload_image=lambda b64: store._write_blob_sync(
                        self.session_id, b64
                    ),
                )
                line = json.dumps(
                    {"run_id": run_id, "event": clean},
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
            except (TypeError, ValueError):
                line = json.dumps(
                    {
                        "run_id": run_id,
                        "event": {
                            "type": event.get("type"),
                            "data": {"_unserializable": True},
                        },
                    },
                    separators=(",", ":"),
                )
            lines.append(line)
            durable = durable or wants_fsync
        payload = ("\n".join(lines) + "\n").encode("utf-8", errors="replace")
        if self._torn_tail:
            payload = b"\n" + payload
        try:
            descriptor = self._ensure_fd()
            store._fs_write(descriptor, payload)
            if durable:
                os.fsync(descriptor)
        except OSError:
            self._torn_tail = True
            self._close_fd()
            raise
        self._torn_tail = False

    async def _flush_batch(
        self, batch: list[tuple[str | None, dict[str, Any], bool]]
    ) -> None:
        """Write a batch with breaker, retry, backoff, and counted drops."""
        store = self.store
        config = store.config
        attempt = 0
        while True:
            try:
                await store._breaker.call(
                    lambda: store._run_io(lambda: self._write_sync(batch)),
                    is_failure=_is_transient,
                )
            except CircuitOpenError:
                await asyncio.sleep(
                    max(store._breaker.remaining_open(), config.backoff_base_s)
                )
                continue
            except (OSError, StoreIOError) as exc:
                store._record_failure(exc)
                attempt += 1
                if attempt > config.retry_max:
                    store._dropped_events += len(batch)
                    logger.error(
                        "Dropped %d history event(s) after retries", len(batch)
                    )
                    return
                await asyncio.sleep(
                    backoff_delay(
                        attempt - 1, config.backoff_base_s, config.backoff_max_s
                    )
                )
                continue
            store._record_success()
            return

    async def _run(self) -> None:
        """Consume the queue until idle or cancelled."""
        config = self.store.config
        try:
            while True:
                try:
                    first = await asyncio.wait_for(
                        self.queue.get(), timeout=config.writer_idle_s
                    )
                except TimeoutError:
                    if self.queue.empty():
                        if self.store._writers.get(self.session_id) is self:
                            del self.store._writers[self.session_id]
                        return
                    continue
                batch = [first]
                while len(batch) < config.batch_max:
                    try:
                        batch.append(self.queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                try:
                    await self._flush_batch(batch)
                finally:
                    for _ in batch:
                        self.queue.task_done()
        finally:
            self._close_fd()

    async def close(self) -> None:
        """Cancel the consumer, release the descriptor, and discard leftovers."""
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task
        self._close_fd()
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()


class SessionStore:
    """Persist OpenManus session metadata, run summaries, events, and blobs.

    Args:
        root: Directory in which per-session records are stored.
        config: Limits and resilience settings; defaults are used when omitted.
        on_state_change: Called with ``"degraded"`` or ``"recovered"`` when disk
            health changes. Must be quick and non-blocking.

    Raises:
        SessionStoreError: If the root cannot be created.

    Example:
        >>> store = SessionStore(Path("session_store"))
        >>> store.query_runs()["runs"]
        []

    Note:
        Metadata writes are atomic and serialized per session. A single webapp
        process is the expected writer. Construction scans the directory and
        may rewrite records, so run it in a worker thread from async code.
    """

    def __init__(
        self,
        root: Path,
        config: HistoryConfig | None = None,
        on_state_change: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config or HistoryConfig()
        self.root = root.expanduser().resolve()
        self._on_state_change = on_state_change
        self._redactor = Redactor(self.config.extra_redaction_patterns)
        self._breaker = CircuitBreaker(
            self.config.breaker_threshold, self.config.breaker_reset_s
        )
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_refs: dict[str, int] = {}
        self._writers: dict[str, _SessionWriter] = {}
        self._index_lock = asyncio.Lock()
        self._known: set[str] = set()
        self._sessions_idx: dict[str, dict[str, Any]] = {}
        self._runs_idx: dict[str, list[dict[str, Any]]] = {}
        self._signatures: dict[str, list[int]] = {}
        self._degraded = False
        self._last_error_class: str | None = None
        self._last_error_at: float | None = None
        self._dropped_events = 0
        self._closed = False
        try:
            created = not self.root.exists()
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if created:
                os.chmod(self.root, 0o700)
        except OSError as exc:
            raise SessionStoreError("Could not create the session store") from exc
        self._load_index_and_reconcile()

    # ------------------------------------------------------------------
    # Paths, locks, and low-level I/O
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_id(value: str) -> str:
        """Return a safe path component or raise for an invalid identifier."""
        if not isinstance(value, str) or not _ID_RE.fullmatch(value):
            raise SessionStoreError("Invalid session or run identifier")
        return value

    @staticmethod
    def is_valid_id(value: str) -> bool:
        """Return whether a session or run identifier is safe to use in a path.

        Args:
            value: Identifier to check.

        Returns:
            True for 1-128 characters of letters, digits, ``_`` and ``-``.

        Example:
            >>> SessionStore.is_valid_id("../etc")
            False
        """
        return isinstance(value, str) and bool(_ID_RE.fullmatch(value))

    def _session_dir(self, session_id: str) -> Path:
        """Return a path-confined session directory."""
        self._validate_id(session_id)
        candidate = (self.root / session_id).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise SessionStoreError("Session path escapes the store") from exc
        return candidate

    @asynccontextmanager
    async def _session_lock(self, session_id: str) -> AsyncIterator[None]:
        """Hold a per-session lock; entries are dropped when unused."""
        self._validate_id(session_id)
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        self._lock_refs[session_id] = self._lock_refs.get(session_id, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._lock_refs[session_id] -= 1
            if self._lock_refs[session_id] <= 0:
                self._lock_refs.pop(session_id, None)
                self._locks.pop(session_id, None)

    @staticmethod
    async def _run_io(operation: Callable[[], T]) -> T:
        """Run blocking work in a thread, finishing it even if cancelled."""
        task = asyncio.ensure_future(asyncio.to_thread(operation))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    @staticmethod
    def _fs_write(descriptor: int, data: bytes) -> None:
        """Write all bytes to a descriptor (a seam for failure-injection tests)."""
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]

    def _record_failure(self, exc: BaseException) -> None:
        """Note a transient failure and signal the degraded transition once."""
        self._last_error_class = type(exc.__cause__ or exc).__name__
        self._last_error_at = time.time()
        if not self._degraded:
            self._degraded = True
            self._notify("degraded")

    def _record_success(self) -> None:
        """Note a successful write and signal recovery once."""
        if self._degraded:
            self._degraded = False
            self._notify("recovered")

    def _notify(self, state: str) -> None:
        """Invoke the state callback without letting it break the store."""
        if self._on_state_change is None:
            return
        try:
            self._on_state_change(state)
        except Exception as exc:  # noqa: BLE001 - isolate caller-supplied callbacks
            logger.warning("History state callback failed (%s)", type(exc).__name__)

    async def _guarded_io(
        self,
        operation: Callable[[], T],
        *,
        retry_max: int,
        wait_on_open: bool = False,
    ) -> T:
        """Run blocking work through the breaker with bounded retries.

        Raises:
            StoreIOError: When retries are exhausted or the circuit is open and
                ``wait_on_open`` is False.
        """
        attempt = 0
        while True:
            try:
                result = await self._breaker.call(
                    lambda: self._run_io(operation), is_failure=_is_transient
                )
            except CircuitOpenError as exc:
                if not wait_on_open:
                    raise StoreIOError("History store temporarily unavailable") from exc
                await asyncio.sleep(
                    max(self._breaker.remaining_open(), self.config.backoff_base_s)
                )
                continue
            except (OSError, StoreIOError) as exc:
                self._record_failure(exc)
                attempt += 1
                if attempt > retry_max:
                    if isinstance(exc, StoreIOError):
                        raise
                    raise StoreIOError("History store write failed") from exc
                await asyncio.sleep(
                    backoff_delay(
                        attempt - 1,
                        self.config.backoff_base_s,
                        self.config.backoff_max_s,
                    )
                )
                continue
            self._record_success()
            return result

    # ------------------------------------------------------------------
    # Records and schema
    # ------------------------------------------------------------------
    @staticmethod
    def _new_record(session_id: str, title: str, created_at: int) -> dict[str, Any]:
        """Build an empty session metadata record."""
        return {
            "version": SCHEMA_VERSION,
            "id": session_id,
            "title": title,
            "created_at": created_at,
            "last_active": created_at,
            "runs": [],
        }

    def _read_record(self, session_id: str) -> dict[str, Any] | None:
        """Read, migrate, and validate one metadata record."""
        path = self._session_dir(session_id) / "session.json"
        if not path.is_file():
            return None
        try:
            with path.open(encoding="utf-8") as handle:
                record = json.load(handle)
        except json.JSONDecodeError as exc:
            raise SessionStoreError("Stored session metadata is unreadable") from exc
        except OSError as exc:
            raise StoreIOError("Stored session metadata could not be read") from exc
        if not isinstance(record, dict):
            raise SessionStoreError("Stored session metadata has an invalid shape")
        version = record.get("version", 0)
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise SessionStoreError("Stored session metadata has an invalid version")
        if version > SCHEMA_VERSION:
            raise StoreVersionError("Stored session metadata is from a newer version")
        while version < SCHEMA_VERSION:
            record = _MIGRATIONS[version](record)
            version = int(record["version"])
        if (
            record.get("id") != session_id
            or not isinstance(record.get("title"), str)
            or not _valid_timestamp(record.get("created_at"))
            or not _valid_timestamp(record.get("last_active"))
            or not isinstance(record.get("runs"), list)
        ):
            raise SessionStoreError("Stored session metadata has an invalid shape")
        for run in record["runs"]:
            if (
                not isinstance(run, dict)
                or not isinstance(run.get("id"), str)
                or not _ID_RE.fullmatch(run["id"])
                or run.get("status") not in _VALID_RUN_STATUSES
                or not _valid_timestamp(run.get("started_at"))
            ):
                raise SessionStoreError("Stored run metadata has an invalid shape")
        return record

    def _write_record(self, record: dict[str, Any]) -> None:
        """Atomically replace one metadata document."""
        session_id = self._validate_id(str(record.get("id", "")))
        directory = self._session_dir(session_id)
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)
            record["version"] = SCHEMA_VERSION
            payload = json.dumps(
                record, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            _atomic_write(directory / "session.json", payload)
        except OSError as exc:
            raise StoreIOError("Could not write session metadata") from exc
        except (TypeError, ValueError) as exc:
            raise SessionStoreError("Could not serialize session metadata") from exc

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Return a copy of one session record without its events.

        Args:
            session_id: Opaque session identifier.

        Returns:
            The session metadata, or None when no record exists.

        Raises:
            SessionStoreError: If the identifier or stored metadata is invalid.

        Example:
            >>> SessionStore(Path("session_store")).get_session("missing") is None
            True
        """
        record = self._read_record(session_id)
        return copy.deepcopy(record) if record is not None else None

    def get_run(self, session_id: str, run_id: str) -> dict[str, Any] | None:
        """Return one full run record, or None when it does not exist.

        Args:
            session_id: Session containing the run.
            run_id: Unique run identifier.

        Returns:
            A copy of the run (full prompt and response) with session fields.

        Raises:
            SessionStoreError: If identifiers or stored metadata are invalid.

        Example:
            >>> SessionStore(Path("session_store")).get_run("missing", "missing") is None
            True
        """
        self._validate_id(run_id)
        record = self._read_record(session_id)
        if record is None:
            return None
        for stored in record["runs"]:
            if stored.get("id") == run_id:
                run = copy.deepcopy(stored)
                run["session_id"] = session_id
                run["session_title"] = record.get("title") or "Task"
                return run
        return None

    # ------------------------------------------------------------------
    # Index (derived, rebuildable)
    # ------------------------------------------------------------------
    def _signature(self, session_id: str) -> list[int] | None:
        """Cheap change signature of a session's metadata file."""
        try:
            stat = (self._session_dir(session_id) / "session.json").stat()
        except OSError:
            return None
        return [stat.st_mtime_ns, stat.st_size]

    @staticmethod
    def _summarize_runs(record: dict[str, Any]) -> list[dict[str, Any]]:
        """Build compact per-run index entries (previews, no full text)."""
        summaries: list[dict[str, Any]] = []
        for position, run in enumerate(record["runs"]):
            prompt = str(run.get("prompt") or "")
            final = str(run.get("final_response") or "")
            title = str(run.get("title") or "")
            files = [str(item) for item in (run.get("files") or [])]
            summaries.append(
                {
                    "seq": position,
                    "id": run["id"],
                    "session_id": record["id"],
                    "session_title": record.get("title") or "Task",
                    "title": title,
                    "status": run["status"],
                    "started_at": run["started_at"],
                    "ended_at": run.get("ended_at"),
                    "duration_ms": int(run.get("duration_ms") or 0),
                    "steps": int(run.get("steps") or 0),
                    "actions": int(run.get("actions") or 0),
                    "input_tokens": int(run.get("input_tokens") or 0),
                    "output_tokens": int(run.get("output_tokens") or 0),
                    "file_count": len(files),
                    "files": files[:_INDEX_FILE_LIMIT],
                    "prompt_preview": prompt[:_PREVIEW_CHARS],
                    "final_preview": final[:_PREVIEW_CHARS],
                    "search": f"{title}\n{prompt[:_SEARCH_CHARS]}\n{final[:_SEARCH_CHARS]}".lower(),
                }
            )
        return summaries

    @staticmethod
    def _summarize_session(record: dict[str, Any]) -> dict[str, Any]:
        """Build the compact session index entry."""
        runs = record["runs"]
        latest = max(runs, key=lambda item: item["started_at"], default=None)
        return {
            "id": record["id"],
            "title": record.get("title") or "Task",
            "created_at": record["created_at"],
            "last_active": record["last_active"],
            "run_count": len(runs),
            "latest_run_id": latest["id"] if latest else None,
        }

    def _index_apply(self, record: dict[str, Any]) -> None:
        """Refresh the in-memory index for one record (event-loop thread only)."""
        session_id = record["id"]
        self._sessions_idx[session_id] = self._summarize_session(record)
        self._runs_idx[session_id] = self._summarize_runs(record)
        signature = self._signature(session_id)
        if signature is not None:
            self._signatures[session_id] = signature

    def _index_remove(self, session_id: str) -> None:
        """Drop one session from the in-memory index."""
        self._sessions_idx.pop(session_id, None)
        self._runs_idx.pop(session_id, None)
        self._signatures.pop(session_id, None)

    def _index_payload(self) -> str:
        """Serialize the index (call from the event-loop thread)."""
        return json.dumps(
            {
                "version": INDEX_VERSION,
                "signatures": self._signatures,
                "sessions": self._sessions_idx,
                "runs": self._runs_idx,
            },
            separators=(",", ":"),
        )

    def _write_index_sync(self, payload: str) -> None:
        """Atomically write ``index.json``."""
        try:
            _atomic_write(self.root / "index.json", payload.encode("utf-8"))
        except OSError as exc:
            raise StoreIOError("Could not write the history index") from exc

    async def _persist_index(self) -> None:
        """Persist the derived index; failures are logged, never fatal."""
        async with self._index_lock:
            payload = self._index_payload()
            try:
                await self._guarded_io(
                    lambda: self._write_index_sync(payload),
                    retry_max=self.config.meta_retry_max,
                )
            except SessionStoreError as exc:
                logger.warning("History index not persisted (%s)", type(exc).__name__)

    def _read_index_file(self) -> dict[str, Any]:
        """Load ``index.json`` if present and structurally valid, else {}."""
        path = self.root / "index.json"
        if not path.is_file():
            return {}
        try:
            with path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            logger.warning("History index unreadable; rebuilding")
            return {}
        if (
            not isinstance(data, dict)
            or data.get("version") != INDEX_VERSION
            or not isinstance(data.get("signatures"), dict)
            or not isinstance(data.get("sessions"), dict)
            or not isinstance(data.get("runs"), dict)
        ):
            logger.warning("History index invalid; rebuilding")
            return {}
        return data

    def _scan_signatures(self) -> dict[str, list[int]]:
        """Stat every session metadata file (no parsing)."""
        current: dict[str, list[int]] = {}
        try:
            entries = list(self.root.iterdir())
        except OSError as exc:
            raise SessionStoreError("Could not list the session store") from exc
        for entry in entries:
            if not entry.is_dir() or not _ID_RE.fullmatch(entry.name):
                continue
            signature = self._signature(entry.name)
            if signature is not None:
                current[entry.name] = signature
        return current

    def _has_running(self, session_id: str) -> bool:
        """Whether the index shows any running run for a session."""
        return any(
            run["status"] == "running" for run in self._runs_idx.get(session_id, [])
        )

    def _last_event_time(self, session_id: str, run_id: str, fallback: int) -> int:
        """Return the last valid event timestamp (seconds) for a run."""
        path = self._session_dir(session_id) / "events.jsonl"
        if not path.is_file():
            return fallback
        last = fallback
        try:
            with path.open("rb") as handle:
                for line in handle:
                    if not line.endswith(b"\n"):
                        continue
                    try:
                        envelope = json.loads(line)
                        if envelope.get("run_id") != run_id:
                            continue
                        timestamp = envelope["event"]["data"].get("ts")
                        if _valid_timestamp(timestamp):
                            last = max(last, int(timestamp / 1000))
                    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
                        continue
        except OSError:
            return fallback
        return last

    def _reconcile_session(self, session_id: str) -> bool:
        """Mark running runs interrupted using their last event, not restart time."""
        try:
            record = self._read_record(session_id)
        except SessionStoreError:
            return False
        if record is None:
            return False
        changed = False
        for run in record["runs"]:
            if run.get("status") == "running":
                started_at = int(run["started_at"])
                ended_at = self._last_event_time(session_id, run["id"], started_at)
                run["status"] = "interrupted"
                run["ended_at"] = ended_at
                run["duration_ms"] = max(0, (ended_at - started_at) * 1000)
                record["last_active"] = max(int(record["last_active"]), ended_at)
                changed = True
        if changed:
            try:
                self._write_record(record)
            except SessionStoreError as exc:
                logger.warning("Could not reconcile a session (%s)", type(exc).__name__)
                return False
            self._index_apply(record)
        return changed

    def _load_index_and_reconcile(self) -> None:
        """Load the index, re-read stale sessions, and reconcile interrupted runs."""
        current = self._scan_signatures()
        cached = self._read_index_file()
        cached_signatures = cached.get("signatures", {})
        cached_sessions = cached.get("sessions", {})
        cached_runs = cached.get("runs", {})
        dirty = not cached
        for session_id, signature in current.items():
            if (
                cached_signatures.get(session_id) == signature
                and isinstance(cached_sessions.get(session_id), dict)
                and isinstance(cached_runs.get(session_id), list)
            ):
                self._sessions_idx[session_id] = cached_sessions[session_id]
                self._runs_idx[session_id] = cached_runs[session_id]
                self._signatures[session_id] = signature
                continue
            dirty = True
            try:
                record = self._read_record(session_id)
            except SessionStoreError as exc:
                logger.warning(
                    "Skipping unreadable session-store record %s (%s)",
                    session_id,
                    type(exc).__name__,
                )
                continue
            if record is not None:
                self._index_apply(record)
        if set(cached_signatures) - set(current):
            dirty = True
        for session_id in list(self._runs_idx):
            if self._has_running(session_id) and self._reconcile_session(session_id):
                dirty = True
        self._known = set(self._sessions_idx)
        if dirty:
            try:
                self._write_index_sync(self._index_payload())
            except SessionStoreError as exc:
                logger.warning("History index not persisted (%s)", type(exc).__name__)

    @staticmethod
    def _public_run(run: dict[str, Any]) -> dict[str, Any]:
        """Copy an index entry without its internal search text."""
        return {
            key: (list(value) if isinstance(value, list) else value)
            for key, value in run.items()
            if key not in ("search", "seq")
        }

    @staticmethod
    def _decode_cursor(cursor: str | None) -> int:
        """Decode an opaque pagination cursor into an offset."""
        if not cursor:
            return 0
        try:
            offset = json.loads(base64.urlsafe_b64decode(cursor.encode()))["o"]
        except (ValueError, KeyError, TypeError, binascii.Error) as exc:
            raise SessionStoreError("Invalid cursor") from exc
        if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0:
            return offset
        raise SessionStoreError("Invalid cursor")

    @staticmethod
    def _encode_cursor(offset: int) -> str:
        """Encode an offset as an opaque pagination cursor."""
        return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode()

    def query_runs(
        self,
        *,
        limit: int | None = 50,
        cursor: str | None = None,
        status: str | None = None,
        q: str | None = None,
        since: float | None = None,
        until: float | None = None,
        sort: str = "newest",
    ) -> dict[str, Any]:
        """Filter, sort, and paginate run summaries from the in-memory index.

        Args:
            limit: Page size, or None for all matches.
            cursor: Opaque cursor from a previous page's ``next_cursor``.
            status: Only runs with this status.
            q: Case-insensitive substring over title, prompt, and final response.
            since: Only runs started at or after this Unix time.
            until: Only runs started at or before this Unix time.
            sort: ``newest``, ``oldest``, ``duration``, or ``tokens``.

        Returns:
            ``{"runs", "next_cursor", "summary", "total_all"}`` where summary
            covers every match (not just the page).

        Raises:
            SessionStoreError: For an invalid sort, status, or cursor.

        Example:
            >>> SessionStore(Path("session_store")).query_runs(limit=5)["next_cursor"]
        """
        if sort not in _SORTS:
            raise SessionStoreError("Invalid sort")
        if status is not None and status not in _VALID_RUN_STATUSES:
            raise SessionStoreError("Invalid status filter")
        offset = self._decode_cursor(cursor)
        needle = (q or "").strip().lower()
        matches: list[dict[str, Any]] = []
        total_all = 0
        for session_runs in self._runs_idx.values():
            total_all += len(session_runs)
            for run in session_runs:
                if status is not None and run["status"] != status:
                    continue
                if since is not None and run["started_at"] < since:
                    continue
                if until is not None and run["started_at"] > until:
                    continue
                if needle and needle not in run["search"]:
                    continue
                matches.append(run)

        def key(run: dict[str, Any]) -> tuple[float, float, float]:
            if sort == "duration":
                primary = float(run["duration_ms"])
            elif sort == "tokens":
                primary = float(run["input_tokens"] + run["output_tokens"])
            else:
                primary = float(run["started_at"])
            return (primary, float(run["started_at"]), float(run.get("seq", 0)))

        matches.sort(key=key, reverse=sort != "oldest")
        finished = [run for run in matches if run["status"] != "running"]
        completed = sum(1 for run in finished if run["status"] == "completed")
        summary = {
            "total": len(matches),
            "completed": completed,
            "failed": sum(1 for run in matches if run["status"] == "failed"),
            "success_rate": (completed / len(finished)) if finished else None,
            "total_tokens": sum(
                run["input_tokens"] + run["output_tokens"] for run in matches
            ),
        }
        page = matches[offset:] if limit is None else matches[offset : offset + limit]
        next_offset = offset + (limit or 0)
        next_cursor = (
            self._encode_cursor(next_offset)
            if limit and next_offset < len(matches)
            else None
        )
        return {
            "runs": [self._public_run(run) for run in page],
            "next_cursor": next_cursor,
            "summary": summary,
            "total_all": total_all,
        }

    def list_session_summaries(self) -> list[dict[str, Any]]:
        """Return compact session summaries from the index, newest activity first.

        Returns:
            Session dictionaries with ``run_count`` and ``latest_run_id``.

        Example:
            >>> SessionStore(Path("session_store")).list_session_summaries()
            []
        """
        sessions = [dict(item) for item in self._sessions_idx.values()]
        sessions.sort(key=lambda item: int(item.get("last_active") or 0), reverse=True)
        return sessions

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------
    async def create_run(
        self,
        session_id: str,
        run_id: str,
        title: str,
        prompt: str,
        started_at: int,
    ) -> dict[str, Any]:
        """Create session metadata if needed and append a running record.

        Args:
            session_id: Opaque session identifier.
            run_id: Unique identifier for this turn.
            title: Short session title derived from the first prompt.
            prompt: Full prompt for this run (redacted before storage).
            started_at: Unix timestamp in seconds.

        Returns:
            The newly created run summary.

        Raises:
            SessionStoreError: If identifiers or metadata are invalid.
            StoreIOError: If the disk write fails after bounded retries.

        Example:
            >>> run = await store.create_run("s1", "r1", "Title", "Prompt", 1)
            >>> run["status"]
            'running'
        """
        self._validate_id(run_id)
        self._validate_id(session_id)
        title = self._redactor.redact_and_cap(title, 200)
        prompt = self._redactor.redact_and_cap(prompt, self.config.max_record_chars)
        self._known.add(session_id)
        async with self._session_lock(session_id):

            def work() -> tuple[dict[str, Any], dict[str, Any]]:
                record = self._read_record(session_id) or self._new_record(
                    session_id, title, started_at
                )
                if any(item.get("id") == run_id for item in record["runs"]):
                    raise SessionStoreError("Run identifier already exists")
                run = self._new_run(run_id, title, prompt, started_at)
                record["runs"].append(run)
                record["title"] = record.get("title") or title
                record["last_active"] = started_at
                self._write_record(record)
                return record, copy.deepcopy(run)

            record, run = await self._guarded_io(
                work, retry_max=self.config.meta_retry_max
            )
            self._index_apply(record)
        await self._persist_index()
        return run

    @staticmethod
    def _new_run(
        run_id: str, title: str, prompt: str, started_at: int
    ) -> dict[str, Any]:
        """Build a fresh running-run record."""
        return {
            "id": run_id,
            "title": title,
            "prompt": prompt,
            "status": "running",
            "started_at": started_at,
            "ended_at": None,
            "duration_ms": 0,
            "steps": 0,
            "actions": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "final_response": "",
            "files": [],
        }

    async def finish_run(
        self,
        session_id: str,
        run_id: str,
        status: str,
        ended_at: int,
        final_response: str,
        files: list[str],
        steps: int,
        actions: int,
        input_tokens: int,
        output_tokens: int,
        *,
        title: str | None = None,
        prompt: str | None = None,
        started_at: int | None = None,
    ) -> dict[str, Any]:
        """Mark one run finished and persist its summary metrics.

        Args:
            session_id: Session containing the run.
            run_id: Run to update.
            status: completed, stopped, failed, or interrupted.
            ended_at: Unix timestamp in seconds.
            final_response: Last assistant response (redacted before storage).
            files: Workspace-relative produced-file paths.
            steps: Steps executed in this run.
            actions: Tool calls started in this run.
            input_tokens: Input tokens consumed by this run.
            output_tokens: Output tokens produced by this run.
            title: With ``prompt`` and ``started_at``, lets a run whose start
                record was lost to a disk failure be created here instead.
            prompt: See ``title``.
            started_at: See ``title``.

        Returns:
            The updated run summary.

        Raises:
            SessionStoreError: For an invalid status/identifier or a missing run
                when no creation details are given.
            StoreIOError: If the disk write fails after bounded retries.

        Example:
            >>> result = await store.finish_run("s1", "r1", "completed", 2, "", [], 0, 0, 0, 0)
            >>> result["status"]
            'completed'
        """
        self._validate_id(run_id)
        self._validate_id(session_id)
        if status not in _VALID_RUN_STATUSES - {"running"}:
            raise SessionStoreError("Invalid finished-run status")
        final_response = self._redactor.redact_and_cap(
            final_response, self.config.max_record_chars
        )
        safe_files = [self._redactor.redact_text(str(item)) for item in files]
        creation: tuple[str, str, int] | None = None
        if title is not None and prompt is not None and started_at is not None:
            creation = (
                self._redactor.redact_and_cap(title, 200),
                self._redactor.redact_and_cap(prompt, self.config.max_record_chars),
                started_at,
            )
        self._known.add(session_id)
        async with self._session_lock(session_id):

            def work() -> tuple[dict[str, Any], dict[str, Any]]:
                record = self._read_record(session_id)
                if record is None:
                    if creation is None:
                        raise SessionStoreError("Cannot finish a missing run")
                    record = self._new_record(session_id, creation[0], creation[2])
                target = next(
                    (item for item in record["runs"] if item.get("id") == run_id), None
                )
                if target is None:
                    if creation is None:
                        raise SessionStoreError("Cannot finish a missing run")
                    target = self._new_run(
                        run_id, creation[0], creation[1], creation[2]
                    )
                    record["runs"].append(target)
                target.update(
                    {
                        "status": status,
                        "ended_at": ended_at,
                        "duration_ms": max(
                            0,
                            (ended_at - int(target.get("started_at") or ended_at))
                            * 1000,
                        ),
                        "final_response": final_response,
                        "files": safe_files,
                        "steps": steps,
                        "actions": actions,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    }
                )
                record["last_active"] = max(int(record["last_active"]), ended_at)
                self._write_record(record)
                return record, copy.deepcopy(target)

            record, run = await self._guarded_io(
                work, retry_max=self.config.meta_retry_max
            )
            self._index_apply(record)
        await self._persist_index()
        return run

    # ------------------------------------------------------------------
    # Events and blobs
    # ------------------------------------------------------------------
    def enqueue_event(
        self,
        session_id: str,
        run_id: str | None,
        event: dict[str, Any],
        *,
        durable: bool | None = None,
    ) -> bool:
        """Queue one event for the session's writer without blocking.

        Args:
            session_id: Session receiving the event.
            run_id: Run associated with the event.
            event: WebSocket event envelope to persist.
            durable: Force fsync after this event's batch. Defaults to True for
                terminal event types.

        Returns:
            True when queued; False when the store is closed, the session is
            unknown (never run, or deleted), or the bounded queue is full.

        Raises:
            SessionStoreError: If an identifier is invalid.
            RuntimeError: If called outside a running event loop.

        Example:
            >>> store.enqueue_event("s1", "r1", {"type": "run_end", "data": {}})
            True
        """
        self._validate_id(session_id)
        if run_id is not None:
            self._validate_id(run_id)
        if self._closed or session_id not in self._known:
            return False
        writer = self._writers.get(session_id)
        if writer is None:
            writer = self._writers[session_id] = _SessionWriter(self, session_id)
        wants_fsync = (
            event.get("type") in TERMINAL_EVENT_TYPES if durable is None else durable
        )
        try:
            writer.queue.put_nowait((run_id, event, wants_fsync))
        except asyncio.QueueFull:
            self._dropped_events += 1
            return False
        return True

    async def flush(self, session_id: str) -> bool:
        """Wait (bounded) until a session's queued events are written.

        Args:
            session_id: Session to flush.

        Returns:
            True when drained, False if the wait timed out.

        Example:
            >>> await store.flush("s1")
            True
        """
        writer = self._writers.get(session_id)
        if writer is None:
            return True
        timeout = (
            min(self.config.flush_timeout_s, 0.2)
            if self._degraded
            else self.config.flush_timeout_s
        )
        try:
            await asyncio.wait_for(writer.queue.join(), timeout=timeout)
        except TimeoutError:
            logger.warning("History flush timed out with events still queued")
            return False
        return True

    def _write_blob_sync(self, session_id: str, image_b64: str) -> str | None:
        """Decode and store an image; return its blob name or None."""
        try:
            data = base64.b64decode(image_b64)
        except (binascii.Error, ValueError):
            return None
        if not data or len(data) > self.config.max_blob_bytes:
            return None
        extension = _sniff_extension(data)
        if extension is None:
            return None
        name = hashlib.sha256(data).hexdigest()[:32] + extension
        directory = self._session_dir(session_id) / "blobs"
        path = directory / name
        if not path.exists():
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            _atomic_write(path, data)
        return name

    def blob_path(self, session_id: str, name: str) -> Path | None:
        """Return a confined path to an existing blob, or None.

        Args:
            session_id: Owning session.
            name: Blob file name as stored in an event's ``image_ref``.

        Returns:
            The file path when the name is valid and the file exists.

        Raises:
            SessionStoreError: If the session identifier is invalid.

        Example:
            >>> store.blob_path("s1", "../../etc/passwd") is None
            True
        """
        if not _BLOB_NAME_RE.fullmatch(name or ""):
            return None
        path = (self._session_dir(session_id) / "blobs" / name).resolve()
        try:
            path.relative_to(self._session_dir(session_id))
        except ValueError:
            return None
        return path if path.is_file() else None

    def _read_page_sync(
        self,
        session_id: str,
        run_id: str | None,
        offset: int,
        limit: int | None,
        types: frozenset[str] | None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Stream the event log, returning one page and the total match count."""
        path = self._session_dir(session_id) / "events.jsonl"
        events: list[dict[str, Any]] = []
        total = 0
        malformed = 0
        try:
            with path.open("rb") as handle:
                for line in handle:
                    if not line.endswith(b"\n"):
                        continue
                    if not line.strip():
                        continue
                    try:
                        envelope = json.loads(line)
                        event = envelope["event"]
                        if not isinstance(event, dict) or not isinstance(
                            event.get("type"), str
                        ):
                            raise TypeError("invalid event")
                        if run_id is not None and envelope.get("run_id") != run_id:
                            continue
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                        malformed += 1
                        continue
                    if types is not None and event["type"] not in types:
                        continue
                    if total >= offset and (limit is None or len(events) < limit):
                        events.append(event)
                    total += 1
        except FileNotFoundError:
            return [], 0
        except OSError as exc:
            raise StoreIOError("Could not read the session event log") from exc
        if malformed:
            logger.warning("Skipped %d malformed event record(s)", malformed)
        return events, total

    async def read_event_page(
        self,
        session_id: str,
        run_id: str | None = None,
        *,
        offset: int = 0,
        limit: int | None = None,
        types: Iterable[str] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Read one page of events, streaming the log without loading it whole.

        Args:
            session_id: Session whose log is read.
            run_id: Optional run filter.
            offset: Matching events to skip.
            limit: Maximum events to return (None for all remaining).
            types: Optional set of event types to keep.

        Returns:
            ``(events, total_matches)`` in chronological order.

        Raises:
            SessionStoreError: For invalid identifiers or an unreadable log.

        Example:
            >>> events, total = await store.read_event_page("s1", "r1", limit=50)
        """
        self._validate_id(session_id)
        if run_id is not None:
            self._validate_id(run_id)
        await self.flush(session_id)
        type_set = frozenset(types) if types else None
        return await asyncio.to_thread(
            self._read_page_sync, session_id, run_id, max(0, offset), limit, type_set
        )

    async def read_events(
        self, session_id: str, run_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Read every valid event for a session or one run.

        Args:
            session_id: Session whose log should be read.
            run_id: Optional run identifier used to filter events.

        Returns:
            WebSocket event envelopes in their original chronological order.

        Raises:
            SessionStoreError: If identifiers are invalid or the log cannot be read.

        Example:
            >>> await store.read_events("missing")
            []
        """
        events, _ = await self.read_event_page(session_id, run_id)
        return events

    async def recent_run_events(
        self, session_id: str, max_events: int
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Collect whole runs, newest first, until ``max_events`` would be exceeded.

        Args:
            session_id: Session to replay.
            max_events: Soft cap on replayed events (the newest run is always included).

        Returns:
            ``(events, runs_total, runs_shown)`` with events in chronological order.

        Raises:
            SessionStoreError: If the session or its metadata is invalid.

        Example:
            >>> events, total, shown = await store.recent_run_events("s1", 2000)
        """
        record = await asyncio.to_thread(self.get_session, session_id)
        if record is None:
            return [], 0, 0
        indexed = list(enumerate(record["runs"]))
        indexed.sort(key=lambda pair: (pair[1]["started_at"], pair[0]), reverse=True)
        runs = [run for _, run in indexed]
        groups: list[list[dict[str, Any]]] = []
        used = 0
        for run in runs:
            remaining = max_events - used
            if groups and remaining <= 0:
                break
            limit = max_events if not groups else remaining + 1
            events, _ = await self.read_event_page(session_id, run["id"], limit=limit)
            if groups and len(events) > remaining:
                break
            groups.append(events)
            used += len(events)
        ordered = [event for group in reversed(groups) for event in group]
        return ordered, len(runs), len(groups)

    # ------------------------------------------------------------------
    # Deletion, retention, health, shutdown
    # ------------------------------------------------------------------
    async def delete_session(self, session_id: str) -> bool:
        """Delete one path-confined session directory and all persisted history.

        Args:
            session_id: Session to remove.

        Returns:
            True when a directory was removed, False when none existed.

        Raises:
            SessionStoreError: If the identifier is invalid or deletion fails.

        Example:
            >>> await SessionStore(Path("session_store")).delete_session("missing")
            False
        """
        self._validate_id(session_id)
        async with self._session_lock(session_id):
            self._known.discard(session_id)
            writer = self._writers.pop(session_id, None)
            if writer is not None:
                await writer.close()

            def work() -> bool:
                directory = self._session_dir(session_id)
                if not directory.exists():
                    return False
                try:
                    shutil.rmtree(directory)
                except OSError as exc:
                    raise SessionStoreError("Could not delete session history") from exc
                return True

            existed = await self._run_io(work)
            self._index_remove(session_id)
        await self._persist_index()
        return existed

    def _session_sizes(self) -> dict[str, int]:
        """Total bytes per session directory."""
        sizes: dict[str, int] = {}
        for session_id in list(self._sessions_idx):
            total = 0
            for dirpath, _dirs, filenames in os.walk(self._session_dir(session_id)):
                for filename in filenames:
                    with contextlib.suppress(OSError):
                        total += os.path.getsize(os.path.join(dirpath, filename))
            sizes[session_id] = total
        return sizes

    async def prune(
        self,
        *,
        older_than_days: float | None = None,
        max_bytes: int | None = None,
        protected: Iterable[str] = (),
    ) -> dict[str, int]:
        """Delete oldest sessions by age and/or total size.

        Args:
            older_than_days: Delete sessions inactive for longer than this.
            max_bytes: Then delete oldest sessions until total size fits.
            protected: Session IDs that must not be deleted (e.g. live runs).
                Sessions whose index shows a running run are always kept.

        Returns:
            ``{"deleted_sessions": n, "freed_bytes": n}``.

        Raises:
            SessionStoreError: If a deletion fails.

        Example:
            >>> await store.prune(older_than_days=30)
            {'deleted_sessions': 0, 'freed_bytes': 0}
        """
        keep = set(protected)
        sizes = await asyncio.to_thread(self._session_sizes)
        candidates = sorted(
            (
                session
                for session in self._sessions_idx.values()
                if session["id"] not in keep and not self._has_running(session["id"])
            ),
            key=lambda session: int(session["last_active"]),
        )
        victims: list[str] = []
        if older_than_days is not None:
            cutoff = time.time() - older_than_days * 86_400
            victims = [
                item["id"] for item in candidates if item["last_active"] < cutoff
            ]
        if max_bytes is not None:
            total = sum(sizes.values()) - sum(sizes.get(item, 0) for item in victims)
            for item in candidates:
                if total <= max_bytes:
                    break
                if item["id"] not in victims:
                    victims.append(item["id"])
                    total -= sizes.get(item["id"], 0)
        deleted = 0
        freed = 0
        for session_id in victims:
            if self._has_running(session_id):
                continue
            if await self.delete_session(session_id):
                deleted += 1
                freed += sizes.get(session_id, 0)
        return {"deleted_sessions": deleted, "freed_bytes": freed}

    @property
    def degraded(self) -> bool:
        """Whether recent writes are failing."""
        return self._degraded

    def health(self) -> dict[str, Any]:
        """Report store health for monitoring.

        Returns:
            Component status (healthy/degraded/unhealthy) with writability, free
            space, last error class (never details), queue depth, and drop count.

        Example:
            >>> SessionStore(Path("session_store")).health()["status"]
            'healthy'
        """
        try:
            free_bytes: int | None = shutil.disk_usage(self.root).free
        except OSError:
            free_bytes = None
        writable = os.access(self.root, os.W_OK)
        if (
            not writable
            or free_bytes is None
            or free_bytes < self.config.min_free_bytes
        ):
            status = "unhealthy"
        elif self._degraded:
            status = "degraded"
        else:
            status = "healthy"
        return {
            "status": status,
            "writable": writable,
            "free_bytes": free_bytes,
            "min_free_bytes": self.config.min_free_bytes,
            "last_error_class": self._last_error_class,
            "last_error_at": self._last_error_at,
            "queue_depth": sum(
                writer.queue.qsize() for writer in self._writers.values()
            ),
            "dropped_events": self._dropped_events,
            "breaker_state": self._breaker.state.value,
            "sessions": len(self._sessions_idx),
            "runs": sum(len(runs) for runs in self._runs_idx.values()),
        }

    async def close(self) -> None:
        """Flush queued events (bounded wait) and release every writer.

        Example:
            >>> await store.close()
        """
        for session_id in list(self._writers):
            await self.flush(session_id)
        self._closed = True
        for writer in list(self._writers.values()):
            await writer.close()
        self._writers.clear()


__all__ = [
    "INDEX_VERSION",
    "SCHEMA_VERSION",
    "SessionStore",
    "SessionStoreError",
    "StoreIOError",
    "StoreVersionError",
]
