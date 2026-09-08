#!/usr/bin/env python3
"""Servidor local do Pipoca & Play.

A chave OpenAI nunca é enviada para o navegador: somente este processo a lê.
"""

import json
import os
import time
from collections import defaultdict, deque
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import api_core
from auth import is_secure_request, public_user, read_session

ROOT = Path(__file__).parent
PUBLIC = ROOT / "public"
REQUESTS_BY_IP = defaultdict(deque)
MAX_REQUESTS_PER_MINUTE = 12


def load_env_file():
    """Carrega somente pares simples KEY=VALUE de .env, sem sobrescrever o ambiente."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()

from recommender import (  # noqa: F401  (reexportado para compatibilidade)
    OFFICIAL_FILTER_OPTIONS,
    RECOMMENDATION_SCHEMA,
    buildRecommendationPrompt,
    call_openai,
    clean_filters,
    extract_response_text,
    validate_recommendation_payload,
)


class AppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC), **kwargs)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        super().end_headers()

    def send_json(self, status, payload, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def current_user(self):
        return read_session(self.headers.get("Cookie"))

    def request_json(self, max_length=8_192):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > max_length:
            raise ValueError("Solicitação inválida.")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def request_json_or_empty(self, max_length=8_192):
        try:
            body = self.request_json(max_length)
        except (ValueError, json.JSONDecodeError):
            return {}
        return body if isinstance(body, dict) else {}

    def send_result(self, result):
        status, payload, headers = result
        self.send_json(status, payload, headers)

    def do_GET(self):
        path = urlparse(self.path)
        if path.path == "/api/health":
            self.send_result(api_core.health())
            return
        if path.path == "/api/auth/me":
            user = self.current_user()
            self.send_json(HTTPStatus.OK, {"authenticated": bool(user), "user": public_user(user)})
            return
        if path.path == "/api/auth/status":
            params = parse_qs(path.query)
            self.send_result(
                api_core.registration_status(params.get("email", [""])[0], params.get("token", [""])[0])
            )
            return
        if path.path in {"/api/admin/status", "/api/admin/users"}:
            self.send_result(api_core.admin_overview(self.current_user()))
            return
        if path.path == "/api/plans":
            self.send_result(api_core.plans_catalog())
            return
        if path.path == "/api/account":
            self.send_result(api_core.account_overview(self.current_user()))
            return
        if path.path == "/api/feedback":
            self.send_result(api_core.feedback_overview(self.current_user()))
            return
        if path.path == "/api/billing/status":
            self.send_result(api_core.billing_status(self.current_user()))
            return
        if path.path == "/api/billing/webhook":
            self.send_json(HTTPStatus.OK, {"ok": True, "endpoint": "asaas-webhook"})
            return
        if path.path.startswith("/api/"):
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Rota não encontrada."})
            return
        if path.path in {"/admin", "/admin/"}:
            self.path = "/admin.html"
        super().do_GET()

    def do_POST(self):
        if self.path == "/api/auth/register":
            self.send_result(api_core.register(self.request_json_or_empty(4_096)))
            return

        if self.path == "/api/auth/login":
            self.send_result(
                api_core.login(self.request_json_or_empty(4_096), is_secure_request(self.headers))
            )
            return

        if self.path == "/api/auth/logout":
            self.send_result(api_core.logout(is_secure_request(self.headers)))
            return

        if self.path == "/api/admin/users":
            self.send_result(
                api_core.admin_action(self.current_user(), self.request_json_or_empty(4_096))
            )
            return

        if self.path == "/api/feedback":
            body = self.request_json_or_empty(4_096)
            session = self.current_user()
            if str(body.get("action", "")).lower() in {"remove", "delete"}:
                self.send_result(api_core.feedback_delete(session, body))
            else:
                self.send_result(api_core.feedback_save(session, body))
            return

        if self.path == "/api/billing/checkout":
            self.send_result(api_core.billing_checkout(self.current_user(), self.request_json_or_empty(4_096)))
            return

        if self.path == "/api/billing/status":
            self.send_result(api_core.billing_status(self.current_user()))
            return

        if self.path == "/api/billing/webhook":
            token = self.headers.get("asaas-access-token")
            self.send_result(api_core.billing_webhook(self.request_json_or_empty(64_000), token))
            return

        if self.path != "/api/recommend":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Rota não encontrada."})
            return

        now = time.monotonic()
        requests = REQUESTS_BY_IP[self.client_address[0]]
        while requests and now - requests[0] > 60:
            requests.popleft()
        if len(requests) >= MAX_REQUESTS_PER_MINUTE:
            self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Aguarde um minuto antes de tentar novamente."})
            return
        requests.append(now)
        self.send_result(api_core.recommend(self.current_user(), self.request_json_or_empty()))

    def do_DELETE(self):
        if self.path == "/api/feedback":
            self.send_result(api_core.feedback_delete(self.current_user(), self.request_json_or_empty(4_096)))
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Rota não encontrada."})

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {args[0]}")


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"Pipoca & Play em http://{host}:{port}")
    ThreadingHTTPServer((host, port), AppHandler).serve_forever()
