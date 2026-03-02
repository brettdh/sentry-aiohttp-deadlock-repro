# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Minimal reproduction of a same-thread GC-triggered deadlock involving three libraries: OpenTelemetry SDK's `BoundedList` (non-reentrant `threading.Lock`), Sentry SDK's logging integration (serializes frame locals), and aiohttp's `ClientSession.__del__` (fires `logging.error` during GC finalization). The fix is swapping `threading.Lock` for `threading.RLock`.

## Branch: production-versions

This branch pins to the production package versions (sentry-sdk 2.42.0, opentelemetry-sdk 1.20.0, instrumentation-tornado 0.41b0) and reproduces the **exact production code path** through `_prepare()` → `extend()`. The `main` branch uses the latest package versions, where a different code path triggers the same deadlock mechanism.

## Commands

```bash
# Install dependencies (uses uv with pyproject.toml)
uv sync

# Reproduce the deadlock (hangs, killed after 5s timeout with SIGABRT stack dump)
uv run python repro.py

# Run with the RLock fix to verify deadlock is resolved
uv run python repro.py --with-fix

# Custom timeout (default 5s; CI uses 120s for the with-fix job)
uv run python repro.py --with-fix --timeout 120
```

## Architecture

- **`repro.py`** — Entry point. Launches `worker.py` as a subprocess with a 5-second timeout. On hang, sends SIGABRT for a faulthandler stack dump.
- **`worker.py`** — Self-contained deadlock reproduction. Initializes OTel tracing globally (TracerProvider + BatchSpanProcessor + ConsoleSpanExporter + SentrySpanProcessor + TornadoInstrumentor) **before** Sentry, matching production init order. Starts a Tornado web server and sends concurrent requests to itself. Each request leaks an aiohttp `ClientSession` with a cyclic self-reference. The OTel Tornado instrumentation creates spans in `_prepare()`; `Span.__init__()` calls `BoundedList.from_seq()` → `extend()` under lock, which is the production deadlock path.
- **`GCTriggeringLock`** (in `worker.py`) — Replaces `BoundedList`'s lock to call `gc.collect()` after acquiring (main thread only), deterministically simulating the rare production timing where GC fires inside a locked section.

## The Deadlock Chain

1. Request N arrives → OTel Tornado `_prepare()` → `start_span()` → `Span.__init__()` → `BoundedList.from_seq()` → `extend()` acquires `threading.Lock`
2. `gc.collect()` fires while lock is held (simulated by `GCTriggeringLock`, rare in production)
3. GC finalizes **previously** leaked aiohttp `ClientSession` from request N-1 → `__del__` → `call_exception_handler` → `logging.error()`
4. Sentry logging integration → `capture_event` → serializes frame locals (`attach_stacktrace=True`)
5. Serializer finds `BoundedList` as `self` in `extend()`'s frame → `__iter__` → same lock → **deadlock**

## Why Production Versions Matter

In OTel SDK 1.20.0, `Span.__init__` checks `if links is None` (identity). Since `Tracer.start_span` defaults `links=()` (empty tuple), `() is not None` is `True`, so `BoundedList.from_seq()` → `extend()` is **always** called — acquiring the lock during every span creation. In SDK 1.39.1+, this was refactored to `if not links` (truthiness), and `not ()` is `True`, so it short-circuits to `BoundedList(maxlen)` with no lock acquisition.

## Constraints

- **Only patch the lock**: The only acceptable patch to third-party code is replacing `BoundedList`'s `threading.Lock` with `GCTriggeringLock`. Do NOT add extra method calls (e.g. `extend([])` in `__init__`), modify span creation, or otherwise alter third-party behavior to force a particular code path.

## Pre-push Checklist

**Always run both repro modes locally before pushing:**

```bash
uv run python repro.py                        # must deadlock (exit 1)
uv run python repro.py --with-fix --timeout 120  # must complete (exit 0)
```

## Environment

- `SENTRY_DSN` is optional. Defaults to `http://key@localhost/0` (events silently dropped). Set in `.env` to send events to a real Sentry project if the captured logging details are of interest.
- Python >=3.12, managed via `uv`
- No test suite — the reproduction itself is the test (exit 0 = success, hang/SIGABRT = deadlock confirmed)
- CI runs both modes via GitHub Actions on every push to any branch
