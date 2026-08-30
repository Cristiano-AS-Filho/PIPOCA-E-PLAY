"""Função serverless da Vercel para recomendações do Pipoca & Play."""

import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path

# A Vercel executa este arquivo a partir da raiz do projeto.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auth import read_session  # noqa: E402
from server import call_openai, clean_filters  # noqa: E402


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
        if not read_session(self.headers.get("Cookie")):
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Faça login para receber recomendações."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8_192:
                raise ValueError("Solicitação inválida.")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            text = call_openai(clean_filters(body.get("filters")))
            self.send_json(HTTPStatus.OK, {"text": text})
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except RuntimeError as error:
            self.send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})

    def do_GET(self):
        self.send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Use POST nesta rota."})
