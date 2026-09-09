"""Roteador único das rotas /api.

A Vercel cobra uma função serverless por arquivo em ``api/``, e o plano Hobby
aceita no máximo 12 por deploy. Todas as rotas passam por este módulo e são
servidas por uma única função (``api/index.py``), que o ``vercel.json``
alcança por reescrita. O servidor local usa exatamente o mesmo roteador, então
produção e desenvolvimento nunca divergem.

``handle`` é uma função pura: recebe método, caminho, query, corpo e cabeçalhos
e devolve ``(status, payload, headers)``.
"""

from __future__ import annotations

from http import HTTPStatus

import api_core
from auth import is_secure_request, public_user, read_session

def _not_found(path=""):
    """Devolve o caminho recebido: se alguma reescrita alterar a rota em
    produção, o diagnóstico aparece na própria resposta."""
    payload = {"error": "Rota não encontrada."}
    if path:
        payload["path"] = path
    return HTTPStatus.NOT_FOUND, payload, None


NOT_FOUND = _not_found()
METHOD_NOT_ALLOWED = (HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Método não permitido nesta rota."}, None)


def _session(headers):
    return read_session(headers.get("Cookie"))


def _first(query, key):
    values = query.get(key) if isinstance(query, dict) else None
    if isinstance(values, list):
        return values[0] if values else ""
    return values or ""


def _remove_requested(body):
    return str(body.get("action", "")).lower() in {"remove", "delete"}


def handle(method, path, query=None, body=None, headers=None):
    """Resolve uma requisição de API. ``body`` já vem como dicionário."""
    method = str(method or "GET").upper()
    path = (path or "").rstrip("/") or "/"
    query = query or {}
    body = body if isinstance(body, dict) else {}
    headers = headers if headers is not None else {}

    if not path.startswith("/api"):
        return _not_found(path)

    # -- diagnóstico e catálogo público -------------------------------------
    if path == "/api/health":
        return api_core.health() if method == "GET" else METHOD_NOT_ALLOWED
    if path == "/api/plans":
        return api_core.plans_catalog() if method == "GET" else METHOD_NOT_ALLOWED

    # -- autenticação --------------------------------------------------------
    if path == "/api/auth/register":
        return api_core.register(body) if method == "POST" else METHOD_NOT_ALLOWED
    if path == "/api/auth/login":
        return api_core.login(body, is_secure_request(headers)) if method == "POST" else METHOD_NOT_ALLOWED
    if path == "/api/auth/logout":
        return api_core.logout(is_secure_request(headers)) if method == "POST" else METHOD_NOT_ALLOWED
    if path == "/api/auth/me":
        if method != "GET":
            return METHOD_NOT_ALLOWED
        user = _session(headers)
        return HTTPStatus.OK, {"authenticated": bool(user), "user": public_user(user)}, None
    if path == "/api/auth/status":
        if method != "GET":
            return METHOD_NOT_ALLOWED
        return api_core.registration_status(_first(query, "email"), _first(query, "token"))

    # -- painel administrativo ----------------------------------------------
    if path in {"/api/admin/status", "/api/admin/users"}:
        if method == "GET":
            return api_core.admin_overview(_session(headers))
        if method in {"POST", "PUT"} and path == "/api/admin/users":
            return api_core.admin_action(_session(headers), body)
        return METHOD_NOT_ALLOWED

    # -- conta do cliente ----------------------------------------------------
    if path == "/api/account":
        return api_core.account_overview(_session(headers)) if method == "GET" else METHOD_NOT_ALLOWED

    # -- marcações -----------------------------------------------------------
    if path == "/api/feedback":
        session = _session(headers)
        if method == "GET":
            return api_core.feedback_overview(session)
        if method == "DELETE":
            return api_core.feedback_delete(session, body)
        if method == "POST":
            # "remove" também chega por POST: alguns proxies descartam corpo em DELETE.
            return api_core.feedback_delete(session, body) if _remove_requested(body) else api_core.feedback_save(session, body)
        return METHOD_NOT_ALLOWED

    # -- cobrança ------------------------------------------------------------
    if path == "/api/billing/checkout":
        return api_core.billing_checkout(_session(headers), body) if method == "POST" else METHOD_NOT_ALLOWED
    if path == "/api/billing/status":
        if method in {"GET", "POST"}:
            return api_core.billing_status(_session(headers))
        return METHOD_NOT_ALLOWED
    if path == "/api/billing/webhook":
        if method == "GET":
            # A Asaas verifica a URL antes de ativar o webhook.
            return HTTPStatus.OK, {"ok": True, "endpoint": "asaas-webhook"}, None
        if method == "POST":
            token = headers.get("asaas-access-token") or headers.get("Asaas-Access-Token")
            return api_core.billing_webhook(body, token)
        return METHOD_NOT_ALLOWED

    # -- recomendação --------------------------------------------------------
    if path == "/api/recommend":
        return api_core.recommend(_session(headers), body) if method == "POST" else METHOD_NOT_ALLOWED

    return _not_found(path)
