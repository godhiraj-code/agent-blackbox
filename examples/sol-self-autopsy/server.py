"""Deterministic local incident lab; standard library only."""
from __future__ import annotations

import argparse
import html
import json
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
TRACE = ROOT / "run-trace.jsonl"
LOCK = threading.Lock()
STATE = {"flag": "checkout_guard", "value": "off", "version": 7}
SEQ = 0


def reset() -> dict:
    global SEQ
    with LOCK:
        STATE.update(flag="checkout_guard", value="off", version=7)
        SEQ = 0
        TRACE.write_text("", encoding="utf-8")
        return dict(STATE)


def event(kind: str, message: str, **data) -> dict:
    global SEQ
    with LOCK:
        SEQ += 1
        row = {"schema_version": "1.0", "seq": SEQ,
               "timestamp": f"2026-07-13T12:00:{SEQ:02d}.000Z",
               "event": kind, "source": "sol-self-autopsy",
               "message": html.escape(str(message)), "data": data}
        with TRACE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        return row


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, status: int, body: dict):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(raw)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/state":
            with LOCK: body = dict(STATE)
            event("backend_state_read", "Independent state read", **body)
            return self._json(200, body)
        if path == "/api/trace":
            rows = [json.loads(x) for x in TRACE.read_text(encoding="utf-8").splitlines()] if TRACE.exists() else []
            return self._json(200, {"events": rows})
        if path == "/": self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        n = int(self.headers.get("Content-Length", "0"))
        try: body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError: return self._json(400, {"error": "invalid_json"})
        if path == "/api/reset":
            return self._json(200, reset())
        if path == "/api/event":
            allowed = {"ui_signal", "agent_claim", "contradiction", "diagnosis", "patch", "verification"}
            kind = str(body.get("event", ""))
            if kind not in allowed:
                return self._json(400, {"error": "unsupported_event"})
            row = event(kind, str(body.get("message", "")), **dict(body.get("data") or {}))
            return self._json(201, {"accepted": True, "seq": row["seq"]})
        if path != "/api/config": return self._json(404, {"error": "not_found"})
        supplied = body.get("version")
        with LOCK: current = dict(STATE)
        event("http_request", "POST /api/config", requested_value=body.get("value"), supplied_version=supplied)
        if supplied != current["version"]:
            event("http_response", "HTTP 409 stale version", status=409, expected_version=current["version"], supplied_version=supplied)
            return self._json(409, {"error": "stale_version", "expected_version": current["version"], "state": current})
        with LOCK:
            STATE["value"] = str(body.get("value")); STATE["version"] += 1; saved = dict(STATE)
        event("http_response", "HTTP 200 persisted", status=200, **saved)
        return self._json(200, saved)


def make_server(host="127.0.0.1", port=8765):
    return ThreadingHTTPServer((host, port), lambda *a, **k: Handler(*a, directory=str(ROOT), **k))


if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=8765)
    a = p.parse_args(); reset(); print(f"Incident lab: http://{a.host}:{a.port}", flush=True)
    make_server(a.host, a.port).serve_forever()
