#!/usr/bin/env python3
"""Servidor local do Pipoca & Play.

A chave OpenAI nunca é enviada para o navegador: somente este processo a lê.
"""

import json
import os
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import router

ROOT = Path(__file__).parent
PUBLIC = ROOT / "public"


def load_env_file():
    """Carrega somente pares simples KEY=VALUE de .env, sem sobrescrever o ambiente."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()

from recommender import (  # noqa: F401  (reexportado para compatibilidade)
    OFFICIAL_FILTER_OPTIONS,
    RECOMMENDATION_SCHEMA,
    buildRecommendationPrompt,
    call_openai,
    clean_filters,
    extract_response_text,
    validate_recommendation_payload,
)


class AppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC), **kwargs)

    # Os mesmos cabeçalhos que o ``vercel.json`` aplica em produção, para que um
    # problema de política apareça já no desenvolvimento local. A CSP fica
    # deliberadamente restrita a frame-ancestors/base-uri/object-src/form-action:
    # as páginas são pacotes do Claude Web Design, com script embutido e recursos
    # em blob:/data:, e uma script-src estrita as quebraria.
    SECURITY_HEADERS = (
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "strict-origin-when-cross-origin"),
        ("X-Frame-Options", "SAMEORIGIN"),
        (
            "Content-Security-Policy",
            "frame-ancestors 'self'; base-uri 'self'; object-src 'none'; form-action 'self'",
        ),
        (
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=(), magnetometer=(), gyroscope=()",
        ),
        ("Cross-Origin-Opener-Policy", "same-origin-allow-popups"),
    )

    def end_headers(self):
        for key, value in self.SECURITY_HEADERS:
            self.send_header(key, value)
        super().end_headers()

    def send_json(self, status, payload, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def request_json(self, max_length=8_192):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > max_length:
            raise ValueError("Solicitação inválida.")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def request_json_or_empty(self, max_length=8_192):
        try:
            body = self.request_json(max_length)
        except (ValueError, json.JSONDecodeError):
            return {}
        return body if isinstance(body, dict) else {}

    def send_result(self, result):
        status, payload, headers = result
        self.send_json(status, payload, headers)

    def do_GET(self):
        target = urlparse(self.path)
        if target.path.startswith("/api"):
            self.send_result(router.handle("GET", target.path, parse_qs(target.query), {}, self.headers))
            return
        # Mesmo efeito do ``cleanUrls`` do vercel.json: em produção /admin e /app
        # resolvem para os arquivos .html, e o servidor local precisa concordar.
        clean = target.path.rstrip("/") or "/"
        if clean in {"/admin", "/app"}:
            self.path = clean + ".html"
        super().do_GET()

    def do_POST(self):
        target = urlparse(self.path)
        if not target.path.startswith("/api"):
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Rota não encontrada."})
            return
        self.send_result(
            router.handle("POST", target.path, parse_qs(target.query), self.request_json_or_empty(64_000), self.headers)
        )

    def do_PUT(self):
        self.do_POST()

    def do_DELETE(self):
        target = urlparse(self.path)
        self.send_result(
            router.handle("DELETE", target.path, parse_qs(target.query), self.request_json_or_empty(64_000), self.headers)
        )

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {args[0]}")


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"Pipoca & Play em http://{host}:{port}")
    ThreadingHTTPServer((host, port), AppHandler).serve_forever()
