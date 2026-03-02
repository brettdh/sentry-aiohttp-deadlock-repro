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

# Custom timeout (default 5s; CI uses 120s for the with-fix job)
uv run python repro.py --with-fix --timeout 120
```

## Architecture

- **`repro.py`** — Entry point. Launches `worker.py` as a subprocess with a 5-second timeout. On hang, sends SIGABRT for a faulthandler stack dump.
- **`worker.py`** — Self-contained deadlock reproduction. Initializes OTel tracing globally (TracerProvider + SimpleSpanProcessor + ConsoleSpanExporter + SentrySpanProcessor + TornadoInstrumentor) **before** Sentry, matching production init order. Starts a Tornado web server and sends concurrent requests to itself. Each request leaks an aiohttp `ClientSession` with a cyclic self-reference. The OTel Tornado instrumentation automatically creates/ends spans per request; `SimpleSpanProcessor` exports synchronously via `to_json()` which iterates `BoundedList` objects under lock.
- **`GCTriggeringLock`** (in `worker.py`) — Replaces `BoundedList`'s lock to call `gc.collect()` after acquiring, deterministically simulating the rare production timing where GC fires inside a locked section.

## The Deadlock Chain

1. `span.end()` → `SimpleSpanProcessor.on_end()` → `ConsoleSpanExporter.export()` → `to_json()` → `BoundedList.__iter__()` acquires `threading.Lock`
2. `gc.collect()` fires while lock is held (simulated by `GCTriggeringLock`, rare in production)
3. GC finalizes leaked aiohttp `ClientSession` → `__del__` → `call_exception_handler` → `logging.error()`
4. Sentry logging integration → `capture_event` → serializes frame locals (`attach_stacktrace=True`)
5. Serializer finds `BoundedList` as `self` in `__iter__()`'s frame → `__iter__` again → same lock → **deadlock**

## Production vs Repro Code Path

In production, the lock is acquired during `_prepare()` → `start_span()` → `Span.__init__()` → `BoundedList.from_seq()` → `extend()` because production spans have links. With the current OTel SDK version, `Span._new_links()` only calls `from_seq()` → `extend()` when links are non-empty; the Tornado instrumentation creates spans without links, so there is no BoundedList lock acquisition during `_prepare()`. The repro uses `SimpleSpanProcessor` to trigger the lock via `__iter__()` during synchronous export instead. The deadlock mechanism (same-thread re-entrance via GC) is identical.

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
