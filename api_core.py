"""Lógica compartilhada pelas rotas HTTP.

O servidor local (`server.py`) e as funções serverless em `api/` chamam as
mesmas funções daqui, de modo que as duas execuções nunca divirjam. Cada
função devolve `(status, payload, headers)`.
"""

from __future__ import annotations

import json
from http import HTTPStatus

from auth import (
    admin_email,
    auth_secret,
    authenticate,
    clear_session_cookie,
    config_status,
    create_session,
    public_user,
    root_admin_configured,
    session_cookie,
)
from user_store import (
    CreditError,
    StorageError,
    SubscriptionRequired,
    activate_subscription,
    add_history,
    admin_summary,
    admin_users,
    consume_credit,
    create_user,
    delete_user,
    find_user,
    find_user_by_billing,
    find_user_by_id,
    get_account,
    grant_credits,
    list_feedback,
    list_history,
    refund_credit,
    register_user,
    remove_feedback,
    remove_history,
    set_feedback,
    set_subscription_status,
    set_user_password,
    start_checkout,
    storage_diagnostics,
    update_user_role,
)
import billing
from plans import get_plan, public_plans


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
        "billing": billing.diagnostics(),
    }
    return HTTPStatus.OK, payload, None


# ---------------------------------------------------------------------------
# Autenticação
# ---------------------------------------------------------------------------


def register(body, secure: bool = False):
    """Cadastro do cliente: cria a conta e já devolve a sessão.

    Não há mais fila de aprovação. Quem controla a geração do resultado é o
    pagamento, conferido em ``recommend``.
    """
    email = _text(body, "email")
    password = _text(body, "password")
    confirmation = body.get("confirmation", body.get("password_confirmation"))
    if confirmation is not None and not isinstance(confirmation, str):
        return HTTPStatus.BAD_REQUEST, {"error": "Informe e-mail e senha."}, None
    try:
        record = register_user(email, password, confirmation)
    except FileExistsError as error:
        return HTTPStatus.CONFLICT, {"error": str(error)}, None
    except StorageError as error:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error), "storage": storage_diagnostics()}, None
    except ValueError as error:
        return HTTPStatus.BAD_REQUEST, {"error": str(error)}, None

    user = {"email": record["email"], "role": "user"}
    payload = {
        "registered": True,
        "email": record["email"],
        "user": public_user(user),
        "message": "Cadastro concluído. Escolha um plano e conclua o pagamento para gerar suas indicações.",
    }
    if not auth_secret():
        # Sem AUTH_SECRET não há como assinar a sessão: a conta existe, mas o
        # cliente precisa entrar pela tela de login (que explica o que falta).
        payload["authenticated"] = False
        return HTTPStatus.CREATED, payload, None
    cookie = session_cookie(create_session(record["email"], "user", record["id"]), secure)
    payload["authenticated"] = True
    return HTTPStatus.CREATED, payload, {"Set-Cookie": cookie}


def login(body, secure: bool):
    email = _text(body, "email")
    password = _text(body, "password")
    if not email or not password:
        return HTTPStatus.BAD_REQUEST, {"error": "Informe e-mail e senha."}, None
    try:
        # Toca a base antes de autenticar: banco fora do ar vira 503 explicado,
        # nunca um "e-mail ou senha inválidos" que mandaria o cliente para o suporte errado.
        find_user(email)
    except StorageError as error:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)}, None
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
SELF_DESTRUCTIVE_ACTIONS = {"delete", "demote"}


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
        if action == "delete":
            payload = {"deleted": True}
            delete_user(user_id)
        elif action == "promote":
            payload = {"user": update_user_role(user_id, "admin")}
        elif action == "demote":
            payload = {"user": update_user_role(user_id, "user")}
        elif action == "set_password":
            payload = {"user": set_user_password(user_id, _text(body, "password"))}
        elif action == "grant_plan":
            plan = get_plan(_text(body, "plan", "plan_id"))
            if not plan:
                return HTTPStatus.BAD_REQUEST, {"error": "Escolha um plano válido."}, None
            payload = {"account": activate_subscription(user_id, plan["id"], "", "ADMIN_GRANT")}
        elif action == "revoke_plan":
            payload = {"account": set_subscription_status(user_id, "canceled", "ADMIN_REVOKE")}
        elif action == "add_credits":
            # Concessão avulsa: soma ao saldo, registra quem concedeu e não cria
            # assinatura nem renovação. A checagem de papel admin já aconteceu acima.
            payload = grant_credits(
                user_id,
                body.get("credits", body.get("amount")),
                origin=_text(body, "origin", default="manual"),
                reason=_text(body, "reason", "note"),
                granted_by=str(session.get("email", "")),
            )
        elif action == "create":
            payload = {
                "user": create_user(
                    _text(body, "email"),
                    _text(body, "password"),
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
# Conta do cliente: plano, créditos e marcações
# ---------------------------------------------------------------------------


UNAUTHENTICATED = (
    HTTPStatus.UNAUTHORIZED,
    {"error": "Faça login para continuar.", "code": "unauthenticated"},
    None,
)


def _session_user_id(session):
    """Identificador da conta do cliente logado (o admin de ambiente não tem um)."""
    return str(session.get("user_id", "")) if session else ""


def _requires_account(session):
    """Valida a sessão e devolve (user_id, erro). O admin raiz não tem conta de cliente."""
    if not session:
        return "", UNAUTHENTICATED
    user_id = _session_user_id(session)
    if not user_id:
        return "", (
            HTTPStatus.FORBIDDEN,
            {
                "error": "Esta conta é administrativa e não consome créditos de cliente.",
                "code": "admin_account",
            },
            None,
        )
    return user_id, None


def plans_catalog():
    return HTTPStatus.OK, {"plans": public_plans(), "billing": billing.diagnostics()}, None


def account_overview(session):
    if not session:
        return UNAUTHENTICATED
    user_id = _session_user_id(session)
    if not user_id:
        return (
            HTTPStatus.OK,
            {
                "user": public_user(session),
                "account": {
                    "email": session.get("email", ""),
                    "role": "admin",
                    "subscription_active": True,
                    "unlimited": True,
                    "credits_remaining": -1,
                    "plan": "admin",
                    "plan_name": "Administrador",
                    "feedback_count": 0,
                },
                "plans": public_plans(),
            },
            None,
        )
    try:
        return (
            HTTPStatus.OK,
            {"user": public_user(session), "account": get_account(user_id), "plans": public_plans()},
            None,
        )
    except LookupError as error:
        return HTTPStatus.NOT_FOUND, {"error": str(error)}, None
    except StorageError as error:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)}, None


def feedback_overview(session):
    user_id, error = _requires_account(session)
    if error:
        return (HTTPStatus.OK, {"feedback": []}, None) if session else error
    try:
        return HTTPStatus.OK, {"feedback": list_feedback(user_id)}, None
    except LookupError as lookup_error:
        return HTTPStatus.NOT_FOUND, {"error": str(lookup_error)}, None
    except StorageError as storage_error:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(storage_error)}, None


def feedback_save(session, body):
    """Cria ou atualiza a marcação de um título para o usuário logado."""
    user_id, error = _requires_account(session)
    if error:
        return error
    try:
        feedback = set_feedback(
            user_id,
            {
                "title_pt": _text(body, "title_pt", "title"),
                "title_original": _text(body, "title_original"),
                "year": body.get("year", 0),
                "opinion": _text(body, "opinion"),
                "watched": bool(body.get("watched", False)),
            },
        )
        return HTTPStatus.OK, {"feedback": feedback}, None
    except LookupError as lookup_error:
        return HTTPStatus.NOT_FOUND, {"error": str(lookup_error)}, None
    except ValueError as value_error:
        return HTTPStatus.BAD_REQUEST, {"error": str(value_error)}, None
    except StorageError as storage_error:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(storage_error)}, None


def feedback_delete(session, body):
    """Remove uma marcação para que o título volte a aparecer nas indicações."""
    user_id, error = _requires_account(session)
    if error:
        return error
    feedback_id = _text(body, "id", "feedback_id")
    if not feedback_id:
        return HTTPStatus.BAD_REQUEST, {"error": "Informe qual marcação deve ser removida."}, None
    try:
        return HTTPStatus.OK, {"feedback": remove_feedback(user_id, feedback_id), "removed": True}, None
    except LookupError as lookup_error:
        return HTTPStatus.NOT_FOUND, {"error": str(lookup_error)}, None
    except StorageError as storage_error:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(storage_error)}, None


# ---------------------------------------------------------------------------
# Histórico de buscas: gravado na conta, nunca só no navegador
# ---------------------------------------------------------------------------


def history_overview(session):
    user_id, error = _requires_account(session)
    if error:
        return (HTTPStatus.OK, {"history": []}, None) if session else error
    try:
        return HTTPStatus.OK, {"history": list_history(user_id)}, None
    except LookupError as lookup_error:
        return HTTPStatus.NOT_FOUND, {"error": str(lookup_error)}, None
    except StorageError as storage_error:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(storage_error)}, None


def history_delete(session, body):
    """Apaga uma busca do histórico da conta logada."""
    user_id, error = _requires_account(session)
    if error:
        return error
    history_id = _text(body, "id", "history_id")
    if not history_id:
        return HTTPStatus.BAD_REQUEST, {"error": "Informe qual busca deve ser removida do histórico."}, None
    try:
        return HTTPStatus.OK, {"history": remove_history(user_id, history_id), "removed": True}, None
    except LookupError as lookup_error:
        return HTTPStatus.NOT_FOUND, {"error": str(lookup_error)}, None
    except StorageError as storage_error:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(storage_error)}, None


# ---------------------------------------------------------------------------
# Checkout (Asaas)
# ---------------------------------------------------------------------------


def billing_checkout(session, body):
    """Abre a assinatura na Asaas e devolve a URL de pagamento do cliente."""
    user_id, error = _requires_account(session)
    if error:
        return error
    plan = get_plan(_text(body, "plan", "plan_id"))
    if not plan:
        return HTTPStatus.BAD_REQUEST, {"error": "Escolha um dos planos disponíveis."}, None
    if not billing.is_configured():
        return (
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "O checkout ainda não está configurado neste deploy.", "code": "billing_unconfigured"},
            None,
        )
    try:
        name = billing.validate_name(_text(body, "name", "full_name"))
        document = billing.validate_document(_text(body, "document", "cpf", "cpf_cnpj"))
        account = get_account(user_id)
        customer_id = billing.ensure_customer(account["email"], name, document, external_reference=user_id)
        subscription = billing.create_subscription(customer_id, plan["id"], external_reference=user_id)
        subscription_id = str(subscription.get("id", ""))
        url = billing.checkout_url(subscription_id)
        updated = start_checkout(user_id, plan["id"], customer_id, subscription_id, url)
        if not url:
            return (
                HTTPStatus.BAD_GATEWAY,
                {"error": "A Asaas criou a assinatura, mas não devolveu a fatura. Tente novamente."},
                None,
            )
        return (
            HTTPStatus.OK,
            {"checkout_url": url, "plan": plan["id"], "subscription_id": subscription_id, "account": updated},
            None,
        )
    except ValueError as value_error:
        return HTTPStatus.BAD_REQUEST, {"error": str(value_error)}, None
    except billing.BillingError as billing_error:
        return HTTPStatus.BAD_GATEWAY, {"error": str(billing_error)}, None
    except LookupError as lookup_error:
        return HTTPStatus.NOT_FOUND, {"error": str(lookup_error)}, None
    except StorageError as storage_error:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(storage_error)}, None


def billing_status(session):
    """Confere o pagamento na Asaas — a tela usa isto enquanto o webhook não chega."""
    user_id, error = _requires_account(session)
    if error:
        return error
    try:
        account = get_account(user_id)
        if account["subscription_active"]:
            return HTTPStatus.OK, {"account": account, "checked": False}, None
        user = find_user_by_id(user_id) or {}
        reference = user.get("billing", {}) if isinstance(user.get("billing"), dict) else {}
        subscription_id = str(reference.get("asaas_subscription_id", ""))
        if not subscription_id or not billing.is_configured():
            return HTTPStatus.OK, {"account": account, "checked": False}, None
        paid, payment_id = billing.subscription_is_paid(subscription_id)
        if paid:
            account = activate_subscription(user_id, reference.get("plan", ""), payment_id, "STATUS_CHECK")
        return HTTPStatus.OK, {"account": account, "checked": True, "paid": paid}, None
    except billing.BillingError as billing_error:
        return HTTPStatus.BAD_GATEWAY, {"error": str(billing_error)}, None
    except LookupError as lookup_error:
        return HTTPStatus.NOT_FOUND, {"error": str(lookup_error)}, None
    except StorageError as storage_error:
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(storage_error)}, None


def billing_webhook(body, token_header):
    """Recebe as notificações da Asaas e libera (ou suspende) o acesso."""
    if not billing.webhook_token_is_valid(token_header or ""):
        return HTTPStatus.UNAUTHORIZED, {"error": "Token do webhook inválido."}, None
    if not isinstance(body, dict) or not body.get("event"):
        return HTTPStatus.BAD_REQUEST, {"error": "Evento inválido."}, None
    event = billing.read_webhook_event(body)
    try:
        user = None
        if event["external_reference"]:
            user = find_user_by_id(event["external_reference"])
        if not user:
            user = find_user_by_billing(event["customer_id"], event["subscription_id"])
        if not user:
            # 200 evita reenvio infinito de um evento que não pertence a esta base.
            return HTTPStatus.OK, {"received": True, "matched": False, "event": event["event"]}, None
        user_id = str(user.get("id", ""))
        reference = user.get("billing", {}) if isinstance(user.get("billing"), dict) else {}
        if event["grants_access"]:
            activate_subscription(user_id, reference.get("plan", ""), event["payment_id"], event["event"])
            return HTTPStatus.OK, {"received": True, "matched": True, "access": "granted"}, None
        if event["suspends_access"]:
            status = "canceled" if event["event"] == "SUBSCRIPTION_DELETED" else "past_due"
            set_subscription_status(user_id, status, event["event"])
            return HTTPStatus.OK, {"received": True, "matched": True, "access": status}, None
        return HTTPStatus.OK, {"received": True, "matched": True, "access": "unchanged"}, None
    except (LookupError, ValueError) as error:
        return HTTPStatus.OK, {"received": True, "matched": False, "detail": str(error)}, None
    except StorageError as error:
        # 503 faz a Asaas reenviar o evento mais tarde, sem perder o pagamento.
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)}, None


# ---------------------------------------------------------------------------
# Recomendação: exige assinatura paga e consome um crédito
# ---------------------------------------------------------------------------


def recommend(session, body):
    """Uma consulta = um crédito. Só roda com assinatura confirmada pela Asaas."""
    from recommender import call_openai, clean_filters  # noqa: PLC0415  (evita ciclo de import)

    if not session:
        return (
            HTTPStatus.UNAUTHORIZED,
            {"error": "Faça login para receber recomendações.", "code": "unauthenticated"},
            None,
        )
    try:
        filters = clean_filters((body or {}).get("filters"))
    except ValueError as error:
        return HTTPStatus.BAD_REQUEST, {"error": str(error)}, None

    user_id = _session_user_id(session)
    account = None
    feedback = []
    if user_id:
        try:
            feedback = list_feedback(user_id)
            account = consume_credit(user_id)
        except SubscriptionRequired as error:
            return (
                HTTPStatus.PAYMENT_REQUIRED,
                {"error": str(error), "code": "subscription_required", "plans": public_plans()},
                None,
            )
        except CreditError as error:
            return (
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": str(error), "code": "no_credits", "account": get_account(user_id)},
                None,
            )
        except LookupError as error:
            return HTTPStatus.NOT_FOUND, {"error": str(error)}, None
        except StorageError as error:
            return HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)}, None

    try:
        text = call_openai(filters, feedback)
    except RuntimeError as error:
        if user_id:
            try:
                refund_credit(user_id)
                account = get_account(user_id)
            except (LookupError, StorageError):
                pass
        return HTTPStatus.BAD_GATEWAY, {"error": str(error), "account": account}, None

    if user_id:
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            try:
                add_history(
                    user_id,
                    {
                        "filters": filters,
                        "interpretation": parsed.get("interpretation", ""),
                        "best_choice": parsed.get("best_choice"),
                        "recommendations": parsed.get("recommendations", []),
                    },
                )
            except (LookupError, StorageError):
                pass  # o histórico é conveniência: uma falha aqui não derruba a recomendação já gerada.

    return HTTPStatus.OK, {"text": text, "account": account}, None
