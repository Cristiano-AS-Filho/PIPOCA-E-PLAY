"""Função serverless da Vercel para iniciar o checkout de assinatura no Asaas."""

import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path

# A Vercel executa este arquivo a partir da raiz do projeto.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from auth import read_session  # noqa: E402
from rate_limit import check_rate_limit  # noqa: E402
from server import create_checkout_session, resolve_billing_user  # noqa: E402
from user_store import StorageError  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4_096:
                raise ValueError("Solicitação inválida.")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            plan_id = body.get("plan_id", "")
            if not isinstance(plan_id, str):
                raise ValueError("Plano inválido.")
            session_user = read_session(self.headers.get("Cookie"))
            user_record = resolve_billing_user(session_user, body)
            if not user_record:
                self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Não foi possível confirmar sua conta para iniciar o pagamento."})
                return
            if not check_rate_limit("checkout:" + user_record["email"], 6, 60):
                self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Aguarde um minuto antes de tentar novamente."})
                return
            checkout_url = create_checkout_session(user_record, plan_id, self.headers)
            self.send_json(HTTPStatus.OK, {"checkout_url": checkout_url})
        except LookupError as error:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": str(error)})
        except StorageError as error:
            self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except RuntimeError as error:
            self.send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})

    def do_GET(self):
        self.send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Use POST nesta rota."})
