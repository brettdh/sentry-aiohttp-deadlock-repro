"""
Worker process for the deadlock reproduction. Launched by repro.py.

Architecture matches production: Tornado web server handles incoming
requests, and request handlers create aiohttp ClientSession objects.
Sessions are intentionally leaked (not closed), creating cyclic garbage
that the GC must finalize.

The deadlock is same-thread re-entrance on BoundedList's threading.Lock:
  1. BoundedList.extend() acquires Lock
  2. An allocation inside extend() triggers Python's automatic GC
  3. GC finalizes leaked aiohttp sessions -> __del__ -> logging.error()
  4. Sentry logging integration -> capture_event -> serialize frame locals
  5. Serializer finds BoundedList `self` in frame -> __iter__ -> Lock -> DEADLOCK

In production this is extremely rare because BoundedList uses a deque
internally - deque.extend() is a C-level operation with essentially zero
GC-tracked Python allocations inside the locked section, so GC almost
never fires while the lock is held.

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
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.util import BoundedList
from opentelemetry.trace import Link, SpanContext, TraceFlags
from sentry_sdk.integrations.opentelemetry import SentrySpanProcessor

load_dotenv()

SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
SERVER_PORT = 8080
CONCURRENCY = 10
NUM_REQUESTS = 200

# Dummy span links - forces BoundedList.from_seq -> extend() to be called
# with a non-empty sequence. Without links, _new_links() just creates an
# empty BoundedList (no extend, no lock acquisition, no deadlock chance).
_dummy_ctx = SpanContext(
    trace_id=0xDEADBEEF,
    span_id=0xCAFEFACE,
    is_remote=False,
    trace_flags=TraceFlags(0x01),
)
SPAN_LINKS = [Link(_dummy_ctx)]


class GCTriggeringLock:
    """A lock wrapper that calls gc.collect() after acquiring.

    This deterministically reproduces the rare production timing where
    Python's automatic GC fires during an allocation inside a locked
    BoundedList method (extend/append/etc).

    gc.collect() while the Lock is held -> finalizes leaked aiohttp sessions
    -> __del__ -> logging.error -> Sentry serializes frame locals -> finds
    BoundedList `self` -> __iter__ -> tries to acquire SAME Lock -> DEADLOCK
    (with threading.Lock) or succeeds (with threading.RLock).
    """

    def __init__(self, use_rlock=False):
        self._lock = threading.RLock() if use_rlock else threading.Lock()

    def __enter__(self):
        self._lock.acquire()
        gc.collect()
        return self

    def __exit__(self, *args):
        self._lock.release()

    def acquire(self, *args, **kwargs):
        return self._lock.acquire(*args, **kwargs)

    def release(self):
        self._lock.release()


def patch_bounded_list(use_rlock=False):
    """Replace BoundedList's Lock with GCTriggeringLock."""
    _original_init = BoundedList.__init__

    def _patched_init(self, maxlen):
        _original_init(self, maxlen)
        self._lock = GCTriggeringLock(use_rlock=use_rlock)

    BoundedList.__init__ = _patched_init


def init_sentry():
    if not SENTRY_DSN:
        print("SENTRY_DSN not set in .env", flush=True)
        sys.exit(1)

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        # attach_stacktrace makes Sentry serialize frame locals (including
        # BoundedList `self`) when capturing the logging event from __del__.
        attach_stacktrace=True,
    )


def init_otel():
    """Set up a real TracerProvider so spans create BoundedList with a Lock.

    Without this, sentry-sdk 2.x only creates NonRecordingSpan (no
    BoundedList, no Lock, no deadlock). In production, instrumentation
    packages (e.g. opentelemetry-instrumentation-tornado) do this setup.
    """
    provider = TracerProvider()
    provider.add_span_processor(SentrySpanProcessor())
    otel_trace.set_tracer_provider(provider)


class TargetHandler(tornado.web.RequestHandler):
    """Trivial endpoint (kept for potential future use)."""

    def get(self):
        self.write(b"ok")


class LeakyHandler(tornado.web.RequestHandler):
    """Tornado handler that leaks aiohttp sessions (matching production).

    Each request:
    1. Creates an aiohttp ClientSession without closing it
    2. Creates a cyclic self-reference so the session requires GC to collect
    3. Creates a recording OTel span with links (BoundedList.extend + Lock)
       -> GCTriggeringLock calls gc.collect() while Lock is held
       -> GC finalizes the leaked session -> deadlock chain fires

    Note: the session doesn't need to make requests - just being unclosed
    is enough for __del__ to call logging.error via call_exception_handler.
    Not making requests also avoids the event loop keeping the session
    reachable through transport -> protocol -> connector references.
    """

    async def get(self):
        # Create an aiohttp session but don't close it.
        # We don't make requests - just creating the session is enough
        # for __del__ to fire. This also avoids event loop references
        # that would prevent the session from becoming cyclic garbage.
        session = aiohttp.ClientSession()

        # Cyclic self-reference: prevents refcount from freeing the session.
        # Only the GC's cycle detector can collect it.
        session._cycle = session

        # Disable automatic GC BEFORE dropping the reference, so the
        # session stays as uncollected cyclic garbage until gc.collect()
        # is called explicitly inside the Lock.
        gc.disable()

        # Drop the local reference. The session is now unreachable except
        # through its self-cycle - pure cyclic garbage.
        del session

        # Create an OTel span with links. Span.__init__ -> _new_links ->
        # BoundedList.from_seq -> BoundedList.extend(links): acquires Lock.
        # GCTriggeringLock.__enter__ calls gc.collect() while Lock is held
        # -> finalizes the leaked session -> __del__ -> logging.error ->
        # Sentry -> serialize frame locals -> BoundedList.__iter__ ->
        # same Lock -> DEADLOCK.
        tracer = otel_trace.get_tracer(__name__)
        span = tracer.start_span("handle-request", links=SPAN_LINKS)
        span.end()

        gc.enable()

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
                if completed % 50 == 0:
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
    args = parser.parse_args()

    faulthandler.enable()
    init_sentry()
    init_otel()
    patch_bounded_list(use_rlock=args.with_fix)

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
