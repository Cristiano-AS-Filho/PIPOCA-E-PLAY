"""Autenticação do Pipoca & Play.

O administrador continua configurado por variáveis de ambiente. As contas de
clientes usam senhas individuais com PBKDF2 e só entram após aprovação.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from http import cookies

from user_store import StorageError, find_user, is_admin_user, is_approved_user, verify_password


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


def root_admin_configured() -> bool:
    """O administrador de ambiente só existe quando e-mail e senha estão definidos."""
    return bool(admin_email() and admin_password())


def config_status():
    from user_store import admin_summary, storage_diagnostics

    storage = storage_diagnostics()
    summary = admin_summary()
    return {
        "auth_secret_configured": bool(auth_secret()),
        "admin_credentials_configured": root_admin_configured(),
        "stored_admin_count": summary["admins"],
        "user_access_configured": True,
        "allowed_user_count": summary["approved"],
        "pending_user_count": summary["pending"],
        "rejected_user_count": summary["rejected"],
        "storage_mode": storage["mode"],
        "storage": storage,
        "persistent_storage_configured": storage["persistent"],
        "production_mode": _is_production(),
    }


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(value: str) -> str:
    return _b64(hmac.new(auth_secret().encode("utf-8"), value.encode("utf-8"), hashlib.sha256).digest())


def create_session(email: str, role: str, user_id: str | None = None) -> str:
    payload = {
        "email": email,
        "role": role,
        "user_id": user_id,
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
    }
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
        email = str(payload["email"]).lower()
        if payload.get("role") == "user" and not is_approved_user(email):
            return None
        if payload.get("role") == "admin" and not _is_root_admin(email) and not is_admin_user(email):
            return None
        result = {"email": email, "role": payload["role"]}
        if payload.get("user_id"):
            result["user_id"] = str(payload["user_id"])
        return result
    except (ValueError, TypeError, json.JSONDecodeError, base64.binascii.Error, StorageError):
        return None


def _is_root_admin(email: str) -> bool:
    """Administrador definido por variáveis de ambiente (ADMIN_EMAIL/ADMIN_PASSWORD)."""
    return root_admin_configured() and hmac.compare_digest(email.strip().lower(), admin_email())


def authenticate(email: str, password: str):
    normalized = email.strip().lower()
    if not normalized or not password or not auth_secret():
        return None
    if _is_root_admin(normalized) and hmac.compare_digest(password, admin_password()):
        return {"email": normalized, "role": "admin"}
    user = find_user(normalized)
    if user and user.get("status") == "approved" and verify_password(password, str(user.get("password_hash", ""))):
        role = "admin" if str(user.get("role", "user")).lower() == "admin" else "user"
        return {"email": normalized, "role": role, "user_id": str(user.get("id", ""))}
    return None


def session_cookie(session_value: str, secure: bool = False) -> str:
    """Cookie de sessão do navegador: sem Max-Age/Expires, some ao fechar a aba/app.

    O prazo de 24h em SESSION_TTL_SECONDS continua valendo como limite máximo,
    verificado no payload assinado por `read_session`.
    """
    jar = cookies.SimpleCookie()
    jar[SESSION_COOKIE] = session_value
    morsel = jar[SESSION_COOKIE]
    morsel["httponly"] = True
    morsel["samesite"] = "Lax"
    morsel["path"] = "/"
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
    return jar[SESSION_COOKIE].OutputString()


def is_secure_request(headers) -> bool:
    forwarded = headers.get("X-Forwarded-Proto", "")
    return _is_production() or forwarded.split(",", 1)[0].strip().lower() == "https"


def public_user(user):
    if not user:
        return None
    return {"email": user["email"], "role": user["role"]}
