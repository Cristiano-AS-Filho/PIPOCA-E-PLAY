"""Webhook de pagamento do ASAAS.

Configure a URL `https://SEU-DOMINIO/api/billing/webhook` no painel do ASAAS
com um token de autenticação e repita o mesmo valor em `ASAAS_WEBHOOK_TOKEN`.
"""

from http.server import BaseHTTPRequestHandler
from http import HTTPStatus
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import api_core  # noqa: E402
from serverless_utils import read_json_or_empty, send_json, send_result  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        send_result(
            self,
            api_core.billing_webhook(
                read_json_or_empty(self, 16_384),
                self.headers.get("asaas-access-token"),
            ),
        )

    def do_GET(self):
        send_json(self, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Use POST nesta rota."})
