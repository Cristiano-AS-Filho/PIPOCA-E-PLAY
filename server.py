#!/usr/bin/env python3
"""Servidor local do Pipoca & Play.

A chave OpenAI nunca é enviada para o navegador: somente este processo a lê.
"""

import json
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
PUBLIC = ROOT / "public"
REQUESTS_BY_IP = defaultdict(deque)
MAX_REQUESTS_PER_MINUTE = 12


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

RECOMMENDATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["interpretation", "best_choice", "recommendations"],
    "properties": {
        "interpretation": {"type": "string"},
        "best_choice": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title_pt", "reason"],
            "properties": {
                "title_pt": {"type": "string"},
                "reason": {"type": "string"},
            },
        },
        "recommendations": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "rank", "title_original", "title_pt", "year", "runtime_minutes",
                    "age_rating_br", "genres", "vibe_tags", "synopsis", "why_it_matches",
                    "match_score", "ratings", "awards", "where_to_watch",
                ],
                "properties": {
                    "rank": {"type": "integer", "minimum": 1, "maximum": 3},
                    "title_original": {"type": "string"},
                    "title_pt": {"type": "string"},
                    "year": {"type": "integer", "minimum": 0},
                    "runtime_minutes": {"type": "integer", "minimum": 0},
                    "age_rating_br": {"type": "integer", "minimum": 0},
                    "genres": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                    "vibe_tags": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                    "synopsis": {"type": "string"},
                    "why_it_matches": {"type": "string"},
                    "match_score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "ratings": {
                        "type": "object", "additionalProperties": False,
                        "required": ["imdb", "rotten_tomatoes_critics"],
                        "properties": {
                            "imdb": {"type": "number", "minimum": 0, "maximum": 10},
                            "rotten_tomatoes_critics": {"type": "integer", "minimum": 0, "maximum": 100},
                        },
                    },
                    "awards": {
                        "type": "object", "additionalProperties": False,
                        "required": ["oscars_won", "highlight"],
                        "properties": {
                            "oscars_won": {"type": "integer", "minimum": 0},
                            "highlight": {"type": "string"},
                        },
                    },
                    "where_to_watch": {
                        "type": "array",
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "required": ["platform", "type"],
                            "properties": {
                                "platform": {"type": "string"},
                                "type": {"type": "string", "enum": ["assinatura", "aluguel_compra", "cinema"]},
                            },
                        },
                    },
                },
            },
        },
    },
}


def clean_filters(value):
    if not isinstance(value, dict):
        raise ValueError("Filtros inválidos.")
    allowed = {"genre", "mood", "duration", "era", "platform", "companionship", "popularity"}
    cleaned = {}
    for key in allowed:
        item = value.get(key, "")
        if not isinstance(item, str) or len(item) > 120:
            raise ValueError("Um dos filtros é inválido.")
        cleaned[key] = item.strip()
    if not all(cleaned.values()):
        raise ValueError("Responda às sete perguntas antes de buscar.")
    return cleaned


def prompt_for(filters):
    return f"""Você é um recomendador de filmes para o público brasileiro. Responda em pt-BR.
Escolha exatamente três filmes reais e priorize compatibilidade, não popularidade genérica.

Preferências:
- Gênero: {filters['genre']}
- Clima: {filters['mood']}
- Tempo disponível: {filters['duration']}
- Época: {filters['era']}
- Plataforma: {filters['platform']}
- Companhia: {filters['companionship']}
- Perfil: {filters['popularity']}

Para dados não confirmados, use 0, string vazia ou array vazio. Não invente disponibilidade,
notas ou prêmios. A resposta deve obedecer exatamente ao JSON solicitado."""


def extract_response_text(response):
    """Extrai texto de uma resposta REST da OpenAI.

    `output_text` é uma conveniência dos SDKs. A API REST retorna os blocos em
    `output[].content[]`, então essa leitura mantém o backend independente de SDK.
    """
    parts = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                parts.append(content["text"])
    return "".join(parts).strip()


def call_openai(filters):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY não foi configurada no servidor.")
    payload = {
        "model": os.environ.get("OPENAI_MODEL", "gpt-5.6-luna"),
        "input": prompt_for(filters),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "movie_recommendations",
                "strict": True,
                "schema": RECOMMENDATION_SCHEMA,
            }
        },
        "max_output_tokens": 1800,
        "reasoning": {"effort": "low"},
        "store": False,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"A consulta à OpenAI falhou (HTTP {error.code}).") from RuntimeError(details)
    except urllib.error.URLError as error:
        raise RuntimeError("Não foi possível conectar ao serviço da OpenAI.") from error

    text = extract_response_text(result)
    if not text:
        status = result.get("status", "desconhecido")
        reason = (result.get("incomplete_details") or {}).get("reason")
        suffix = f" Motivo: {reason}." if reason else ""
        raise RuntimeError(f"A OpenAI não retornou texto final (status: {status}).{suffix}")
    return text


class AppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC), **kwargs)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        super().end_headers()

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/api/recommend":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Rota não encontrada."})
            return
        now = time.monotonic()
        requests = REQUESTS_BY_IP[self.client_address[0]]
        while requests and now - requests[0] > 60:
            requests.popleft()
        if len(requests) >= MAX_REQUESTS_PER_MINUTE:
            self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Aguarde um minuto antes de tentar novamente."})
            return
        requests.append(now)
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

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {args[0]}")


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"Pipoca & Play em http://{host}:{port}")
    ThreadingHTTPServer((host, port), AppHandler).serve_forever()
