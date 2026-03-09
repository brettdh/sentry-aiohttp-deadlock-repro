# sentry-aiohttp-deadlock-repro

Minimal reproduction of a GC-triggered deadlock involving `sentry-sdk`,
`aiohttp`, and the OpenTelemetry SDK (used internally by sentry-sdk 2.x
as its tracing backend).

> **This is the `production-versions` branch**, pinned to the exact package
> versions from our production environment. See the [`main`](../../tree/main)
> branch for a reproduction using the latest package versions.

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

## Sentry SDK workarounds

Three Sentry SDK configuration changes were tested as potential workarounds
that don't require patching third-party library code:

| Flag | Prevents deadlock? | Why |
|------|-------------------|-----|
| `--ignore-asyncio-logger` | **Yes** | `ignore_logger("asyncio")` tells Sentry to skip events from the `asyncio` logger, breaking the deadlock chain at step 4 |
| `--ignore-aiohttp-logger` | No | The deadlock-triggering log comes from the `asyncio` logger, not `aiohttp`. aiohttp's `__del__` calls `loop.call_exception_handler()`, which delegates to asyncio's `default_exception_handler` -> `logger.error()` on the `"asyncio"` logger |
| `--disable-aiohttp-integration` | No | Sentry's `AioHttpIntegration` instruments HTTP request/response tracing. The deadlock is triggered through `LoggingIntegration`, which is a separate integration |

### The effective workaround: `ignore_logger("asyncio")`

```python
from sentry_sdk.integrations.logging import ignore_logger
ignore_logger("asyncio")
```

**Side effects:**

- `logging.error()` (and above) from the `asyncio` logger will no longer
  appear as Sentry events. This includes legitimate asyncio errors like
  unhandled exceptions in tasks and slow callback warnings.
- Breadcrumbs from the `asyncio` logger are also suppressed, so they won't
  appear in breadcrumb trails of other Sentry events.
- The logs themselves still go to Python's normal logging handlers (console,
  file, etc.). Only Sentry's capture is affected.
- Sentry's `AioHttpIntegration` for request tracing continues to work normally.

In practice, the primary source of `asyncio` logger errors is the "Unclosed
connector/session" noise from aiohttp `__del__` methods. The real fix is
closing sessions properly (or patching OTel's `BoundedList` to use `RLock`),
but `ignore_logger("asyncio")` is a safe and minimal workaround to deploy
immediately while upstream fixes are pursued.

## Versions tested

This branch pins to the **exact production versions** where the deadlock was
first observed. These versions are important because they determine which code
path acquires the `BoundedList` lock:

| Package | Version | Why pinned |
|---------|---------|-----------|
| `sentry-sdk` | 2.42.0 | Production version |
| `opentelemetry-sdk` | 1.20.x | [`Span.__init__`][v1.20.0-init] uses [`if links is None`][v1.20.0-links-check] (identity check) — always calls [`extend()`][v1.20.0-extend] |
| `opentelemetry-api` | 1.20.x | Matched to SDK |
| `opentelemetry-instrumentation-tornado` | 0.41b0 | Production version; requires `setuptools<74` for `pkg_resources` |

**Why these versions matter:** In OTel SDK 1.20.0, [`Span.__init__`][v1.20.0-init] checks
[`if links is None`][v1.20.0-links-check] (identity). Since [`Tracer.start_span`][v1.20.0-start_span] defaults `links=()`
(empty tuple), `() is not None` is `True`, so [`BoundedList.from_seq()`][v1.20.0-from_seq] ->
[`extend()`][v1.20.0-extend] is always called, acquiring the [lock][v1.20.0-lock] during every span creation in
[`_prepare()`][v0.41b0-prepare]. This matches the exact production stack trace. In SDK 1.39.1,
this was changed to [`if not links`][v1.39.1-links-check] (truthiness), and `not ()` is `True`, so
it short-circuits with no lock acquisition during span creation.

| Branch | OTel SDK | Lock acquired during | Processor |
|--------|----------|---------------------|-----------|
| **`main`** (this branch) | 1.39.1 | `span.end()` -> [`to_json()`][v1.39.1-to_json] -> [`__iter__()`][v1.39.1-iter] | [`SimpleSpanProcessor`][v1.39.1-simple] |
| [`production-versions`](../../tree/production-versions) | 1.20.0 | [`_prepare()`][v0.41b0-prepare] -> [`Span.__init__()`][v1.20.0-init] -> [`extend()`][v1.20.0-extend] | `BatchSpanProcessor` |

**Why the code path differs:** In OTel SDK 1.20.0, [`Span.__init__`][v1.20.0-init] checks
[`if links is None`][v1.20.0-links-check] (identity). Since [`Tracer.start_span`][v1.20.0-start_span] defaults `links=()`
(empty tuple), `() is not None` is `True`, so [`BoundedList.from_seq()`][v1.20.0-from_seq] ->
[`extend()`][v1.20.0-extend] is always called, acquiring the [lock][v1.20.0-lock] during every span creation.
In SDK 1.39.1, this was changed to [`if not links`][v1.39.1-links-check] (truthiness), and `not ()`
is `True`, so it short-circuits with no lock acquisition during span creation.
This branch uses [`SimpleSpanProcessor`][v1.39.1-simple] to trigger the lock via [`__iter__()`][v1.39.1-iter]
during synchronous span export instead.

[v1.20.0-init]: https://github.com/open-telemetry/opentelemetry-python/blob/v1.20.0/opentelemetry-sdk/src/opentelemetry/sdk/trace/__init__.py#L741
[v1.20.0-links-check]: https://github.com/open-telemetry/opentelemetry-python/blob/v1.20.0/opentelemetry-sdk/src/opentelemetry/sdk/trace/__init__.py#L792
[v1.20.0-start_span]: https://github.com/open-telemetry/opentelemetry-python/blob/v1.20.0/opentelemetry-sdk/src/opentelemetry/sdk/trace/__init__.py#L1047-L1053
[v1.20.0-lock]: https://github.com/open-telemetry/opentelemetry-python/blob/v1.20.0/opentelemetry-sdk/src/opentelemetry/sdk/util/__init__.py#L54
[v1.20.0-extend]: https://github.com/open-telemetry/opentelemetry-python/blob/v1.20.0/opentelemetry-sdk/src/opentelemetry/sdk/util/__init__.py#L78-L84
[v1.20.0-from_seq]: https://github.com/open-telemetry/opentelemetry-python/blob/v1.20.0/opentelemetry-sdk/src/opentelemetry/sdk/util/__init__.py#L86-L91
[v1.39.1-links-check]: https://github.com/open-telemetry/opentelemetry-python/blob/v1.39.1/opentelemetry-sdk/src/opentelemetry/sdk/trace/__init__.py#L825-L827
[v1.39.1-to_json]: https://github.com/open-telemetry/opentelemetry-python/blob/v1.39.1/opentelemetry-sdk/src/opentelemetry/sdk/trace/__init__.py#L486
[v1.39.1-iter]: https://github.com/open-telemetry/opentelemetry-python/blob/v1.39.1/opentelemetry-sdk/src/opentelemetry/sdk/util/__init__.py#L67-L69
[v1.39.1-simple]: https://github.com/open-telemetry/opentelemetry-python/blob/v1.39.1/opentelemetry-sdk/src/opentelemetry/sdk/trace/export/__init__.py#L104-L109
[v1.39.1-lock]: https://github.com/open-telemetry/opentelemetry-python/blob/v1.39.1/opentelemetry-sdk/src/opentelemetry/sdk/util/__init__.py#L56
[v0.41b0-prepare]: https://github.com/open-telemetry/opentelemetry-python-contrib/blob/v0.41b0/instrumentation/opentelemetry-instrumentation-tornado/src/opentelemetry/instrumentation/tornado/__init__.py#L368-L382
[v0.60b1-prepare]: https://github.com/open-telemetry/opentelemetry-python-contrib/blob/v0.60b1/instrumentation/opentelemetry-instrumentation-tornado/src/opentelemetry/instrumentation/tornado/__init__.py#L403-L421

## How it works

The worker matches the production environment: OTel tracing is initialized
globally with `TornadoInstrumentor` (and a [`SimpleSpanProcessor`][v1.39.1-simple] for
synchronous export) **before** Sentry is initialized.

The OTel Tornado instrumentation automatically creates a span in [`_prepare()`][v0.60b1-prepare]
for each incoming request and ends it in `on_finish()`. When the span ends,
[`SimpleSpanProcessor`][v1.39.1-simple] synchronously calls `ConsoleSpanExporter.export()`,
which calls [`to_json()`][v1.39.1-to_json] on the span. `to_json()` iterates the span's
`BoundedList` objects (links, events) via [`__iter__`][v1.39.1-iter], acquiring their [Lock][v1.39.1-lock].

Each request handler:

1. **Creates** an `aiohttp.ClientSession` without closing it
2. **Creates a cyclic self-reference** (`session._cycle = session`) so the
   session can only be freed by the GC's cycle detector, not by refcounting
3. **Drops the reference** - the session is now pure cyclic garbage

Automatic GC is disabled globally; the only `gc.collect()` calls happen
inside `GCTriggeringLock` (a wrapper that replaces BoundedList's Lock)
when the lock is acquired. When `span.end()` triggers [`to_json()`][v1.39.1-to_json] ->
[`BoundedList.__iter__()`][v1.39.1-iter] -> `GCTriggeringLock.__enter__()` -> `gc.collect()`,
the leaked session from the current request is finalized, triggering the
deadlock chain.

`BoundedList` uses a `collections.deque` internally - deque operations are
C-level with essentially zero GC-tracked Python allocations, so in production
the GC almost never fires while the Lock is held (making this bug very rare).
At sufficient request volume, however, GC is much more frequent, so the bug
occurs much more reliably after a long enough duration (about 45 minutes
or so, with our specific production application and automated testing load).

## Dependencies

- `sentry-sdk` (==2.42.0) - logging integration intercepts `logging.error()`
  and serializes frame locals (`attach_stacktrace=True`)
- `aiohttp` (>=3.9) - `ClientSession.__del__` fires during GC when sessions
  aren't closed
- `opentelemetry-sdk` / `opentelemetry-api` (>=1.20, <1.21) - pinned to 1.20.x
  for the [`if links is None`][v1.20.0-links-check] identity check in [`Span.__init__`][v1.20.0-init];
  `BoundedList` uses a non-reentrant [`threading.Lock`][v1.20.0-lock]
- `opentelemetry-instrumentation-tornado` (>=0.41b0, <0.42) - automatically
  creates spans in [`_prepare()`][v0.41b0-prepare], triggering [`BoundedList.extend()`][v1.20.0-extend] under lock
- `tornado` (>=6.0) - web server matching production architecture
- `setuptools` (<74) - required by `opentelemetry-instrumentation` 0.41b0
  which imports `pkg_resources` (removed in setuptools 74+)
