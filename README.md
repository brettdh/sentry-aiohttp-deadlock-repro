# sentry-aiohttp-deadlock-repro

Minimal reproduction of a GC-triggered deadlock involving `sentry-sdk`,
`aiohttp`, and the OpenTelemetry SDK (used internally by sentry-sdk 2.x
as its tracing backend).

> **This is the `main` branch**, which uses the latest package versions.
> See the [`production-versions`](../../tree/production-versions) branch
> for a reproduction pinned to the exact versions from our production
> environment.

## The deadlock chain

This is a **same-thread** deadlock caused by GC re-entrance:

1. A `BoundedList` method (`__iter__`, `extend`, etc.) **acquires** its non-reentrant `threading.Lock`
2. An allocation inside the locked section triggers Python's automatic GC
3. GC finalizes an unclosed `aiohttp.ClientSession` **on the same thread**
   -> `__del__` -> `asyncio.call_exception_handler` -> `logging.error()`
4. Sentry's logging integration intercepts -> `capture_event` -> serializes frame locals
5. Serializer finds `BoundedList` as `self` in the locked method's frame
   -> calls `__iter__` -> tries to acquire the **same Lock** -> **DEADLOCK**

## Setup

```bash
uv sync
```

Optionally, set `SENTRY_DSN` in `.env` to send captured events to a real
Sentry project (useful for inspecting the logging details that trigger the
deadlock). If unset, a dummy DSN is used and events are silently dropped.

## Running

```bash
# Reproduce the deadlock (process will hang and be killed after 5s)
uv run python repro.py

# Run with the RLock fix to verify the deadlock is resolved
uv run python repro.py --with-fix
```

`repro.py` launches `worker.py` as a subprocess with a 5-second timeout.
If the worker hangs (deadlock), it sends SIGABRT to trigger a faulthandler
stack dump showing the deadlock chain.

### The `--with-fix` flag

The deadlock occurs because `BoundedList` uses a non-reentrant `threading.Lock`.
When GC fires while the lock is held (step 2 in the chain above), and Sentry's
serializer tries to iterate the same `BoundedList` on the same thread (step 5),
the second `acquire()` blocks forever.

Passing `--with-fix` swaps `threading.Lock` for `threading.RLock` (reentrant
lock). An `RLock` allows the same thread to acquire it multiple times without
blocking, so the serializer's `__iter__` call succeeds and the process completes
normally.

## Versions tested

This branch uses the **latest** versions of all packages (`sentry-sdk>=2.0`,
`opentelemetry-sdk>=1.20`, etc.) to confirm the deadlock still exists in
current releases. The deadlock mechanism is identical across versions, but the
specific code path that acquires the `BoundedList` lock differs:

| Branch | OTel SDK | Lock acquired during | Processor |
|--------|----------|---------------------|-----------|
| **`main`** (this branch) | latest (1.39+) | `span.end()` -> `to_json()` -> `__iter__()` | `SimpleSpanProcessor` |
| [`production-versions`](../../tree/production-versions) | 1.20.0 | `_prepare()` -> `Span.__init__()` -> `extend()` | `BatchSpanProcessor` |

**Why the code path differs:** In OTel SDK 1.20.0, `Span.__init__` checks
`if links is None` (identity). Since `Tracer.start_span` defaults `links=()`
(empty tuple), `() is not None` is `True`, so `BoundedList.from_seq()` ->
`extend()` is always called, acquiring the lock during every span creation.
In SDK 1.39.1+, this was changed to `if not links` (truthiness), and `not ()`
is `True`, so it short-circuits with no lock acquisition during span creation.
This branch uses `SimpleSpanProcessor` to trigger the lock via `__iter__()`
during synchronous span export instead.

## How it works

The worker matches the production environment: OTel tracing is initialized
globally with `TornadoInstrumentor` (and a `SimpleSpanProcessor` for
synchronous export) **before** Sentry is initialized.

The OTel Tornado instrumentation automatically creates a span in `_prepare()`
for each incoming request and ends it in `on_finish()`. When the span ends,
`SimpleSpanProcessor` synchronously calls `ConsoleSpanExporter.export()`,
which calls `to_json()` on the span. `to_json()` iterates the span's
`BoundedList` objects (links, events) via `__iter__`, acquiring their Lock.

Each request handler:

1. **Creates** an `aiohttp.ClientSession` without closing it
2. **Creates a cyclic self-reference** (`session._cycle = session`) so the
   session can only be freed by the GC's cycle detector, not by refcounting
3. **Drops the reference** - the session is now pure cyclic garbage

Automatic GC is disabled globally; the only `gc.collect()` calls happen
inside `GCTriggeringLock` (a wrapper that replaces BoundedList's Lock)
when the lock is acquired. When `span.end()` triggers `to_json()` ->
`BoundedList.__iter__()` -> `GCTriggeringLock.__enter__()` -> `gc.collect()`,
the leaked session from the current request is finalized, triggering the
deadlock chain.

`BoundedList` uses a `collections.deque` internally - deque operations are
C-level with essentially zero GC-tracked Python allocations, so in production
the GC almost never fires while the Lock is held (making this bug very rare).
At sufficient request volume, however, GC is much more frequent, so the bug
occurs much more reliably after a long enough duration (about 45 minutes
or so, with our specific production application and automated testing load).

## Dependencies

- `sentry-sdk` (>=2.0) - logging integration intercepts `logging.error()` and
  serializes frame locals (`attach_stacktrace=True`)
- `aiohttp` (>=3.9) - `ClientSession.__del__` fires during GC when sessions
  aren't closed
- `opentelemetry-sdk` / `opentelemetry-api` (>=1.20) - sentry-sdk uses these
  internally for tracing; `BoundedList` uses a non-reentrant `threading.Lock`
- `opentelemetry-instrumentation-tornado` (>=0.40b0) - automatically creates
  spans for each request (matching production), triggering BoundedList lock
  acquisition on export
- `opentelemetry-instrumentation-aiohttp-client` (>=0.40b0) - instruments
  aiohttp sessions (matching production environment)
- `tornado` (>=6.0) - web server matching production architecture
