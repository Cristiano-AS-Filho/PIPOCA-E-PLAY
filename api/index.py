"""Função serverless única do Pipoca & Play.

O ``vercel.json`` reescreve todo ``/api/*`` para cá. Manter uma só função
respeita o limite de funções por deploy do plano Hobby, reduz partidas a frio
e garante que o roteamento seja exatamente o mesmo do servidor local.
"""

import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import router  # noqa: E402
from serverless_utils import send_json, send_result  # noqa: E402

MAX_BODY_BYTES = 64_000


class handler(BaseHTTPRequestHandler):
    def _body(self):
        """Corpo JSON tolerante: entrada inválida vira um dicionário vazio."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            return {}
        if length <= 0 or length > MAX_BODY_BYTES:
            return {}
        try:
            parsed = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _dispatch(self, method):
        target = urlparse(self.path)
        try:
            result = router.handle(
                method,
                target.path,
                parse_qs(target.query),
                self._body() if method in {"POST", "PUT", "DELETE"} else {},
                self.headers,
            )
        except Exception:  # noqa: BLE001 - nenhuma exceção deve virar 500 mudo
            send_json(
                self,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "Erro inesperado ao processar a requisição."},
            )
            return
        send_result(self, result)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")
