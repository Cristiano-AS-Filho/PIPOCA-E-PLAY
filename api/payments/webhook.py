"""Webhook público do ASAAS: confirma pagamentos e libera/revoga o acesso do cliente.

A rota é pública (o ASAAS não envia cookies), mas cada chamada é validada pelo
header ``asaas-access-token`` configurado em ``ASAAS_WEBHOOK_TOKEN``.
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
        send_result(self, api_core.payment_webhook(self.headers, read_json_or_empty(self, 65_536)))

    def do_GET(self):
        send_json(self, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Use POST nesta rota."})
