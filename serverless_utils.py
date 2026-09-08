"""Utilitários pequenos compartilhados pelas funções serverless."""

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Nome do parâmetro que os rewrites do vercel.json usam para dizer qual rota do
# grupo foi pedida. Ver a seção "rewrites" em vercel.json.
ROUTE_PARAM = "__route"


def project_root_on_path():
    """Permite que as funções em api/ importem os módulos da raiz do projeto."""
    root = str(Path(__file__).resolve().parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


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


def send_result(handler, result):
    """Envia uma tripla (status, payload, headers) vinda de api_core."""
    status, payload, headers = result
    send_json(handler, status, payload, headers)


def read_json(handler, max_length=8_192):
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0 or length > max_length:
        raise ValueError("Solicitação inválida.")
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def read_json_or_empty(handler, max_length=8_192):
    """Corpo JSON tolerante: entradas inválidas viram um dicionário vazio."""
    try:
        body = read_json(handler, max_length)
    except (ValueError, json.JSONDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


def route_action(handler, allowed, default=""):
    """Descobre qual rota do grupo foi pedida em uma função consolidada.

    A Vercel limita o número de funções por deploy, então rotas irmãs
    (`/api/auth/*`, `/api/admin/*`, `/api/billing/*`) moram em uma função só e
    o `vercel.json` reescreve o caminho para `?__route=<ação>`.

    O segmento do caminho tem prioridade sobre o parâmetro, de modo que quando
    o caminho original chega intacto ele manda. O retorno é sempre um valor de
    `allowed`, nunca texto arbitrário vindo da requisição.
    """
    parsed = urlparse(handler.path)
    segment = parsed.path.rstrip("/").rsplit("/", 1)[-1].lower()
    if segment in allowed:
        return segment
    for value in parse_qs(parsed.query).get(ROUTE_PARAM, []):
        if value.lower() in allowed:
            return value.lower()
    return default
