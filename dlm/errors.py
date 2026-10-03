"""Error classification.

The whole point of this module: decide RETRY vs GIVE UP before burning a
retry budget. Retrying a 403 wastes 30s and never succeeds; retrying a
connection reset succeeds on attempt 2.

Rules derived from local measurement (2026-10-03, see knowledge/2026-10-03-*):
  - Tsinghua answers 403 to a Chrome UA but 200 to `pip/xx.x`  -> 403 on a
    pypi index means "bad UA", not "try another mirror".
  - huggingface.co / github.com fail TLS verify (cert chain missing SKI)
    both direct and via proxy -> GIVE UP unless a proxy is explicitly on.
  - Mirror 404 on a *file* path often means "index synced, blob not yet"
    -> it is worth trying the next mirror.
"""
from __future__ import annotations

import errno
import socket
import ssl
from dataclasses import dataclass
from http import HTTPStatus
from typing import Optional


class Verdict:
    """What to do about a failure."""

    RETRY_SAME = "retry_same"      # transient, same mirror is fine
    RETRY_NEXT = "retry_next"      # mirror-specific, switch mirror
    RETRY_PROXY = "retry_proxy"    # needs the 65532 proxy route
    GIVE_UP = "give_up"            # fatal, stop burning time


@dataclass(frozen=True)
class Failure:
    verdict: str
    reason: str
    status: Optional[int] = None

    @property
    def fatal(self) -> bool:
        return self.verdict == Verdict.GIVE_UP


# 5xx / 429 / 408 are the classic "come back later" set.
_RETRY_STATUS = {
    HTTPStatus.REQUEST_TIMEOUT,
    HTTPStatus.TOO_MANY_REQUESTS,
    HTTPStatus.INTERNAL_SERVER_ERROR,
    HTTPStatus.BAD_GATEWAY,
    HTTPStatus.SERVICE_UNAVAILABLE,
    HTTPStatus.GATEWAY_TIMEOUT,
    HTTPStatus.INSUFFICIENT_STORAGE,
}

# TLS interception / verification problems. Retrying the same route is
# pointless; the only fix is a different network path.
# MUST be a tuple, not a set: isinstance() rejects a set as its second
# argument, and the resulting TypeError masked every transport failure as
# E_INTERNAL. Do not "simplify" these braces into {}.
_TLS_ERRNOS = (
    getattr(ssl, "SSLCertVerificationError", ssl.SSLError),
    ssl.SSLError,
    ssl.CertificateError,
)

_NET_RESET = {
    errno.ECONNRESET,
    errno.ECONNABORTED,
    errno.ECONNREFUSED,
    errno.EHOSTUNREACH,
    errno.ENETUNREACH,
    errno.ENETDOWN,
    errno.ETIMEDOUT,
    errno.EPIPE,
}


def classify_status(status: int, *, is_index: bool = False,
                    url: str = "") -> Failure:
    """Map an HTTP status onto a retry decision.

    `is_index` matters: a 403 on a package *index* is the Tsinghua UA block,
    and switching mirrors is the correct fix because the blob host 403s are
    genuinely fatal (wrong path / gated repo).
    """
    if status in _RETRY_STATUS:
        return Failure(Verdict.RETRY_SAME, "http %d" % status, status)
    if status == HTTPStatus.FORBIDDEN:
        return Failure(Verdict.RETRY_NEXT, "http 403 forbidden", status)
    if status == HTTPStatus.NOT_FOUND:
        if is_index:
            return Failure(Verdict.RETRY_NEXT, "http 404 index", status)
        return Failure(Verdict.RETRY_NEXT, "http 404 blob (mirror not synced)", status)
    if status == HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE:
        # Server disagrees with our resume offset: the partial file is from a
        # different build. Only a full restart can fix this.
        return Failure(Verdict.GIVE_UP, "http 416 range not satisfiable -> restart", status)
    if status == HTTPStatus.UNAUTHORIZED:
        return Failure(Verdict.GIVE_UP, "http 401 needs a token", status)
    if 400 <= status < 500:
        return Failure(Verdict.GIVE_UP, "http %d client error" % status, status)
    if status >= 500:
        return Failure(Verdict.RETRY_SAME, "http %d server" % status, status)
    return Failure(Verdict.RETRY_SAME, "http %d" % status, status)


def _unwrap(exc: BaseException) -> BaseException:
    """Peel urllib/http wrappers so the real cause is visible.

    urllib raises URLError(<reason>) where <reason> is often an SSLError or a
    ConnectionRefusedError. Without unwrapping, every transport failure looks
    like a generic URLError and callers cannot tell TLS from a dead port.
    """
    seen = 0
    while seen < 5:
        nxt = getattr(exc, "reason", None)
        if isinstance(nxt, BaseException) and nxt is not exc:
            exc = nxt
            seen += 1
            continue
        return exc
    return exc


def classify_exception(exc: BaseException) -> Failure:
    """Map a transport-level exception onto a retry decision."""
    inner = _unwrap(exc)
    if isinstance(inner, _TLS_ERRNOS):
        return Failure(Verdict.RETRY_PROXY,
                       "tls verify failed (needs proxy route)")
    # urllib's URLError keeps the socket error as .reason.
    if isinstance(inner, socket.timeout):
        return Failure(Verdict.RETRY_SAME, "read timeout")
    if isinstance(inner, socket.gaierror):
        return Failure(Verdict.RETRY_NEXT, "dns failure")
    if isinstance(inner, OSError) and inner.errno in _NET_RESET:
        return Failure(Verdict.RETRY_SAME,
                       "net %s" % errno.errorcode.get(inner.errno, inner.errno))
    if isinstance(inner, ConnectionRefusedError):
        return Failure(Verdict.RETRY_SAME, "connection refused")
    if isinstance(inner, OSError):
        return Failure(Verdict.RETRY_SAME, "oserror %s" % (inner.errno,))
    if isinstance(exc, MemoryError):
        return Failure(Verdict.GIVE_UP, "out of memory")
    return Failure(Verdict.RETRY_SAME, "unknown: %s" % type(exc).__name__)
