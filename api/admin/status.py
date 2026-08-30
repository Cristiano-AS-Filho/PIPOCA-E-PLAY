"""Endpoint serverless do painel administrativo."""

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from auth import config_status, public_user, read_session  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        user = read_session(self.headers.get("Cookie"))
        if not user or user.get("role") != "admin":
            payload = {"error": "Acesso reservado ao administrador."}
            status = HTTPStatus.FORBIDDEN
        else:
            payload = {"user": public_user(user), "config": config_status()}
            status = HTTPStatus.OK
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.send_response(405)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
