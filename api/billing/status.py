"""Assinatura e créditos do cliente logado.

Cada leitura confirma o pagamento junto ao ASAAS, então o acesso é liberado
mesmo quando o webhook não chega. `?sync=0` lê apenas o que já está gravado.
"""

from http.server import BaseHTTPRequestHandler
from http import HTTPStatus
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import api_core  # noqa: E402
from auth import read_session  # noqa: E402
from serverless_utils import send_json, send_result  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        sync = parse_qs(urlparse(self.path).query).get("sync", ["1"])[0] != "0"
        send_result(self, api_core.billing_status(read_session(self.headers.get("Cookie")), sync))

    def do_POST(self):
        send_json(self, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Use GET nesta rota."})
