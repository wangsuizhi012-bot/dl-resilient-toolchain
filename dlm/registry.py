"""Mirror registry + health probing.

Every URL template below was verified on this machine on 2026-10-03.
Do not add a mirror on faith: run `python -m dlm.cli probe` first.

Verified facts that shaped this table:
  - Tsinghua 403s a Chrome UA, 200s `pip/xx.x`. UA pinning is mandatory.
  - pypi *index* and pypi *file* live under different path roots per mirror:
      tuna    index /simple/           files /packages/
      aliyun  index /pypi/simple/     files /pypi/web/packages/
      tencent index /pypi/simple/     files /pypi/packages/       (no /web)
      ustc    index /pypi/simple/     files /pypi/web/packages/
      huawei  index /repository/pypi/simple/  files /repository/pypi/packages/
    Getting this wrong yields 404 on the blob while the index looks healthy,
    which is exactly the "mirror is slow/broken" symptom people report.
  - huggingface.co and github.com FAIL TLS verification on this box
    (cert chain missing Subject Key Identifier), direct *and* via proxy.
    They are registered but gated behind the explicit proxy flag.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

# A short, honest UA. Never a browser UA: Tsinghua 403s those.
UA = "pip/24.0"
JSON_UA = "python-requests/2.32.3"

STATE_DIR = os.path.join(os.path.expanduser("~"), ".dlm")
HEALTH_FILE = os.path.join(STATE_DIR, "health.json")
# Health results stay valid for 10 minutes: long enough that a batch run does
# not re-probe every host, short enough to notice a mirror going bad.
HEALTH_TTL = 600


@dataclass
class Mirror:
    name: str
    index_base: str      # simple-index root
    file_base: str       # blob root (may differ from index_base!)
    note: str = ""

    def index_url(self, pkg: str) -> str:
        return "%s/%s/" % (self.index_base.rstrip("/"), pkg)

    def file_url(self, path: str) -> str:
        return "%s%s" % (self.file_base.rstrip("/"), path)


# --- PyPI ------------------------------------------------------------------
# Ordered by measured index latency. All of these answered the index probe.
PYPI_MIRRORS: List[Mirror] = [
    Mirror("huawei", "https://repo.huaweicloud.com/repository/pypi/simple",
           "https://repo.huaweicloud.com/repository/pypi",
           "lowest index latency on this box (0.96s)"),
    Mirror("aliyun", "https://mirrors.aliyun.com/pypi/simple",
           "https://mirrors.aliyun.com/pypi/web",
           "fast, does not police UA"),
    Mirror("ustc", "https://mirrors.ustc.edu.cn/pypi/simple",
           "https://mirrors.ustc.edu.cn/pypi/web",
           "reliable fallback"),
    Mirror("tuna", "https://pypi.tuna.tsinghua.edu.cn/simple",
           "https://pypi.tuna.tsinghua.edu.cn",
           "403s browser UA; fine with pip UA"),
    Mirror("tencent", "https://mirrors.cloud.tencent.com/pypi/simple",
           "https://mirrors.cloud.tencent.com/pypi",
           "note: blob root has no /web segment"),
]

# --- Hugging Face ----------------------------------------------------------
# Only hf-mirror is reachable without a proxy. huggingface.co is registered so
# that --proxy has something to switch to, but it is NOT probed by default:
# probing it wastes ~2 TLS handshakes and always fails on this machine.
HF_MIRRORS: List[Mirror] = [
    Mirror("hf-mirror", "https://hf-mirror.com", "https://hf-mirror.com",
           "default; supports Range/206 so resume works"),
    Mirror("huggingface", "https://huggingface.co", "https://huggingface.co",
           "TLS verify fails on this box; needs --proxy"),
]

HF_OFFICIAL_PROXY = "huggingface"


@dataclass
class Health:
    name: str
    ok: bool
    latency_ms: int
    checked_at: float
    detail: str = ""

    def fresh(self) -> bool:
        return (time.time() - self.checked_at) < HEALTH_TTL


class HealthCache:
    """Persisted probe results, so a batch of 50 files probes once, not 50x."""

    def __init__(self, path: str = HEALTH_FILE):
        self.path = path
        self._data: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                self._data = json.load(fh)
        except (OSError, ValueError):
            self._data = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=1)
        os.replace(tmp, self.path)

    def get(self, name: str) -> Optional[Health]:
        raw = self._data.get(name)
        if not raw:
            return None
        h = Health(**raw)
        return h if h.fresh() else None

    def put(self, h: Health) -> None:
        self._data[h.name] = asdict(h)
        self._save()

    def clear(self) -> None:
        self._data = {}
        self._save()


def _probe_once(url: str, ua: str, timeout: float,
                use_proxy: bool = False) -> tuple:
    """Return (ok, latency_ms, detail). One request, no body read.

    `use_proxy=False` (the default) forces a DIRECT connection, bypassing any
    http_proxy/HTTPS_PROXY in the environment. This matters: without it, a
    machine that happens to have a proxy running reports every mirror as
    healthy, including hosts that are genuinely blocked direct. That
    misreporting then makes `dl probe` contradict a real download minutes
    later. Probe what you will actually use.
    """
    req = urllib.request.Request(url, headers={"User-Agent": ua}, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if use_proxy:
        opener = urllib.request.build_opener()
    t0 = time.perf_counter()
    try:
        with opener.open(req, timeout=timeout) as resp:
            resp.read(2048)  # force the header exchange, keep it tiny
            ok = resp.status == 200
            dt = int((time.perf_counter() - t0) * 1000)
            route = "via-proxy" if use_proxy else "direct"
            return ok, dt, "http %d %s" % (resp.status, route)
    except urllib.error.HTTPError as e:
        dt = int((time.perf_counter() - t0) * 1000)
        return False, dt, "http %d" % e.code
    except Exception as e:  # noqa: BLE001 - we want the class name
        dt = int((time.perf_counter() - t0) * 1000)
        route = "via-proxy" if use_proxy else "direct"
        return False, dt, "%s (%s)" % (type(e).__name__, route)


def probe_mirror(mirror: Mirror, *, kind: str = "pypi",
                  pkg: str = "six", timeout: float = 6.0,
                  use_proxy: bool = False) -> Health:
    """Probe a mirror's index endpoint. Cheap: one small GET.

    Default is a DIRECT probe (no proxy). Hosts that are blocked direct on
    this machine will correctly report FAIL here, which is the truth the
    router needs.
    """
    if kind == "pypi":
        url = mirror.index_url(pkg)
    else:
        url = "%s/api/models/%s" % (mirror.index_base.rstrip("/"), pkg)
    ok, ms, detail = _probe_once(url, UA, timeout, use_proxy=use_proxy)
    return Health(mirror.name, ok, ms, time.time(), detail)


def probe_all(mirrors: List[Mirror], *, kind: str = "pypi",
              pkg: str = "six", use_cache: bool = True,
              timeout: float = 6.0, use_proxy: bool = False) -> List[Health]:
    """Probe mirrors concurrently, using the cache where it is still valid.

    `use_proxy=False` (default) probes DIRECT and ignores the shared cache,
    because a cached "healthy via proxy" answer must not be reported as
    "healthy direct". Pass use_cache=False whenever the caller cares about
    the direct/proxy distinction.
    """
    cache = HealthCache()
    # Proxy-route answers are only valid for proxy-route questions.
    use_cache = use_cache and use_proxy
    results: List[Health] = []
    todo = []
    for m in mirrors:
        cached = cache.get(m.name) if use_cache else None
        if cached:
            results.append(cached)
        else:
            todo.append(m)
    if todo:
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(max_workers=min(6, len(todo))) as ex:
            futs = {ex.submit(probe_mirror, m, kind=kind, pkg=pkg,
                              timeout=timeout,
                              use_proxy=use_proxy): m for m in todo}
            for fut in cf.as_completed(futs):
                h = fut.result()
                cache.put(h)
                results.append(h)
    order = {m.name: i for i, m in enumerate(mirrors)}
    results.sort(key=lambda h: (not h.ok, h.latency_ms, order.get(h.name, 99)))
    return results


def order_by_health(mirrors: List[Mirror], health: List[Health]) -> List[Mirror]:
    """Reorder a mirror list by measured health: healthy+fast first."""
    by_name = {h.name: h for h in health}
    scored = []
    for i, m in enumerate(mirrors):
        h = by_name.get(m.name)
        if h is None:
            rank = (1, 10 ** 6, i)
        elif not h.ok:
            rank = (1, h.latency_ms, i)
        else:
            rank = (0, h.latency_ms, i)
        scored.append((rank, m))
    scored.sort(key=lambda t: t[0])
    return [m for _, m in scored]
