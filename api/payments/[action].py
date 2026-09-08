"""Agrupa /api/payments/plans, /status, /checkout e /webhook num único arquivo.

O plano Hobby da Vercel permite no máximo 12 Serverless Functions por deploy;
uma rota dinâmica conta como uma função só, então isso evita estourar o
limite ao invés de ter um arquivo .py por sub-rota. A Vercel injeta o
segmento dinâmico (`plans`, `status`, `checkout` ou `webhook`) como parâmetro
de querystring `action` para runtimes sem objeto de request nativo, como o
Python — daí o parse manual abaixo em vez de um valor pronto.
"""

from http.server import BaseHTTPRequestHandler
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import api_core  # noqa: E402
from auth import read_session  # noqa: E402
from serverless_utils import read_json_or_empty, send_json, send_result  # noqa: E402


def _action(handler_instance) -> str:
    query = parse_qs(urlparse(handler_instance.path).query)
    return (query.get("action", [""])[0] or "").strip()


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        action = _action(self)
        if action == "plans":
            send_result(self, api_core.list_plans_route())
        elif action == "status":
            send_result(self, api_core.payment_status(read_session(self.headers.get("Cookie"))))
        else:
            send_json(self, HTTPStatus.NOT_FOUND, {"error": "Rota não encontrada."})

    def do_POST(self):
        action = _action(self)
        if action == "checkout":
            session = read_session(self.headers.get("Cookie"))
            send_result(self, api_core.create_checkout(session, read_json_or_empty(self, 4_096)))
        elif action == "webhook":
            send_result(self, api_core.payment_webhook(self.headers, read_json_or_empty(self, 65_536)))
        else:
            send_json(self, HTTPStatus.NOT_FOUND, {"error": "Rota não encontrada."})
