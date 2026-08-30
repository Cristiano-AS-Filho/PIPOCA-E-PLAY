"""Endpoint serverless de autenticação."""

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from auth import authenticate, create_session, is_secure_request, public_user, session_cookie  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def send_json(self, status, payload, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4_096:
                raise ValueError("Solicitação inválida.")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            email = body.get("email", "")
            password = body.get("password", "")
            if not isinstance(email, str) or not isinstance(password, str):
                raise ValueError("Informe e-mail e senha.")
            user = authenticate(email, password)
            if not user:
                self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "E-mail ou senha inválidos."})
                return
            self.send_json(
                HTTPStatus.OK,
                {"authenticated": True, "user": public_user(user)},
                {"Set-Cookie": session_cookie(create_session(user["email"], user["role"]), is_secure_request(self.headers))},
            )
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

    def do_GET(self):
        self.send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Use POST nesta rota."})
