"""
Reproduce the GC-triggered deadlock between:
  - OpenTelemetry BoundedList (holds a non-reentrant threading.Lock)
  - Sentry serializer (iterates BoundedList via __iter__ while serializing frame locals)
  - aiohttp ClientSession.__del__ (fires during GC, logs an error about unclosed session)

The deadlock is same-thread re-entrance via GC:
  1. span.end() -> SimpleSpanProcessor exports synchronously -> to_json()
     -> BoundedList.__iter__() ACQUIRES threading.Lock
  2. GC fires while the lock is held (simulated deterministically)
  3. GC finalizes an unclosed aiohttp ClientSession on the SAME thread
     -> __del__ -> asyncio.call_exception_handler -> logging.error()
  4. Sentry logging integration intercepts the log -> capture_event
     -> serialize current stack frames -> walk frame locals
  5. Serializer finds `self` (the BoundedList) in __iter__()'s frame locals
     -> calls __iter__ -> tries to acquire the SAME Lock -> DEADLOCK

This script launches worker.py as a subprocess with a timeout. If the
worker hangs (deadlock), it sends SIGABRT to get a faulthandler stack dump.
"""

import argparse
import os
import signal
import subprocess
import sys
import time

TIMEOUT_SECONDS = 5


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--with-fix",
        action="store_true",
        help="Use threading.RLock instead of threading.Lock to prevent the deadlock",
    )
    args = parser.parse_args()

    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker.py")

    mode = "WITH FIX (RLock)" if args.with_fix else "WITHOUT fix (Lock - expect deadlock)"
    print(f"Launching worker {mode} with {TIMEOUT_SECONDS}s timeout...\n")

    cmd = [sys.executable, worker]
    if args.with_fix:
        cmd.append("--with-fix")

    proc = subprocess.Popen(
        cmd,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )

    try:
        exit_code = proc.wait(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        print(f"\n--- Worker did not exit after {TIMEOUT_SECONDS}s (likely deadlocked) ---")
        print("Sending SIGABRT to trigger faulthandler stack dump...\n")
        proc.send_signal(signal.SIGABRT)
        time.sleep(2)
        proc.kill()
        proc.wait()
        sys.exit(1)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
