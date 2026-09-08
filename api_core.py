"""Lógica compartilhada pelas rotas HTTP.

O servidor local (`server.py`) e as funções serverless em `api/` chamam as
mesmas funções daqui, de modo que as duas execuções nunca divirjam. Cada
função devolve `(status, payload, headers)`.
"""

from __future__ import annotations

import hmac
import json
import os
import urllib.error
import urllib.request
from http import HTTPStatus

import asaas_client
from asaas_client import AsaasError
from auth import (
    admin_email,
    authenticate,
    auth_secret,
    clear_session_cookie,
    config_status,
    create_session,
    public_user,
    root_admin_configured,
    session_cookie,
)
from metadata import enrich_result
from user_store import (
    PLAN_CATALOG,
    StorageError,
    admin_summary,
    admin_users,
    consume_credit,
    create_user,
    delete_reaction,
    delete_user,
    find_user,
    find_user_by_asaas_subscription,
    find_user_by_id,
    get_billing_status,
    get_status_by_token,
    list_reactions,
    plan_catalog_public,
    refund_credit,
    register_user,
    set_billing_plan,
    set_user_password,
    storage_diagnostics,
    update_user_role,
    update_user_status,
    upsert_reaction,
)


FORBIDDEN_ADMIN = (
    HTTPStatus.FORBIDDEN,
    {"error": "Acesso reservado ao administrador."},
    None,
)


def _text(body, *keys, default=""):
    for key in keys:
        value = body.get(key)
        if isinstance(value, str):
            return value
    return default


# ---------------------------------------------------------------------------
# Diagnóstico público
# ---------------------------------------------------------------------------


def health():
    """Somente indicadores booleanos: nenhum segredo é exposto."""
    storage = storage_diagnostics(probe=True)
    payload = {
        "ok": bool(storage["persistent"]) and storage.get("healthy") is not False,
        "storage": {
            "mode": storage["mode"],
            "label": storage["label"],
            "persistent": storage["persistent"],
            "healthy": storage["healthy"],
            "available": storage["available"],
            "error": storage["error"],
            "setup_hint": storage["setup_hint"],
            "postgres_source_env_var": storage.get("postgres_source_env_var"),
            "postgres_dsn_preview": storage.get("postgres_dsn_preview"),
        },
        "admin_credentials_configured": root_admin_configured(),
        "admin_panel": "/admin",
    }
    return HTTPStatus.OK, payload, None


# ---------------------------------------------------------------------------
# Autenticação
# ---------------------------------------------------------------------------


def register(body):
    email = _text(body, "email")
    password = _text(body, "password")
    confirmation = body.get("confirmation", body.get("password_confirmation"))
    if confirmation is not None and not isinstance(confirmation, str):
        return HTTPStatus.BAD_REQUEST, {"error": "Informe e-mail e senha."}, None
    try:
        record, token, was_reopened = register_user(email, password, confirmation)
    except FileExistsError as error:
        return HTTPStatus.CONFLICT, {"error": str(error)}, None
    except StorageError as error:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error), "storage": storage_diagnostics()}, None
    except ValueError as error:
        return HTTPStatus.BAD_REQUEST, {"error": str(error)}, None
    return (
        HTTPStatus.OK if was_reopened else HTTPStatus.CREATED,
        {
            "registered": True,
            "email": record["email"],
            "status": record["status"],
            "token": token,
            "message": "Seu acesso será liberado assim que o administrador validar. Por favor, aguarde a liberação.",
        },
        None,
    )


def login(body, secure: bool):
    email = _text(body, "email")
    password = _text(body, "password")
    if not email or not password:
        return HTTPStatus.BAD_REQUEST, {"error": "Informe e-mail e senha."}, None
    if not auth_secret():
        return (
            HTTPStatus.SERVICE_UNAVAILABLE,
            {
                "error": "O login está indisponível porque este deploy não tem AUTH_SECRET configurado. "
                "Defina a variável de ambiente AUTH_SECRET (um valor longo e aleatório) nas configurações "
                "do projeto e faça um novo deploy."
            },
            None,
        )
    try:
        account = find_user(email)
    except StorageError as error:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)}, None
    if account and account.get("status") == "pending":
        return (
            HTTPStatus.FORBIDDEN,
            {"error": "Seu cadastro ainda está aguardando a validação do administrador."},
            None,
        )
    if account and account.get("status") == "rejected":
        return (
            HTTPStatus.FORBIDDEN,
            {"error": "Seu pedido de acesso foi rejeitado. Você pode realizar um novo cadastro."},
            None,
        )
    user = authenticate(email, password)
    if not user:
        if email.strip().lower() == admin_email() and not root_admin_configured():
            return (
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "error": "As credenciais de administrador não estão configuradas neste deploy. "
                    "Defina ADMIN_EMAIL e ADMIN_PASSWORD nas variáveis de ambiente do projeto."
                },
                None,
            )
        return HTTPStatus.UNAUTHORIZED, {"error": "E-mail ou senha inválidos."}, None
    cookie = session_cookie(create_session(user["email"], user["role"], user.get("user_id")), secure)
    return HTTPStatus.OK, {"authenticated": True, "user": public_user(user)}, {"Set-Cookie": cookie}


def logout(secure: bool):
    return HTTPStatus.OK, {"authenticated": False}, {"Set-Cookie": clear_session_cookie(secure)}


def registration_status(email: str, token: str):
    try:
        status = get_status_by_token(email, token)
    except StorageError as error:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)}, None
    if not status:
        return HTTPStatus.NOT_FOUND, {"error": "Pedido de acesso não encontrado."}, None
    return HTTPStatus.OK, {"email": email.strip().lower(), "status": status}, None


# ---------------------------------------------------------------------------
# Painel administrativo
# ---------------------------------------------------------------------------


def admin_overview(session):
    if not session or session.get("role") != "admin":
        return FORBIDDEN_ADMIN
    try:
        return (
            HTTPStatus.OK,
            {
                "user": public_user(session),
                "config": config_status(),
                "summary": admin_summary(),
                "users": admin_users(),
            },
            None,
        )
    except StorageError as error:
        return (
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": str(error), "storage": storage_diagnostics(), "user": public_user(session)},
            None,
        )


ADMIN_ACTIONS_WITHOUT_ID = {"create"}
SELF_DESTRUCTIVE_ACTIONS = {"delete", "reject", "pending", "demote"}


def _blocks_own_access(session, action: str, user_id: str) -> bool:
    """Impede que o administrador logado tire o próprio acesso ao painel."""
    if action not in SELF_DESTRUCTIVE_ACTIONS:
        return False
    target = find_user_by_id(user_id)
    if not target:
        return False
    return str(target.get("email", "")).strip().lower() == str(session.get("email", "")).strip().lower()


def admin_action(session, body):
    if not session or session.get("role") != "admin":
        return FORBIDDEN_ADMIN

    action = _text(body, "action")
    user_id = _text(body, "user_id")
    if action not in ADMIN_ACTIONS_WITHOUT_ID and not user_id:
        return HTTPStatus.BAD_REQUEST, {"error": "Ação administrativa inválida."}, None

    try:
        if _blocks_own_access(session, action, user_id):
            return (
                HTTPStatus.BAD_REQUEST,
                {"error": "Você não pode remover o seu próprio acesso de administrador. "
                          "Peça a outro administrador, ou use outra conta para fazer isso."},
                None,
            )
        if action == "approve":
            payload = {"user": update_user_status(user_id, "approved")}
        elif action == "reject":
            payload = {"user": update_user_status(user_id, "rejected")}
        elif action == "pending":
            payload = {"user": update_user_status(user_id, "pending")}
        elif action == "delete":
            payload = {"deleted": True}
            delete_user(user_id)
        elif action == "promote":
            payload = {"user": update_user_role(user_id, "admin")}
        elif action == "demote":
            payload = {"user": update_user_role(user_id, "user")}
        elif action == "set_password":
            payload = {"user": set_user_password(user_id, _text(body, "password"))}
        elif action == "create":
            payload = {
                "user": create_user(
                    _text(body, "email"),
                    _text(body, "password"),
                    status=_text(body, "status", default="approved"),
                    role=_text(body, "role", default="user"),
                )
            }
        else:
            return HTTPStatus.BAD_REQUEST, {"error": "Ação administrativa inválida."}, None
        payload["summary"] = admin_summary()
        payload["users"] = admin_users()
        return HTTPStatus.OK, payload, None
    except FileExistsError as error:
        return HTTPStatus.CONFLICT, {"error": str(error)}, None
    except LookupError as error:
        return HTTPStatus.NOT_FOUND, {"error": str(error)}, None
    except StorageError as error:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error), "storage": storage_diagnostics()}, None
    except (ValueError, json.JSONDecodeError) as error:
        return HTTPStatus.BAD_REQUEST, {"error": str(error)}, None


# ---------------------------------------------------------------------------
# Motor de recomendação
# ---------------------------------------------------------------------------

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


def _preferences_prompt_block(user_id: str | None) -> str:
    """Resumo das marcações anteriores do usuário, para o prompt evitar repetições."""
    if not user_id:
        return ""
    try:
        reactions = list_reactions(user_id)
    except LookupError:
        return ""
    if not reactions:
        return ""

    def label(item):
        title = item.get("title_pt") or item.get("title_original") or ""
        return f"{title} ({item['year']})" if item.get("year") else title

    avoid = sorted({label(item) for item in reactions if item.get("watched") or item.get("liked") is False})
    liked = sorted({label(item) for item in reactions if item.get("liked") is True})
    if not avoid and not liked:
        return ""

    lines = ["\nPreferências conhecidas deste usuário, com base em marcações anteriores dele na plataforma:"]
    if avoid:
        lines.append(f"- Já assistiu ou não gostou (NÃO recomende de novo nenhum destes): {', '.join(avoid)}.")
    if liked:
        lines.append(f"- Gostou anteriormente (pode considerar estilos parecidos, mas não repita o mesmo título): {', '.join(liked)}.")
    return "\n".join(lines) + "\n"


def buildRecommendationPrompt(filters, preferences_block: str = ""):
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
{preferences_block}
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


def call_openai(filters, preferences_block: str = ""):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY não foi configurada no servidor.")
    payload = {
        "model": os.environ.get("OPENAI_MODEL", "gpt-5.6-luna"),
        "input": buildRecommendationPrompt(filters, preferences_block),
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


def recommend(session, body):
    if not session:
        return HTTPStatus.UNAUTHORIZED, {"error": "Faça login para receber recomendações."}, None
    is_admin = session.get("role") == "admin"
    user_id = session.get("user_id")
    if not is_admin:
        if not user_id:
            return HTTPStatus.FORBIDDEN, {"error": "Sessão inválida."}, None
        try:
            consume_credit(user_id)
        except PermissionError as error:
            return HTTPStatus.PAYMENT_REQUIRED, {"error": str(error)}, None
        except LookupError as error:
            return HTTPStatus.NOT_FOUND, {"error": str(error)}, None

    try:
        filters = clean_filters(body.get("filters"))
    except ValueError as error:
        if not is_admin:
            refund_credit(user_id)
        return HTTPStatus.BAD_REQUEST, {"error": str(error)}, None

    preferences_block = "" if is_admin else _preferences_prompt_block(user_id)
    try:
        text = call_openai(filters, preferences_block)
    except RuntimeError as error:
        if not is_admin:
            refund_credit(user_id)
        return HTTPStatus.BAD_GATEWAY, {"error": str(error)}, None
    return HTTPStatus.OK, {"text": text}, None


# ---------------------------------------------------------------------------
# Assinatura e créditos (ASAAS)
# ---------------------------------------------------------------------------

ASAAS_ACTIVE_STATUSES = {"CONFIRMED", "RECEIVED", "RECEIVED_IN_CASH"}
ASAAS_OVERDUE_STATUSES = {"OVERDUE"}
ASAAS_CANCELED_STATUSES = {"REFUNDED", "REFUND_REQUESTED", "CHARGEBACK_REQUESTED", "CHARGEBACK_DISPUTE", "AWAITING_CHARGEBACK_REVERSAL", "DELETED"}


def billing_status(session):
    if not session:
        return HTTPStatus.UNAUTHORIZED, {"error": "Faça login para ver sua assinatura."}, None
    payload = {"plans": plan_catalog_public()}
    if session.get("role") == "admin":
        payload["account"] = {
            "plan": "admin", "plan_label": "Administrador", "plan_status": "active",
            "daily_limit": None, "unlimited": True, "used_today": 0, "remaining_today": None,
        }
        return HTTPStatus.OK, payload, None
    user_id = session.get("user_id")
    if not user_id:
        payload["account"] = None
        return HTTPStatus.OK, payload, None
    try:
        payload["account"] = get_billing_status(user_id)
    except LookupError:
        return HTTPStatus.NOT_FOUND, {"error": "Usuário não encontrado."}, None
    return HTTPStatus.OK, payload, None


def _only_digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def start_checkout(session, body):
    if not session or session.get("role") == "admin":
        return HTTPStatus.FORBIDDEN, {"error": "Apenas contas de cliente podem assinar um plano."}, None
    user_id = session.get("user_id")
    if not user_id:
        return HTTPStatus.FORBIDDEN, {"error": "Sessão inválida."}, None

    plan = _text(body, "plan")
    name = _text(body, "name").strip()
    cpf_cnpj = _only_digits(_text(body, "cpf_cnpj"))
    if plan not in PLAN_CATALOG:
        return HTTPStatus.BAD_REQUEST, {"error": "Escolha um plano válido."}, None
    if not name or len(name) > 150:
        return HTTPStatus.BAD_REQUEST, {"error": "Informe seu nome completo."}, None
    if len(cpf_cnpj) not in (11, 14):
        return HTTPStatus.BAD_REQUEST, {"error": "Informe um CPF ou CNPJ válido."}, None
    if not asaas_client.is_configured():
        return (
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "O pagamento está indisponível neste deploy. Configure ASAAS_API_KEY nas variáveis de ambiente."},
            None,
        )

    user = find_user_by_id(user_id)
    if not user:
        return HTTPStatus.NOT_FOUND, {"error": "Usuário não encontrado."}, None
    try:
        customer = asaas_client.get_or_create_customer(name, cpf_cnpj, user["email"], user_id)
        subscription = asaas_client.create_subscription(customer["id"], plan, PLAN_CATALOG[plan]["price"], user_id)
        payments = asaas_client.list_subscription_payments(subscription["id"])
        invoice_url = payments[0].get("invoiceUrl") if payments else subscription.get("invoiceUrl", "")
        set_billing_plan(
            user_id,
            plan=plan,
            plan_status="pending",
            asaas_customer_id=customer.get("id", ""),
            asaas_subscription_id=subscription.get("id", ""),
        )
    except AsaasError as error:
        return HTTPStatus.BAD_GATEWAY, {"error": f"Não foi possível iniciar o pagamento: {error}"}, None

    return HTTPStatus.OK, {"checkout_url": invoice_url, "plan": plan, "subscription_id": subscription.get("id", "")}, None


def billing_webhook(body, headers):
    expected_token = os.environ.get("ASAAS_WEBHOOK_TOKEN", "").strip()
    if expected_token:
        received = (headers.get("asaas-access-token") or "").strip()
        if not received or not hmac.compare_digest(received, expected_token):
            return HTTPStatus.UNAUTHORIZED, {"error": "Token de webhook inválido."}, None
    if not isinstance(body, dict):
        return HTTPStatus.BAD_REQUEST, {"error": "Payload inválido."}, None

    payment = body.get("payment") or {}
    subscription_id = payment.get("subscription") or ""
    external_reference = payment.get("externalReference") or ""
    status = str(payment.get("status") or "").upper()

    user = find_user_by_id(external_reference) if external_reference else None
    if not user and subscription_id:
        user = find_user_by_asaas_subscription(subscription_id)
    if not user:
        return HTTPStatus.OK, {"ignored": True}, None

    if status in ASAAS_ACTIVE_STATUSES:
        set_billing_plan(user["id"], plan_status="active")
    elif status in ASAAS_OVERDUE_STATUSES:
        set_billing_plan(user["id"], plan_status="past_due")
    elif status in ASAAS_CANCELED_STATUSES:
        set_billing_plan(user["id"], plan_status="canceled")
    return HTTPStatus.OK, {"received": True}, None


# ---------------------------------------------------------------------------
# Marcações do usuário (gostei / não gostei / já assisti)
# ---------------------------------------------------------------------------


def list_preferences(session):
    if not session:
        return HTTPStatus.UNAUTHORIZED, {"error": "Faça login para ver suas marcações."}, None
    user_id = session.get("user_id")
    if not user_id:
        return HTTPStatus.OK, {"reactions": []}, None
    try:
        return HTTPStatus.OK, {"reactions": list_reactions(user_id)}, None
    except LookupError:
        return HTTPStatus.NOT_FOUND, {"error": "Usuário não encontrado."}, None


def mutate_preference(session, body):
    if not session:
        return HTTPStatus.UNAUTHORIZED, {"error": "Faça login para marcar títulos."}, None
    user_id = session.get("user_id")
    if not user_id:
        return HTTPStatus.FORBIDDEN, {"error": "Sessão inválida."}, None

    action = _text(body, "action", default="upsert")
    try:
        if action == "delete":
            key = _text(body, "key")
            if not key:
                return HTTPStatus.BAD_REQUEST, {"error": "Informe a marcação a remover."}, None
            delete_reaction(user_id, key)
            return HTTPStatus.OK, {"deleted": True, "reactions": list_reactions(user_id)}, None

        title_original = _text(body, "title_original")
        title_pt = _text(body, "title_pt")
        if not title_original and not title_pt:
            return HTTPStatus.BAD_REQUEST, {"error": "Informe o título do filme."}, None
        year = body.get("year") or 0
        liked = body.get("liked", None)
        if liked not in (True, False, None):
            return HTTPStatus.BAD_REQUEST, {"error": "Valor de 'liked' inválido."}, None
        watched = bool(body.get("watched", False))
        record = upsert_reaction(user_id, title_original, title_pt, year, liked, watched)
        return HTTPStatus.OK, {"reaction": record}, None
    except LookupError as error:
        return HTTPStatus.NOT_FOUND, {"error": str(error) or "Marcação não encontrada."}, None
