"""Secret redaction, size capping, and image offloading for persisted history.

Everything written to ``session_store/`` passes through ``Redactor`` first, so
API keys, bearer tokens, passwords, and similar secrets that appear in prompts
or tool output never reach disk. Live WebSocket traffic is untouched; only the
persisted copy is sanitized. Oversized strings are truncated with an explicit
marker and base64 screenshots are replaced by a reference to a blob file.

Key classes / functions:
    Redactor: compiled pattern set with ``redact_text`` and ``sanitize_event``.
    REDACTION_MARKER: the replacement string.

Usage:
    redactor = Redactor(extra_patterns=[r"corp-[a-z0-9]{12}"])
    clean = redactor.sanitize_event(event, max_chars=20000, offload_image=save)

Dependencies: Python 3.11+ standard library only.
Tier 1: a missed redaction leaks secrets to disk silently and cannot be undone
once the file is copied or backed up, so patterns are covered by dedicated tests.
"""

import logging
import re
from collections.abc import Callable, Iterable
from typing import Any

logger = logging.getLogger(__name__)

REDACTION_MARKER = "[REDACTED]"

_TOKEN_PATTERNS: tuple[str, ...] = (
    r"sk-[A-Za-z0-9_\-]{16,}",
    r"GOCSPX-[A-Za-z0-9_\-]{10,}",
    r"AIza[0-9A-Za-z_\-]{30,}",
    r"gh[pousr]_[A-Za-z0-9]{30,}",
    r"xox[abprs]-[A-Za-z0-9\-]{10,}",
    r"AKIA[0-9A-Z]{16}",
    r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
)
_BEARER_RE = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/\-]{8,}=*")
_AUTH_HEADER_RE = re.compile(r"""(?ix)(authorization["']?\s*[:=]\s*["']?)
    (?:(?:Bearer|Basic|Digest|Token)\s+)?[^\s"',;]{4,}""")
_KEY_VALUE_RE = re.compile(
    r"""(?ix)\b((?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token
    |client[_-]?secret|token|password|passwd|secret)["']?\s*[:=]\s*)
    ("[^"\n]{1,500}"|'[^'\n]{1,500}'|[^\s,;&}"']{1,500})"""
)
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|(^|[_-])token$|password|passwd|secret|^authorization$|^cookie$)"
)


class Redactor:
    """Redact secrets, cap string sizes, and offload images in event payloads.

    Args:
        extra_patterns: Additional regular expressions whose matches are
            replaced by ``REDACTION_MARKER``. Invalid patterns are skipped.

    Example:
        >>> Redactor().redact_text("api_key=hunter2hunter2")
        'api_key=[REDACTED]'

    Note:
        Instances are immutable after construction and safe to share across
        threads.
    """

    def __init__(self, extra_patterns: Iterable[str] = ()) -> None:
        patterns = list(_TOKEN_PATTERNS)
        for pattern in extra_patterns:
            try:
                re.compile(pattern)
            except re.error:
                logger.warning("Skipping invalid custom redaction pattern")
                continue
            patterns.append(pattern)
        self._token_re = re.compile("|".join(f"(?:{pattern})" for pattern in patterns))

    def redact_text(self, text: str) -> str:
        """Replace known secret shapes in a string with ``REDACTION_MARKER``.

        Args:
            text: Arbitrary text that may contain secrets.

        Returns:
            The text with matches replaced.

        Example:
            >>> Redactor().redact_text("Authorization: Bearer abcdefgh12345")
            'Authorization: [REDACTED]'
        """
        text = _AUTH_HEADER_RE.sub(rf"\1{REDACTION_MARKER}", text)
        text = _BEARER_RE.sub(rf"\1 {REDACTION_MARKER}", text)
        text = _KEY_VALUE_RE.sub(rf"\1{REDACTION_MARKER}", text)
        return self._token_re.sub(REDACTION_MARKER, text)

    def redact_and_cap(self, text: str, max_chars: int) -> str:
        """Redact a string, then truncate it to ``max_chars`` with a marker.

        Args:
            text: Source text.
            max_chars: Maximum characters to keep.

        Returns:
            Redacted text, truncated when longer than ``max_chars``.

        Example:
            >>> Redactor().redact_and_cap("abcdef", 3)
            'abc…[truncated 3 chars]'
        """
        cleaned = self.redact_text(text)
        if len(cleaned) > max_chars:
            return f"{cleaned[:max_chars]}…[truncated {len(cleaned) - max_chars} chars]"
        return cleaned

    def sanitize_event(
        self,
        event: dict[str, Any],
        *,
        max_chars: int,
        offload_image: Callable[[str], str | None],
    ) -> dict[str, Any]:
        """Return a persistence-safe copy of a WebSocket event envelope.

        Args:
            event: Event with ``type`` and ``data``; never mutated.
            max_chars: Per-string size cap.
            offload_image: Writes a base64 image to a blob file and returns its
                reference name, or None when it cannot be stored.

        Returns:
            A new envelope where secrets are redacted, long strings truncated,
            and ``browser_screenshot`` images replaced by ``image_ref``.

        Raises:
            OSError: Propagated from ``offload_image`` so callers can retry.

        Example:
            >>> r = Redactor()
            >>> r.sanitize_event({"type": "x", "data": {"token": "abc"}},
            ...                  max_chars=100, offload_image=lambda b: None)
            {'type': 'x', 'data': {'token': '[REDACTED]'}}
        """
        raw = event.get("data")
        data: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
        if event.get("type") == "browser_screenshot":
            image = data.pop("image", None)
            if isinstance(image, str):
                reference = offload_image(image)
                data["image_ref"] = reference
                if reference is None:
                    data["image_omitted"] = True
        truncated: list[str] = []
        cleaned = self._clean(data, "", max_chars, truncated)
        if truncated:
            cleaned["_truncated"] = sorted(set(truncated))[:20]
        return {"type": event.get("type"), "data": cleaned}

    def _clean(
        self, value: Any, path: str, max_chars: int, truncated: list[str]
    ) -> Any:
        """Recursively redact and cap a JSON-like value."""
        if isinstance(value, str):
            cleaned = self.redact_text(value)
            if len(cleaned) > max_chars:
                truncated.append(path or "data")
                return f"{cleaned[:max_chars]}…[truncated {len(cleaned) - max_chars} chars]"
            return cleaned
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key)
                if _SENSITIVE_KEY_RE.search(key_text) and isinstance(
                    item, str | int | float
                ):
                    result[key_text] = REDACTION_MARKER
                else:
                    child_path = f"{path}.{key_text}" if path else key_text
                    result[key_text] = self._clean(
                        item, child_path, max_chars, truncated
                    )
            return result
        if isinstance(value, list | tuple):
            return [self._clean(item, path, max_chars, truncated) for item in value]
        return value


__all__ = ["REDACTION_MARKER", "Redactor"]
