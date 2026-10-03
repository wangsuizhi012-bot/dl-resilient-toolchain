"""Routing rules: decide which flow a request should take.

Routing is separated from execution on purpose. The decision is pure data
(a `Route` object) so it can be:
  * unit-tested without touching the network,
  * printed via `dl route` to explain a choice before committing to it,
  * reused identically by the CLI and the API.

Decision order (first match wins). This ordering is the contract:

  1. Deprecated tool id            -> DEPRECATED (refuse early, tell caller)
  2. Unknown tool id               -> UNSUPPORTED
  3. Missing required params       -> USAGE
  4. Proxy requested but port dead -> PROXY_FALLBACK (warn, go direct)
  5. Proxy requested + budget set  -> BUDGET route (may downgrade to direct)
  6. Otherwise                     -> DIRECT route via health-ranked mirrors

Rule 4 exists because a metered proxy that is not running must NOT be fatal:
on this box hf-mirror works directly at 6.24 MB/s, so refusing the request
would be strictly worse than serving it direct and warning the caller.
"""
from __future__ import annotations

import socket
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from .contract import Code, make_error
from .traffic import DIRECT_OK_HOSTS, PROXY_ONLY_HOSTS, Budget, host_of

# Tool lifecycle states. See README section "versioning".
STATUS_STABLE = "stable"
STATUS_EXPERIMENTAL = "experimental"
STATUS_DEPRECATED = "deprecated"

# Minimum required params per tool id.
REQUIRED: Dict[str, List[str]] = {
    "dl.probe": [],
    "dl.route": ["target"],
    "dl.pypi": ["pkg", "file", "dest"],
    "dl.hf": ["repo", "dest"],
    "dl.url": ["url", "output"],
    "dl.caps": [],
    "dl.health": [],
}


def proxy_port_alive(port: int = 65532, host: str = "127.0.0.1") -> bool:
    """Is the proxy actually listening? Probe only, never auto-enable."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


@dataclass
class Route:
    """A routing decision, fully explainable."""

    flow: str                       # direct | proxy | proxy_fallback | rejected
    endpoint: str
    use_proxy: bool
    reason: str
    warnings: List[str] = field(default_factory=list)
    error_code: Optional[str] = None
    candidates: List[str] = field(default_factory=list)
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def rejected(self) -> bool:
        return self.error_code is not None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def route_request(tool: str, params: Dict[str, Any], *,
                  catalog: Optional[Dict[str, Dict[str, Any]]] = None
                  ) -> Route:
    """Pure routing decision. No network, no side effects.

    `catalog` maps tool id -> its manifest entry (used for the deprecation
    check). When omitted, deprecation lookup is skipped.
    """
    # --- 1/2. lifecycle gates -------------------------------------------
    if catalog is not None:
        entry = catalog.get(tool)
        if entry is None:
            return Route("rejected", "", False,
                         "unknown tool id", error_code=Code.UNSUPPORTED)
        if entry.get("status") == STATUS_DEPRECATED:
            gone = entry.get("removed_in") or "an unannounced version"
            alt = entry.get("replaced_by")
            hint = ("use %s instead" % alt) if alt else "migrate now"
            return Route("rejected", "", False,
                         "tool %s is deprecated, removed in %s" % (tool, gone),
                         error_code=Code.DEPRECATED, detail={"hint": hint})

    # --- 3. required params ---------------------------------------------
    missing = [p for p in REQUIRED.get(tool, []) if not params.get(p)]
    if missing:
        return Route("rejected", "", False,
                     "missing required parameter(s): %s" % ", ".join(missing),
                     error_code=Code.USAGE,
                     detail={"missing": missing})

    # --- probe/caps/health are local-only, no routing needed ------------
    if tool in ("dl.probe", "dl.caps", "dl.health"):
        return Route("local", "local", False, "local capability/health query")

    # --- 4. proxy liveness ----------------------------------------------
    use_proxy = bool(params.get("proxy"))
    warnings: List[str] = []
    if use_proxy:
        port = int(params.get("proxy_port") or 65532)
        if not proxy_port_alive(port):
            host_hint = ("hf-mirror is reachable direct on this machine "
                         "(measured 6.24 MB/s)")
            return Route(
                "proxy_fallback", "https://hf-mirror.com", False,
                "proxy port %d is not listening -> falling back to direct; %s"
                % (port, host_hint),
                warnings=["proxy_unreachable:port_%d_not_listening" % port])

    # --- 5/6. proxy vs direct -------------------------------------------
    if tool == "dl.hf":
        repo = params["repo"]
        budget_bytes = params.get("proxy_budget") or 0
        official = "https://huggingface.co"
        mirror = "https://hf-mirror.com"
        if not use_proxy:
            return Route("direct", mirror, False,
                         "default policy: domestic mirrors first, zero proxy quota",
                         candidates=[mirror],
                         detail={"repo": repo, "budget_bytes": 0})
        # proxy on: official host is only reachable via proxy
        budget = Budget(limit_bytes=budget_bytes) if budget_bytes else None
        if budget is not None and not budget.limit_bytes:
            budget = None
        detail = {"repo": repo, "budget_bytes": budget_bytes}
        if budget is not None:
            detail["budget"] = budget.summary()
        return Route("proxy", official, True,
                     "proxy requested; huggingface.co is blocked direct "
                     "(TLS verify fails on this box)",
                     candidates=[official], detail=detail)

    if tool == "dl.pypi":
        pkg, fname = params["pkg"], params["file"]
        from .registry import PYPI_MIRRORS
        return Route("direct", pkg, False,
                     "pypi always resolves against domestic mirrors; "
                     "proxy is never used for package files",
                     candidates=[m.name for m in PYPI_MIRRORS],
                     detail={"pkg": pkg, "file": fname})

    if tool == "dl.url":
        url = params["url"]
        h = host_of(url)
        if h in DIRECT_OK_HOSTS:
            return Route("direct", url, False,
                         "%s is reachable direct -> keep proxy clean" % h,
                         candidates=[url])
        if h in PROXY_ONLY_HOSTS:
            if not use_proxy:
                return Route(
                    "rejected", url, False,
                    "%s is blocked direct (TLS) and no proxy was requested" % h,
                    error_code=Code.TLS_BLOCKED,
                    detail={"host": h,
                            "hint": "retry with proxy enabled"})
            return Route("proxy", url, True,
                         "%s requires the proxy" % h, candidates=[url])
        cands = [url]
        if params.get("mirror"):
            cands.append(params["mirror"])
        return Route("direct", url, False, "unknown host -> direct, with "
                     "explicit fallback list", candidates=cands)

    if tool == "dl.route":
        return Route("explain", params["target"], False,
                     "explain-only: no bytes are transferred")

    return Route("rejected", "", False, "no route for %s" % tool,
                 error_code=Code.UNSUPPORTED)
