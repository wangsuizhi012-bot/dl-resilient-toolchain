"""Traffic budget: spend the proxy only where it is actually needed.

The problem this solves
-----------------------
Proxy plans are metered in bytes. A naive "route everything through the proxy"
setup burns the quota on multi-GB weight files and then has nothing left.

The measured reality on this box (2026-10-03):
  - hf-mirror direct:  6.24 MB/s, works, no quota
  - huggingface.co:    TLS verify fails direct AND via proxy
  - hf-mirror supports Range/206 -> resume works without any proxy
  - 8 concurrent shards: 6.67 MB/s aggregate vs 6.24 MB/s single stream
    -> the mirror is already the bottleneck; more connections buy nothing

Conclusion: for the workloads on this machine, the proxy is a *fallback for
reachability*, not a speed booster. So the budget policy is:

    META traffic  (repo metadata, file lists, HEAD)  -> proxy is CHEAP here,
                                                       use it freely
    BLOB traffic  (weights, wheels, archives)         -> this is what drains
                                                       a quota, so it goes
                                                       DIRECT by default and
                                                       only touches the proxy
                                                       when direct is blocked

That ordering is the whole trick: you keep the proxy for the 1% of requests
that genuinely need it, instead of the 99% that do not.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

# Hosts reachable directly on this box. Blob traffic to these never touches
# the proxy, which is what keeps a metered plan from draining.
DIRECT_OK_HOSTS = {
    "hf-mirror.com",
    "pypi.tuna.tsinghua.edu.cn",
    "mirrors.aliyun.com",
    "mirrors.cloud.tencent.com",
    "mirrors.ustc.edu.cn",
    "repo.huaweicloud.com",
    "mirrors.huaweicloud.com",
    "www.modelscope.cn",
    "pypi.org",
    "files.pythonhosted.org",
}

# Hosts that are known-broken direct and *may* work through a proxy.
PROXY_ONLY_HOSTS = {
    "huggingface.co",
    "cdn-lfs.huggingface.co",
    "cas-server.xethub.hf.co",
    "github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
}

PROXY_ENV_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")


def host_of(url: str) -> str:
    try:
        return url.split("/")[2].lower()
    except (IndexError, IndexError):  # pragma: no cover
        return ""


def classify_url(url: str) -> str:
    """'meta' (cheap), 'blob' (expensive), or 'unknown'."""
    h = host_of(url)
    if h in PROXY_ONLY_HOSTS:
        return "unknown"          # must go through proxy to even resolve
    if any(k in url for k in ("/resolve/", "/repos/", "/packages/")):
        return "blob"
    if any(k in url for k in ("/api/", "/simple/", "config.json")):
        return "meta"
    return "blob"                 # default to expensive, it is safer


@dataclass
class Budget:
    """Track bytes sent through the proxy and refuse to exceed a cap."""

    limit_bytes: int = 0            # 0 = unlimited
    used_bytes: int = 0
    on_exceed: str = "direct"       # 'direct' | 'abort'

    @property
    def remaining(self) -> int:
        if not self.limit_bytes:
            return 1 << 62
        return max(0, self.limit_bytes - self.used_bytes)

    def charge(self, n: int) -> bool:
        """Record n bytes. Returns False if the cap is already blown."""
        self.used_bytes += n
        return self.used_bytes <= self.limit_bytes

    def can_afford(self, expected: int) -> bool:
        if not self.limit_bytes:
            return True
        return expected <= self.remaining

    def summary(self) -> str:
        if not self.limit_bytes:
            return "proxy traffic: %s (unlimited)" % human(self.used_bytes)
        return "proxy traffic: %s / %s (%.0f%%)" % (
            human(self.used_bytes), human(self.limit_bytes),
            100.0 * self.used_bytes / self.limit_bytes)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fTB" % n


def route(url: str, *, use_proxy: bool, budget: Optional[Budget] = None,
          known_total: int = 0) -> tuple:
    """Decide (use_proxy, reason) for one URL.

    `use_proxy` is the user's master switch: they turn the proxy on when the
    direct route is blocked. We only ever *remove* traffic from the proxy,
    never add, so this can only save quota.
    """
    if not use_proxy:
        return False, "direct mode (proxy off)"
    h = host_of(url)
    if h in DIRECT_OK_HOSTS:
        return False, "%s is reachable direct -> keep proxy clean" % h
    kind = classify_url(url)
    if kind == "meta":
        return True, "metadata request is cheap via proxy"
    if budget and known_total and not budget.can_afford(known_total):
        return False, ("blob would exceed budget (%s left) -> direct"
                       % human(budget.remaining))
    return True, "blob via proxy (direct not known-good for %s)" % h


def apply_env(use_proxy: bool, port: int = 65532) -> None:
    """Set or clear proxy env vars for the current process.

    Per project rule: the proxy is a MANUAL switch. Nothing here auto-detects
    a port and turns the proxy on; the caller must pass the flag.
    """
    for k in PROXY_ENV_VARS:
        if use_proxy:
            os.environ[k] = "socks5h://127.0.0.1:%d" % port
        else:
            os.environ.pop(k, None)
