"""
Reproduce the GC-triggered deadlock between:
  - OpenTelemetry BoundedList (holds a non-reentrant threading.Lock in extend())
  - Sentry serializer (iterates BoundedList via __iter__ while serializing frame locals)
  - aiohttp ClientSession.__del__ (fires during GC, logs an error about unclosed session)

The deadlock is same-thread re-entrance via GC (matching the production
code path through _prepare()):
  1. OTel Tornado _prepare() -> start_span() -> Span.__init__() ->
     BoundedList.from_seq() -> extend() ACQUIRES threading.Lock
  2. GC fires while the lock is held (simulated deterministically)
  3. GC finalizes an unclosed aiohttp ClientSession on the SAME thread
     -> __del__ -> asyncio.call_exception_handler -> logging.error()
  4. Sentry logging integration intercepts the log -> capture_event
     -> serialize current stack frames -> walk frame locals
  5. Serializer finds `self` (the BoundedList) in extend()'s frame locals
     -> calls __iter__ -> tries to acquire the SAME Lock -> DEADLOCK

This works because OTel SDK 1.20.0 checks `if links is None` (identity)
and the default is (), so from_seq -> extend is always called. See the
main branch for a variant that reproduces with the latest SDK versions.

This script launches worker.py as a subprocess with a timeout. If the
worker hangs (deadlock), it sends SIGABRT to get a faulthandler stack dump.
"""

import argparse
import os
import signal
import subprocess
import sys
import time

DEFAULT_TIMEOUT = 5


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--with-fix",
        action="store_true",
        help="Use threading.RLock instead of threading.Lock to prevent the deadlock",
    )
    parser.add_argument(
        "--ignore-asyncio-logger",
        action="store_true",
        help="Tell Sentry to ignore the 'asyncio' logger",
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
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Seconds to wait before declaring deadlock (default: {DEFAULT_TIMEOUT})",
    )
    args = parser.parse_args()
    timeout = args.timeout

    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker.py")

    flags = []
    if args.with_fix:
        flags.append("RLock fix")
    if args.ignore_asyncio_logger:
        flags.append("ignore asyncio logger")
    if args.ignore_aiohttp_logger:
        flags.append("ignore aiohttp logger")
    if args.disable_aiohttp_integration:
        flags.append("disable aiohttp integration")
    mode = ", ".join(flags) if flags else "no workarounds (expect deadlock)"
    print(f"Launching worker [{mode}] with {timeout}s timeout...\n")

    cmd = [sys.executable, worker]
    if args.with_fix:
        cmd.append("--with-fix")
    if args.ignore_asyncio_logger:
        cmd.append("--ignore-asyncio-logger")
    if args.ignore_aiohttp_logger:
        cmd.append("--ignore-aiohttp-logger")
    if args.disable_aiohttp_integration:
        cmd.append("--disable-aiohttp-integration")

    proc = subprocess.Popen(
        cmd,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )

    try:
        exit_code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"\n--- Worker did not exit after {timeout}s (likely deadlocked) ---")
        print("Sending SIGABRT to trigger faulthandler stack dump...\n")
        proc.send_signal(signal.SIGABRT)
        time.sleep(2)
        proc.kill()
        proc.wait()
        sys.exit(1)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
