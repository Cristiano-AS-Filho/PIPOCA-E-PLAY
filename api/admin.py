"""Rotas do painel administrativo: /api/admin/status e /api/admin/users.

Ambas moram na mesma função; o `vercel.json` reescreve `/api/admin/<ação>`
para cá.
"""

from http.server import BaseHTTPRequestHandler
from http import HTTPStatus
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_core  # noqa: E402
from auth import read_session  # noqa: E402
from serverless_utils import read_json_or_empty, route_action, send_json, send_result  # noqa: E402


ACTIONS = {"status", "users"}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if route_action(self, ACTIONS) not in ACTIONS:
            send_json(self, HTTPStatus.NOT_FOUND, {"error": "Rota não encontrada."})
            return
        send_result(self, api_core.admin_overview(read_session(self.headers.get("Cookie"))))

    def do_POST(self):
        if route_action(self, ACTIONS) != "users":
            send_json(self, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Use GET nesta rota."})
            return
        session = read_session(self.headers.get("Cookie"))
        send_result(self, api_core.admin_action(session, read_json_or_empty(self, 4_096)))

    def do_PUT(self):
        self.do_POST()
