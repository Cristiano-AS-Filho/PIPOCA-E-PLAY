#!/usr/bin/env python3
"""Servidor local do Pipoca & Play.

A chave OpenAI nunca é enviada para o navegador: somente este processo a lê.
"""

import json
import os
import secrets
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from auth import (
    authenticate,
    clear_session_cookie,
    config_status,
    create_session,
    is_secure_request,
    public_user,
    read_session,
    session_cookie,
)
from user_store import (
    StorageError,
    activate_subscription,
    admin_summary,
    admin_users,
    consume_credit,
    delete_user,
    find_user,
    find_user_by_asaas_customer_id,
    find_user_by_token,
    get_status_by_token,
    get_user_by_id,
    public_subscription_fields,
    register_user,
    set_asaas_customer_id,
    set_subscription_status,
    update_user_status,
)
from metadata import enrich_result
from rate_limit import check_rate_limit
from plans import PLANS, plan_exists
import asaas_client

ROOT = Path(__file__).parent
PUBLIC = ROOT / "public"
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
                    "match_score", "where_to_watch",
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


# Seção 4 da especificação: a duração é uma restrição, não uma sugestão.
# (mínimo exclusivo, máximo inclusivo) em minutos; None = sem limite nesse lado.
DURATION_BOUNDS = {
    "Curto (Até 90 min)": (None, 90),
    "Padrão (90 a 120 min)": (90, 120),
    "Longo (Mais de 120 min)": (120, None),
}


def duration_violations(duration_filter, recommendations):
    bounds = DURATION_BOUNDS.get(duration_filter)
    if not bounds:
        return []
    minimum, maximum = bounds
    violating = []
    for recommendation in recommendations:
        runtime = recommendation.get("runtime_minutes") or 0
        if runtime <= 0:
            continue  # duração desconhecida: não há como validar, não pune o candidato
        if minimum is not None and runtime <= minimum:
            violating.append(recommendation)
        elif maximum is not None and runtime > maximum:
            violating.append(recommendation)
    return violating


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


def _fetch_openai_recommendations(filters, extra_instructions=""):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY não foi configurada no servidor.")
    prompt = buildRecommendationPrompt(filters)
    if extra_instructions:
        prompt = f"{prompt}\n\n{extra_instructions}"
    payload = {
        "model": os.environ.get("OPENAI_MODEL", "gpt-5.6-luna"),
        "input": prompt,
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
        return validate_recommendation_payload(json.loads(text))
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise RuntimeError("A OpenAI retornou um JSON inválido.") from error


def call_openai(filters):
    structured = _fetch_openai_recommendations(filters)
    violations = duration_violations(filters.get("duration"), structured["recommendations"])
    if violations:
        offenders = ", ".join(
            f"{item.get('title_original') or item.get('title_pt')} ({item.get('runtime_minutes')} min)"
            for item in violations
        )
        corrective_note = (
            "Sua resposta anterior violou a restrição de tempo disponível: "
            f"{offenders}. Tempo disponível é uma restrição obrigatória, não uma preferência — "
            f"substitua por filmes reais cuja duração respeite \"{filters.get('duration')}\"."
        )
        try:
            retry = _fetch_openai_recommendations(filters, corrective_note)
        except RuntimeError:
            retry = None
        if retry and not duration_violations(filters.get("duration"), retry["recommendations"]):
            structured = retry
    enriched = enrich_result(structured)
    return json.dumps(enriched, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Assinatura e cobrança (Asaas)
# ---------------------------------------------------------------------------

def _absolute_base_url(headers) -> str:
    configured = os.environ.get("PUBLIC_BASE_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    host = headers.get("Host", "127.0.0.1:8000")
    scheme = "https" if is_secure_request(headers) else "http"
    return f"{scheme}://{host}"


def user_payload(session_user):
    """public_user() + estado de assinatura, pra front saber quantos créditos
    restam sem precisar de uma chamada separada."""
    payload = public_user(session_user)
    if session_user and session_user.get("role") == "user" and session_user.get("user_id"):
        record = get_user_by_id(session_user["user_id"])
        if record:
            payload.update(public_subscription_fields(record))
    return payload


def resolve_billing_user(session_user, body):
    """Acha o cadastro completo pra iniciar um checkout.

    Uma conta recém-criada ainda não tem sessão (login é bloqueado até
    aprovação/pagamento), então além da sessão normal aceitamos provar posse
    da conta com o par (email, token) que o cadastro já devolveu — o mesmo
    usado pra tela de "aguardando liberação".
    """
    if session_user and session_user.get("role") == "user" and session_user.get("user_id"):
        record = get_user_by_id(session_user["user_id"])
        if record:
            return record
    email = body.get("email", "")
    token = body.get("token", "")
    if isinstance(email, str) and isinstance(token, str) and email and token:
        return find_user_by_token(email, token)
    return None


def create_checkout_session(user_record, plan_id, headers) -> str:
    if not plan_exists(plan_id):
        raise ValueError("Plano desconhecido.")
    if not asaas_client.is_configured():
        raise RuntimeError("A cobrança ainda não foi configurada no servidor.")

    plan = PLANS[plan_id]
    base_url = _absolute_base_url(headers)
    external_reference = f"{user_record['id']}:{plan_id}"
    try:
        customer = asaas_client.get_or_create_customer(
            name=user_record["email"].split("@")[0].title(),
            email=user_record["email"],
            external_reference=user_record["id"],
        )
        customer_id = customer.get("id", "")
        if customer_id and customer_id != user_record.get("asaas_customer_id"):
            set_asaas_customer_id(user_record["id"], customer_id)
        checkout = asaas_client.create_subscription_checkout(
            customer_id=customer_id,
            plan_name=plan["name"],
            price=plan["price"],
            external_reference=external_reference,
            success_url=f"{base_url}/?checkout=success",
            cancel_url=f"{base_url}/?checkout=cancelled",
        )
    except asaas_client.AsaasError as error:
        raise RuntimeError(f"Não foi possível iniciar o pagamento: {error}") from error
    return asaas_client.checkout_redirect_url(checkout)


# Eventos do Asaas que ativam/renovam a assinatura (créditos voltam a valer).
_ACTIVATE_EVENTS = {"PAYMENT_CONFIRMED", "PAYMENT_RECEIVED", "CHECKOUT_PAID"}
# Cobrança atrasada: mantemos o cadastro, mas suspendemos o acesso até regularizar.
_OVERDUE_EVENTS = {"PAYMENT_OVERDUE"}
# Assinatura encerrada de vez (cancelamento, estorno, exclusão da cobrança).
_CANCEL_EVENTS = {
    "PAYMENT_DELETED",
    "PAYMENT_REFUNDED",
    "SUBSCRIPTION_DELETED",
    "SUBSCRIPTION_INACTIVATED",
    "CHECKOUT_CANCELED",
    "CHECKOUT_EXPIRED",
}


def verify_asaas_webhook(headers) -> bool:
    expected = os.environ.get("ASAAS_WEBHOOK_TOKEN", "").strip()
    if not expected:
        return False
    received = headers.get("asaas-access-token", "")
    return bool(received) and secrets.compare_digest(received, expected)


def _resolve_webhook_user(payment: dict):
    external_reference = str(payment.get("externalReference") or "")
    if ":" in external_reference:
        user_id, plan_id = external_reference.split(":", 1)
        user = get_user_by_id(user_id)
        if user:
            return user, plan_id
    customer_id = str(payment.get("customer") or "")
    user = find_user_by_asaas_customer_id(customer_id)
    if user:
        return user, user.get("plan_id")
    return None, None


def handle_asaas_webhook_event(payload: dict) -> None:
    event = str(payload.get("event") or "")
    payment = payload.get("payment") or {}
    user, plan_id = _resolve_webhook_user(payment)
    if not user:
        return  # nada pra mapear de volta — ignora silenciosamente (idempotente)

    if event in _ACTIVATE_EVENTS and plan_id and plan_exists(plan_id):
        activate_subscription(
            user["id"],
            plan_id,
            asaas_subscription_id=str(payment.get("subscription") or ""),
        )
    elif event in _OVERDUE_EVENTS:
        set_subscription_status(user["id"], "overdue")
    elif event in _CANCEL_EVENTS:
        set_subscription_status(user["id"], "cancelled")
    # Outros eventos (ex: PAYMENT_CREATED, PAYMENT_UPDATED) não mudam acesso — ignorados.


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

    def do_GET(self):
        path = urlparse(self.path)
        if path.path == "/api/auth/me":
            user = self.current_user()
            self.send_json(HTTPStatus.OK, {"authenticated": bool(user), "user": user_payload(user)})
            return
        if path.path == "/api/auth/status":
            try:
                params = parse_qs(path.query)
                email = params.get("email", [""])[0]
                token = params.get("token", [""])[0]
                status = get_status_by_token(email, token)
                if not status:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "Pedido de acesso não encontrado."})
                    return
                self.send_json(HTTPStatus.OK, {"email": email.strip().lower(), "status": status})
            except StorageError as error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
            return
        if path.path in {"/api/admin/status", "/api/admin/users"}:
            user = self.current_user()
            if not user or user.get("role") != "admin":
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "Acesso reservado ao administrador."})
                return
            try:
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "user": public_user(user),
                        "config": config_status(),
                        "summary": admin_summary(),
                        "users": admin_users(),
                    },
                )
            except StorageError as error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
            return
        if path.path.startswith("/api/"):
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Rota não encontrada."})
            return
        super().do_GET()

    def do_POST(self):
        if self.path == "/api/auth/register":
            try:
                body = self.request_json(4_096)
                email = body.get("email", "")
                password = body.get("password", "")
                confirmation = body.get("confirmation", body.get("password_confirmation"))
                if not isinstance(email, str) or not isinstance(password, str) or (confirmation is not None and not isinstance(confirmation, str)):
                    raise ValueError("Informe e-mail e senha.")
                record, token, was_reopened = register_user(email, password, confirmation)
                self.send_json(
                    HTTPStatus.OK if was_reopened else HTTPStatus.CREATED,
                    {
                        "registered": True,
                        "email": record["email"],
                        "status": record["status"],
                        "token": token,
                        "message": "Seu acesso será liberado assim que o administrador validar. Por favor, aguarde a liberação.",
                    },
                )
            except FileExistsError as error:
                self.send_json(HTTPStatus.CONFLICT, {"error": str(error)})
            except StorageError as error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
            except (ValueError, json.JSONDecodeError) as error:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return

        if self.path == "/api/auth/login":
            try:
                body = self.request_json(4_096)
                email = body.get("email", "")
                password = body.get("password", "")
                if not isinstance(email, str) or not isinstance(password, str):
                    raise ValueError("Informe e-mail e senha.")
                account = find_user(email)
                if account and account.get("status") == "pending":
                    self.send_json(HTTPStatus.FORBIDDEN, {"error": "Seu cadastro ainda está aguardando a validação do administrador."})
                    return
                if account and account.get("status") == "rejected":
                    self.send_json(HTTPStatus.FORBIDDEN, {"error": "Seu pedido de acesso foi rejeitado. Você pode realizar um novo cadastro."})
                    return
                user = authenticate(email, password)
                if not user:
                    self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "E-mail ou senha inválidos."})
                    return
                self.send_json(
                    HTTPStatus.OK,
                    {"authenticated": True, "user": user_payload(user)},
                    {"Set-Cookie": session_cookie(create_session(user["email"], user["role"], user.get("user_id")), is_secure_request(self.headers))},
                )
            except StorageError as error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
            except (ValueError, json.JSONDecodeError) as error:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return

        if self.path == "/api/auth/logout":
            self.send_json(
                HTTPStatus.OK,
                {"authenticated": False},
                {"Set-Cookie": clear_session_cookie(is_secure_request(self.headers))},
            )
            return

        if self.path == "/api/admin/users":
            user = self.current_user()
            if not user or user.get("role") != "admin":
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "Acesso reservado ao administrador."})
                return
            try:
                body = self.request_json(4_096)
                action = body.get("action", "")
                user_id = body.get("user_id", "")
                if not isinstance(action, str) or not isinstance(user_id, str) or not user_id:
                    raise ValueError("Ação administrativa inválida.")
                if action == "delete":
                    delete_user(user_id)
                    payload = {"deleted": True}
                elif action in {"approve", "reject"}:
                    payload = {"user": update_user_status(user_id, "approved" if action == "approve" else "rejected")}
                else:
                    raise ValueError("Ação administrativa inválida.")
                payload["summary"] = admin_summary()
                payload["users"] = admin_users()
                self.send_json(HTTPStatus.OK, payload)
            except LookupError as error:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": str(error)})
            except StorageError as error:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
            except (ValueError, json.JSONDecodeError) as error:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return

        if self.path == "/api/billing/checkout":
            try:
                body = self.request_json(4_096)
                plan_id = body.get("plan_id", "")
                if not isinstance(plan_id, str):
                    raise ValueError("Plano inválido.")
                user_record = resolve_billing_user(self.current_user(), body)
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
            return

        if self.path == "/api/webhooks/asaas":
            if not verify_asaas_webhook(self.headers):
                self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Assinatura do webhook inválida."})
                return
            try:
                payload = self.request_json(65_536)
                handle_asaas_webhook_event(payload)
            except (ValueError, json.JSONDecodeError, LookupError, StorageError):
                pass  # webhook sempre responde 200 pra evitar reenvio em loop; erro fica só no log
            self.send_json(HTTPStatus.OK, {"received": True})
            return

        if self.path != "/api/recommend":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Rota não encontrada."})
            return
        user = self.current_user()
        if not user:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Faça login para receber recomendações."})
            return
        if not check_rate_limit(user["email"], MAX_REQUESTS_PER_MINUTE, 60):
            self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Aguarde um minuto antes de tentar novamente."})
            return
        if user.get("role") != "admin":
            user_id = user.get("user_id", "")
            try:
                allowed, credit_info = consume_credit(user_id)
            except LookupError as error:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": str(error)})
                return
            if not allowed:
                if credit_info.get("subscription_status") != "active":
                    message = "Assine um plano para buscar recomendações."
                else:
                    message = "Seus créditos de hoje acabaram. Eles renovam amanhã, ou você pode subir de plano."
                self.send_json(HTTPStatus.PAYMENT_REQUIRED, {"error": message, "subscription": credit_info})
                return
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
