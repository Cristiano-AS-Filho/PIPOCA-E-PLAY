"""Função serverless da Vercel para recomendações do Pipoca & Play."""

import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path

MAX_REQUESTS_PER_MINUTE = 12

# A Vercel executa este arquivo a partir da raiz do projeto.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auth import read_session  # noqa: E402
from rate_limit import check_rate_limit  # noqa: E402
from server import call_openai, clean_filters  # noqa: E402
from user_store import consume_credit  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        user = read_session(self.headers.get("Cookie"))
        if not user:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Faça login para receber recomendações."})
            return
        if not check_rate_limit(user["email"], MAX_REQUESTS_PER_MINUTE, 60):
            self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Aguarde um minuto antes de tentar novamente."})
            return
        if user.get("role") != "admin":
            try:
                allowed, credit_info = consume_credit(user.get("user_id", ""))
            except LookupError as error:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": str(error)})
                return
            if not allowed:
                message = (
                    "Assine um plano para buscar recomendações."
                    if credit_info.get("subscription_status") != "active"
                    else "Seus créditos de hoje acabaram. Eles renovam amanhã, ou você pode subir de plano."
                )
                self.send_json(HTTPStatus.PAYMENT_REQUIRED, {"error": message, "subscription": credit_info})
                return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8_192:
                raise ValueError("Solicitação inválida.")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            text = call_openai(clean_filters(body.get("filters")))
            self.send_json(HTTPStatus.OK, {"text": text})
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except RuntimeError as error:
            self.send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})

    def do_GET(self):
        self.send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Use POST nesta rota."})
