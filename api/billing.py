"""Planos, assinatura e checkout: /api/billing/plans, status, checkout e webhook.

As quatro moram na mesma função; o `vercel.json` reescreve
`/api/billing/<ação>` para cá.

O webhook é a URL que deve ser cadastrada no painel do ASAAS:
`https://SEU-DOMINIO/api/billing/webhook`, com um token de autenticação igual
ao valor de `ASAAS_WEBHOOK_TOKEN`.
"""

from http.server import BaseHTTPRequestHandler
from http import HTTPStatus
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_core  # noqa: E402
from auth import read_session  # noqa: E402
from serverless_utils import read_json_or_empty, route_action, send_json, send_result  # noqa: E402


GET_ACTIONS = {"plans", "status"}
POST_ACTIONS = {"checkout", "webhook"}
ACTIONS = GET_ACTIONS | POST_ACTIONS


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        action = route_action(self, ACTIONS)
        if action == "plans":
            send_result(self, api_core.plans())
            return
        if action == "status":
            # `?sync=0` lê apenas o que já está gravado, sem consultar o ASAAS.
            sync = parse_qs(urlparse(self.path).query).get("sync", ["1"])[0] != "0"
            send_result(self, api_core.billing_status(read_session(self.headers.get("Cookie")), sync))
            return
        if action in POST_ACTIONS:
            send_json(self, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Use POST nesta rota."})
            return
        send_json(self, HTTPStatus.NOT_FOUND, {"error": "Rota não encontrada."})

    def do_POST(self):
        action = route_action(self, ACTIONS)
        if action == "checkout":
            session = read_session(self.headers.get("Cookie"))
            send_result(self, api_core.billing_checkout(session, read_json_or_empty(self, 4_096)))
            return
        if action == "webhook":
            send_result(
                self,
                api_core.billing_webhook(
                    read_json_or_empty(self, 16_384),
                    self.headers.get("asaas-access-token"),
                ),
            )
            return
        if action in GET_ACTIONS:
            send_json(self, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Use GET nesta rota."})
            return
        send_json(self, HTTPStatus.NOT_FOUND, {"error": "Rota não encontrada."})
