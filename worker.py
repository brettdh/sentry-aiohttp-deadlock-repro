"""
Worker process for the deadlock reproduction. Launched by repro.py.

Architecture matches production: Tornado web server with OTel
instrumentation handles incoming requests. Request handlers leak
aiohttp ClientSession objects, creating cyclic garbage that only
the GC can finalize.

The deadlock is same-thread re-entrance on BoundedList's threading.Lock:
  1. span.end() -> SimpleSpanProcessor exports synchronously on the main
     thread -> to_json() -> BoundedList.__iter__() acquires Lock
  2. GCTriggeringLock calls gc.collect() while Lock is held
  3. GC finalizes leaked aiohttp sessions -> __del__ ->
     call_exception_handler -> logging.error()
  4. Sentry logging integration -> capture_event -> serialize frame locals
  5. Serializer finds BoundedList `self` in __iter__()'s frame
     -> calls __iter__ again -> tries to acquire SAME Lock -> DEADLOCK

Note: in production, the lock is acquired during _prepare() ->
start_span() -> Span.__init__() -> BoundedList.from_seq() -> extend(),
because production spans have links. With the current OTel SDK version,
spans without links don't call extend(), so there's no lock acquisition
during _prepare(). The repro uses SimpleSpanProcessor to trigger the
lock via __iter__() during export instead, but the deadlock mechanism
is identical.

In production this is extremely rare because BoundedList operations
on deques are C-level with essentially zero GC-tracked Python
allocations inside the locked section, so GC almost never fires
while the lock is held. The likelihood of this increases with sufficient
request volume, to the point where we can reproduce the issue reliably
with our production application using a suite of UI-focused automated tests.
After the issue manifests, the affected server can no longer serve any requests
until it is restarted.

To reproduce deterministically, we replace BoundedList's Lock with a
wrapper that calls gc.collect() after acquiring. This simulates the rare
timing where automatic GC fires during an allocation inside a locked
BoundedList method.
"""

import argparse
import asyncio
import faulthandler
import gc
import os
import sys
import threading

import aiohttp
import sentry_sdk
import tornado.web
from dotenv import load_dotenv

from opentelemetry import trace as otel_trace
from opentelemetry.instrumentation.tornado import TornadoInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.sdk.util import BoundedList
from sentry_sdk.integrations.opentelemetry import SentrySpanProcessor

load_dotenv()

# Default to a well-formed DSN pointing at localhost so the repro works without
# configuring a real Sentry project (e.g. in CI). So long as nothing listens on localhost:80,
# connections are refused instantly and events are silently dropped. And if something
# is listening for some reason, it will get some nonsense requests; no big deal.
SENTRY_DSN = os.environ.get("SENTRY_DSN", "http://key@localhost/0")
SERVER_PORT = 8080
CONCURRENCY = 10
NUM_REQUESTS = 20


class GCTriggeringLock:
    """A lock wrapper that calls gc.collect() after acquiring.

    This deterministically reproduces the rare production timing where
    Python's automatic GC fires during an allocation inside a locked
    BoundedList method (extend/append/__iter__/etc).

    gc.collect() while the Lock is held -> finalizes leaked aiohttp sessions
    -> __del__ -> logging.error -> Sentry serializes frame locals -> finds
    BoundedList `self` -> __iter__ -> tries to acquire SAME Lock -> DEADLOCK
    (with threading.Lock) or succeeds (with threading.RLock).
    """

    def __init__(self, use_rlock=False):
        self._lock = threading.RLock() if use_rlock else threading.Lock()
        self._owner = None  # thread ident of current holder

    def _log_acquire(self, caller):
        current = threading.current_thread()
        owner = self._owner
        if owner is not None:
            if owner == current.ident:
                print(
                    f"[LOCK] {caller}: thread {current.name!r} (ident={current.ident}) "
                    f"re-entering — already holds this lock",
                    flush=True,
                )
            else:
                print(
                    f"[LOCK] {caller}: thread {current.name!r} (ident={current.ident}) "
                    f"waiting — held by thread ident={owner}",
                    flush=True,
                )
        else:
            print(
                f"[LOCK] {caller}: thread {current.name!r} (ident={current.ident}) "
                f"acquiring (lock is free)",
                flush=True,
            )

    def __enter__(self):
        self._log_acquire("__enter__")
        self._lock.acquire()
        self._owner = threading.current_thread().ident
        gc.collect()
        return self

    def __exit__(self, *args):
        self._owner = None
        self._lock.release()

    def acquire(self, *args, **kwargs):
        self._log_acquire("acquire")
        result = self._lock.acquire(*args, **kwargs)
        if result:
            self._owner = threading.current_thread().ident
        return result

    def release(self):
        self._owner = None
        self._lock.release()


def patch_bounded_list(use_rlock=False):
    """Replace BoundedList's Lock with GCTriggeringLock."""
    _original_init = BoundedList.__init__

    def _patched_init(self, maxlen):
        _original_init(self, maxlen)
        self._lock = GCTriggeringLock(use_rlock=use_rlock)

    BoundedList.__init__ = _patched_init


def init_otel():
    """Set up OTel tracing globally, matching the production environment.

    Production initializes OTel with instrumentation packages (Tornado,
    aiohttp) BEFORE Sentry. The TracerProvider is configured with:
    - SimpleSpanProcessor + ConsoleSpanExporter: exports spans synchronously
      on the main thread. During export, to_json() iterates BoundedList
      objects (events/links) via __iter__, acquiring their Lock. This is the
      code path where GC can trigger the deadlock.
    - SentrySpanProcessor: bridges OTel spans to Sentry transactions.
    """
    provider = TracerProvider()

    # SimpleSpanProcessor exports synchronously during span.end() on the
    # calling thread. ConsoleSpanExporter.export() calls span.to_json(),
    # which iterates the span's BoundedList objects (_format_links,
    # _format_events) via __iter__, acquiring each BoundedList's Lock.
    # Discard the output - we only need the iteration side-effect.
    devnull = open(os.devnull, "w")
    provider.add_span_processor(
        SimpleSpanProcessor(ConsoleSpanExporter(out=devnull))
    )
    provider.add_span_processor(SentrySpanProcessor())
    otel_trace.set_tracer_provider(provider)

    # Instrument Tornado: monkey-patches RequestHandler._execute so that
    # _prepare() creates an OTel span for each incoming request, and
    # on_finish() ends it (triggering the synchronous export above).
    TornadoInstrumentor().instrument()


def init_sentry(disable_aiohttp_integration=False):
    kwargs = dict(
        dsn=SENTRY_DSN,
        # attach_stacktrace makes Sentry serialize frame locals (including
        # BoundedList `self`) when capturing the logging event from __del__.
        attach_stacktrace=True,
    )
    if disable_aiohttp_integration:
        from sentry_sdk.integrations.aiohttp import AioHttpIntegration
        # disabled_integrations prevents this integration from being auto-enabled
        kwargs["disabled_integrations"] = [AioHttpIntegration()]
    sentry_sdk.init(**kwargs)


class TargetHandler(tornado.web.RequestHandler):
    """Trivial endpoint (kept for potential future use)."""

    def get(self):
        self.write(b"ok")


class LeakyHandler(tornado.web.RequestHandler):
    """Tornado handler that leaks aiohttp sessions (matching production).

    Each request creates an aiohttp ClientSession without closing it,
    with a cyclic self-reference so it requires GC to collect. The OTel
    Tornado instrumentation automatically creates a span in _prepare()
    for each request; when the span ends (after the handler completes),
    SimpleSpanProcessor exports it synchronously, calling to_json() which
    iterates BoundedList objects via __iter__. GCTriggeringLock then calls
    gc.collect() while the Lock is held, collecting the leaked session
    from a previous request -> deadlock chain fires.

    Note: the session doesn't need to make requests - just being unclosed
    is enough for __del__ to call logging.error via call_exception_handler.
    """

    async def get(self):
        # Create an aiohttp session but don't close it.
        session = aiohttp.ClientSession()

        # Cyclic self-reference: prevents refcount from freeing the session.
        # Only the GC's cycle detector can collect it.
        session._cycle = session

        # Drop the local reference. The session is now unreachable except
        # through its self-cycle - pure cyclic garbage. Since automatic GC
        # is disabled globally, the session stays uncollected until
        # gc.collect() is called inside the Lock during span export.
        del session

        self.set_status(200)
        self.finish(b"ok")


async def generate_load():
    """Fire concurrent requests at the Tornado server."""
    completed = 0
    connector = aiohttp.TCPConnector(limit=CONCURRENCY)
    async with aiohttp.ClientSession(connector=connector) as client:
        sem = asyncio.Semaphore(CONCURRENCY)

        async def make_request(i):
            nonlocal completed
            async with sem:
                try:
                    async with client.get(
                        f"http://127.0.0.1:{SERVER_PORT}/leak"
                    ) as resp:
                        await resp.read()
                except Exception:
                    pass
                completed += 1
                if completed % 10 == 0:
                    print(f"  {completed}/{NUM_REQUESTS} requests", flush=True)

        tasks = [make_request(i) for i in range(NUM_REQUESTS)]
        await asyncio.gather(*tasks)

    return completed


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--with-fix",
        action="store_true",
        help="Use threading.RLock instead of threading.Lock to prevent the deadlock",
    )
    parser.add_argument(
        "--ignore-asyncio-logger",
        action="store_true",
        help="Tell Sentry to ignore the 'asyncio' logger (breaks the deadlock chain)",
    )
    parser.add_argument(
        "--ignore-aiohttp-logger",
        action="store_true",
        help="Tell Sentry to ignore the 'aiohttp' logger (ineffective: the log comes from the 'asyncio' logger)",
    )
    parser.add_argument(
        "--disable-aiohttp-integration",
        action="store_true",
        help="Disable Sentry's AioHttpIntegration (ineffective: the deadlock is via LoggingIntegration, not AioHttpIntegration)",
    )
    args = parser.parse_args()

    faulthandler.enable()

    # Match production init order: OTel first, then Sentry.
    # OTel sets up the TracerProvider with instrumentors and exporters.
    # Sentry is initialized after, so it does NOT replace the TracerProvider.
    init_otel()
    init_sentry(disable_aiohttp_integration=args.disable_aiohttp_integration)

    if args.ignore_asyncio_logger or args.ignore_aiohttp_logger:
        from sentry_sdk.integrations.logging import ignore_logger
        if args.ignore_asyncio_logger:
            ignore_logger("asyncio")
        if args.ignore_aiohttp_logger:
            ignore_logger("aiohttp")

    patch_bounded_list(use_rlock=args.with_fix)

    # Disable automatic GC so leaked sessions accumulate as uncollected
    # cyclic garbage. The only gc.collect() calls happen inside
    # GCTriggeringLock when BoundedList acquires its lock.
    gc.disable()

    lock_type = "RLock (fix applied)" if args.with_fix else "Lock (expect deadlock)"

    app = tornado.web.Application([
        (r"/leak", LeakyHandler),
        (r"/target", TargetHandler),
    ])
    app.listen(SERVER_PORT)

    print(f"Tornado server on :{SERVER_PORT}", flush=True)
    print(f"Lock type: threading.{lock_type}", flush=True)
    print(f"Sending {NUM_REQUESTS} requests (concurrency={CONCURRENCY})", flush=True)
    if args.with_fix:
        print("RLock allows same-thread re-entrance - deadlock should NOT occur.\n", flush=True)
    else:
        print("If the process hangs, the deadlock has been reproduced.\n", flush=True)

    completed = await generate_load()

    if args.with_fix:
        print(f"\nDone - completed {completed} requests with RLock fix (no deadlock).", flush=True)
    else:
        print(f"\nDone - deadlock did not trigger after {completed} requests.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
