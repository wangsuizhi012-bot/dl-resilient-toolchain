"""Batch downloader with bounded concurrency and a shared health view.

Two resource guards, because "download 40 model files" is how you melt an
8 GB-VRAM laptop's free RAM and saturate a link that is already struggling:

  1. At most `concurrency` files in flight (default 3). Each streaming file
     holds one 256 KB buffer, not the whole file in memory.
  2. At most `per_host` connections to any single mirror. Without this, 12
     parallel files from one mirror is what turns a flaky mirror into a
     banned IP.

Mirrors are probed once for the whole batch (not once per file) and results
are cached on disk for 10 minutes.
"""
from __future__ import annotations

import concurrent.futures as cf
import os
import threading
import time
from typing import Callable, List, Optional, Sequence, Tuple

from .core import Downloader, Result
from .registry import Mirror, probe_all

# A mirror is a shared resource; cap per-host parallelism low on purpose.
PER_HOST_CAP = 3


class _HostSemaphore:
    """Fair-ish per-host connection cap shared across worker threads."""

    def __init__(self, cap: int = PER_HOST_CAP):
        self.cap = cap
        self._lock = threading.Lock()
        self._in_use: dict = {}

    def acquire(self, host: str) -> None:
        while True:
            with self._lock:
                if self._in_use.get(host, 0) < self.cap:
                    self._in_use[host] = self._in_use.get(host, 0) + 1
                    return
            time.sleep(0.05)

    def release(self, host: str) -> None:
        with self._lock:
            n = self._in_use.get(host, 1) - 1
            if n <= 0:
                self._in_use.pop(host, None)
            else:
                self._in_use[host] = n


def download_many(tasks: Sequence[Tuple[str, List[Tuple[str, str]]]],
                  *, concurrency: int = 3,
                  progress: bool = True,
                  on_done: Optional[Callable[[int, int, Result], None]] = None
                  ) -> List[Result]:
    """Download a batch.

    `tasks` is [(target_path, [(mirror_name, url), ...]), ...].
    Returns results in the same order as `tasks`.
    """
    host_sem = _HostSemaphore(PER_HOST_CAP)
    results: List[Optional[Result]] = [None] * len(tasks)
    lock = threading.Lock()
    done_count = [0]

    def run(i: int, target: str, cands: List[Tuple[str, str]]) -> None:
        host = cands[0][1].split("/")[2] if cands else "?"
        host_sem.acquire(host)
        try:
            dl = Downloader(progress=progress)
            res = dl.fetch(cands, target, label=os.path.basename(target))
        finally:
            host_sem.release(host)
        with lock:
            results[i] = res
            done_count[0] += 1
            if on_done:
                on_done(done_count[0], len(tasks), res)

    with cf.ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futs = [ex.submit(run, i, t, c) for i, (t, c) in enumerate(tasks)]
        cf.wait(futs)
    return [r for r in results if r is not None]


def best_mirrors(mirrors: List[Mirror], *, kind: str = "pypi",
                 pkg: str = "six", limit: int = 3) -> List[Mirror]:
    """Probe once, return the top `limit` healthy mirrors fastest-first."""
    health = probe_all(mirrors, kind=kind, pkg=pkg)
    from .registry import order_by_health
    ordered = order_by_health(mirrors, health)
    return [m for m in ordered if next(
        (h.ok for h in health if h.name == m.name), False)][:limit]
