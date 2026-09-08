"""Webhook da Asaas: confirma o pagamento e libera o acesso do cliente."""

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import api_core  # noqa: E402
from serverless_utils import read_json_or_empty, send_json, send_result  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = read_json_or_empty(self, 64_000)
        token = self.headers.get("asaas-access-token") or self.headers.get("Asaas-Access-Token")
        send_result(self, api_core.billing_webhook(body, token))

    def do_GET(self):
        # A Asaas faz uma verificação simples da URL antes de ativar o webhook.
        send_json(self, HTTPStatus.OK, {"ok": True, "endpoint": "asaas-webhook"})
