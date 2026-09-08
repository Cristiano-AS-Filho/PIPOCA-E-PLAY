"""Rotas de autenticação: /api/auth/login, logout, me, register e status.

As cinco moram na mesma função porque a Vercel limita quantas funções um
deploy pode ter; o `vercel.json` reescreve `/api/auth/<ação>` para cá.
"""

from http.server import BaseHTTPRequestHandler
from http import HTTPStatus
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_core  # noqa: E402
from auth import is_secure_request, public_user, read_session  # noqa: E402
from serverless_utils import read_json_or_empty, route_action, send_json, send_result  # noqa: E402


GET_ACTIONS = {"me", "status"}
POST_ACTIONS = {"login", "logout", "register"}
ACTIONS = GET_ACTIONS | POST_ACTIONS


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        action = route_action(self, ACTIONS)
        if action == "me":
            user = read_session(self.headers.get("Cookie"))
            send_json(self, HTTPStatus.OK, {"authenticated": bool(user), "user": public_user(user)})
            return
        if action == "status":
            params = parse_qs(urlparse(self.path).query)
            send_result(
                self,
                api_core.registration_status(params.get("email", [""])[0], params.get("token", [""])[0]),
            )
            return
        if action in POST_ACTIONS:
            send_json(self, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Use POST nesta rota."})
            return
        send_json(self, HTTPStatus.NOT_FOUND, {"error": "Rota não encontrada."})

    def do_POST(self):
        action = route_action(self, ACTIONS)
        if action == "logout":
            send_result(self, api_core.logout(is_secure_request(self.headers)))
            return
        if action == "login":
            send_result(self, api_core.login(read_json_or_empty(self, 4_096), is_secure_request(self.headers)))
            return
        if action == "register":
            send_result(self, api_core.register(read_json_or_empty(self, 4_096)))
            return
        if action in GET_ACTIONS:
            send_json(self, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Use GET nesta rota."})
            return
        send_json(self, HTTPStatus.NOT_FOUND, {"error": "Rota não encontrada."})
