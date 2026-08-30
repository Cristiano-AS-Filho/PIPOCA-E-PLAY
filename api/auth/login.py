import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from auth import authenticate, create_session, is_secure_request, public_user, session_cookie  # noqa: E402
from serverless_utils import read_json, send_json  # noqa: E402
from user_store import StorageError, find_user  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            body = read_json(self, 4_096)
            email = body.get("email", "")
            password = body.get("password", "")
            if not isinstance(email, str) or not isinstance(password, str):
                raise ValueError("Informe e-mail e senha.")
            account = find_user(email)
            if account and account.get("status") == "pending":
                send_json(self, HTTPStatus.FORBIDDEN, {"error": "Seu cadastro ainda está aguardando a validação do administrador."})
                return
            if account and account.get("status") == "rejected":
                send_json(self, HTTPStatus.FORBIDDEN, {"error": "Seu pedido de acesso foi rejeitado. Você pode realizar um novo cadastro."})
                return
            user = authenticate(email, password)
            if not user:
                send_json(self, HTTPStatus.UNAUTHORIZED, {"error": "E-mail ou senha inválidos."})
                return
            send_json(
                self,
                HTTPStatus.OK,
                {"authenticated": True, "user": public_user(user)},
                {"Set-Cookie": session_cookie(create_session(user["email"], user["role"], user.get("user_id")), is_secure_request(self.headers))},
            )
        except StorageError as error:
            send_json(self, HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
        except (ValueError, json.JSONDecodeError) as error:
            send_json(self, HTTPStatus.BAD_REQUEST, {"error": str(error)})

    def do_GET(self):
        send_json(self, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Use POST nesta rota."})
