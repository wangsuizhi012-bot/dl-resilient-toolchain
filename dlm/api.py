"""Toolchain API: the single execution path.

`run(tool, **params)` is the only place where work happens. The CLI is a thin
argparse shell over this function, which is what makes CLI and API behaviour
identical by construction rather than by convention.

Guarantees:
  * every call returns the envelope from `contract.envelope`
  * every call maps failures onto a stable error code + exit code
  * no call raises: exceptions are converted into error envelopes
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

from . import router as R
from .contract import (Code, TOOLCHAIN_VERSION, Timer, classify_failure,
                       envelope, make_error)
from .core import Downloader, human
from .registry import HF_MIRRORS, PYPI_MIRRORS, HealthCache, probe_all

# --- tool manifest --------------------------------------------------------
# Single source of truth for capabilities. The CLI `caps` command and the
# machine-readable manifest file are both generated from this dict, so a tool
# cannot exist in one place and be missing from the other.

TOOLS: Dict[str, Dict[str, Any]] = {
    "dl.probe": {
        "version": "1.1.0", "status": R.STATUS_STABLE, "since": "1.0.0",
        "summary": "Check mirror health and rank by measured latency. Probes "
                   "DIRECT by default; --proxy changes the question being asked",
        "params": {
            "fresh": {"type": "bool", "default": False, "cli": "--fresh",
                      "desc": "ignore the 10-minute health cache"},
            "pypi_only": {"type": "bool", "default": False,
                          "cli": "--pypi-only", "desc": "skip HF probes"},
            "proxy": {"type": "bool", "default": False, "cli": "--proxy",
                      "desc": "probe THROUGH the proxy instead of direct. "
                              "Use only to answer 'is it reachable via "
                              "proxy'; results do not reflect direct "
                              "reachability"},
        },
        "returns": "mirrors[{kind,name,ok,latency_ms,detail}], "
                   "summary{healthy,total,fastest[]}, probe_mode",
        "examples": [
            {"cli": "dl probe --fresh", "api": "run(\"dl.probe\", fresh=True)"},
            {"cli": "dl probe --proxy --fresh",
             "api": "run(\"dl.probe\", proxy=True, fresh=True)"},
        ],
        "errors": [Code.NO_HEALTHY_SOURCE],
        "network": "yes (read-only probes)",
    },
    "dl.route": {
        "version": "1.0.0", "status": R.STATUS_STABLE, "since": "1.1.0",
        "summary": "Explain which flow a target should take, without "
                   "downloading anything",
        "params": {
            "target": {"type": "str", "required": True, "cli": "positional",
                       "desc": "a pypi package name, a HF repo id "
                               "(org/name), or any http(s) URL"},
            "proxy": {"type": "bool", "default": False, "cli": "-p",
                      "desc": "assume the proxy is available (still falls "
                              "back to direct if the port is dead)"},
        },
        "returns": "route{flow,endpoint,use_proxy,reason,warnings[],candidates[]}",
        "examples": [
            {"cli": "dl route Qwen/Qwen2-0.5B",
             "api": "run(\"dl.route\", target=\"Qwen/Qwen2-0.5B\")"},
        ],
        "errors": [Code.USAGE],
        "network": "no (local decision; does one liveness probe)",
    },
    "dl.pypi": {
        "version": "1.0.0", "status": R.STATUS_STABLE, "since": "1.0.0",
        "summary": "Download one PyPI distribution file with mirror failover",
        "params": {
            "pkg": {"type": "str", "required": True, "cli": "positional",
                    "desc": "PyPI project name, e.g. six"},
            "file": {"type": "str", "required": True, "cli": "positional",
                     "desc": "exact distribution filename including "
                             "extension, e.g. six-1.16.0.tar.gz"},
            "dest": {"type": "str", "required": True, "cli": "-d/--dest",
                     "desc": "output directory (created if absent), "
                             "e.g. D:/tmp/wheels"},
            "retries": {"type": "int", "default": 4, "cli": "--retries",
                        "desc": "attempts per mirror before switching"},
            "quiet": {"type": "bool", "default": False, "cli": "-q/--quiet",
                      "desc": "suppress the progress line"},
        },
        "returns": "path, size, size_human, mirror, resumed_from",
        "examples": [
            {"cli": 'dl pypi six six-1.16.0.tar.gz -d D:/tmp',
             "api": 'run("dl.pypi", pkg="six", '
                    'file="six-1.16.0.tar.gz", dest="D:/tmp")'},
        ],
        "errors": [Code.NOT_FOUND, Code.NO_HEALTHY_SOURCE, Code.INTEGRITY,
                   Code.TIMEOUT],
        "network": "yes",
    },
    "dl.hf": {
        "version": "1.1.0", "status": R.STATUS_STABLE, "since": "1.0.0",
        "summary": "Snapshot a Hugging Face repo, direct by default so proxy "
                   "quota is not spent on blobs",
        "params": {
            "repo": {"type": "str", "required": True, "cli": "positional",
                     "desc": "Hugging Face repo id, e.g. Qwen/Qwen2-0.5B"},
            "dest": {"type": "str", "required": True, "cli": "-d/--dest",
                     "desc": "local directory to write the snapshot into "
                             "(created if absent), e.g. E:/AI/LLM/GGUF/Qwen2-0.5B"},
            "allow": {"type": "list[str]", "default": [], "cli": "--allow",
                      "desc": "repeatable glob, e.g. '*.safetensors'"},
            "proxy": {"type": "bool", "default": False, "cli": "-p",
                      "desc": "use huggingface.co via 127.0.0.1:65532"},
            "proxy_budget": {"type": "int", "default": 0,
                             "cli": "--proxy-budget",
                             "desc": "cap proxy bytes, e.g. 512MB"},
            "token": {"type": "str", "default": "", "cli": "--token",
                      "desc": "HF token for gated repos"},
        },
        "returns": "path, files[], route{flow,reason}",
        "examples": [
            {"cli": 'dl hf Qwen/Qwen2-0.5B -d E:/models/qwen --allow "*.safetensors"',
             "api": 'run("dl.hf", repo="Qwen/Qwen2-0.5B", '
                    'dest="E:/models/qwen", allow=["*.safetensors"])'},
            {"cli": 'dl hf some-org/private-model -d E:/models/p -p --token $HF_TOKEN',
             "api": 'run("dl.hf", repo="some-org/private-model", '
                    'dest="E:/models/p", proxy=True, token=os.environ["HF_TOKEN"])'},
        ],
        "errors": [Code.AUTH_REQUIRED, Code.PROXY_UNREACHABLE,
                   Code.TLS_BLOCKED, Code.TIMEOUT, Code.NOT_FOUND],
        "network": "yes",
    },
    "dl.url": {
        "version": "1.0.0", "status": R.STATUS_STABLE, "since": "1.0.0",
        "summary": "Download an arbitrary URL with resume and failover",
        "params": {
            "url": {"type": "str", "required": True, "cli": "positional",
                    "desc": "absolute http(s) URL to fetch"},
            "output": {"type": "str", "required": True, "cli": "-o/--output",
                       "desc": "destination file path (parent dirs created)",
                       },
            "mirror": {"type": "str", "default": "", "cli": "--mirror",
                       "desc": "fallback URL for the same object, used when "
                               "the primary fails"},
            "retries": {"type": "int", "default": 4, "cli": "--retries",
                        "desc": "attempts per URL before giving up"},
            "quiet": {"type": "bool", "default": False, "cli": "-q/--quiet",
                      "desc": "suppress the progress line"},
        },
        "returns": "path, size, size_human, mirror, resumed_from",
        "examples": [
            {"cli": 'dl url https://example.com/f.bin -o D:/tmp/f.bin',
             "api": 'run("dl.url", url="https://example.com/f.bin", '
                    'output="D:/tmp/f.bin")'},
        ],
        "errors": [Code.TIMEOUT, Code.INTEGRITY, Code.TLS_BLOCKED,
                   Code.NOT_FOUND],
        "network": "yes",
    },
    "dl.caps": {
        "version": "1.0.0", "status": R.STATUS_STABLE, "since": "1.1.0",
        "summary": "Return the machine-readable capability manifest",
        "params": {},
        "returns": "manifest{name,version,schema_version,tools[],error_codes{}}",
        "examples": [
            {"cli": "dl caps --json",
             "api": "run(\"dl.caps\")"},
        ],
        "errors": [],
        "network": "no",
    },
    "dl.health": {
        "version": "1.0.0", "status": R.STATUS_STABLE, "since": "1.1.0",
        "summary": "Return cached mirror health without re-probing",
        "params": {
            "max_age_s": {"type": "int", "default": 600, "cli": "--max-age",
                          "desc": "ignore cache entries older than this many "
                                  "seconds"},
        },
        "returns": "mirrors[{name,ok,latency_ms,age_s,detail}], cache_path, ttl_s",
        "examples": [
            {"cli": "dl health --max-age 60", "api": "run(\"dl.health\", max_age_s=60)"},
        ],
        "errors": [],
        "network": "no",
    },
}


def manifest() -> Dict[str, Any]:
    """The capability manifest, as a plain dict."""
    from .contract import ERROR_CATALOG, EXIT_CODES, SCHEMA_VERSION
    return {
        "name": "dl",
        "version": TOOLCHAIN_VERSION,
        "schema_version": SCHEMA_VERSION,
        "description": "Resilient download toolchain: mirror probing, "
                       "failover, resume, progress, and proxy budget control",
        "entrypoints": {
            "cli": "python dl.py <tool> [params]",
            "api": "from dlm.api import run; run('dl.hf', repo=..., dest=...)",
        },
        "invariants": [
            "CLI and API share this single execution path, so behaviour and "
            "error codes cannot diverge.",
            "Every response is the same envelope; no call raises.",
            "Routing is decided before any bytes move and is reportable "
            "via dl.route.",
            "Default route is direct; proxy is an explicit manual switch "
            "(project rule 13).",
        ],
        "tools": [
            dict({"id": tid}, **spec) for tid, spec in sorted(TOOLS.items())
        ],
        "error_codes": {
            code: dict(meta, exit=EXIT_CODES.get(code))
            for code, meta in sorted(ERROR_CATALOG.items())
        },
    }


# --- tool implementations -------------------------------------------------

def _t_probe(p: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"mirrors": []}
    warnings: List[str] = []
    use_proxy = bool(p.get("proxy"))
    out["probe_mode"] = "via-proxy" if use_proxy else "direct"
    if use_proxy:
        warnings.append("probe is running THROUGH the proxy; results do NOT "
                        "reflect direct reachability")
    # When probing direct, bypass the cache: a cached "ok" may have been
    # recorded while a proxy was up, which would misreport direct health.
    cache_ok = use_proxy and not p.get("fresh")
    hs = probe_all(PYPI_MIRRORS, kind="pypi", use_cache=cache_ok,
                   use_proxy=use_proxy)
    for h in hs:
        out["mirrors"].append({"kind": "pypi", "name": h.name, "ok": h.ok,
                               "latency_ms": h.latency_ms, "detail": h.detail})
    ok = [h for h in hs if h.ok]
    if not p.get("pypi_only"):
        hh = probe_all(HF_MIRRORS, kind="hf", pkg="bert-base-uncased",
                       use_cache=cache_ok, use_proxy=use_proxy)
        for h in hh:
            out["mirrors"].append({"kind": "hf", "name": h.name, "ok": h.ok,
                                   "latency_ms": h.latency_ms,
                                   "detail": h.detail})
        if not use_proxy and not next(
                (h for h in hh if h.name == "hf-mirror" and h.ok), None):
            warnings.append("hf-mirror is down; HF downloads will fail")
        if not use_proxy:
            warnings.append(
                "probe ran DIRECT: huggingface.co and github.com are blocked "
                "on this machine (TLS). If a proxy is running, "
                "'dl probe --proxy' will show them reachable -- that answers "
                "a different question than 'can I download without a proxy'.")
    out["summary"] = {"healthy": len(ok), "total": len(hs),
                      "fastest": [h.name for h in ok[:3]]}
    if not ok:
        return envelope("dl.probe", "1.1.0", "error",
                        error=make_error(Code.NO_HEALTHY_SOURCE,
                                         "no pypi mirror is healthy (%s)"
                                         % out["probe_mode"]),
                        warnings=warnings)
    return envelope("dl.probe", "1.1.0", "ok", result=out, warnings=warnings)


def _t_route(p: Dict[str, Any]) -> Dict[str, Any]:
    """Explain the flow for a target without transferring bytes."""
    target = p["target"]
    tool, params = classify_target(target)
    if p.get("proxy"):
        params["proxy"] = True
    r = R.route_request(tool, params, catalog=TOOLS)
    if r.rejected and r.error_code == Code.USAGE:
        # A bare target is legal for dl.route: infer the params.
        r.error_code = None
        r.reason = "inferred tool %s from target shape" % tool
    body = {"target": target, "inferred_tool": tool, "route": r.to_dict()}
    if r.rejected:
        return envelope("dl.route", "1.0.0", "error",
                        error=make_error(r.error_code, r.reason,
                                         hint=r.detail.get("hint", "")),
                        result=None)
    return envelope("dl.route", "1.0.0", "ok", result=body,
                    warnings=r.warnings)


def classify_target(target: str) -> tuple:
    """Infer (tool_id, params) from a target string. Pure string logic."""
    t = target.strip()
    low = t.lower()
    if low.startswith(("http://", "https://")):
        if "huggingface.co" in low or "hf-mirror.com" in low:
            parts = t.split("/")
            if len(parts) >= 5:
                return "dl.hf", {"repo": "/".join(parts[3:5]), "dest": "."}
        return "dl.url", {"url": t, "output": os.path.basename(t) or "out.bin"}
    if "/" in t:  # looks like org/repo
        return "dl.hf", {"repo": t, "dest": "."}
    return "dl.pypi", {"pkg": t, "file": "", "dest": "."}


def _t_pypi(p: Dict[str, Any]) -> Dict[str, Any]:
    from . import adapters
    dl = Downloader(max_retries=int(p.get("retries") or 4),
                    progress=not p.get("quiet"))
    res = adapters.download_pypi_file(p["pkg"], p["file"], p["dest"], dl=dl)
    if not res.ok:
        return envelope("dl.pypi", "1.0.0", "error",
                        error=classify_failure(res.reason, res.attempts),
                        metrics={"attempts": res.attempts,
                                 "elapsed_s": round(res.elapsed, 3)})
    return envelope("dl.pypi", "1.0.0", "ok",
                    result={"path": res.path, "size": res.size,
                            "size_human": human(res.size), "mirror": res.mirror,
                            "resumed_from": res.resumed_from},
                    metrics={"attempts": res.attempts,
                             "elapsed_s": round(res.elapsed, 3),
                             "bytes": res.size})


def _t_hf(p: Dict[str, Any]) -> Dict[str, Any]:
    from . import adapters
    from .traffic import apply_env
    # Pre-flight: dl.hf needs huggingface_hub, which is only installed in the
    # managed venv. Failing with a bare ModuleNotFoundError wastes the caller's
    # time, so check up front and name the interpreter that does have it.
    try:
        import huggingface_hub  # noqa: F401
    except ImportError:
        return envelope(
            "dl.hf", "1.1.0", "error",
            error=make_error(
                Code.UNSUPPORTED,
                "huggingface_hub is not installed in this interpreter",
                hint="run with the venv python: "
                     "C:/Users/wsz945/.workbuddy/binaries/python/envs/"
                     "default/Scripts/python.exe  "
                     "(or: pip install huggingface_hub). "
                     "dl.pypi and dl.url do NOT need it.",
                interpreter=sys.executable))
    repo, dest = p["repo"], p["dest"]
    route = R.route_request("dl.hf", p, catalog=TOOLS)
    if route.rejected:
        return envelope("dl.hf", "1.1.0", "error",
                        error=make_error(route.error_code, route.reason,
                                         hint=route.detail.get("hint", "")))
    if p.get("token"):
        import os as _os
        _os.environ["HF_TOKEN"] = p["token"]
    apply_env(bool(p.get("proxy")))
    try:
        path = adapters.snapshot(repo, dest,
                                 allow_patterns=p.get("allow") or None,
                                 use_proxy=bool(p.get("proxy")))
        files = []
        for root, _, names in os.walk(path):
            for n in names:
                fp = os.path.join(root, n)
                try:
                    files.append({"name": n, "size": os.path.getsize(fp)})
                except OSError:
                    pass
        return envelope("dl.hf", "1.1.0", "ok",
                        result={"path": path, "files": files,
                                "route": route.to_dict()},
                        warnings=route.warnings)
    except Exception as e:  # noqa: BLE001
        reason = "%s: %s" % (type(e).__name__, str(e)[:180])
        info = classify_failure(reason, attempts=1)
        if p.get("proxy") and info.code == Code.PROXY_UNREACHABLE:
            return envelope(
                "dl.hf", "1.1.0", "error", error=info,
                warnings=["proxy was requested but is not listening; "
                          "hf-mirror direct works on this machine"])
        return envelope("dl.hf", "1.1.0", "error", error=info,
                        warnings=route.warnings)


def _t_url(p: Dict[str, Any]) -> Dict[str, Any]:
    dl = Downloader(max_retries=int(p.get("retries") or 4),
                    progress=not p.get("quiet"))
    cands = [("origin", p["url"])]
    if p.get("mirror"):
        cands.append(("mirror", p["mirror"]))
    res = dl.fetch(cands, p["output"], label=os.path.basename(p["output"]))
    if not res.ok:
        return envelope("dl.url", "1.0.0", "error",
                        error=classify_failure(res.reason, res.attempts),
                        metrics={"attempts": res.attempts,
                                 "elapsed_s": round(res.elapsed, 3)})
    return envelope("dl.url", "1.0.0", "ok",
                    result={"path": res.path, "size": res.size,
                            "size_human": human(res.size),
                            "mirror": res.mirror,
                            "resumed_from": res.resumed_from},
                    metrics={"attempts": res.attempts,
                             "elapsed_s": round(res.elapsed, 3),
                             "bytes": res.size})


def _t_caps(_p: Dict[str, Any]) -> Dict[str, Any]:
    return envelope("dl.caps", "1.0.0", "ok", result=manifest())


def _t_health(p: Dict[str, Any]) -> Dict[str, Any]:
    cache = HealthCache()
    data = cache._data  # noqa: SLF001 - deliberate: read-only view
    out = []
    for name, raw in sorted(data.items()):
        out.append({"name": name, "ok": raw.get("ok"),
                    "latency_ms": raw.get("latency_ms"),
                    "age_s": round(__import__("time").time()
                                   - raw.get("checked_at", 0), 1),
                    "detail": raw.get("detail", "")})
    return envelope("dl.health", "1.0.0", "ok",
                    result={"mirrors": out, "cache_path": cache.path,
                            "ttl_s": 600,
                            "max_age_s": int(p.get("max_age_s") or 600)})


DISPATCH = {
    "dl.probe": _t_probe,
    "dl.route": _t_route,
    "dl.pypi": _t_pypi,
    "dl.hf": _t_hf,
    "dl.url": _t_url,
    "dl.caps": _t_caps,
    "dl.health": _t_health,
}


def run(tool: str, **params) -> Dict[str, Any]:
    """Execute a tool by id. Never raises; always returns an envelope."""
    entry = TOOLS.get(tool)
    # Lifecycle check MUST come before the dispatch lookup: a deprecated tool
    # is usually no longer in DISPATCH, and reporting it as "unknown" would
    # hide both the deprecation and its replacement.
    if entry is not None and entry.get("status") == R.STATUS_DEPRECATED:
        return envelope(tool, entry.get("version", "0"), "error",
                        error=make_error(Code.DEPRECATED,
                                         "%s is deprecated" % tool,
                                         hint="replaced_by=%s removed_in=%s"
                                              % (entry.get("replaced_by"),
                                                 entry.get("removed_in"))))
    if tool not in DISPATCH:
        return envelope(tool, "0", "error",
                        error=make_error(Code.UNSUPPORTED,
                                         "unknown tool id %r" % tool,
                                         hint="call dl.caps for the list"))
    entry = TOOLS.get(tool, {})
    # Required-param gate happens inside the router for network tools so the
    # reason is consistent; run a cheap pre-check here for local tools.
    missing = [k for k, spec in entry.get("params", {}).items()
               if spec.get("required") and not params.get(k)]
    if missing:
        return envelope(tool, entry.get("version", "0"), "error",
                        error=make_error(Code.USAGE,
                                         "missing required parameter(s): %s"
                                         % ", ".join(missing),
                                         missing=missing))
    try:
        return DISPATCH[tool](params)
    except Exception as e:  # noqa: BLE001 - the API must not raise
        return envelope(tool, entry.get("version", "0"), "error",
                        error=make_error(Code.INTERNAL,
                                         "%s: %s" % (type(e).__name__, e)))


def exit_code_for(env: Dict[str, Any]) -> int:
    from .contract import EXIT_CODES
    if env.get("ok"):
        return 0
    return EXIT_CODES.get((env.get("error") or {}).get("code", ""), 20)
