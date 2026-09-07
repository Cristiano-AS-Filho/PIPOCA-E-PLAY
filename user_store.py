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
from urllib.parse import quote, urlencode
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from plans import PLANS, daily_credits_for, plan_exists


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PASSWORD_MIN_LENGTH = 8
PBKDF2_ITERATIONS = 260_000
STORE_KEY = "pipoca-play:users"
BILLING_TIMEZONE = ZoneInfo("America/Sao_Paulo")
BLOB_PATH = os.environ.get("USER_STORE_BLOB_PATH", "pipoca-play/users.json").strip() or "pipoca-play/users.json"
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


def _blob_token() -> str | None:
    return (
        os.environ.get("BLOB_READ_WRITE_TOKEN", "").strip()
        or os.environ.get("VERCEL_BLOB_READ_WRITE_TOKEN", "").strip()
        or None
    )


def _blob_oidc_credentials() -> tuple[str, str] | None:
    token = os.environ.get("VERCEL_OIDC_TOKEN", "").strip()
    store_id = os.environ.get("BLOB_STORE_ID", "").strip()
    if not token or not store_id:
        return None
    return token, store_id.removeprefix("store_")


def _blob_is_configured() -> bool:
    return bool(_blob_token() or _blob_oidc_credentials())


def storage_mode() -> str:
    if _blob_is_configured():
        return "vercel-blob"
    if _kv_credentials():
        return "redis-rest"
    return "arquivo-local"


def _blob_sdk():
    try:
        from vercel.blob import get, put
        from vercel.blob.errors import BlobNotFoundError
    except ImportError as error:
        raise StorageError(
            "O SDK do Vercel Blob não está instalado. Reinstale as dependências do projeto."
        ) from error
    return get, put, BlobNotFoundError


def _blob_oidc_url() -> tuple[str, str]:
    credentials = _blob_oidc_credentials()
    if not credentials:
        raise StorageError(
            "Configure BLOB_STORE_ID e VERCEL_OIDC_TOKEN para usar a loja Blob privada."
        )
    token, store_id = credentials
    object_url = f"https://{store_id}.private.blob.vercel-storage.com/{quote(BLOB_PATH, safe='/')}"
    return token, object_url


def _read_blob_oidc() -> list[dict]:
    token, object_url = _blob_oidc_url()
    request = urllib.request.Request(
        object_url,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            content = response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return []
        raise StorageError("Não foi possível ler a base de contas no Vercel Blob.") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise StorageError("Não foi possível acessar a base de contas no Vercel Blob.") from error
    try:
        data = json.loads(content.decode("utf-8"))
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise StorageError("A base de contas no Vercel Blob contém dados inválidos.") from error
    if not isinstance(data, list):
        raise StorageError("A base de contas no Vercel Blob está inválida.")
    return [item for item in data if isinstance(item, dict)]


def _write_blob_oidc(users: list[dict]) -> None:
    credentials = _blob_oidc_credentials()
    if not credentials:
        raise StorageError(
            "Configure BLOB_STORE_ID e VERCEL_OIDC_TOKEN para usar a loja Blob privada."
        )
    token, store_id = credentials
    payload = json.dumps(users, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        f"https://vercel.com/api/blob/?{urlencode({'pathname': BLOB_PATH})}",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "x-vercel-blob-access": "private",
            "x-vercel-blob-store-id": store_id,
            "x-allow-overwrite": "1",
            "x-cache-control-max-age": "0",
            "x-api-blob-request-id": f"{store_id}:{secrets.token_hex(8)}",
            "x-api-blob-request-attempt": "0",
            "x-api-version": "12",
        },
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
        raise StorageError("Não foi possível gravar a base de contas no Vercel Blob.") from error


def _read_blob() -> list[dict]:
    token = _blob_token()
    if not token:
        if _blob_oidc_credentials():
            return _read_blob_oidc()
        raise StorageError(
            "Configure BLOB_READ_WRITE_TOKEN ou conecte BLOB_STORE_ID com VERCEL_OIDC_TOKEN."
        )
    get, _, BlobNotFoundError = _blob_sdk()
    try:
        result = get(BLOB_PATH, access="private", token=token, use_cache=False)
    except BlobNotFoundError:
        return []
    except Exception as error:
        raise StorageError("Não foi possível ler a base de contas no Vercel Blob.") from error
    try:
        data = json.loads(bytes(result).decode("utf-8"))
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise StorageError("A base de contas no Vercel Blob contém dados inválidos.") from error
    if not isinstance(data, list):
        raise StorageError("A base de contas no Vercel Blob está inválida.")
    return [item for item in data if isinstance(item, dict)]


def _write_blob(users: list[dict]) -> None:
    token = _blob_token()
    if not token:
        if _blob_oidc_credentials():
            _write_blob_oidc(users)
            return
        raise StorageError(
            "Configure BLOB_READ_WRITE_TOKEN ou conecte BLOB_STORE_ID com VERCEL_OIDC_TOKEN."
        )
    _, put, _ = _blob_sdk()
    try:
        put(
            BLOB_PATH,
            json.dumps(users, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            access="private",
            content_type="application/json",
            overwrite=True,
            cache_control_max_age=0,
            token=token,
        )
    except Exception as error:
        raise StorageError("Não foi possível gravar a base de contas no Vercel Blob.") from error


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
        if _blob_is_configured():
            return _read_blob()
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
        if _blob_is_configured():
            _write_blob(users)
            return
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
            # Assinatura/cobrança (Asaas). Uma conta sem plano ativo não
            # consegue usar /api/recommend — ver consume_credit().
            "plan_id": existing.get("plan_id") if existing else None,
            "subscription_status": existing.get("subscription_status", "none") if existing else "none",
            "asaas_customer_id": existing.get("asaas_customer_id", "") if existing else "",
            "asaas_subscription_id": existing.get("asaas_subscription_id", "") if existing else "",
            "credits_daily_limit": existing.get("credits_daily_limit") if existing else None,
            "credits_used_today": existing.get("credits_used_today", 0) if existing else 0,
            "credits_date": existing.get("credits_date", "") if existing else "",
            "subscription_updated_at": existing.get("subscription_updated_at") if existing else None,
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
        "plan_id": user.get("plan_id"),
        "subscription_status": user.get("subscription_status", "none"),
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


# ---------------------------------------------------------------------------
# Assinatura e créditos (Asaas)
# ---------------------------------------------------------------------------

def _today_in_billing_timezone() -> str:
    return datetime.now(BILLING_TIMEZONE).date().isoformat()


def get_user_by_id(user_id: str) -> dict | None:
    return next((user for user in load_users() if str(user.get("id")) == str(user_id)), None)


def find_user_by_token(email: str, token: str) -> dict | None:
    """Confirma posse do cadastro pendente pelo par (e-mail, token) — o mesmo
    token devolvido no cadastro e usado hoje só pra consultar status. É o que
    permite iniciar o checkout antes de existir uma sessão logada (a conta
    ainda não foi aprovada, então ainda não pode logar)."""
    if not isinstance(token, str) or not token:
        return None
    user = find_user(email)
    if not user:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not hmac_compare(token_hash, str(user.get("status_token_hash", ""))):
        return None
    return user


def find_user_by_asaas_customer_id(customer_id: str) -> dict | None:
    if not customer_id:
        return None
    return next((user for user in load_users() if user.get("asaas_customer_id") == customer_id), None)


def set_asaas_customer_id(user_id: str, customer_id: str) -> None:
    with _LOCK:
        users = load_users()
        target = next((user for user in users if str(user.get("id")) == str(user_id)), None)
        if not target:
            raise LookupError("Usuário não encontrado.")
        target["asaas_customer_id"] = customer_id
        target["updated_at"] = _now()
        save_users(users)


def activate_subscription(user_id: str, plan_id: str, asaas_customer_id: str = "", asaas_subscription_id: str = "") -> dict:
    """Ativa (ou renova) o plano de um usuário — chamado pelo webhook quando
    o Asaas confirma um pagamento. Um pagamento confirmado aprova a conta na
    hora: quem pagou não deveria esperar validação manual do administrador."""
    if not plan_exists(plan_id):
        raise ValueError("Plano desconhecido.")
    with _LOCK:
        users = load_users()
        target = next((user for user in users if str(user.get("id")) == str(user_id)), None)
        if not target:
            raise LookupError("Usuário não encontrado.")
        now = _now()
        target["plan_id"] = plan_id
        target["subscription_status"] = "active"
        target["credits_daily_limit"] = daily_credits_for(plan_id)
        target["credits_used_today"] = 0
        target["credits_date"] = _today_in_billing_timezone()
        target["subscription_updated_at"] = now
        if asaas_customer_id:
            target["asaas_customer_id"] = asaas_customer_id
        if asaas_subscription_id:
            target["asaas_subscription_id"] = asaas_subscription_id
        if target.get("status") != "approved":
            target["status"] = "approved"
            target["approved_at"] = now
        target["updated_at"] = now
        save_users(users)
        return target


def set_subscription_status(user_id: str, status: str) -> dict:
    """Usado pelo webhook pra marcar 'overdue' (cobrança atrasada, ainda em
    carência) ou 'cancelled' (assinatura encerrada/estornada/excluída)."""
    if status not in {"overdue", "cancelled"}:
        raise ValueError("Status de assinatura inválido.")
    with _LOCK:
        users = load_users()
        target = next((user for user in users if str(user.get("id")) == str(user_id)), None)
        if not target:
            raise LookupError("Usuário não encontrado.")
        target["subscription_status"] = status
        if status == "cancelled":
            target["plan_id"] = None
            target["credits_daily_limit"] = None
        target["subscription_updated_at"] = _now()
        target["updated_at"] = _now()
        save_users(users)
        return target


def public_subscription_fields(user: dict) -> dict:
    """Estado de assinatura pra devolver em /api/auth/me — computa os
    créditos restantes de hoje sem gravar nada (a gravação de verdade só
    acontece em consume_credit, no momento da consulta)."""
    plan_id = user.get("plan_id")
    limit = user.get("credits_daily_limit")
    today = _today_in_billing_timezone()
    used_today = user.get("credits_used_today", 0) if user.get("credits_date") == today else 0
    remaining = None if limit is None else max(0, limit - used_today)
    plan_meta = PLANS.get(plan_id) if plan_id else None
    return {
        "plan_id": plan_id,
        "plan_name": plan_meta["name"] if plan_meta else None,
        "subscription_status": user.get("subscription_status", "none"),
        "credits_daily_limit": limit,
        "credits_remaining_today": remaining,
    }


def consume_credit(user_id: str) -> tuple[bool, dict]:
    """Gasta 1 crédito (1 consulta) se houver saldo. Retorna (ok, info) —
    info sempre traz o estado de créditos pra exibir na UI, tenha dado certo
    ou não."""
    with _LOCK:
        users = load_users()
        target = next((user for user in users if str(user.get("id")) == str(user_id)), None)
        if not target:
            raise LookupError("Usuário não encontrado.")
        if target.get("subscription_status") != "active":
            return False, public_subscription_fields(target)

        today = _today_in_billing_timezone()
        if target.get("credits_date") != today:
            target["credits_used_today"] = 0
            target["credits_date"] = today

        limit = target.get("credits_daily_limit")
        if limit is not None and target.get("credits_used_today", 0) >= limit:
            save_users(users)
            return False, public_subscription_fields(target)

        target["credits_used_today"] = target.get("credits_used_today", 0) + 1
        save_users(users)
        return True, public_subscription_fields(target)
