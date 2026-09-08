"""Função serverless da Vercel para recomendações do Pipoca & Play.

O motor só responde para quem tem sessão válida, pagamento confirmado e
crédito disponível no dia. A regra vive em `api_core.recommend`, a mesma que o
servidor local usa.
"""

import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path

# A Vercel executa este arquivo a partir da raiz do projeto.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_core  # noqa: E402
from auth import read_session  # noqa: E402
from serverless_utils import read_json_or_empty, send_json, send_result  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        session = read_session(self.headers.get("Cookie"))
        send_result(self, api_core.recommend(session, read_json_or_empty(self)))

    def do_GET(self):
        send_json(self, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Use POST nesta rota."})
