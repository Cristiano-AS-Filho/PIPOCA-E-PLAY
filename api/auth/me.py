"""Endpoint serverless para consultar a sessão atual."""

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from auth import public_user, read_session  # noqa: E402
from serverless_utils import send_json  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        user = read_session(self.headers.get("Cookie"))
        send_json(self, HTTPStatus.OK, {"authenticated": bool(user), "user": public_user(user)})

    def do_POST(self):
        send_json(self, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Use GET nesta rota."})
