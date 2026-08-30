"""Utilitários pequenos compartilhados pelas funções serverless."""

import json


def send_json(handler, status, payload, headers=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler, max_length=8_192):
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0 or length > max_length:
        raise ValueError("Solicitação inválida.")
    return json.loads(handler.rfile.read(length).decode("utf-8"))
