# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Minimal reproduction of a same-thread GC-triggered deadlock involving three libraries: OpenTelemetry SDK's `BoundedList` (non-reentrant `threading.Lock`), Sentry SDK's logging integration (serializes frame locals), and aiohttp's `ClientSession.__del__` (fires `logging.error` during GC finalization). The fix is swapping `threading.Lock` for `threading.RLock`.

## Commands

```bash
# Install dependencies (uses uv with pyproject.toml)
uv sync

# Reproduce the deadlock (hangs, killed after 5s timeout with SIGABRT stack dump)
uv run python repro.py

# Run with the RLock fix to verify deadlock is resolved
uv run python repro.py --with-fix
```

## Architecture

- **`repro.py`** — Entry point. Launches `worker.py` as a subprocess with a 5-second timeout. On hang, sends SIGABRT for a faulthandler stack dump.
- **`worker.py`** — Self-contained deadlock reproduction. Initializes OTel tracing globally (TracerProvider + SimpleSpanProcessor + ConsoleSpanExporter + SentrySpanProcessor + TornadoInstrumentor) **before** Sentry, matching production init order. Starts a Tornado web server and sends concurrent requests to itself. Each request leaks an aiohttp `ClientSession` with a cyclic self-reference. The OTel Tornado instrumentation automatically creates/ends spans per request; `SimpleSpanProcessor` exports synchronously via `to_json()` which iterates `BoundedList` objects under lock.
- **`GCTriggeringLock`** (in `worker.py`) — Replaces `BoundedList`'s lock to call `gc.collect()` after acquiring, deterministically simulating the rare production timing where GC fires inside a locked section.

## The Deadlock Chain

1. `span.end()` → `SimpleSpanProcessor.on_end()` → `ConsoleSpanExporter.export()` → `to_json()` → `BoundedList.__iter__()` acquires `threading.Lock`
2. `gc.collect()` fires while lock is held (simulated by `GCTriggeringLock`, rare in production)
3. GC finalizes leaked aiohttp `ClientSession` → `__del__` → `logging.error()`
4. Sentry logging integration → `capture_event` → serializes frame locals (`attach_stacktrace=True`)
5. Serializer finds `BoundedList` as `self` in `__iter__()`'s frame → `__iter__` again → same lock → **deadlock**

## Environment

- Requires `SENTRY_DSN` in `.env` (copy from `.env` template and set a valid DSN)
- Python >=3.12, managed via `uv`
- No test suite — the reproduction itself is the test (exit 0 = success, hang/SIGABRT = deadlock confirmed)
