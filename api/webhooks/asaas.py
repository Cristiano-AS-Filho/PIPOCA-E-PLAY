"""Função serverless da Vercel para receber webhooks de cobrança do Asaas.

Configure a mesma URL (https://SEU_DOMINIO/api/webhooks/asaas) e o mesmo
token no painel do Asaas (Integrações → Webhooks) e na variável de ambiente
ASAAS_WEBHOOK_TOKEN — é esse token, enviado de volta no header
`asaas-access-token`, que prova que a requisição veio do Asaas mesmo.
"""

import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server import handle_asaas_webhook_event, verify_asaas_webhook  # noqa: E402
from user_store import StorageError  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not verify_asaas_webhook(self.headers):
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Assinatura do webhook inválida."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if 0 < length <= 65_536:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                handle_asaas_webhook_event(payload)
        except (ValueError, json.JSONDecodeError, LookupError, StorageError):
            pass  # sempre 200 pra evitar reenvio em loop do lado do Asaas
        self.send_json(HTTPStatus.OK, {"received": True})

    def do_GET(self):
        self.send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Use POST nesta rota."})
