"""Autenticação leve e sem dependências para o Pipoca & Play.

As credenciais ficam em variáveis de ambiente. A sessão é um cookie assinado,
sem armazenar senhas no navegador.
"""

import base64
import hashlib
import hmac
import json
import os
import time
from http import cookies

SESSION_COOKIE = "pipoca_session"
SESSION_TTL_SECONDS = 60 * 60 * 24


def _is_production():
    return bool(os.environ.get("VERCEL")) or os.environ.get("ENVIRONMENT", "").lower() in {"prod", "production"}


def auth_secret():
    configured = os.environ.get("AUTH_SECRET", "").strip()
    if configured:
        return configured
    if not _is_production():
        return "local-development-secret-change-me"
    return ""


def admin_email():
    return os.environ.get("ADMIN_EMAIL", "admin@pipocaplay.com").strip().lower()


def admin_password():
    configured = os.environ.get("ADMIN_PASSWORD", "").strip()
    if configured:
        return configured
    if not _is_production():
        return "admin123"
    return ""


def user_password():
    configured = os.environ.get("USER_PASSWORD", "").strip()
    if configured:
        return configured
    if not _is_production():
        return "pipoca123"
    return ""


def allowed_user_emails():
    raw = os.environ.get("USER_EMAILS", "")
    return sorted({item.strip().lower() for item in raw.split(",") if item.strip()})


def config_status():
    return {
        "auth_secret_configured": bool(auth_secret()),
        "admin_credentials_configured": bool(admin_email() and admin_password()),
        "user_access_configured": bool(allowed_user_emails() and user_password()),
        "allowed_user_count": len(allowed_user_emails()),
        "production_mode": _is_production(),
    }


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(value: str) -> str:
    return _b64(hmac.new(auth_secret().encode("utf-8"), value.encode("utf-8"), hashlib.sha256).digest())


def create_session(email: str, role: str) -> str:
    payload = {"email": email, "role": role, "exp": int(time.time()) + SESSION_TTL_SECONDS}
    encoded = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    return encoded + "." + _sign(encoded)


def read_session(cookie_header: str | None):
    if not cookie_header or not auth_secret():
        return None
    jar = cookies.SimpleCookie()
    try:
        jar.load(cookie_header)
        raw = jar.get(SESSION_COOKIE)
        value = raw.value if raw else ""
        encoded, signature = value.split(".", 1)
        expected = _sign(encoded)
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_unb64(encoded).decode("utf-8"))
        if payload.get("exp", 0) < int(time.time()):
            return None
        if payload.get("role") not in {"admin", "user"} or not payload.get("email"):
            return None
        return {"email": str(payload["email"]).lower(), "role": payload["role"]}
    except (ValueError, TypeError, json.JSONDecodeError, base64.binascii.Error):
        return None


def authenticate(email: str, password: str):
    normalized = email.strip().lower()
    if not normalized or not password or not auth_secret():
        return None
    if hmac.compare_digest(normalized, admin_email()) and hmac.compare_digest(password, admin_password()):
        return {"email": normalized, "role": "admin"}
    if normalized in allowed_user_emails() and hmac.compare_digest(password, user_password()):
        return {"email": normalized, "role": "user"}
    return None


def session_cookie(session_value: str, secure: bool = False) -> str:
    jar = cookies.SimpleCookie()
    jar[SESSION_COOKIE] = session_value
    morsel = jar[SESSION_COOKIE]
    morsel["httponly"] = True
    morsel["samesite"] = "Lax"
    morsel["path"] = "/"
    morsel["max-age"] = str(SESSION_TTL_SECONDS)
    if secure:
        morsel["secure"] = True
    return morsel.OutputString()


def clear_session_cookie(secure: bool = False) -> str:
    jar = cookies.SimpleCookie()
    jar[SESSION_COOKIE] = ""
    morsel = jar[SESSION_COOKIE]
    morsel["httponly"] = True
    morsel["samesite"] = "Lax"
    morsel["path"] = "/"
    morsel["max-age"] = "0"
    morsel["expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"
    if secure:
        morsel["secure"] = True
    return morsel.OutputString()


def is_secure_request(headers) -> bool:
    forwarded = headers.get("X-Forwarded-Proto", "")
    return _is_production() or forwarded.split(",", 1)[0].strip().lower() == "https"


def public_user(user):
    if not user:
        return None
    return {"email": user["email"], "role": user["role"]}
