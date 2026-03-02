# sentry-aiohttp-deadlock-repro

Minimal reproduction of a GC-triggered deadlock involving `sentry-sdk`,
`aiohttp`, and the OpenTelemetry SDK (used internally by sentry-sdk 2.x
as its tracing backend).

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
cp .env.example .env
# Edit .env and set your SENTRY_DSN
```

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

- `sentry-sdk` - logging integration intercepts `logging.error()` and serializes
  frame locals (`attach_stacktrace=True`)
- `aiohttp` - `ClientSession.__del__` fires during GC when sessions aren't closed
- `opentelemetry-sdk` / `opentelemetry-api` - sentry-sdk uses these internally
  for tracing; `BoundedList` uses a non-reentrant `threading.Lock`
- `opentelemetry-instrumentation-tornado` - automatically creates spans for each
  request (matching production), triggering BoundedList lock acquisition on export
- `opentelemetry-instrumentation-aiohttp-client` - instruments aiohttp sessions
  (matching production environment)
- `tornado` - web server matching production architecture
