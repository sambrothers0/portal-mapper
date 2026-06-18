#!/usr/bin/env python3
"""Keep an Oracle Always Free Ampere A1 instance out of idle reclamation.

Oracle may reclaim an Always Free compute instance only if, across a rolling
7-day window, ALL of these are simultaneously true:

    * 95th-percentile CPU         < 20%
    * network utilization         < 20%
    * memory utilization          < 20%   (A1 shapes only)

It's an AND, so keeping any single metric above its line is enough. Memory is
the cheapest lever: parking a few GB resident costs ~0% CPU and no network,
versus a CPU/traffic loop that actually burns the free box you're trying to
keep. This process allocates KEEPALIVE_MB of RAM, forces every page resident,
and periodically re-touches them so the pages can't be paged out and stop
counting toward utilization.

Sizing: free A1 is 12 GB, so the 20% line is ~2.4 GB. An idle host (OS + an
idle uvicorn) sits well under that, so we hold 3 GiB by default -> ~30% total,
comfortably above the threshold with margin. That still leaves ~8.5 GB for a
real scan (~4 GB resident, see deploy/README.md). Raise KEEPALIVE_MB only if
Oracle tightens the threshold; lower it if you bump MAX_CONCURRENT_SCANS and
need more scan headroom.

Pure stdlib on purpose (this repo compiles nothing on the ARM host). Run it
under systemd; see deploy/keepalive.service.
"""

from __future__ import annotations

import os
import signal
import sys
import time

PAGE = 4096
# How much RAM to hold resident. 3 GiB clears the ~2.4 GB (20% of 12 GB) line
# with margin while leaving plenty for a scan.
TARGET_MB = int(os.environ.get("KEEPALIVE_MB", "3072"))
# Re-touch cadence. The box has no swap by default, so a single touch would do;
# re-walking every few minutes is belt-and-suspenders against any swap/compaction
# and is what keeps the heartbeat log fresh. Cost is ~0.1s of CPU per cycle.
INTERVAL_SEC = int(os.environ.get("KEEPALIVE_INTERVAL_SEC", "300"))

_running = True


def _stop(signum: int, _frame: object) -> None:
    global _running
    _running = False
    _log(f"received signal {signum}, releasing {TARGET_MB} MiB and exiting")


def _log(msg: str) -> None:
    # Line-buffered to stdout so `journalctl -u keepalive` shows it live.
    print(f"[keepalive] {msg}", flush=True)


def _touch_all(buf: bytearray) -> None:
    """Write one byte per page to force/keep every page resident (counted as RSS)."""
    # Flip a byte per page; the value alternates so the kernel can't dedupe pages
    # to a single shared zero page (which would drop real memory utilization).
    marker = (int(time.time()) & 0xFF) or 1
    for off in range(0, len(buf), PAGE):
        buf[off] = marker


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    nbytes = TARGET_MB * 1024 * 1024
    _log(f"allocating {TARGET_MB} MiB ({nbytes:,} bytes)")
    try:
        buf = bytearray(nbytes)
    except MemoryError:
        _log("MemoryError on allocation — KEEPALIVE_MB too large for this host")
        return 1

    _touch_all(buf)
    pages = (nbytes + PAGE - 1) // PAGE
    _log(f"resident: {pages:,} pages held; re-touch every {INTERVAL_SEC}s")

    cycles = 0
    while _running:
        # Sleep in 1s slices so SIGTERM is honored promptly on shutdown/restart.
        for _ in range(INTERVAL_SEC):
            if not _running:
                break
            time.sleep(1)
        if not _running:
            break
        _touch_all(buf)
        cycles += 1
        # Heartbeat hourly-ish so the log proves it's alive without spamming.
        if cycles % max(1, 3600 // INTERVAL_SEC) == 0:
            _log(f"alive: holding {TARGET_MB} MiB, {cycles} re-touch cycles")

    return 0


if __name__ == "__main__":
    sys.exit(main())
