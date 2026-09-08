"""Lógica compartilhada pelas rotas HTTP.

O servidor local (`server.py`) e as funções serverless em `api/` chamam as
mesmas funções daqui, de modo que as duas execuções nunca divirjam. Cada
função devolve `(status, payload, headers)`.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from http import HTTPStatus

import asaas
from auth import (
    admin_email,
    authenticate,
    clear_session_cookie,
    config_status,
    create_session,
    public_user,
    root_admin_configured,
    session_cookie,
)
from metadata import enrich_result
from plans import PLAN_CATALOG, public_plans
from user_store import (
    NoActiveSubscriptionError,
    OutOfCreditsError,
    StorageError,
    activate_subscription,
    admin_summary,
    admin_users,
    create_user,
    deactivate_subscription,
    delete_user,
    find_user,
    find_user_by_asaas_customer,
    find_user_by_asaas_subscription,
    find_user_by_id,
    get_status_by_token,
    list_marks,
    refund_credit,
    register_user,
    remove_mark,
    set_checkout_pending,
    set_user_password,
    storage_diagnostics,
    subscription_status,
    try_consume_credit,
    update_user_role,
    update_user_status,
    upsert_mark,
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
# Planos e checkout (ASAAS)
# ---------------------------------------------------------------------------


def list_plans_route():
    return HTTPStatus.OK, {"plans": public_plans()}, None


def payment_status(session):
    if not session or not session.get("user_id"):
        return HTTPStatus.UNAUTHORIZED, {"error": "Faça login para ver sua assinatura."}, None
    try:
        status = subscription_status(session["user_id"])
    except LookupError as error:
        return HTTPStatus.NOT_FOUND, {"error": str(error)}, None
    return HTTPStatus.OK, {"subscription": status, "plans": public_plans()}, None


def create_checkout(session, body):
    if not session or not session.get("user_id"):
        return HTTPStatus.UNAUTHORIZED, {"error": "Faça login para assinar um plano."}, None
    plan_id = _text(body, "plan")
    if plan_id not in PLAN_CATALOG:
        return HTTPStatus.BAD_REQUEST, {"error": "Escolha um plano válido."}, None
    if not asaas.is_configured():
        return (
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "O checkout de pagamento ainda não foi configurado neste deploy. Defina ASAAS_API_KEY."},
            None,
        )
    try:
        customer = asaas.ensure_customer(session["email"])
        subscription = asaas.create_subscription(customer["id"], plan_id, session["user_id"])
        checkout_url = asaas.get_first_payment_checkout_url(subscription["id"])
        set_checkout_pending(session["user_id"], plan_id, customer["id"], subscription["id"])
    except asaas.AsaasError as error:
        return HTTPStatus.BAD_GATEWAY, {"error": str(error)}, None
    except StorageError as error:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)}, None
    if not checkout_url:
        return (
            HTTPStatus.BAD_GATEWAY,
            {"error": "Não foi possível gerar o link de pagamento agora. Tente novamente em instantes."},
            None,
        )
    return HTTPStatus.OK, {"checkout_url": checkout_url, "plan": plan_id}, None


def payment_webhook(headers, body):
    """Rota pública chamada pelo ASAAS quando o status de um pagamento muda.

    Só libera os resultados do cliente depois de um evento de pagamento confirmado;
    o header ``asaas-access-token`` precisa bater com ``ASAAS_WEBHOOK_TOKEN``.
    """
    token = headers.get("asaas-access-token") or headers.get("Asaas-Access-Token")
    if not asaas.verify_webhook_token(token):
        return HTTPStatus.UNAUTHORIZED, {"error": "Token de webhook inválido."}, None

    event = str(body.get("event") or "")
    payment = body.get("payment") or {}
    subscription_id = payment.get("subscription")
    customer_id = payment.get("customer")

    user = None
    try:
        if subscription_id:
            user = find_user_by_asaas_subscription(subscription_id)
        if not user and customer_id:
            user = find_user_by_asaas_customer(customer_id)
        if not user:
            return HTTPStatus.OK, {"received": True, "matched": False}, None

        if event in {"PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"}:
            activate_subscription(user["id"])
        elif event in {"PAYMENT_OVERDUE", "PAYMENT_DELETED", "PAYMENT_REFUNDED", "PAYMENT_CHARGEBACK_REQUESTED"}:
            deactivate_subscription(user["id"], status="past_due")
        elif event == "SUBSCRIPTION_DELETED":
            deactivate_subscription(user["id"], status="canceled")
    except (LookupError, StorageError):
        return HTTPStatus.OK, {"received": True, "matched": True, "applied": False}, None

    return HTTPStatus.OK, {"received": True, "matched": True}, None


# ---------------------------------------------------------------------------
# Marcações do usuário: gostei / não gostei / já assisti
# ---------------------------------------------------------------------------


def marks_list(session):
    if not session or not session.get("user_id"):
        return HTTPStatus.UNAUTHORIZED, {"error": "Faça login para ver suas marcações."}, None
    try:
        return HTTPStatus.OK, {"marks": list_marks(session["user_id"])}, None
    except LookupError as error:
        return HTTPStatus.NOT_FOUND, {"error": str(error)}, None
    except StorageError as error:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)}, None


def marks_action(session, body):
    if not session or not session.get("user_id"):
        return HTTPStatus.UNAUTHORIZED, {"error": "Faça login para marcar um título."}, None

    action = _text(body, "action", default="set")
    try:
        if action == "remove":
            mark_id = _text(body, "id")
            if not mark_id:
                return HTTPStatus.BAD_REQUEST, {"error": "Informe a marcação a remover."}, None
            remove_mark(session["user_id"], mark_id)
            return HTTPStatus.OK, {"removed": True, "marks": list_marks(session["user_id"])}, None

        if action == "set":
            mark_kwargs = {}
            if "liked" in body:
                mark_kwargs["liked"] = body.get("liked")
            if "watched" in body:
                mark_kwargs["watched"] = body.get("watched")
            record = upsert_mark(
                session["user_id"],
                title_original=_text(body, "title_original"),
                title_pt=_text(body, "title_pt"),
                year=body.get("year", 0),
                **mark_kwargs,
            )
            return HTTPStatus.OK, {"mark": record}, None

        return HTTPStatus.BAD_REQUEST, {"error": "Ação de marcação inválida."}, None
    except ValueError as error:
        return HTTPStatus.BAD_REQUEST, {"error": str(error)}, None
    except LookupError as error:
        return HTTPStatus.NOT_FOUND, {"error": str(error)}, None
    except StorageError as error:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)}, None


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


def build_marks_context(marks: list[dict]) -> str:
    """Traduz o histórico de marcações do usuário em instruções para o prompt da IA."""
    if not marks:
        return ""

    def _fmt(items):
        parts = []
        for item in items[:40]:
            label = item.get("title_pt") or item.get("title_original") or ""
            if not label:
                continue
            year = item.get("year")
            parts.append(f"{label} ({year})" if year else label)
        return "; ".join(parts)

    liked = [m for m in marks if m.get("liked") is True]
    disliked = [m for m in marks if m.get("liked") is False]
    watched = [m for m in marks if m.get("watched")]

    all_titles = _fmt(marks)
    if not all_titles:
        return ""

    lines = [
        "Histórico pessoal deste usuário na plataforma (filmes/séries/novelas que ele já marcou):",
        f"- NÃO repita nenhum destes títulos na resposta desta vez: {all_titles}.",
    ]
    if liked:
        lines.append(f"- Ele(a) GOSTOU destes (prefira recomendações com gênero/vibe parecidos, sem repetir o próprio título): {_fmt(liked)}.")
    if disliked:
        lines.append(f"- Ele(a) NÃO GOSTOU destes (evite recomendações muito parecidas em gênero/vibe): {_fmt(disliked)}.")
    if watched:
        lines.append(f"- Ele(a) já assistiu estes (não repita): {_fmt(watched)}.")
    return "\n".join(lines)


def buildRecommendationPrompt(filters, marks_context=""):
    prompt = f"""Atue como um especialista em cinema e recomendador personalizado para o público brasileiro. Responda em pt-BR.

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
    if marks_context:
        prompt += "\n\n" + marks_context
    return prompt


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


def call_openai(filters, marks_context=""):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY não foi configurada no servidor.")
    payload = {
        "model": os.environ.get("OPENAI_MODEL", "gpt-5.6-luna"),
        "input": buildRecommendationPrompt(filters, marks_context),
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
    """Gera as três recomendações: exige sessão de usuário aprovado e assinatura paga com crédito disponível."""
    if not session or not session.get("user_id"):
        return HTTPStatus.UNAUTHORIZED, {"error": "Faça login para receber recomendações."}, None
    user_id = session["user_id"]

    try:
        filters = clean_filters(body.get("filters"))
    except ValueError as error:
        return HTTPStatus.BAD_REQUEST, {"error": str(error)}, None

    try:
        credit_view = try_consume_credit(user_id)
    except NoActiveSubscriptionError as error:
        return (
            HTTPStatus.PAYMENT_REQUIRED,
            {"error": str(error), "code": "no_subscription", "plans": public_plans()},
            None,
        )
    except OutOfCreditsError as error:
        return (
            HTTPStatus.PAYMENT_REQUIRED,
            {"error": str(error), "code": "out_of_credits"},
            None,
        )
    except LookupError as error:
        return HTTPStatus.NOT_FOUND, {"error": str(error)}, None
    except StorageError as error:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)}, None

    try:
        marks_context = build_marks_context(list_marks(user_id))
        text = call_openai(filters, marks_context)
    except RuntimeError as error:
        refund_credit(user_id)
        return HTTPStatus.BAD_GATEWAY, {"error": str(error)}, None
    except StorageError as error:
        refund_credit(user_id)
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)}, None

    return HTTPStatus.OK, {"text": text, "credits": credit_view}, None
