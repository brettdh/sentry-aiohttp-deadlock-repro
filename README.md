# sentry-aiohttp-deadlock-repro

Minimal reproduction of a GC-triggered deadlock involving `sentry-sdk`,
`aiohttp`, and the OpenTelemetry SDK (used internally by sentry-sdk 2.x
as its tracing backend).

## The deadlock chain

This is a **same-thread** deadlock caused by GC re-entrance:

1. OTel `Span.__init__` -> `BoundedList.extend()` **acquires** non-reentrant `threading.Lock`
2. An allocation inside `extend()` triggers Python's automatic GC
3. GC finalizes an unclosed `aiohttp.ClientSession` **on the same thread**
   -> `__del__` -> `asyncio.call_exception_handler` -> `logging.error()`
4. Sentry's logging integration intercepts -> `capture_event` -> serializes frame locals
5. Serializer finds `BoundedList` as `self` in `extend()`'s frame
   -> calls `__iter__` -> tries to acquire the **same Lock** -> **DEADLOCK**

## Setup

```bash
uv sync
cp .env.example .env
# Edit .env and set your SENTRY_DSN
```

## Running

```bash
# Reproduce the deadlock (process will hang and be killed after 30s)
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

The worker runs a **Tornado web server** (matching production architecture).
Each incoming request handler:

1. **Creates** an `aiohttp.ClientSession` without closing it
2. **Creates a cyclic self-reference** (`session._cycle = session`) so the
   session can only be freed by the GC's cycle detector, not by refcounting
3. **Disables automatic GC** and drops the session reference (pure cyclic garbage)
4. **Creates an OTel span** with links, triggering `BoundedList.extend()`

`BoundedList` uses a `collections.deque` internally - `deque.extend()` is
C-level with essentially zero GC-tracked Python allocations, so in production
the GC almost never fires while the Lock is held (making this bug very rare).
At sufficient request volume, however, GC is much more frequent, so the bug
occurs much more reliably after a long enough duration (about 45 minutes
or so, with our specific production application and automated testing load).

To reproduce deterministically, we **replace BoundedList's Lock** with a
wrapper that calls `gc.collect()` after acquiring. This simulates the rare
timing where automatic GC fires during an allocation inside the locked section.

## Dependencies

- `sentry-sdk` - logging integration intercepts `logging.error()` and serializes
  frame locals (`attach_stacktrace=True`)
- `aiohttp` - `ClientSession.__del__` fires during GC when sessions aren't closed
- `opentelemetry-sdk` / `opentelemetry-api` - sentry-sdk uses these internally
  for tracing; `BoundedList` uses a non-reentrant `threading.Lock`
- `tornado` - web server matching production architecture
