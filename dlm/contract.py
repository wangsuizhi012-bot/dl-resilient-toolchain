"""Contract layer: error codes, response envelope, exit codes.

This module is the single source of truth for the toolchain's external
interface. Both the CLI and the Python API funnel through it, which is the
only practical way to guarantee that `dl hf ...` and `api.run(...)` cannot
drift apart.

Envelope shape (always the same, success or failure):

    {
      "schema_version": "1.0.0",
      "tool": "dl.hf",
      "tool_version": "1.0.0",
      "status": "ok" | "error" | "partial",
      "ok": true | false,
      "error": null | {"code": "...", "message": "...", "retryable": bool,
                       "hint": "...", "detail": {...}},
      "result": {...} | null,
      "metrics": {"elapsed_s": 0.0, "attempts": 0, "bytes": 0, ...},
      "warnings": ["..."]
    }

Exit codes are grouped so a caller can branch without string matching:
    0  success
    10 usage / bad arguments
    11 no healthy source
    12 not found
    13 auth required
    14 budget exceeded
    15 proxy unreachable
    16 integrity failure
    17 timeout
    18 partial (some items ok, some not)
    19 unsupported / deprecated
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "1.0.0"
TOOLCHAIN_NAME = "dl"
TOOLCHAIN_VERSION = "1.1.0"


class Code:
    """Stable error codes. Never renumber or reuse one."""

    OK = "E_OK"
    USAGE = "E_USAGE"
    NO_HEALTHY_SOURCE = "E_NO_HEALTHY_SOURCE"
    NOT_FOUND = "E_NOT_FOUND"
    AUTH_REQUIRED = "E_AUTH_REQUIRED"
    BUDGET_EXCEEDED = "E_BUDGET_EXCEEDED"
    PROXY_UNREACHABLE = "E_PROXY_UNREACHABLE"
    INTEGRITY = "E_INTEGRITY"
    TIMEOUT = "E_TIMEOUT"
    PARTIAL = "E_PARTIAL"
    UNSUPPORTED = "E_UNSUPPORTED"
    DEPRECATED = "E_DEPRECATED"
    TLS_BLOCKED = "E_TLS_BLOCKED"
    INTERNAL = "E_INTERNAL"


EXIT_CODES: Dict[str, int] = {
    Code.OK: 0,
    Code.USAGE: 10,
    Code.NO_HEALTHY_SOURCE: 11,
    Code.NOT_FOUND: 12,
    Code.AUTH_REQUIRED: 13,
    Code.BUDGET_EXCEEDED: 14,
    Code.PROXY_UNREACHABLE: 15,
    Code.INTEGRITY: 16,
    Code.TIMEOUT: 17,
    Code.PARTIAL: 18,
    Code.UNSUPPORTED: 19,
    Code.DEPRECATED: 19,
    Code.TLS_BLOCKED: 15,
    Code.INTERNAL: 20,
}

# Machine-readable meaning, shipped in the manifest so callers can branch
# programmatically without hardcoding our strings.
ERROR_CATALOG: Dict[str, Dict[str, Any]] = {
    Code.OK: {"exit": 0, "retryable": False, "meaning": "success"},
    Code.USAGE: {"exit": 10, "retryable": False,
                 "meaning": "bad arguments or missing required field"},
    Code.NO_HEALTHY_SOURCE: {"exit": 11, "retryable": True,
                             "meaning": "every candidate mirror failed; retry later"},
    Code.NOT_FOUND: {"exit": 12, "retryable": False,
                     "meaning": "object does not exist on any source"},
    Code.AUTH_REQUIRED: {"exit": 13, "retryable": False,
                         "meaning": "needs an HF token; supply --token"},
    Code.BUDGET_EXCEEDED: {"exit": 14, "retryable": False,
                           "meaning": "proxy budget would be exceeded"},
    Code.PROXY_UNREACHABLE: {"exit": 15, "retryable": True,
                             "meaning": "proxy port not listening"},
    Code.TLS_BLOCKED: {"exit": 15, "retryable": True,
                       "meaning": "TLS verify failed; route is intercepted"},
    Code.INTEGRITY: {"exit": 16, "retryable": True,
                     "meaning": "checksum mismatch; corrupt transfer"},
    Code.TIMEOUT: {"exit": 17, "retryable": True,
                   "meaning": "no data within the timeout window"},
    Code.PARTIAL: {"exit": 18, "retryable": True,
                   "meaning": "batch finished with some items failed"},
    Code.UNSUPPORTED: {"exit": 19, "retryable": False,
                       "meaning": "unknown tool id or removed capability"},
    Code.DEPRECATED: {"exit": 19, "retryable": False,
                      "meaning": "tool is deprecated; see removed_in/replaced_by"},
    Code.INTERNAL: {"exit": 20, "retryable": False, "meaning": "unexpected error"},
}


@dataclass
class ErrorInfo:
    code: str
    message: str
    retryable: bool = False
    hint: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def make_error(code: str, message: str, *, hint: str = "",
               retryable: Optional[bool] = None, **detail) -> ErrorInfo:
    if retryable is None:
        retryable = ERROR_CATALOG.get(code, {}).get("retryable", False)
    return ErrorInfo(code=code, message=message, retryable=retryable,
                     hint=hint, detail=detail)


def envelope(tool: str, tool_version: str, status: str, *,
             result: Optional[Dict[str, Any]] = None,
             error: Optional[ErrorInfo] = None,
             metrics: Optional[Dict[str, Any]] = None,
             warnings: Optional[List[str]] = None) -> Dict[str, Any]:
    """Build the response envelope. Every tool returns exactly this shape."""
    ok = (status == "ok")
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": tool,
        "tool_version": tool_version,
        "status": status,
        "ok": ok,
        "error": error.to_dict() if error else None,
        "result": result if ok else None,
        "metrics": metrics or {},
        "warnings": warnings or [],
    }


def classify_failure(reason: str, attempts: int = 0) -> ErrorInfo:
    """Map a human reason string from the engine onto a stable error code.

    The engine already produces good reasons; this keeps the mapping in one
    place so the API and CLI never disagree about what a failure means.
    """
    r = (reason or "").lower()
    if not r:
        return make_error(Code.INTERNAL, "unknown failure")
    if any(k in r for k in ("tls", "certificate", "ssl", "verify failed")):
        return make_error(Code.TLS_BLOCKED, reason,
                          hint="route via proxy (-p) if the proxy is up")
    if "401" in r or "token" in r:
        return make_error(Code.AUTH_REQUIRED, reason,
                          hint="pass --token <HF_TOKEN> for gated repos")
    if "404" in r or "not found" in r or "no mirror carries" in r:
        return make_error(Code.NOT_FOUND, reason)
    if "416" in r:
        return make_error(Code.INTEGRITY, reason,
                          hint="stale partial file; it has been discarded")
    # Connection-level failures must be checked BEFORE the generic
    # no-healthy-source rule, otherwise "net WSAECONNREFUSED" is swallowed
    # by the mirror catch-all and the caller loses the actionable cause.
    if any(k in r for k in ("connection refused", "refused", "10061",
                            "connecterror", "tunnel connection failed",
                            "wsaconnrefused", "econnrefused")):
        return make_error(Code.PROXY_UNREACHABLE, reason,
                          hint="the proxy/target port is not listening")
    # An OSError with no errno surfaces when an ambient proxy env var
    # (e.g. an injected HTTPS_PROXY) intercepts the connection. It is a
    # transport failure, not an internal bug, so it must not be E_INTERNAL.
    if r.startswith("oserror none") or "oserror" in r:
        return make_error(Code.NO_HEALTHY_SOURCE, reason,
                          hint="transport failed; check proxy env vars "
                               "(HTTP_PROXY/HTTPS_PROXY) or connectivity")
    if "timeout" in r or "timed out" in r:
        return make_error(Code.TIMEOUT, reason)
    if "sha256" in r or "mismatch" in r or "corrupt" in r:
        return make_error(Code.INTEGRITY, reason)
    if "dns" in r or "name resolution" in r or "getaddrinfo" in r:
        return make_error(Code.NO_HEALTHY_SOURCE, reason,
                          hint="DNS failure; check connectivity or use a mirror")
    if "budget" in r:
        return make_error(Code.BUDGET_EXCEEDED, reason)
    if "mirror" in r or "all candidates" in r or "attempts" in r:
        return make_error(Code.NO_HEALTHY_SOURCE, reason, attempts=attempts)
    return make_error(Code.NO_HEALTHY_SOURCE if attempts else Code.INTERNAL,
                      reason, attempts=attempts)


class Timer:
    """Context manager producing the `metrics` block."""

    def __init__(self):
        self.t0 = time.time()
        self.extra: Dict[str, Any] = {}

    def __enter__(self) -> "Timer":
        return self

    def __exit__(self, *exc) -> None:
        self.elapsed = time.time() - self.t0

    def metrics(self, **kw) -> Dict[str, Any]:
        return {"elapsed_s": round(time.time() - self.t0, 3), **self.extra, **kw}
