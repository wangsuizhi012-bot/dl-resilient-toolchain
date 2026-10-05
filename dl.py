#!/usr/bin/env python
"""dl - resilient download toolchain (CLI shell over dlm.api).

Design rule: this file contains NO download logic. Every subcommand builds a
params dict and calls `dlm.api.run()`. That is what keeps CLI and API
behaviour identical: there is only one implementation.

Two output modes for every command:
    (default)  human-readable ASCII  -> for a person
    --json     the raw envelope      -> for an agent or script

Stdout is ASCII-only on purpose (project rule 8): a GBK console mangles
non-ASCII and a mangled success message reads as a failure.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dlm.api import exit_code_for, run
from dlm.contract import TOOLCHAIN_VERSION


def _parse_size(text: str) -> int:
    """Accept 512MB / 1.5GB / 2G / 1048576 for --proxy-budget."""
    t = text.strip().upper().rstrip("B")
    mult = 1
    if t.endswith("K"):
        mult, t = 1024, t[:-1]
    elif t.endswith("M"):
        mult, t = 1024 ** 2, t[:-1]
    elif t.endswith("G"):
        mult, t = 1024 ** 3, t[:-1]
    return int(float(t) * mult)


def _emit(env, as_json: bool) -> int:
    """Print the result and return the process exit code."""
    if as_json:
        # Must emit LF-only, CRLF-free JSON. print() on Windows writes \r\n,
        # and a bare \r inside a JSON stream makes json.load() fail for every
        # caller -- which silently breaks every agent on this machine.
        # Agents pipe this straight into a parser, so byte-exactness matters.
        blob = json.dumps(env, ensure_ascii=False, indent=2)
        data = blob.replace("\r\n", "\n").replace("\r", "\n")
        out = getattr(sys.stdout, "buffer", None)
        if out is not None:
            out.write(data.encode("utf-8"))
            out.write(b"\n")
            out.flush()
        else:  # pragma: no cover - exotic stdout replacement
            sys.stdout.write(data + "\n")
            sys.stdout.flush()
        return exit_code_for(env)
    if env.get("ok"):
        res = env.get("result") or {}
        tool = env.get("tool")
        if tool == "dl.caps":
            print("dl %s  schema=%s  tools=%d  errors=%d" % (
                env["result"]["version"], env["result"]["schema_version"],
                len(env["result"]["tools"]),
                len(env["result"]["error_codes"])))
            for t in env["result"]["tools"]:
                print("  %-10s %-8s v%-7s %s" % (
                    t["id"], t["status"], t["version"], t["summary"]))
        elif tool == "dl.route":
            r = res["route"]
            print("target  : %s" % res["target"])
            print("inferred: %s" % res["inferred_tool"])
            print("flow    : %s" % r["flow"])
            print("endpoint: %s" % r["endpoint"])
            print("proxy   : %s" % r["use_proxy"])
            print("reason  : %s" % r["reason"])
            for c in r.get("candidates", []):
                print("  cand: %s" % c)
        elif tool == "dl.health":
            print("cache: %s (ttl %ds)" % (res["cache_path"], res["ttl_s"]))
            for m in res["mirrors"]:
                print("  [%s] %-12s %6s ms  age %ss" % (
                    "OK  " if m["ok"] else "FAIL", m["name"],
                    m["latency_ms"], m["age_s"]))
        elif tool == "dl.probe":
            for m in res["mirrors"]:
                print("  [%s] %-6s %-12s %6s ms  %s" % (
                    "OK  " if m["ok"] else "FAIL", m["kind"], m["name"],
                    m["latency_ms"], m["detail"]))
            s = res["summary"]
            print("  -> %d/%d healthy; fastest: %s" % (
                s["healthy"], s["total"], ", ".join(s["fastest"]) or "none"))
        else:
            print("[OK] %s" % res.get("path", ""))
            for k in ("size_human", "mirror", "resumed_from"):
                if k in res:
                    print("  %-12s %s" % (k, res[k]))
            if "files" in res:
                print("  %-12s %d" % ("files", len(res["files"])))
            print("  %-12s %.1fs  %d attempt(s)" % (
                "elapsed", env["metrics"].get("elapsed_s", 0),
                env["metrics"].get("attempts", 0)))
    else:
        err = env.get("error") or {}
        print("[FAIL] %s: %s" % (err.get("code", "?"), err.get("message", "")))
        if err.get("hint"):
            print("  hint: %s" % err["hint"])
    for w in env.get("warnings", []):
        print("  warn: %s" % w)
    # When a panel was requested, put the URL front and centre: the whole
    # point is that the user can watch the transfer instead of guessing.
    for w in env.get("warnings", []):
        if "panel started at" in w:
            print("")
            print("  >>> PANEL: %s   (leave this window open to watch)" % w)
    if not as_json:
        print("  (add --json for the machine-readable envelope)")
    return exit_code_for(env)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="dl", description="Resilient download toolchain",
        epilog="Add --json to any command for the machine-readable envelope.")
    ap.add_argument("--version", action="version",
                    version="dl %s" % TOOLCHAIN_VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--json", action="store_true",
                       help="emit the raw response envelope")
        # Visibility: project rule 2026-09-21 forbids black-box long tasks.
        p.add_argument("--panel", action="store_true",
                       help="start a built-in web panel (default :8790) so the "
                            "download is visible in a browser")
        p.add_argument("--panel-port", type=int, default=8790,
                       help="port for the built-in panel")
        p.add_argument("--panel-url", default="",
                       help="report progress to an existing panel endpoint, "
                            "e.g. http://127.0.0.1:8125/event")
        return p

    p = common(sub.add_parser("probe", help="check mirror health"))
    p.add_argument("--fresh", action="store_true")
    p.add_argument("--pypi-only", action="store_true")
    p.add_argument("--proxy", action="store_true",
                   help="probe THROUGH the proxy instead of direct; use to "
                        "test whether a host is reachable via proxy only")

    p = common(sub.add_parser("route", help="explain the flow (no download)"))
    p.add_argument("target")
    p.add_argument("-p", "--proxy", action="store_true")

    p = common(sub.add_parser("pypi", help="download one PyPI file"))
    p.add_argument("pkg")
    p.add_argument("file")
    p.add_argument("-d", "--dest", required=True)
    p.add_argument("--retries", type=int, default=4)
    p.add_argument("-q", "--quiet", action="store_true")

    p = common(sub.add_parser("hf", help="snapshot a Hugging Face repo"))
    p.add_argument("repo")
    p.add_argument("-d", "--dest", required=True)
    p.add_argument("--allow", action="append")
    p.add_argument("-p", "--proxy", action="store_true")
    p.add_argument("--proxy-budget", metavar="SIZE")
    p.add_argument("--token", default="")

    p = common(sub.add_parser("url", help="download an arbitrary URL"))
    p.add_argument("url")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--mirror")
    p.add_argument("--retries", type=int, default=4)
    p.add_argument("-q", "--quiet", action="store_true")

    common(sub.add_parser("caps", help="machine-readable capability manifest"))
    p = common(sub.add_parser("health", help="cached health, no re-probe"))
    p.add_argument("--max-age", type=int, default=600)

    args = ap.parse_args(argv)
    as_json = getattr(args, "json", False)

    if args.cmd == "probe":
        if args.fresh:
            from dlm.registry import HealthCache
            HealthCache().clear()
        env = run("dl.probe", fresh=args.fresh, pypi_only=args.pypi_only,
                  proxy=args.proxy)
    elif args.cmd == "route":
        env = run("dl.route", target=args.target, proxy=args.proxy)
    elif args.cmd == "pypi":
        env = run("dl.pypi", pkg=args.pkg, file=args.file, dest=args.dest,
                  retries=args.retries, quiet=args.quiet,
                  panel=args.panel, panel_port=args.panel_port,
                  panel_url=args.panel_url)
    elif args.cmd == "hf":
        env = run("dl.hf", repo=args.repo, dest=args.dest,
                  allow=args.allow or [], proxy=args.proxy,
                  proxy_budget=_parse_size(args.proxy_budget)
                  if args.proxy_budget else 0,
                  token=args.token)
    elif args.cmd == "url":
        env = run("dl.url", url=args.url, output=args.output,
                  mirror=args.mirror, retries=args.retries, quiet=args.quiet,
                  panel=args.panel, panel_port=args.panel_port,
                  panel_url=args.panel_url)
    elif args.cmd == "caps":
        env = run("dl.caps")
    elif args.cmd == "health":
        env = run("dl.health", max_age_s=args.max_age)
    else:  # pragma: no cover - argparse enforces the choices
        return 20

    return _emit(env, as_json)


if __name__ == "__main__":
    sys.exit(main())
