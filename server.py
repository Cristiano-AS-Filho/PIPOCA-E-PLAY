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
from urllib.parse import parse_qs, urlparse

import api_core
from auth import is_secure_request, public_user, read_session
from metadata import enrich_result

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

OFFICIAL_FILTER_OPTIONS = {
    "genre": {"Livre (Qualquer)", "Ação", "Comédia", "Drama", "Ficção Científica", "Terror", "Romance", "Suspense / Thriller", "Animação", "Documentário", "Aventura", "Fantasia"},
    "mood": {"Livre (Qualquer)", "Quer dar risada / Divertido", "Para chorar / Emocionante", "Tensão / Adrenalina", "Para pensar / Cabeça", "Leve / Relaxante para descansar", "Inspirador / Motivacional", "Sombrio / Assustador"},
    "duration": {"Livre (Qualquer)", "Curto (Até 90 min)", "Padrão (90 a 120 min)", "Longo (Mais de 120 min)"},
    "era": {"Livre (Qualquer)", "Lançamentos Recentes (2023-2026)", "Anos 2010s", "Anos 2000s", "Anos 90s", "Clássicos (Antes de 1990)"},
    "platform": {"Livre (Qualquer)", "Netflix", "Amazon Prime Video", "Max (HBO)", "Disney+", "Apple TV+", "Paramount+", "Cinema / Aluguel"},
    "companionship": {"Sozinho(a)", "Em Casal", "Com Amigos", "Em Família (com crianças)"},
    "popularity": {"Indiferente", "Grandes Sucessos / Blockbusters", "Filmes Cult / Menos Conhecidos", "Aclamados pela Crítica / Premiações (Oscar, Cannes)"},
}

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
        item = item.strip()
        if item not in OFFICIAL_FILTER_OPTIONS[key]:
            raise ValueError("Uma das respostas não pertence às opções oficiais.")
        cleaned[key] = item
    if not all(cleaned.values()):
        raise ValueError("Responda às sete perguntas antes de buscar.")
    return cleaned


def buildRecommendationPrompt(filters):
    return f"""Atue como um especialista em cinema e recomendador personalizado para o público brasileiro. Responda em pt-BR.

Estou procurando uma recomendação perfeita para assistir agora. Considere conjuntamente estas sete dimensões:
- Gênero principal: {filters['genre']}
- Vibe/clima emocional desejado: {filters['mood']}
- Tempo disponível: {filters['duration']}
- Época do filme: {filters['era']}
- Plataforma de streaming: {filters['platform']}
- Companhia: {filters['companionship']}
- Perfil de popularidade/estilo: {filters['popularity']}

Priorize gênero, vibe e plataforma especificada; depois duração e companhia; por fim época e popularidade. Os filtros são preferências contextuais, não generalizações rígidas. A duração curta deve favorecer títulos de até 90 minutos; a faixa padrão, 90 a 120; a longa, acima de 120. Quando houver plataforma específica, trate disponibilidade como dado a ser validado por uma fonte externa, nunca como fato conhecido apenas pela IA. Para família com crianças, evite conteúdo inadequado quando a classificação for conhecida.

Selecione exatamente três filmes reais, ordenados da maior para a menor compatibilidade, e explique por que cada um combina com o perfil. O match_score é a compatibilidade própria do sistema entre 0 e 100, não é nota do IMDb, da crítica ou de qualquer outra fonte. Não escolha simplesmente os filmes mais populares.

Não invente avaliações, plataformas, disponibilidade, URLs, preços, datas, classificação indicativa ou premiações. Quando não tiver certeza, use 0, string vazia ou array vazio. A resposta deve obedecer exatamente ao JSON solicitado."""


def validate_recommendation_payload(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("recommendations"), list):
        raise RuntimeError("A resposta do motor não tem o formato esperado.")
    recommendations = payload["recommendations"]
    if len(recommendations) != 3:
        raise RuntimeError("O motor deve retornar exatamente 3 recomendações.")
    required = {"rank", "title_original", "title_pt", "year", "runtime_minutes", "genres", "synopsis", "why_it_matches", "match_score"}
    for index, recommendation in enumerate(recommendations, start=1):
        if not isinstance(recommendation, dict) or not required.issubset(recommendation):
            raise RuntimeError(f"A recomendação {index} está incompleta.")
        if recommendation.get("rank") != index:
            raise RuntimeError("As recomendações devem estar ordenadas por posição.")
        if not 0 <= int(recommendation.get("match_score", 0)) <= 100:
            raise RuntimeError("A pontuação de compatibilidade é inválida.")
    return payload


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
        "input": buildRecommendationPrompt(filters),
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
    try:
        structured = validate_recommendation_payload(json.loads(text))
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise RuntimeError("A OpenAI retornou um JSON inválido.") from error
    enriched = enrich_result(structured)
    return json.dumps(enriched, ensure_ascii=False, separators=(",", ":"))


class AppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC), **kwargs)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
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

    def current_user(self):
        return read_session(self.headers.get("Cookie"))

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
        path = urlparse(self.path)
        if path.path == "/api/health":
            self.send_result(api_core.health())
            return
        if path.path == "/api/auth/me":
            user = self.current_user()
            self.send_json(HTTPStatus.OK, {"authenticated": bool(user), "user": public_user(user)})
            return
        if path.path == "/api/auth/status":
            params = parse_qs(path.query)
            self.send_result(
                api_core.registration_status(params.get("email", [""])[0], params.get("token", [""])[0])
            )
            return
        if path.path in {"/api/admin/status", "/api/admin/users"}:
            self.send_result(api_core.admin_overview(self.current_user()))
            return
        if path.path.startswith("/api/"):
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Rota não encontrada."})
            return
        if path.path in {"/admin", "/admin/"}:
            self.path = "/admin.html"
        super().do_GET()

    def do_POST(self):
        if self.path == "/api/auth/register":
            self.send_result(api_core.register(self.request_json_or_empty(4_096)))
            return

        if self.path == "/api/auth/login":
            self.send_result(
                api_core.login(self.request_json_or_empty(4_096), is_secure_request(self.headers))
            )
            return

        if self.path == "/api/auth/logout":
            self.send_result(api_core.logout(is_secure_request(self.headers)))
            return

        if self.path == "/api/admin/users":
            self.send_result(
                api_core.admin_action(self.current_user(), self.request_json_or_empty(4_096))
            )
            return

        if self.path != "/api/recommend":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Rota não encontrada."})
            return
        user = self.current_user()
        if not user:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Faça login para receber recomendações."})
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
            body = self.request_json()
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
