"""Resumable downloader with mirror failover.

Design notes that matter:
  * Resume uses HTTP Range + `If-Range: <etag>`. If the server's etag moved,
    it answers 200 (not 206) and we restart cleanly instead of appending
    garbage to a stale partial file.
  * Partial data lives in `<target>.part` plus `<target>.part.json` (meta).
    A crash leaves both on disk; the next run reads the meta and continues.
  * The UA is pinned to a short pip string. This is not cosmetic: Tsinghua
    returns 403 to a browser UA (measured 2026-10-03).
  * Progress goes to stderr as a single rewritten line: speed, done/total,
    ETA. Nothing is buffered, so a slow mirror still shows movement.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from . import errors as E
from .registry import UA, HealthCache, Mirror

CHUNK = 256 * 1024
# Backoff: 1s, 2s, 4s, 8s (capped), with jitter so parallel batch jobs do not
# synchronize into a thundering herd against a recovering mirror.
BACKOFF_BASE = 1.0
BACKOFF_CAP = 8.0


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fTB" % n


def human_time(sec: float) -> str:
    if sec < 0 or sec != sec or sec == float("inf"):
        return "--:--"
    sec = int(sec)
    if sec < 60:
        return "0:%02d" % sec
    if sec < 3600:
        return "%d:%02d" % (sec // 60, sec % 60)
    return "%d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


@dataclass
class Result:
    ok: bool
    path: str
    size: int = 0
    elapsed: float = 0.0
    attempts: int = 0
    mirror: str = ""
    reason: str = ""
    resumed_from: int = 0

    def __bool__(self) -> bool:
        return self.ok


class Progress:
    """Single-line stderr progress. Disabled when stderr is not a TTY and
    --quiet is set; otherwise it degrades to periodic lines so a log file
    still shows movement."""

    def __init__(self, label: str, total: int, *, enabled: bool = True):
        self.label = label
        self.total = total
        self.enabled = enabled and sys.stderr.isatty()
        self.plain = enabled and not sys.stderr.isatty()
        self.done = 0
        self.t0 = time.time()
        self._last = 0.0
        self._last_done = 0

    def update(self, done: int, force: bool = False) -> None:
        self.done = done
        now = time.time()
        if self.enabled:
            if not force and now - self._last < 0.25:
                return
            self._last = now
            dt = max(now - self.t0, 1e-6)
            speed = done / dt
            eta = (self.total - done) / speed if speed > 1 and self.total else 0
            bar_w = 24
            frac = (done / self.total) if self.total else 0.0
            filled = int(bar_w * min(frac, 1.0))
            bar = "#" * filled + "-" * (bar_w - filled)
            sys.stderr.write(
                "\r[%s] %s %s/%s %s/s eta %s" % (
                    self.label, bar, human(done), human(self.total),
                    human(speed), human_time(eta)))
            sys.stderr.flush()
        elif self.plain and (force or now - self._last >= 5.0):
            self._last = now
            dt = max(now - self.t0, 1e-6)
            sys.stderr.write("[%s] %s/%s %s/s\n" % (
                self.label, human(done), human(self.total), human(done / dt)))
            sys.stderr.flush()

    def done_line(self, extra: str = "") -> None:
        if self.enabled:
            sys.stderr.write("\r" + " " * 78 + "\r")
            sys.stderr.flush()
        if self.plain or extra:
            sys.stderr.write("[%s] done %s %s\n" % (
                self.label, human(self.done), extra))
            sys.stderr.flush()


class Downloader:
    def __init__(self, *, timeout: float = 30.0, max_retries: int = 4,
                 max_mirror_switches: int = 3, progress: bool = True,
                 verify_sha256: bool = True):
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_mirror_switches = max_mirror_switches
        self.progress = progress
        self.verify_sha256 = verify_sha256

    # ---- low level -------------------------------------------------------
    def _open(self, url: str, offset: int = 0, etag: str = ""):
        headers = {"User-Agent": UA, "Accept-Encoding": "identity"}
        if offset > 0:
            headers["Range"] = "bytes=%d-" % offset
            if etag:
                headers["If-Range"] = etag
        req = urllib.request.Request(url, headers=headers)
        return urllib.request.urlopen(req, timeout=self.timeout)

    def _meta_path(self, target: str) -> str:
        return target + ".part.json"

    def _load_meta(self, target: str) -> dict:
        try:
            with open(self._meta_path(target), "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save_meta(self, target: str, meta: dict) -> None:
        try:
            with open(self._meta_path(target), "w", encoding="utf-8") as fh:
                json.dump(meta, fh)
        except OSError:
            pass  # meta is an optimisation, never fatal

    def _clear_partial(self, target: str) -> None:
        for p in (target + ".part", self._meta_path(target)):
            try:
                os.remove(p)
            except OSError:
                pass

    # ---- main entry ------------------------------------------------------
    def fetch(self, urls, target: str, *, label: str = "") -> Result:
        """Download `target` trying each URL in turn.

        `urls` is an ordered list of (mirror_name, url) candidates. All of them
        must point at the *same* logical object (same path on different
        mirrors) so a fallback cannot silently substitute different content.
        """
        if isinstance(urls, tuple):
            urls = [urls]
        label = label or os.path.basename(target)
        t0 = time.time()
        attempts = 0
        switches = 0
        last = "no attempt"
        resumed_from = 0

        for idx, (mirror_name, url) in enumerate(urls):
            if switches > self.max_mirror_switches:
                break
            if idx > 0:
                switches += 1
            for attempt in range(self.max_retries):
                attempts += 1
                meta = self._load_meta(target)
                part = target + ".part"
                have = os.path.getsize(part) if os.path.exists(part) else 0
                # Only trust a partial file that came from the same URL.
                if meta.get("url") not in (None, url) and have:
                    self._clear_partial(target)
                    have = 0
                resumed_from = have

                try:
                    resp = self._open(url, have, meta.get("etag", ""))
                    code = resp.status
                    headers = resp.headers
                    total = int(headers.get("Content-Length") or 0)
                    if code == 200 and have:
                        # Server ignored our Range: the partial is unusable.
                        self._clear_partial(target)
                        have = 0
                        resp.close()
                        resp = self._open(url, 0, "")
                        total = int(resp.headers.get("Content-Length") or 0)
                    etag = headers.get("ETag", "")
                    if total and have >= total:
                        resp.close()
                        self._finalize(part, target, meta)
                        return Result(True, target, total, time.time() - t0,
                                      attempts, mirror_name, "cached-complete",
                                      resumed_from)
                    if total:
                        total = total + have
                    else:
                        total = 0

                    self._save_meta(target, {"url": url, "etag": etag,
                                             "mirror": mirror_name})
                    prog = Progress(label, total, enabled=self.progress)
                    prog.update(have)
                    mode = "ab" if have else "wb"
                    digest = hashlib.sha256() if (self.verify_sha256 and total
                                                  and not have) else None
                    with open(part, mode) as fh:
                        while True:
                            block = resp.read(CHUNK)
                            if not block:
                                break
                            fh.write(block)
                            if digest is not None:
                                digest.update(block)
                            have += len(block)
                            prog.update(have)
                    resp.close()
                    prog.done_line("via %s" % mirror_name)

                    if total and have < total:
                        last = "short read %d/%d" % (have, total)
                        self._save_meta(target, {"url": url, "etag": etag,
                                                 "mirror": mirror_name})
                        self._sleep(attempt)
                        continue
                    sha = digest.hexdigest() if digest is not None else None
                    self._finalize(part, target,
                                   {"sha256": sha} if sha else {})
                    if sha and meta.get("sha256") and meta["sha256"] != sha:
                        self._clear_partial(target)
                        return Result(False, target, have, time.time() - t0,
                                      attempts, mirror_name,
                                      "sha256 mismatch (corrupt transfer)", resumed_from)
                    return Result(True, target, have, time.time() - t0,
                                  attempts, mirror_name, "", resumed_from)

                except urllib.error.HTTPError as e:
                    f = E.classify_status(e.code)
                    last = "%s (%s)" % (f.reason, mirror_name)
                    if f.fatal:
                        return Result(False, target, 0, time.time() - t0,
                                      attempts, mirror_name, last, resumed_from)
                    if e.code in (416,):
                        self._clear_partial(target)
                    if f.verdict in (E.Verdict.RETRY_NEXT, E.Verdict.RETRY_PROXY):
                        break  # switch mirror now, do not burn retries here
                    self._sleep(attempt)
                except Exception as e:  # noqa: BLE001
                    f = E.classify_exception(e)
                    last = "%s (%s)" % (f.reason, mirror_name)
                    if f.fatal:
                        return Result(False, target, 0, time.time() - t0,
                                      attempts, mirror_name, last, resumed_from)
                    if f.verdict in (E.Verdict.RETRY_NEXT, E.Verdict.RETRY_PROXY):
                        break
                    self._sleep(attempt)

        return Result(False, target, 0, time.time() - t0, attempts, "", last,
                      resumed_from)

    def _sleep(self, attempt: int) -> None:
        delay = min(BACKOFF_BASE * (2 ** attempt), BACKOFF_CAP)
        time.sleep(delay * (0.7 + 0.6 * random.random()))

    def _finalize(self, part: str, target: str, meta: dict) -> None:
        if os.path.exists(part):
            os.replace(part, target)
        try:
            os.remove(self._meta_path(target))
        except OSError:
            pass
