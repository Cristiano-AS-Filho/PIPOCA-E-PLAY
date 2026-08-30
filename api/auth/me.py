"""Endpoint serverless para consultar a sessão atual."""

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from auth import public_user, read_session  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        user = read_session(self.headers.get("Cookie"))
        body = json.dumps({"authenticated": bool(user), "user": public_user(user)}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.send_response(405)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
