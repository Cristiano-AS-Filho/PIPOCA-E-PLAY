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

import time
from collections import defaultdict, deque
from http import HTTPStatus

import api_core
from auth import is_secure_request, public_user, read_session


# ---------------------------------------------------------------------------
# Freio por origem nas rotas que dão para abusar
# ---------------------------------------------------------------------------
#
# A Vercel isola cada invocação, mas mantém o processo quente entre chamadas
# seguidas — que é justamente o padrão de quem varre senhas, cria cadastros em
# série ou dispara buscas para queimar o motor. O freio é, portanto, uma
# barreira de rajada e não uma cota exata: corta o abuso vindo de uma origem só,
# sem depender de banco nem de estado compartilhado entre regiões. Os tetos
# ficam muito acima do uso normal (um login, um cadastro, uma busca por vez),
# então ninguém legítimo esbarra neles.
#
# ``(tentativas, janela em segundos)`` por rota.
RATE_LIMITS = {
    "/api/auth/login": (10, 60),
    "/api/auth/register": (5, 300),
    "/api/recommend": (12, 60),
}

# Teto de origens lembradas ao mesmo tempo, para o processo quente não crescer
# sem limite sob uma varredura que troca de IP a cada requisição.
MAX_TRACKED_CLIENTS = 2048

_ATTEMPTS: dict[tuple[str, str], deque] = defaultdict(deque)

TOO_MANY_REQUESTS = (
    HTTPStatus.TOO_MANY_REQUESTS,
    {"error": "Muitas tentativas seguidas. Aguarde um minuto e tente de novo.", "code": "rate_limited"},
    None,
)


def reset_rate_limits() -> None:
    """Zera o contador. Usado pelos testes, que repetem login e cadastro."""
    _ATTEMPTS.clear()


def _client_ip(headers) -> str:
    """Origem da requisição. Na Vercel vem em ``X-Forwarded-For``."""
    for name in ("X-Forwarded-For", "x-forwarded-for", "X-Real-IP", "x-real-ip"):
        value = headers.get(name)
        if value:
            return str(value).split(",", 1)[0].strip()[:64]
    return "desconhecido"


def _forget_expired(now: float) -> None:
    for key in [key for key, hits in _ATTEMPTS.items() if not hits or now - hits[-1] > 900]:
        _ATTEMPTS.pop(key, None)


def rate_limited(path: str, headers) -> bool:
    """Registra a tentativa e diz se esta origem já passou do teto da rota."""
    limit = RATE_LIMITS.get(path)
    if not limit:
        return False
    allowed, window = limit
    now = time.monotonic()
    if len(_ATTEMPTS) > MAX_TRACKED_CLIENTS:
        _forget_expired(now)
    hits = _ATTEMPTS[(path, _client_ip(headers))]
    while hits and now - hits[0] > window:
        hits.popleft()
    if len(hits) >= allowed:
        return True
    hits.append(now)
    return False


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

    # O freio vale para o mesmo conjunto de rotas no servidor local e na Vercel.
    if method in {"POST", "PUT"} and rate_limited(path, headers):
        return TOO_MANY_REQUESTS

    # -- diagnóstico e catálogo público -------------------------------------
    if path == "/api/health":
        return api_core.health() if method == "GET" else METHOD_NOT_ALLOWED
    if path == "/api/plans":
        return api_core.plans_catalog() if method == "GET" else METHOD_NOT_ALLOWED
    if path == "/api/landing/posters":
        # Leitura pública: é a landing que consome. A gravação é administrativa.
        return api_core.landing_posters() if method == "GET" else METHOD_NOT_ALLOWED

    # -- autenticação --------------------------------------------------------
    if path == "/api/auth/register":
        return api_core.register(body, is_secure_request(headers)) if method == "POST" else METHOD_NOT_ALLOWED
    if path == "/api/auth/login":
        return api_core.login(body, is_secure_request(headers)) if method == "POST" else METHOD_NOT_ALLOWED
    if path == "/api/auth/logout":
        return api_core.logout(is_secure_request(headers)) if method == "POST" else METHOD_NOT_ALLOWED
    if path == "/api/auth/me":
        if method != "GET":
            return METHOD_NOT_ALLOWED
        user = _session(headers)
        return HTTPStatus.OK, {"authenticated": bool(user), "user": public_user(user)}, None

    # -- painel administrativo ----------------------------------------------
    if path in {"/api/admin/status", "/api/admin/users"}:
        if method == "GET":
            return api_core.admin_overview(_session(headers))
        if method in {"POST", "PUT"} and path == "/api/admin/users":
            return api_core.admin_action(_session(headers), body)
        return METHOD_NOT_ALLOWED
    if path == "/api/admin/landing":
        if method == "GET":
            # O painel lê por aqui (e não pela rota pública) porque só esta
            # devolve quem salvou e quando — dado que não pertence à landing.
            return api_core.admin_landing_overview(_session(headers))
        if method in {"POST", "PUT"}:
            return api_core.admin_landing_save(_session(headers), body)
        return METHOD_NOT_ALLOWED
    if path == "/api/admin/landing/testimonials":
        if method in {"POST", "PUT"}:
            return api_core.admin_landing_testimonial(_session(headers), body)
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

    # -- histórico -------------------------------------------------------------
    if path == "/api/history":
        session = _session(headers)
        if method == "GET":
            return api_core.history_overview(session)
        if method in {"POST", "DELETE"}:
            # A remoção chega por POST ou DELETE: alguns proxies descartam corpo em DELETE.
            return api_core.history_delete(session, body)
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
