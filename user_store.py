"""Persistência das contas de acesso do Pipoca & Play.

Em desenvolvimento, os usuários são armazenados em um JSON local. Em produção
serverless, configure KV_REST_API_URL/KV_REST_API_TOKEN ou
UPSTASH_REDIS_REST_URL/UPSTASH_REDIS_REST_TOKEN para usar Redis REST persistente.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PASSWORD_MIN_LENGTH = 8
PBKDF2_ITERATIONS = 260_000
STORE_KEY = "pipoca-play:users"
_LOCK = threading.RLock()


class StorageError(RuntimeError):
    """Indica que a base de contas não pôde ser lida ou gravada."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_registration(email: str, password: str, confirmation: str | None = None) -> str:
    normalized = normalize_email(email)
    if not normalized or len(normalized) > 254 or not EMAIL_RE.fullmatch(normalized):
        raise ValueError("Informe um e-mail válido.")
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"A senha deve ter pelo menos {PASSWORD_MIN_LENGTH} caracteres.")
    if len(password) > 128:
        raise ValueError("A senha deve ter no máximo 128 caracteres.")
    if confirmation is not None and password != confirmation:
        raise ValueError("As senhas não conferem.")
    return normalized


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            _unb64(salt),
            int(iterations),
        )
        return hmac_compare(_b64(digest), expected)
    except (AttributeError, TypeError, ValueError):
        return False


def hmac_compare(left: str, right: str) -> bool:
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _local_path() -> Path:
    configured = os.environ.get("USER_STORE_FILE", "").strip()
    path = Path(configured) if configured else Path(__file__).parent / "data" / "users.json"
    if not path.is_absolute():
        path = Path(__file__).parent / path
    return path


def _kv_credentials() -> tuple[str, str] | None:
    url = (
        os.environ.get("KV_REST_API_URL", "").strip()
        or os.environ.get("UPSTASH_REDIS_REST_URL", "").strip()
    )
    token = (
        os.environ.get("KV_REST_API_TOKEN", "").strip()
        or os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
    )
    return (url.rstrip("/"), token) if url and token else None


def storage_mode() -> str:
    return "redis-rest" if _kv_credentials() else "arquivo-local"


def _kv_command(command: str, *args: str):
    credentials = _kv_credentials()
    if not credentials:
        return None
    url, token = credentials
    payload = json.dumps([command, *args], ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as error:
        raise StorageError("Não foi possível acessar o armazenamento persistente das contas.") from error
    if isinstance(body, dict) and body.get("error"):
        raise StorageError("O armazenamento persistente das contas retornou um erro.")
    return body.get("result") if isinstance(body, dict) else None


def _read_local() -> list[dict]:
    path = _local_path()
    if not path.exists():
        return []
    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content or "[]")
    except (OSError, json.JSONDecodeError) as error:
        raise StorageError("A base local de contas está indisponível ou inválida.") from error
    if not isinstance(data, list):
        raise StorageError("A base local de contas está inválida.")
    return [item for item in data if isinstance(item, dict)]


def _write_local(users: list[dict]) -> None:
    path = _local_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(users, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError as error:
        raise StorageError("Não foi possível gravar a base de contas. Configure um armazenamento persistente.") from error


def load_users() -> list[dict]:
    with _LOCK:
        if _kv_credentials():
            raw = _kv_command("GET", STORE_KEY)
            if not raw:
                return []
            try:
                data = json.loads(raw)
            except (TypeError, json.JSONDecodeError) as error:
                raise StorageError("O armazenamento persistente das contas contém dados inválidos.") from error
            if not isinstance(data, list):
                raise StorageError("O armazenamento persistente das contas está inválido.")
            return [item for item in data if isinstance(item, dict)]
        return _read_local()


def save_users(users: list[dict]) -> None:
    serialized = json.dumps(users, ensure_ascii=False, separators=(",", ":"))
    with _LOCK:
        if _kv_credentials():
            _kv_command("SET", STORE_KEY, serialized)
            return
        _write_local(users)


def find_user(email: str, users: list[dict] | None = None) -> dict | None:
    normalized = normalize_email(email)
    records = users if users is not None else load_users()
    return next((user for user in records if normalize_email(str(user.get("email", ""))) == normalized), None)


def register_user(email: str, password: str, confirmation: str | None = None) -> tuple[dict, str, bool]:
    normalized = validate_registration(email, password, confirmation)
    with _LOCK:
        users = load_users()
        existing = find_user(normalized, users)
        if existing and existing.get("status") in {"pending", "approved"}:
            raise FileExistsError("Já existe um pedido ou uma conta com este e-mail.")

        token = secrets.token_urlsafe(32)
        now = _now()
        record = {
            "id": existing.get("id") if existing else secrets.token_urlsafe(16),
            "email": normalized,
            "password_hash": hash_password(password),
            "status": "pending",
            "status_token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "created_at": existing.get("created_at", now) if existing else now,
            "updated_at": now,
            "approved_at": None,
            "rejected_at": None,
        }
        if existing:
            users = [record if user.get("id") == existing.get("id") else user for user in users]
        else:
            users.append(record)
        save_users(users)
        return record, token, bool(existing)


def get_status_by_token(email: str, token: str) -> str | None:
    if not isinstance(token, str) or not token:
        return None
    user = find_user(email)
    if not user:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not hmac_compare(token_hash, str(user.get("status_token_hash", ""))):
        return None
    return str(user.get("status", "pending"))


def is_approved_user(email: str) -> bool:
    user = find_user(email)
    return bool(user and user.get("status") == "approved")


def _public_admin_user(user: dict) -> dict:
    return {
        "id": str(user.get("id", "")),
        "email": str(user.get("email", "")),
        "status": str(user.get("status", "pending")),
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
        "approved_at": user.get("approved_at"),
        "rejected_at": user.get("rejected_at"),
    }


def admin_users() -> list[dict]:
    users = load_users()
    order = {"pending": 0, "approved": 1, "rejected": 2}
    return [
        _public_admin_user(user)
        for user in sorted(users, key=lambda item: (order.get(item.get("status"), 3), item.get("created_at", "")))
    ]


def admin_summary() -> dict:
    users = load_users()
    return {
        "total": len(users),
        "pending": sum(user.get("status") == "pending" for user in users),
        "approved": sum(user.get("status") == "approved" for user in users),
        "rejected": sum(user.get("status") == "rejected" for user in users),
    }


def update_user_status(user_id: str, status: str) -> dict:
    if status not in {"approved", "rejected"}:
        raise ValueError("Status de acesso inválido.")
    with _LOCK:
        users = load_users()
        target = next((user for user in users if str(user.get("id")) == str(user_id)), None)
        if not target:
            raise LookupError("Usuário não encontrado.")
        now = _now()
        target["status"] = status
        target["updated_at"] = now
        target["approved_at"] = now if status == "approved" else None
        target["rejected_at"] = now if status == "rejected" else None
        save_users(users)
        return _public_admin_user(target)


def delete_user(user_id: str) -> None:
    with _LOCK:
        users = load_users()
        remaining = [user for user in users if str(user.get("id")) != str(user_id)]
        if len(remaining) == len(users):
            raise LookupError("Usuário não encontrado.")
        save_users(remaining)
