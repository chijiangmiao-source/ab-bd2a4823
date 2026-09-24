"""HTTP API for the Steiner audit service (Python standard library only).

Routes:
  GET  /healthz     liveness probe used by the Compose healthcheck
  POST /api/audit   solve one audit request and return the canonical subnet

Every error response has the stable shape
``{"error": {"code": ..., "message": ..., "field": ...}}`` and never
carries a partial subnet or a previous request's result.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.solver import SolveError, solve_payload

API_PATH = "/api/audit"
HEALTH_PATH = "/healthz"
MAX_BODY_BYTES = 1 << 20  # 1 MiB


class Handler(BaseHTTPRequestHandler):
    server_version = "SteinerAudit/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers ---------------------------------------------------------
    def _send_json(self, status, obj, close=False):
        body = json.dumps(obj, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_error(self, status, code, message, field=None):
        error = {"code": code, "message": message}
        if field is not None:
            error["field"] = field
        # Close the connection: the request body may not have been consumed.
        self._send_json(status, {"error": error}, close=True)

    @staticmethod
    def _clean_path(raw):
        return raw.split("?", 1)[0].split("#", 1)[0]

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        path = self._clean_path(self.path)
        if path == HEALTH_PATH:
            self._send_json(200, {"status": "ok"})
        elif path == API_PATH:
            self._send_error(405, "METHOD_NOT_ALLOWED", "use POST for /api/audit")
        else:
            self._send_error(404, "NOT_FOUND", "unknown path '%s'" % path)

    def do_POST(self):
        path = self._clean_path(self.path)
        if path == HEALTH_PATH:
            self._send_error(405, "METHOD_NOT_ALLOWED", "use GET for /healthz")
            return
        if path != API_PATH:
            self._send_error(404, "NOT_FOUND", "unknown path '%s'" % path)
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            self._send_error(413, "PAYLOAD_TOO_LARGE", "request body exceeds 1 MiB")
            return
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_error(400, "MALFORMED_JSON", "request body is not valid JSON")
            return
        try:
            result = solve_payload(payload)
        except SolveError as exc:
            self._send_json(exc.http_status, {"error": exc.to_dict()})
            return
        except Exception:  # defensive: never leak a stack trace to the client
            traceback.print_exc()
            self._send_error(500, "INTERNAL", "unexpected internal error")
            return
        self._send_json(200, {"status": "ok", **result})


def main():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    sys.stderr.write("steiner-audit listening on 0.0.0.0:%d\n" % port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
