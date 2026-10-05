"""Progress event bus: let callers see inside a download.

Why this exists
---------------
Project rule (2026-09-21): long tasks must not run as a black box. A plain
progress line in a terminal is invisible to a browser panel, and invisible
to another agent. So the engine emits structured events, and any consumer
(single-file panel, taskviz-panel, an orchestrator) can subscribe.

Two sinks, both optional and both fail-soft:
    * EventPrinter  - human-readable lines (default; preserves old behaviour)
    * PanelFeed     - POSTs JSON events to an HTTP panel

Design rules:
  - A slow or dead sink must NEVER break a download. Every emit is wrapped.
  - Events are plain dicts, JSON-serialisable, with a stable `type` field.
  - No new required dependencies: the panel sink uses urllib only.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

# Event types (stable contract; add, never rename)
EV_START = "start"          # a transfer attempt began
EV_PROGRESS = "progress"    # periodic tick while bytes flow
EV_ATTEMPT = "attempt"      # about to try a mirror / retry
EV_SOURCE = "source"        # switched to a different source
EV_RESUME = "resume"        # resumed from a partial file
EV_DONE = "done"            # transfer finished successfully
EV_FAIL = "fail"            # transfer failed permanently
EV_NOTE = "note"            # informational message


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fTB" % n


def _eta(seconds: Optional[float]) -> str:
    if not seconds or seconds != seconds or seconds == float("inf"):
        return "--:--"
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return "0:%02d" % seconds
    if seconds < 3600:
        return "%d:%02d" % (seconds // 60, seconds % 60)
    return "%d:%02d:%02d" % (seconds // 3600, (seconds % 3600) // 60,
                             seconds % 60)


class EventBus:
    """Fan-out to sinks. Thread-safe; never raises into the caller."""

    def __init__(self, job: str = "download"):
        self.job = job
        self.t0 = time.time()
        self._lock = threading.Lock()
        self._sinks: List[Any] = []
        self._last_emit = 0.0
        self._interval = 0.25
        self._state: Dict[str, Any] = {"done": 0, "total": 0}

    # -- sink management -------------------------------------------------
    def add_printer(self, stream=None, quiet: bool = False) -> "EventBus":
        if not quiet:
            self._sinks.append(EventPrinter(stream or sys.stderr))
        return self

    def add_panel(self, endpoint: str, timeout: float = 1.5) -> "EventBus":
        self._sinks.append(PanelFeed(endpoint, timeout))
        return self

    def add_callback(self, fn: Callable[[Dict[str, Any]], None]) -> "EventBus":
        self._sinks.append(fn)
        return self

    # -- emit ------------------------------------------------------------
    def emit(self, etype: str, **kw) -> None:
        evt = {
            "type": etype,
            "job": self.job,
            "t": round(time.time() - self.t0, 2),
        }
        evt.update(kw)
        with self._lock:
            sinks = list(self._sinks)
        for s in sinks:
            try:
                if callable(s) and not hasattr(s, "handle"):
                    s(evt)
                else:
                    s.handle(evt)
            except Exception:
                # A broken sink must never kill a download.
                pass

    def progress(self, done: int, total: int, speed: float = 0.0,
                 force: bool = False) -> None:
        """Emit a progress tick, rate-limited to avoid flooding the sink."""
        now = time.time()
        with self._lock:
            if not force and now - self._last_emit < self._interval:
                self._state["done"] = done
                self._state["total"] = total
                return
            self._last_emit = now
        eta = (total - done) / speed if speed > 1 and total else None
        self.emit(EV_PROGRESS, done=done, total=total,
                  done_h=_human(done), total_h=_human(total),
                  speed=round(speed, 2), speed_h="%s/s" % _human(speed),
                  eta_s=(round(eta) if eta else None), eta_h=_eta(eta),
                  pct=(round(100.0 * done / total, 1) if total else None))


class EventPrinter:
    """Single-line progress on stderr (TTY) or periodic lines (log file)."""

    def __init__(self, stream):
        self.stream = stream
        try:
            self.tty = stream.isatty()
        except Exception:
            self.tty = False
        self._last = 0.0

    def handle(self, evt: Dict[str, Any]) -> None:
        et = evt.get("type")
        if et == EV_PROGRESS:
            now = time.time()
            if self.tty:
                bar_w = 24
                pct = (evt.get("pct") or 0) / 100.0
                filled = int(bar_w * min(pct, 1.0))
                bar = "#" * filled + "-" * (bar_w - filled)
                self.stream.write(
                    "\r[%s] %s %s/%s %s eta %s" % (
                        evt["job"], bar, evt["done_h"], evt["total_h"],
                        evt["speed_h"], evt["eta_h"]))
                self.stream.flush()
            elif now - self._last >= 5.0:
                self._last = now
                self.stream.write("[%s] %s/%s %s\n" % (
                    evt["job"], evt["done_h"], evt["total_h"],
                    evt["speed_h"]))
                self.stream.flush()
        elif et == EV_DONE:
            if self.tty:
                self.stream.write("\r" + " " * 78 + "\r")
            self.stream.write("[%s] DONE %s in %ss via %s\n" % (
                evt["job"], evt.get("done_h", "?"), evt.get("elapsed", "?"),
                evt.get("source", "?")))
            self.stream.flush()
        elif et in (EV_ATTEMPT, EV_SOURCE, EV_RESUME, EV_FAIL, EV_NOTE):
            self.stream.write("[%s] %s: %s\n" % (
                evt["job"], et.upper(), evt.get("message", "")))
            self.stream.flush()


class PanelFeed:
    """POST each event to an HTTP panel. Fire-and-forget, short timeout."""

    def __init__(self, endpoint: str, timeout: float = 1.5):
        self.endpoint = endpoint
        self.timeout = timeout
        self._dropped = 0

    def handle(self, evt: Dict[str, Any]) -> None:
        body = json.dumps(evt, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint, data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=self.timeout).close()
        except urllib.error.HTTPError:
            self._dropped += 1
        except Exception:
            self._dropped += 1


# ---------------------------------------------------------------------------
# Minimal built-in panel: a single HTML file + tiny HTTP server.
# Standard library only, so it works in any interpreter that can run dl.py.
# ---------------------------------------------------------------------------

PANEL_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>dl progress</title>
<style>
 :root{--bg:#11131a;--card:#1a1d27;--fg:#e6e8ee;--dim:#8b90a0;--ok:#3ddc97;
       --warn:#ffb020;--err:#ff5c5c;--acc:#5b8cff}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);
      font:14px/1.5 -apple-system,Segoe UI,Roboto,"Microsoft Yahei",sans-serif}
 .wrap{max-width:860px;margin:24px auto;padding:0 16px}
 h1{font-size:18px;margin:0 0 4px}
 .sub{color:var(--dim);font-size:12px;margin-bottom:18px}
 .card{background:var(--card);border:1px solid #262a36;border-radius:10px;
       padding:18px;margin-bottom:14px}
 .job{font-weight:600;margin-bottom:10px;word-break:break-all}
 .bar{height:8px;background:#2a2f3d;border-radius:5px;overflow:hidden}
 .fill{height:100%;background:linear-gradient(90deg,var(--acc),var(--ok));
       width:0;transition:width .2s}
 .row{display:flex;gap:20px;flex-wrap:wrap;margin-top:12px}
 .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.4px}
 .v{font-size:17px;font-variant-numeric:tabular-nums;margin-top:2px}
 .log{margin-top:8px;font:12px/1.7 ui-monospace,Consolas,monospace;
      color:var(--dim);max-height:220px;overflow:auto}
 .log div{border-left:2px solid #2f3545;padding-left:8px;margin-bottom:2px}
 .log .SRC{border-color:var(--acc)} .log .DONE{border-color:var(--ok)}
 .log .FAIL{border-color:var(--err)} .log .RESUME{border-color:var(--warn)}
 .empty{color:var(--dim);text-align:center;padding:30px 0}
</style></head><body>
<div class="wrap">
  <h1>dl &mdash; download progress</h1>
  <div class="sub">updated <span id="upd">never</span> &middot; auto-refresh 1s</div>
  <div id="body"><div class="card empty">waiting for a download&hellip;</div></div>
</div>
<script>
const $=id=>document.getElementById(id);
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function card(j){
  const p=j.pct==null?0:j.pct;
  let h='<div class="card"><div class="job">'+esc(j.job)+'</div><div class="bar">';
  h+='<div class="fill" style="width:'+p+'%"></div></div><div class="row">';
  const cell=(k,v)=>'<div><div class="k">'+k+'</div><div class="v">'+esc(v)+'</div></div>';
  h+=cell('done',j.done_h+'') ;
  h+=cell('total',j.total_h||'?');
  h+=cell('speed',j.speed_h||'-');
  h+=cell('eta',j.eta_h||'-');
  h+=cell('source',j.source||'-');
  h+='</div>';
  if(j.log&&j.log.length){
    h+='<div class="log">'+j.log.slice(-40).map(l=>
      '<div class="'+esc(l.cls)+'">'+esc(l.msg)+'</div>').join('')+'</div>';
  }
  return h+'</div>';
}
async function tick(){
  try{
    const r=await fetch('state?t='+Date.now(),{cache:'no-store'});
    const d=await r.json();
    $('upd').textContent=new Date().toLocaleTimeString();
    $('body').innerHTML = (d.jobs&&d.jobs.length)
      ? d.jobs.map(card).join('') : '<div class="card empty">waiting for a download&hellip;</div>';
  }catch(e){/* panel gone: keep the last render, no noise */}
}
tick(); setInterval(tick,1000);
</script></body></html>
"""


class PanelServer:
    """Tiny stdlib HTTP server that serves the page and collects events."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8790):
        self.host, self.port = host, port
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def bus(self, job: str = "download") -> EventBus:
        b = EventBus(job)
        b.add_panel("http://%s:%d/event" % (self.host, self.port))
        return b

    def handle(self, evt: Dict[str, Any]) -> None:
        job = evt.get("job", "download")
        with self._lock:
            j = self.jobs.setdefault(job, {"job": job, "log": [],
                                           "done": 0, "total": 0})
            et = evt.get("type")
            if et == EV_PROGRESS:
                j.update({k: evt.get(k) for k in
                          ("done", "total", "done_h", "total_h", "speed",
                           "speed_h", "eta_h", "pct") if evt.get(k) is not None})
            elif et in (EV_DONE, EV_FAIL):
                j["status"] = "done" if et == EV_DONE else "fail"
                if evt.get("source"):
                    j["source"] = evt["source"]
                j["done_h"] = evt.get("done_h", j.get("done_h"))
            else:
                j["source"] = evt.get("source", j.get("source"))
            # Log only milestones. Logging every progress tick would flood the
            # panel's event list and make the interesting lines impossible to
            # find; the bar itself already shows the fine-grained movement.
            if et in (EV_PROGRESS, EV_DONE):
                msg = ""
            else:
                msg = evt.get("message") or _describe(evt)
            if msg:
                j["log"].append({"cls": _cls_of(et), "msg": msg})
                del j["log"][:-60]

    def serve_forever(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass  # silence per-request logging

            def _send(self, code, body, ctype="application/json; charset=utf-8"):
                b = body if isinstance(body, bytes) else body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                p = self.path.split("?")[0]
                if p in ("/", "/index.html"):
                    self._send(200, PANEL_HTML, "text/html; charset=utf-8")
                elif p == "/state":
                    with outer._lock:
                        self._send(200, json.dumps(
                            {"jobs": list(outer.jobs.values())},
                            ensure_ascii=False))
                elif p == "/health":
                    self._send(200, '{"ok":true}')
                else:
                    self._send(404, "not found", "text/plain")

            def do_POST(self):
                p = self.path.split("?")[0]
                if p == "/event":
                    n = int(self.headers.get("Content-Length") or 0)
                    try:
                        outer.handle(json.loads(self.rfile.read(n)))
                        self._send(200, '{"ok":true}')
                    except Exception as e:
                        self._send(400, json.dumps({"error": str(e)}))
                else:
                    self._send(404, "not found", "text/plain")

        srv = ThreadingHTTPServer((self.host, self.port), H)
        srv.daemon_threads = True
        srv.serve_forever()


def _cls_of(et: str) -> str:
    return {EV_SOURCE: "SRC", EV_DONE: "DONE", EV_FAIL: "FAIL",
            EV_RESUME: "RESUME"}.get(et, "")


def _describe(evt: Dict[str, Any]) -> str:
    et = evt.get("type")
    if et == EV_ATTEMPT:
        return "attempt %s: %s" % (evt.get("n"), evt.get("message", ""))
    if et == EV_RESUME:
        return "resumed from %s" % evt.get("from_h", "?")
    if et == EV_NOTE:
        return evt.get("message", "")
    return ""
