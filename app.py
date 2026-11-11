"""Loopback-only control panel. No remote services, telemetry, or audio uploads."""

import argparse
import hmac
import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from align_core import align
from audacity_adapter import Audacity, install_macro


class Session:
    def __init__(self, adapter=None):
        self.adapter = adapter or Audacity()
        self.reference = self.target = self.result = self.result_id = None
        self.applied = None
        self.lock = threading.Lock()

    def public(self):
        # Captured samples, hashes, project snapshots, and paths never enter HTTP responses.
        return {
            "reference": self.reference.public() if self.reference else None,
            "target": self.target.public() if self.target else None,
            "result": self.result.to_dict() if self.result else None,
            "result_id": self.result_id,
            "applied": self.applied,
        }

    def action(self, action, data):
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("Another operation is in progress. Wait for it to finish.")
        try:
            if action in ("reference", "target"):
                # A failed new capture must not leave an old Apply confirmation usable.
                self.result = self.result_id = self.applied = None
                setattr(self, action, None)
                setattr(self, action, self.adapter.capture())
            elif action == "analyze":
                self.result = self.result_id = None
                a, b = self.reference, self.target
                if a is None or b is None:
                    raise ValueError("Capture both selections first.")
                if a.rate != b.rate:
                    raise ValueError(
                        "The tracks must have the same sample rate in this version. The project sample rate may differ."
                    )
                if (a.track, a.clip_start, a.clip_end) == (b.track, b.clip_start, b.clip_end):
                    raise ValueError("Reference and target must be different clips.")
                try:
                    maximum = float(data.get("max_ms", 100))
                    raw_count = data.get("candidates", 5)
                    if isinstance(raw_count, bool) or str(raw_count) != str(int(raw_count)):
                        raise ValueError
                    count = int(raw_count)
                except (TypeError, ValueError, OverflowError):
                    raise ValueError("Invalid analysis settings.") from None
                method = data.get("method", "NCC")
                # Validate settings before any potentially expensive Audacity reads.
                if (
                    method not in ("NCC", "Huber")
                    or not 0 <= maximum <= 10000
                    or not 1 <= count <= 20
                ):
                    raise ValueError("Invalid analysis settings.")
                self.adapter.validate([a, b])
                self.result = align(
                    a.audio,
                    b.audio,
                    a.rate,
                    reference_start=a.selection_start,
                    target_start=b.selection_start,
                    max_ms=maximum,
                    method=method,
                    candidates=count,
                )
                self.result_id = secrets.token_urlsafe(16)
                self.applied = None
            elif action == "apply":
                if self.result is None or data.get("result_id") != self.result_id:
                    raise ValueError("The result is stale or already applied. Analyze again.")
                # Consume even on verification failure. Never replay an uncertain move.
                result, self.result_id = self.result, None
                self.applied = self.adapter.apply(self.reference, self.target, result.total_seconds)
                self.reference = self.target = self.result = None
            elif action == "reset":
                self.reference = self.target = self.result = self.result_id = self.applied = None
            else:
                raise ValueError("Unknown action.")
            return self.public()
        finally:
            self.lock.release()


class LocalServer(ThreadingHTTPServer):
    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


def make_server(port=8765, *, session=None, token=None):
    session = session or Session()
    token = token or secrets.token_urlsafe(32)
    web = Path(__file__).parent / "web"
    # An explicit asset allowlist; URLs are never converted to filesystem paths.
    assets = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        "/style.css": ("style.css", "text/css; charset=utf-8"),
        "/locales/ru.json": ("locales/ru.json", "application/json; charset=utf-8"),
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            # Avoid logging project names, local URLs/tokens, or browser activity.
            pass

        def send(self, status, payload, content_type="application/json; charset=utf-8"):
            body = (
                json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
                if not isinstance(payload, bytes)
                else payload
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                "form-action 'none'; object-src 'none'",
            )
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def allowed(self):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            hosts = self.headers.get_all("Host", [])
            origins = self.headers.get_all("Origin", [])
            return hosts == [origin[7:]] and (not origins or origins == [origin])

        def authenticated(self):
            supplied = self.headers.get_all("X-Repaint-Token", [])
            return (
                self.allowed()
                and len(supplied) == 1
                and len(supplied[0]) <= 256
                and hmac.compare_digest(supplied[0].encode("utf-8"), token.encode("utf-8"))
            )

        def do_GET(self):
            if not self.allowed():
                return self.send(403, {"error": "Invalid request address."})
            if self.path in assets:
                name, mime = assets[self.path]
                return self.send(200, (web / name).read_bytes(), mime)
            if self.path == "/api/state" and self.authenticated():
                if not session.lock.acquire(blocking=False):
                    return self.send(
                        409, {"error": "Another operation is in progress. Wait for it to finish."}
                    )
                try:
                    return self.send(200, session.public())
                finally:
                    session.lock.release()
            return self.send(
                403, {"error": "Open the full URL printed in the Repaint Align terminal."}
            )

        def do_POST(self):
            if not self.authenticated():
                return self.send(403, {"error": "Access denied."})
            try:
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("Invalid request.")
                size = int(lengths[0])
                if not 0 <= size <= 65536:
                    raise ValueError("Request body is too large.")
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("Expected a JSON request.")
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ValueError("Incomplete request body.")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("Invalid request.")
                if self.path not in (
                    "/api/reference",
                    "/api/target",
                    "/api/analyze",
                    "/api/apply",
                    "/api/reset",
                ):
                    raise ValueError("Unknown endpoint.")
                self.send(200, session.action(self.path[5:], data))
            except (ValueError, RuntimeError, OSError, TimeoutError) as exc:
                self.send(400, {"error": str(exc)})

    server = LocalServer(("127.0.0.1", port), Handler)
    server.repaint_session = session
    return server, token


def main():
    parser = argparse.ArgumentParser(description="Local waveform alignment for Audacity clips")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--install-macro", action="store_true")
    parser.add_argument("--macro-dir", type=Path, help="Override the Audacity Macros directory")
    args = parser.parse_args()
    if args.install_macro:
        path = install_macro(args.macro_dir) if args.macro_dir else install_macro()
        print("Installed the plain-text helper macro:", path)
        print("Save your project and restart Audacity once to load the helper macro.")
        return
    session = Session(Audacity(macro_folder=args.macro_dir) if args.macro_dir else Audacity())
    server, token = make_server(args.port, session=session)
    url = f"http://127.0.0.1:{server.server_port}/#{token}"
    print("Repaint Align:", url, flush=True)
    print("Do not edit Audacity during capture or Apply. Press Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        with session.lock:
            session.adapter.close()


if __name__ == "__main__":
    main()
