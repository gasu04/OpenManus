"""Unit tests for the run-history helper modules.

Covers settings parsing (``HistoryConfig``), secret redaction and size capping
(``Redactor``), and the circuit breaker / backoff used by the store. These are
pure-Python tests with no server, network, or disk dependency.

Run: .venv/bin/python -m pytest workspace/tests/test_history_helpers.py -v
Dependencies: pytest, pytest-asyncio.
Tier 1 (redaction): a missed pattern leaks secrets to disk silently.
"""

import asyncio

import pytest

from workspace.history_config import HistoryConfig
from workspace.history_redaction import REDACTION_MARKER, Redactor
from workspace.history_resilience import (
    BreakerState,
    CircuitBreaker,
    CircuitOpenError,
    backoff_delay,
)

# Built from fragments so no complete credential-shaped literal sits in the source
# (secret scanners flag those even when they are obviously fake).
FAKE_SECRETS = [
    "sk" + "-" + "FAKE" * 5 + "1234",
    "GOC" + "SPX-" + "fake" * 3 + "1234",
    "AI" + "za" + "Sy" + "FAKE" * 7 + "12345",
    "gh" + "p_" + "FAKE" * 8 + "1234",
    "xo" + "xb-" + "1234567890-" + "fake" * 2,
    "AK" + "IA" + "FAKE" * 3 + "1234",
    "ey"
    + "J"
    + "hbGciOiJIUzI1NiJ9"
    + "."
    + "eyJzdWIiOiIxMjM0NTY3ODkwIn0"
    + "."
    + "FAKEsignature12345",
]


# ------------------------------------------------------------------ config --
def test_history_config_from_env_defaults_when_unset():
    config = HistoryConfig.from_env({})
    assert config.max_age_days is None and config.max_bytes is None
    assert config.retry_max == 5 and config.queue_max == 5000


def test_history_config_from_env_parses_values():
    config = HistoryConfig.from_env(
        {
            "WEBAPP_HISTORY_MAX_AGE_DAYS": "30",
            "WEBAPP_HISTORY_MAX_BYTES": "1048576",
            "WEBAPP_HISTORY_RETRY_MAX": "9",
            "WEBAPP_HISTORY_BACKOFF_BASE_MS": "50",
            "WEBAPP_HISTORY_REDACTION_PATTERNS": '["corp-[a-z0-9]{6}"]',
        }
    )
    assert config.max_age_days == 30.0
    assert config.max_bytes == 1048576
    assert config.retry_max == 9
    assert config.backoff_base_s == pytest.approx(0.05)
    assert config.extra_redaction_patterns == ("corp-[a-z0-9]{6}",)


def test_history_config_from_env_invalid_values_fall_back():
    config = HistoryConfig.from_env(
        {
            "WEBAPP_HISTORY_RETRY_MAX": "not-a-number",
            "WEBAPP_HISTORY_QUEUE_MAX": "-5",
            "WEBAPP_HISTORY_REDACTION_PATTERNS": "{bad json",
        }
    )
    assert config.retry_max == HistoryConfig().retry_max
    assert config.queue_max == HistoryConfig().queue_max
    assert config.extra_redaction_patterns == ()


# --------------------------------------------------------------- redaction --
@pytest.mark.parametrize("secret", FAKE_SECRETS)
def test_redactor_known_token_shapes_are_replaced(secret):
    text = f"the credential is {secret} and nothing else"
    cleaned = Redactor().redact_text(text)
    assert secret not in cleaned
    assert REDACTION_MARKER in cleaned


def test_redactor_authorization_bearer_and_key_value_forms():
    redactor = Redactor()
    assert "abcdefgh12345" not in redactor.redact_text(
        "Authorization: Bearer abcdefgh12345"
    )
    assert "dXNlcjpwYXNz" not in redactor.redact_text(
        "Authorization: Basic dXNlcjpwYXNz"
    )
    assert "hunter2hunter2" not in redactor.redact_text("api_key=hunter2hunter2")
    assert "s3cretValue" not in redactor.redact_text('{"password": "s3cretValue"}')
    assert "tok123456" not in redactor.redact_text("access_token: tok123456")


def test_redactor_private_key_block_is_replaced():
    block = (
        "-----BEGIN RSA "
        + "PRIVATE KEY-----\nMIIFAKEFAKE\n-----END RSA "
        + "PRIVATE KEY-----"
    )
    assert "MIIFAKEFAKE" not in Redactor().redact_text(f"key:\n{block}\nend")


def test_redactor_normal_text_and_token_counts_are_untouched():
    redactor = Redactor()
    text = "Used 120 tokens and a basic knowledge of the secret garden."
    assert redactor.redact_text(text) == text
    event = {"type": "usage", "data": {"delta_in": 120, "total_out": 30}}
    cleaned = redactor.sanitize_event(
        event, max_chars=1000, offload_image=lambda b: None
    )
    assert cleaned["data"] == {"delta_in": 120, "total_out": 30}


def test_redactor_custom_patterns_apply_and_invalid_ones_are_skipped():
    redactor = Redactor(extra_patterns=[r"corp-[a-z0-9]{6}", "("])
    assert redactor.redact_text("id corp-abc123 ok") == f"id {REDACTION_MARKER} ok"


def test_sanitize_event_redacts_sensitive_keys_nested_values_and_does_not_mutate_input():
    event = {
        "type": "tool_start",
        "data": {
            "name": "http",
            "arguments": {
                "headers": {"api_key": "plain-secret-value"},
                "url": "https://x.test",
            },
            "result": f"saw {FAKE_SECRETS[0]}",
        },
    }
    cleaned = Redactor().sanitize_event(
        event, max_chars=1000, offload_image=lambda b: None
    )
    flat = str(cleaned)
    assert "plain-secret-value" not in flat and FAKE_SECRETS[0] not in flat
    assert cleaned["data"]["arguments"]["url"] == "https://x.test"
    # the live stream's copy is untouched
    assert event["data"]["arguments"]["headers"]["api_key"] == "plain-secret-value"
    assert FAKE_SECRETS[0] in event["data"]["result"]


def test_sanitize_event_truncates_long_strings_and_marks_them():
    event = {"type": "terminal", "data": {"output": "x" * 500, "code": "short"}}
    cleaned = Redactor().sanitize_event(
        event, max_chars=100, offload_image=lambda b: None
    )
    assert cleaned["data"]["output"].startswith("x" * 100)
    assert "truncated 400 chars" in cleaned["data"]["output"]
    assert cleaned["data"]["_truncated"] == ["output"]
    assert cleaned["data"]["code"] == "short"


def test_sanitize_event_offloads_screenshot_to_reference():
    seen: list[str] = []

    def offload(b64: str) -> str:
        seen.append(b64)
        return "abc123.png"

    event = {"type": "browser_screenshot", "data": {"image": "QUJD", "ts": 1}}
    cleaned = Redactor().sanitize_event(event, max_chars=100, offload_image=offload)
    assert seen == ["QUJD"]
    assert cleaned["data"] == {"image_ref": "abc123.png", "ts": 1}
    omitted = Redactor().sanitize_event(
        event, max_chars=100, offload_image=lambda b: None
    )
    assert (
        omitted["data"]["image_omitted"] is True
        and omitted["data"]["image_ref"] is None
    )


# ------------------------------------------------------------- resilience --
def test_backoff_delay_grows_exponentially_is_capped_and_jittered():
    no_jitter = lambda low, high: 0.0
    assert backoff_delay(0, 0.1, 5.0, jitter=no_jitter) == pytest.approx(0.1)
    assert backoff_delay(3, 0.1, 5.0, jitter=no_jitter) == pytest.approx(0.8)
    assert backoff_delay(20, 0.1, 5.0, jitter=no_jitter) == pytest.approx(5.0)
    for _ in range(50):
        assert 0.4 <= backoff_delay(2, 0.1, 5.0) <= 0.5


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_breaker_opens_after_threshold_then_rejects_without_calling():
    clock = _Clock()
    breaker = CircuitBreaker(2, 10.0, clock=clock)
    calls = 0

    async def failing():
        nonlocal calls
        calls += 1
        raise OSError("disk")

    for _ in range(2):
        with pytest.raises(OSError):
            await breaker.call(failing)
    assert breaker.state is BreakerState.OPEN
    with pytest.raises(CircuitOpenError):
        await breaker.call(failing)
    assert calls == 2
    clock.now = 4.0
    assert breaker.remaining_open() == pytest.approx(6.0)


@pytest.mark.asyncio
async def test_breaker_half_open_admits_exactly_one_probe_then_closes_on_success():
    clock = _Clock()
    breaker = CircuitBreaker(1, 5.0, clock=clock)

    async def failing():
        raise OSError("disk")

    with pytest.raises(OSError):
        await breaker.call(failing)
    assert breaker.state is BreakerState.OPEN

    clock.now = 6.0  # reset window elapsed -> half-open
    release = asyncio.Event()
    executed = 0

    async def slow_probe():
        nonlocal executed
        executed += 1
        await release.wait()
        return "ok"

    results = await _race_calls(breaker, slow_probe, release, callers=6)
    assert executed == 1, "half-open must let exactly one probe through"
    assert results.count("ok") == 1
    assert sum(isinstance(item, CircuitOpenError) for item in results) == 5
    assert breaker.state is BreakerState.CLOSED


async def _race_calls(breaker, operation, release, callers):
    tasks = [asyncio.create_task(breaker.call(operation)) for _ in range(callers)]
    await asyncio.sleep(0.05)  # let every caller reach the breaker
    release.set()
    return await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_breaker_failed_probe_reopens_and_logical_errors_do_not_count():
    clock = _Clock()
    breaker = CircuitBreaker(1, 5.0, clock=clock)

    async def failing():
        raise OSError("disk")

    with pytest.raises(OSError):
        await breaker.call(failing)
    clock.now = 6.0
    with pytest.raises(OSError):
        await breaker.call(failing)  # the probe fails
    assert breaker.state is BreakerState.OPEN

    healthy = CircuitBreaker(1, 5.0, clock=clock)

    async def logical():
        raise ValueError("not a disk problem")

    with pytest.raises(ValueError):
        await healthy.call(logical, is_failure=lambda exc: isinstance(exc, OSError))
    assert healthy.state is BreakerState.CLOSED


@pytest.mark.asyncio
async def test_breaker_cancelled_probe_releases_the_latch():
    clock = _Clock()
    breaker = CircuitBreaker(1, 5.0, clock=clock)

    async def failing():
        raise OSError("disk")

    with pytest.raises(OSError):
        await breaker.call(failing)
    clock.now = 6.0
    started = asyncio.Event()

    async def hang():
        started.set()
        await asyncio.sleep(30)

    task = asyncio.create_task(breaker.call(hang))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async def ok():
        return "ok"

    assert await breaker.call(ok) == "ok"
