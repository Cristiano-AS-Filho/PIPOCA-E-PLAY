"""Conta do cliente: plano, assinatura e créditos restantes do dia."""

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_core  # noqa: E402
from auth import read_session  # noqa: E402
from serverless_utils import send_json, send_result  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        send_result(self, api_core.account_overview(read_session(self.headers.get("Cookie"))))

    def do_POST(self):
        send_json(self, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Use GET nesta rota."})
