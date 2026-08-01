# CLAUDE.md — OpenManus Coding Standards

**Version:** 1.0.0 **Scope:** Coding rules for all Claude Code sessions in the OpenManus repository **Re-read from disk at every session start**

---

## Core Philosophy

**"Build systems that survive their creators."**

Four qualities matter above all:

- **Resilience** — graceful degradation over catastrophic failure
- **Clarity** — code that explains itself
- **Portability** — run anywhere, depend on nothing specific
- **Maintainability** — future readers (including you in six months) are users too

---

## How to Use This Document

This file has three layers, and they have an explicit order of precedence:

1. **Mandatory Baseline (Part 2)** — non-negotiable rules, each marked \[MANDATORY\].
2. **Operating Principles (Part 1)** — govern *how* Claude approaches work within that baseline. When the baseline doesn't explicitly require something, these principles apply.
3. **Reference Material (Parts 3–4)** — concrete values (naming, file layouts) and session-management tips.

**Precedence rule:** When Part 1 and Part 2 appear to conflict, Part 2 wins. Example: Simplicity First's "no error handling for impossible scenarios" does not override a Mandatory Baseline requirement to wrap every external service call with a circuit breaker. Likewise, Surgical Changes' "don't touch adjacent code" does not excuse leaving a module without a docstring when §2.5 requires one.

**Honest tradeoff:** §2.5 (documentation) and §2.6 (testing) are strict. They will sometimes feel heavier than §1.2 Simplicity First would call for. Simplicity First governs the *implementation* — the code you write to solve the problem. Documentation and test thoroughness are part of the baseline and override simplicity when they conflict.

---

## Table of Contents

- [Part 1 — The Four Operating Principles](#part-1--the-four-operating-principles)
  - [1.1 Think Before Coding](#11-think-before-coding)
  - [1.2 Simplicity First](#12-simplicity-first)
  - [1.3 Surgical Changes](#13-surgical-changes)
  - [1.4 Goal-Driven Execution](#14-goal-driven-execution)
- [Part 2 — Mandatory Baseline](#part-2--mandatory-baseline)
  - [2.1 Configuration](#21-configuration-mandatory)
  - [2.2 Async-First](#22-async-first-mandatory)
  - [2.3 Resilience](#23-resilience-mandatory)
  - [2.4 Sensitive-Data Logging](#24-sensitive-data-logging-mandatory)
  - [2.5 Documentation](#25-documentation-mandatory)
  - [2.6 Testing](#26-testing-mandatory)
  - [2.7 Security](#27-security-mandatory)
  - [2.8 Code Organization](#28-code-organization-mandatory)
- [Part 3 — Reference](#part-3--reference)
- [Part 4 — Session Management](#part-4--session-management)
- [Appendix A — Quick Reference Checklists](#appendix-a--quick-reference-checklists)

---

## Part 1 — The Four Operating Principles

These govern Claude's process for any task not otherwise specified by Part 2.

### 1.1 Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before writing implementation:

- State assumptions explicitly. If uncertain, ask.
- If multiple reasonable interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop and name what's unclear. Ask.

### 1.2 Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked — unless required by Part 2.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If 200 lines could be 50, rewrite it.

Ask: *"Would a senior engineer say this is overcomplicated?"* If yes, simplify.

### 1.3 Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:

- Don't "improve" adjacent code, comments, or formatting — unless Part 2 explicitly requires it (e.g., §2.5 module docstrings must be added if missing).
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.

When your changes create orphans:

- Remove imports, variables, or functions that *your* changes made unused.
- Don't remove pre-existing dead code unless asked.

**The test:** every changed line should trace directly to the user's request, or to a Part 2 mandate.

### 1.4 Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform imperative tasks into verifiable goals:

| Instead of... | Transform to... |
|:---:|:---:|
| "Add validation" | "Write tests for invalid inputs, then make them pass" |
| "Fix the bug" | "Write a test that reproduces it, then make it pass" |
| "Refactor X" | "Ensure tests pass before and after" |

For multi-step tasks, state a brief plan before acting:

1. \[Step\] → verify: \[check\]
2. \[Step\] → verify: \[check\]
3. \[Step\] → verify: \[check\]

Strong success criteria let Claude loop independently. Weak criteria ("make it work") require constant clarification.

---

## Part 2 — Mandatory Baseline

Every rule in this part is \[MANDATORY\]. They hold regardless of task size or apparent simplicity.

### 2.1 Configuration \[MANDATORY\]

**No hardcoded values in application code. Ever.**

- All network addresses (IPs, hostnames) come from environment variables or a typed settings object.
- All ports come from environment variables.
- All credentials come from environment variables or a secrets manager.
- All timeouts, retry counts, and thresholds come from configuration.

Required artifacts in every subproject:

- `.env.example` — template listing every variable, with safe defaults and comments
- `.env` — actual values (gitignored)
- `settings.py` (or equivalent) — typed configuration loader (Pydantic BaseSettings or `@dataclass` with `field(default_factory=...)`)

**Self-test:** `grep -rE "\b[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\b" <source_dir>/*.py` should return nothing from application code — hardcoded IPs belong in configuration, not source.

### 2.2 Async-First \[MANDATORY\]

When calling external services (LLM APIs, MCP, databases, HTTP APIs) from async code:

- Use **async clients**: `AsyncOpenAI` (not `OpenAI`), `httpx.AsyncClient` (not `httpx.Client`), `asyncpg` (not `psycopg2`). Never the sync variant inside an async function — it blocks the event loop and freezes the entire service.
- On connection error: **explicitly `aclose()`** the client and recreate it. Never reuse a client that may have corrupted connection state. A boolean flag (`_connected = False`) is not sufficient.
- Use `@asynccontextmanager` for any resource with a lifecycle (connections, sessions, transactions).

**Self-test for non-blocking:** if an external call takes 2 seconds, an unrelated background coroutine must continue making progress during that window. If a counter incremented every 100ms doesn't advance, you're blocking.

### 2.3 Resilience \[MANDATORY\]

Every external service call must be wrapped with:

1. **Circuit Breaker with Probe Latch** — in HALF_OPEN state, exactly one probe request is allowed through. All others are rejected until the probe resolves. Naïve circuit breakers that let all HALF_OPEN traffic through cause thundering-herd failures.
2. **Retry with exponential backoff + jitter** — `delay = min(base * 2^attempt, max) + random_jitter`. The jitter is not optional; it prevents synchronized retry storms.
3. **Proper time source** — `time.monotonic()` for elapsed-time measurements, never `time.time()` (which can jump backward on NTP sync).
4. **Thread safety** — circuit breakers shared across coroutines need `asyncio.Lock`.
5. **Connection invalidation on error** — see §2.2.
6. **Health checks** — every service exposes a health endpoint reporting per-component status (healthy / degraded / unhealthy) with a timestamp.

Implement circuit breaker, retry/backoff, and connection-lifecycle logic **once per project**, in a shared module, and import it everywhere an external call is made. Do not reimplement these from memory in each new module.

### 2.4 Sensitive-Data Logging \[MANDATORY\]

OpenManus agents can handle arbitrary user prompts, API keys, and file contents. This data must never appear in logs. Logs may be persisted indefinitely, shipped to monitoring systems, and viewed by debugging tools.

**Never log:**

- API keys, tokens, or credentials
- Full user prompts or full model responses containing sensitive content
- File contents read/written by tools, when they may contain secrets

**Do log:**

- Message/step counts
- Tool names invoked
- Timing metrics (milliseconds)
- Error types and classes — not error details that may embed content
- Token counts

Use a formatter that actively redacts known-sensitive field names (`api_key`, `token`, `password`, `secret`).

**Self-test before merging:** run a sample task through the pipeline with logging at DEBUG, then grep the log file for credentials or secrets. Any hit is a bug.

### 2.5 Documentation \[MANDATORY\]

Every module must have a header docstring covering:

- What this module does
- How it fits into the larger system
- Key classes / functions it provides
- A usage example
- Dependencies (with version constraints where they matter)
- **Tier classification:** Tier 1 (silent-failure-critical, 100% coverage) or Tier 2 (standard, 80% coverage). For Tier 1, state *why* in one sentence. See §2.6.

Every public function, method, and class must have a full Google-style docstring:

- **Args:** each argument with type and meaning
- **Returns:** what comes back
- **Raises:** each exception that can propagate
- **Example:** at least one concrete invocation, for public API functions
- **Note:** any non-obvious behavior (caching, side effects, thread safety)

Private helpers (leading underscore) may use a one-line docstring if their behavior is self-evident from the signature and their caller is in the same module.

### 2.6 Testing \[MANDATORY\]

**Tier-based coverage — defined by failure mode, not module importance.**

- **Tier 1 (100% coverage):** code whose *silent* failure causes harm that can't be rolled back. Tests must specifically probe the quiet failure modes, not just the loud ones.
- **Tier 2 (80% minimum):** everything else. Loud failures (exceptions, None returns, failing health checks) are their own warning system; tests cover happy path plus obvious error branches.

**The operational test:** *"If this fails silently at 2am, can I tell? And can I undo the damage?"* If the answer is no-and-no, it's Tier 1. If failure is noisy or recoverable, it's Tier 2. Don't let Tier 1 sprawl — scope creep here is how test maintenance becomes a drag on shipping.

**Other requirements:**

- **Error handling:** explicitly tested — if a code path raises, there must be a test that exercises that raise
- **Async tests:** use pytest-asyncio with properly scoped fixtures
- **Naming:** `test_<function>_<scenario>_<expected>()` — descriptive names serve as documentation

Test structure:

```
tests/
├── unit/           # fast, isolated, no network
├── integration/    # cross-module, may touch local services
├── e2e/            # full pipeline tests
├── fixtures/       # sample data, mock responses
└── conftest.py
```

### 2.7 Security \[MANDATORY\]

- **Secrets** via environment variables or a secrets manager. Never in source control.
- **Input validation** via Pydantic models or equivalent. Never trust caller-supplied strings.
- **Dependency scanning** in CI (`pip-audit` or `safety`).
- **Pinned versions** in production (`requirements.lock` from `pip freeze`).
- **Non-root users** in containers.
- **No sensitive data** in URL parameters, query strings, or filenames.

### 2.8 Code Organization \[MANDATORY\]

- **Single responsibility:** one class / function, one reason to change.
- **Dependency injection:** construct dependencies outside the class that uses them. Never `self.db = PostgresDatabase()` inside `__init__`.
- **Type hints everywhere:** all function signatures, all class attributes, all return types. mypy must pass in CI.
- **Custom exception hierarchy:** rooted at a project-specific base. Never raise bare `Exception`. Never catch bare `Exception` except at top-level request handlers.
- **Explicit exports:** every `__init__.py` defines `__all__` and exports the public API.

---

## Part 3 — Reference

### 3.1 Project Structure

```
project_root/
├── config/              # .env, .env.example, settings.py
├── app/ or <module>/    # source code
├── tests/               # unit/, integration/, e2e/, fixtures/
├── scripts/             # setup.sh, health_check.py, run_service.sh
├── docs/                # architecture.md, api.md, deployment.md
├── logs/                # gitignored
├── workspace/            # generated/runtime artifacts, gitignored
├── requirements.txt
├── pyproject.toml
├── README.md
└── CLAUDE.md            # this file
```

### 3.2 Naming Conventions

| Type | Convention | Example |
|:---:|:---:|:---:|
| Files | snake_case | `str_replace_editor.py` |
| Classes | PascalCase | `Manus` |
| Functions | snake_case | `get_recent_messages()` |
| Constants | SCREAMING_SNAKE | `MAX_STEPS = 20` |
| Private members | Leading underscore | `_internal_state` |
| Config vars (.env) | SCREAMING_SNAKE | `LLM_API_KEY` |

### 3.3 Cross-Platform

- Use `pathlib.Path` for all filesystem paths. Never string concatenation, never hardcoded `/home/user/...` or `C:\Users\...`.
- Detect OS via `platform.system()` when platform-specific logic is unavoidable.
- Quote shell paths that may contain spaces (`shlex.quote()`), especially on macOS where volume names can contain spaces.
- Docker (if used): multi-stage builds, non-root user, explicit `HEALTHCHECK` directive, pinned base image digest.

---

## Part 4 — Session Management

### 4.1 Model Selection

CLAUDE.md is re-read from disk at the start of every session.

| Task type | Model |
|:---:|:---:|
| Bug fixes, unit tests, documentation | Sonnet |
| Refactoring core architecture, complex race conditions | Opus |

Override per-session: `/model <alias|name>` (e.g., `/model opus`).

### 4.2 Configuration Priority

Highest to lowest:

1. Session command — `/model <alias|name>`
2. CLI flag — `claude --model <alias|name>`
3. Environment variable — `ANTHROPIC_MODEL`
4. Settings file — `~/.claude/settings.json` → `"model": "..."`

### 4.3 Context Window

Run `/compact` every 20–30 turns to keep context lean.

`/compact` affects: the current session's conversation history (summarized and compressed).

`/compact` does **not** affect:

- `CLAUDE.md` — always re-read fresh from disk at session start
- `~/.claude/projects/*/memory/` — file-based, persist independently
- File edits already written to disk

### 4.4 Autonomy & Permissions

**Default to acting. Reserve interruptions for what can't be undone.**

Proceed without asking for anything reversible and scoped to the OpenManus project
directory: reading/searching the codebase, editing and creating files, running
tests/lint/type-checks (`pytest`, `mypy`, `ruff`, `black`), `git add`/`commit`/
`status`/`diff`/`log`, installing or updating project dependencies, iterating
on build/test output.

Ask first for anything high-risk or hard to reverse:
- `git push --force`, `git reset --hard`, rewriting history, or pushing
  directly to a protected branch (`main`/`master`)
- Deleting or overwriting files that aren't your own scratch output
- Editing `.env` files or anything holding credentials or API keys
- Exposing a local server to the public internet (tunnels, port forwarding)
- Any action outside the OpenManus project directory

`sudo` and other destructive system commands stay hard-denied at the settings
layer (`.claude/settings.json` → `permissions.deny`) — this section is for
judgment calls the allow/ask/deny lists don't explicitly cover, not a way to
route around them.

---

## Appendix A — Quick Reference Checklists

### File Creation

- [ ] Module docstring (purpose, usage, dependencies)
- [ ] Type hints on every function signature
- [ ] Configuration loaded from env (no hardcoded values)
- [ ] Custom exceptions for error cases
- [ ] Context manager for any resource with a lifecycle
- [ ] Health check exposed (if a service)
- [ ] Test file created alongside

### Code Review

- [ ] No hardcoded values (grep confirms)
- [ ] Async client used inside async functions (`AsyncOpenAI`, `httpx.AsyncClient`)
- [ ] Circuit breaker + retry wrap every external call
- [ ] Logs contain no secrets, credentials, or sensitive content
- [ ] Resources explicitly `aclose()`'d on error
- [ ] Type hints accurate, mypy passes
- [ ] Docstrings complete (Args / Returns / Raises)
- [ ] Tests cover error paths, not just happy path

### Performance

- [ ] Lazy loading for expensive resources (`@property` pattern)
- [ ] Connection pooling enabled (`httpx.Limits`, `asyncpg.create_pool`)
- [ ] Caching present where warranted (`@lru_cache`, TTL cache)
- [ ] Circuit breaker on every external call
- [ ] `AsyncOpenAI` (not `OpenAI`) in async paths
- [ ] No N+1 query patterns

### Before Declaring a Feature Done

- [ ] All tests pass (`pytest`)
- [ ] mypy passes
- [ ] ruff + black pass
- [ ] Logs reviewed at DEBUG level — no sensitive fields leak
- [ ] Success criteria from the original request all verified (§1.4)
- [ ] CLAUDE.md consulted for anything that might apply

---

## Version History

| Version | Date | Changes |
|:---:|:---:|:---:|
| 1.0.0 | 2026-08-01 | Adapted from another project's CLAUDE.md: removed all project-specific infrastructure references (ports, node names, canonical-code registry, cloud-sync automation); retained the general coding principles and the mandatory session-journaling requirement, repointed to `openmanus_sessions.md` |

---

*"The best code is no code at all. The second best is code so clear it documents itself."*

---

## Session Journaling (MANDATORY)

**`openmanus_sessions.md`** is the canonical session log for all OpenManus work, kept at the project root.

At the START of every session:
- Read the last 20 lines of `openmanus_sessions.md` (at the OpenManus project root)
- State what you found before proceeding

At the END of every session (or when asked to wrap up):
- Append a structured entry to `openmanus_sessions.md` with this exact format:

## Session: [YYYY-MM-DD HH:MM]
**Goal:** [what was attempted]
**Completed:**
- [bullet per thing actually done]
**State left in:**
- [what is running, what is broken, what is half-done]
**Files changed:**
- [path]: [one-line description of change]
**Next session should:**
- [the most important thing to do next]
---
