"""Typed, environment-driven settings for the portal's run-history subsystem.

Every limit, timeout, and threshold used by ``workspace.session_store`` lives
here so nothing is hardcoded in application code. Invalid environment values
fall back to documented defaults and are reported by variable name only.

Key classes / functions:
    HistoryConfig: frozen settings object with a ``from_env`` loader.

Usage:
    config = HistoryConfig.from_env()
    store = SessionStore(Path("session_store"), config)

Dependencies: Python 3.11+ standard library only.
Tier 2: a bad value degrades to a safe default and is logged; it cannot corrupt data.
"""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_MB = 1024 * 1024


def _read_number(
    environ: Mapping[str, str],
    name: str,
    default: float | None,
    *,
    minimum: float = 0.0,
) -> float | None:
    """Read a numeric environment variable, falling back on invalid input."""
    raw = environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Ignoring invalid numeric setting %s", name)
        return default
    if value < minimum:
        logger.warning("Ignoring out-of-range setting %s", name)
        return default
    return value


def _read_patterns(environ: Mapping[str, str], name: str) -> tuple[str, ...]:
    """Read a JSON list of extra redaction regexes; ignore malformed input."""
    raw = environ.get(name)
    if not raw:
        return ()
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Ignoring malformed JSON in %s", name)
        return ()
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) for item in parsed
    ):
        logger.warning("Ignoring %s: expected a JSON list of strings", name)
        return ()
    return tuple(parsed)


@dataclass(frozen=True)
class HistoryConfig:
    """Settings for storage limits, resilience, redaction, and retention.

    Attributes:
        max_age_days: Delete sessions inactive longer than this (None = keep).
        max_bytes: Keep total history under this size (None = unlimited).
        prune_interval_minutes: Period of the background retention task.
        min_free_bytes: Free-space floor below which health is unhealthy.
        max_event_chars: Per-string cap for persisted event payloads.
        max_record_chars: Cap for persisted prompts and final responses.
        max_blob_bytes: Largest image written to the blob directory.
        queue_max: Bounded spill queue length per session writer.
        batch_max: Maximum events flushed per write.
        retry_max: Attempts per event batch before it is dropped (counted).
        meta_retry_max: Attempts for foreground run-metadata writes.
        backoff_base_s: Exponential backoff base delay in seconds.
        backoff_max_s: Exponential backoff ceiling in seconds.
        breaker_threshold: Consecutive failures that open the circuit.
        breaker_reset_s: Seconds before a single half-open probe is allowed.
        flush_timeout_s: Longest a reader waits for queued events to land.
        writer_idle_s: Idle seconds before a session writer releases its file handle.
        extra_redaction_patterns: Additional regexes redacted before writing.
    """

    max_age_days: float | None = None
    max_bytes: int | None = None
    prune_interval_minutes: float = 60.0
    min_free_bytes: int = 100 * _MB
    max_event_chars: int = 20_000
    max_record_chars: int = 200_000
    max_blob_bytes: int = 10 * _MB
    queue_max: int = 5_000
    batch_max: int = 200
    retry_max: int = 5
    meta_retry_max: int = 2
    backoff_base_s: float = 0.2
    backoff_max_s: float = 5.0
    breaker_threshold: int = 3
    breaker_reset_s: float = 2.0
    flush_timeout_s: float = 3.0
    writer_idle_s: float = 30.0
    extra_redaction_patterns: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "HistoryConfig":
        """Build settings from ``WEBAPP_HISTORY_*`` environment variables.

        Args:
            environ: Mapping to read; defaults to ``os.environ``.

        Returns:
            A validated, immutable configuration object.

        Example:
            >>> HistoryConfig.from_env({"WEBAPP_HISTORY_RETRY_MAX": "7"}).retry_max
            7
        """
        if environ is None:
            import os

            environ = os.environ
        defaults = cls()
        max_age = _read_number(
            environ, "WEBAPP_HISTORY_MAX_AGE_DAYS", None, minimum=0.0
        )
        max_bytes = _read_number(environ, "WEBAPP_HISTORY_MAX_BYTES", None, minimum=1.0)

        def number(name: str, default: float, minimum: float = 0.0) -> float:
            value = _read_number(environ, name, default, minimum=minimum)
            return default if value is None else value

        return cls(
            max_age_days=max_age if max_age else None,
            max_bytes=int(max_bytes) if max_bytes else None,
            prune_interval_minutes=number(
                "WEBAPP_HISTORY_PRUNE_INTERVAL_MINUTES",
                defaults.prune_interval_minutes,
                0.1,
            ),
            min_free_bytes=int(
                number("WEBAPP_HISTORY_MIN_FREE_MB", defaults.min_free_bytes / _MB)
                * _MB
            ),
            max_event_chars=int(
                number("WEBAPP_HISTORY_MAX_EVENT_CHARS", defaults.max_event_chars, 64)
            ),
            max_record_chars=int(
                number("WEBAPP_HISTORY_MAX_RECORD_CHARS", defaults.max_record_chars, 64)
            ),
            max_blob_bytes=int(
                number(
                    "WEBAPP_HISTORY_MAX_BLOB_MB", defaults.max_blob_bytes / _MB, 0.01
                )
                * _MB
            ),
            queue_max=int(number("WEBAPP_HISTORY_QUEUE_MAX", defaults.queue_max, 1)),
            batch_max=int(number("WEBAPP_HISTORY_BATCH_MAX", defaults.batch_max, 1)),
            retry_max=int(number("WEBAPP_HISTORY_RETRY_MAX", defaults.retry_max, 0)),
            meta_retry_max=int(
                number("WEBAPP_HISTORY_META_RETRY_MAX", defaults.meta_retry_max, 0)
            ),
            backoff_base_s=number(
                "WEBAPP_HISTORY_BACKOFF_BASE_MS", defaults.backoff_base_s * 1000, 1
            )
            / 1000,
            backoff_max_s=number(
                "WEBAPP_HISTORY_BACKOFF_MAX_MS", defaults.backoff_max_s * 1000, 1
            )
            / 1000,
            breaker_threshold=int(
                number(
                    "WEBAPP_HISTORY_BREAKER_THRESHOLD", defaults.breaker_threshold, 1
                )
            ),
            breaker_reset_s=number(
                "WEBAPP_HISTORY_BREAKER_RESET_MS", defaults.breaker_reset_s * 1000, 1
            )
            / 1000,
            flush_timeout_s=number(
                "WEBAPP_HISTORY_FLUSH_TIMEOUT_MS", defaults.flush_timeout_s * 1000, 1
            )
            / 1000,
            writer_idle_s=number(
                "WEBAPP_HISTORY_WRITER_IDLE_SECONDS", defaults.writer_idle_s, 0.05
            ),
            extra_redaction_patterns=_read_patterns(
                environ, "WEBAPP_HISTORY_REDACTION_PATTERNS"
            ),
        )


__all__ = ["HistoryConfig"]
