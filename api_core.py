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
    authenticate,
    auth_secret,
    clear_session_cookie,
    config_status,
    create_session,
    public_user,
    root_admin_configured,
    session_cookie,
)
from user_store import (
    StorageError,
    admin_summary,
    admin_users,
    create_user,
    delete_user,
    find_user,
    find_user_by_id,
    get_status_by_token,
    register_user,
    set_user_password,
    storage_diagnostics,
    update_user_role,
    update_user_status,
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
