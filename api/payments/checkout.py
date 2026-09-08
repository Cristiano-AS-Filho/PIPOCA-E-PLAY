"""Cria a assinatura no ASAAS e devolve o link de pagamento hospedado."""

from http.server import BaseHTTPRequestHandler
from http import HTTPStatus
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import api_core  # noqa: E402
from auth import read_session  # noqa: E402
from serverless_utils import read_json_or_empty, send_json, send_result  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        session = read_session(self.headers.get("Cookie"))
        send_result(self, api_core.create_checkout(session, read_json_or_empty(self, 4_096)))

    def do_GET(self):
        send_json(self, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Use POST nesta rota."})
