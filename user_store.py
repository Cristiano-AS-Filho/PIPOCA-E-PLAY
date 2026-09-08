"""Persistência das contas de acesso do Pipoca & Play.

A base de contas é uma lista JSON. O backend é escolhido automaticamente a
partir das variáveis de ambiente disponíveis, na seguinte ordem:

1. ``postgres``    — ``POSTGRES_URL`` / ``DATABASE_URL`` (Neon, Supabase ou
   qualquer Postgres criado em Vercel → Storage → Create Database).
2. ``vercel-blob`` — ``BLOB_READ_WRITE_TOKEN`` (Vercel Blob privado).
3. ``redis-rest``  — ``KV_REST_API_URL`` + ``KV_REST_API_TOKEN`` (Upstash).
4. ``arquivo-local`` — apenas para desenvolvimento; o filesystem das funções
   serverless é somente leitura, então este modo não persiste em produção.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import unicodedata
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode, urlparse
from datetime import datetime, timedelta, timezone
from pathlib import Path


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PASSWORD_MIN_LENGTH = 8
PBKDF2_ITERATIONS = 260_000
STORE_KEY = "pipoca-play:users"
POSTGRES_TABLE = "pipoca_play_store"
BLOB_PATH = os.environ.get("USER_STORE_BLOB_PATH", "pipoca-play/users.json").strip() or "pipoca-play/users.json"
VALID_STATUSES = ("pending", "approved", "rejected")
VALID_ROLES = ("user", "admin")
VALID_OPINIONS = ("liked", "disliked", "")
# Fuso oficial do Brasil (sem horário de verão desde 2019): o dia de crédito
# vira à meia-noite de Brasília, não à meia-noite UTC.
BRAZIL_TZ = timezone(timedelta(hours=-3))
MAX_MARKS_PER_USER = 500
_LOCK = threading.RLock()

SETUP_HINT = (
    "Abra o projeto na Vercel em Storage → Create Database e conecte um banco "
    "Postgres (Neon), um Upstash Redis ou um Blob store. A Vercel injeta as "
    "variáveis automaticamente; depois faça um novo deploy."
)


class StorageError(RuntimeError):
    """Indica que a base de contas não pôde ser lida ou gravada."""


class CreditsExhaustedError(RuntimeError):
    """O usuário já usou todos os créditos do dia."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def brazil_today() -> str:
    """Data corrente em Brasília, no formato AAAA-MM-DD."""
    return datetime.now(BRAZIL_TZ).date().isoformat()


def _is_serverless() -> bool:
    return bool(os.environ.get("VERCEL")) or os.environ.get("ENVIRONMENT", "").lower() in {"prod", "production"}


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


# ---------------------------------------------------------------------------
# Detecção dos backends disponíveis
# ---------------------------------------------------------------------------


def _local_path() -> Path:
    configured = os.environ.get("USER_STORE_FILE", "").strip()
    path = Path(configured) if configured else Path(__file__).parent / "data" / "users.json"
    if not path.is_absolute():
        path = Path(__file__).parent / path
    return path


POSTGRES_ENV_VARS = (
    "USER_STORE_POSTGRES_URL",
    "POSTGRES_URL",
    "DATABASE_URL",
    "POSTGRES_URL_NON_POOLING",
    "DATABASE_URL_UNPOOLED",
)


def _postgres_dsn_source() -> tuple[str, str] | None:
    """Retorna (nome_da_variável, valor) da primeira URL de Postgres válida encontrada."""
    for name in POSTGRES_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value.startswith("postgres://") or value.startswith("postgresql://"):
            return name, value
    return None


def _postgres_dsn() -> str | None:
    found = _postgres_dsn_source()
    return found[1] if found else None


def _redact_dsn(dsn: str) -> str:
    """Prévia seguro da string de conexão: sem senha, com host/porta/base/parâmetros visíveis."""
    try:
        parsed = urlparse(dsn.replace("postgres://", "postgresql://", 1))
    except ValueError:
        return "(não foi possível interpretar a URL)"
    userinfo = parsed.username or ""
    if parsed.password:
        userinfo += ":***"
    netloc = userinfo + "@" if userinfo else ""
    netloc += parsed.hostname or ""
    if parsed.port:
        netloc += f":{parsed.port}"
    preview = f"{parsed.scheme}://{netloc}{parsed.path}"
    if parsed.query:
        preview += f"?{parsed.query}"
    return preview


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
    """Nome do backend efetivamente usado nesta invocação."""
    forced = os.environ.get("USER_STORE_MODE", "").strip().lower()
    if forced in {"postgres", "vercel-blob", "redis-rest", "arquivo-local"}:
        return forced
    if _postgres_dsn():
        return "postgres"
    if _blob_is_configured():
        return "vercel-blob"
    if _kv_credentials():
        return "redis-rest"
    return "arquivo-local"


STORAGE_LABELS = {
    "postgres": "Postgres (Neon/Vercel)",
    "vercel-blob": "Vercel Blob privado",
    "redis-rest": "Redis REST (Upstash/Vercel KV)",
    "arquivo-local": "Arquivo local (somente desenvolvimento)",
}


def storage_is_persistent() -> bool:
    """Um modo é persistente quando sobrevive ao fim da invocação serverless."""
    mode = storage_mode()
    if mode in {"postgres", "vercel-blob", "redis-rest"}:
        return True
    return not _is_serverless()


def storage_diagnostics(probe: bool = False) -> dict:
    """Resumo do armazenamento para o painel administrativo e para /api/health."""
    mode = storage_mode()
    postgres_source = _postgres_dsn_source()
    diagnostics = {
        "mode": mode,
        "label": STORAGE_LABELS.get(mode, mode),
        "persistent": storage_is_persistent(),
        "serverless": _is_serverless(),
        "available": {
            "postgres": bool(postgres_source),
            "vercel-blob": _blob_is_configured(),
            "redis-rest": bool(_kv_credentials()),
        },
        "setup_hint": SETUP_HINT,
        "healthy": None,
        "error": None,
    }
    if postgres_source:
        diagnostics["postgres_source_env_var"] = postgres_source[0]
        diagnostics["postgres_dsn_preview"] = _redact_dsn(postgres_source[1])
    if not diagnostics["persistent"]:
        diagnostics["error"] = (
            "Nenhum armazenamento persistente está conectado a este deploy. " + SETUP_HINT
        )
        diagnostics["healthy"] = False
        return diagnostics
    if probe:
        try:
            load_users()
            diagnostics["healthy"] = True
        except StorageError as error:
            diagnostics["healthy"] = False
            diagnostics["error"] = str(error)
    return diagnostics


# ---------------------------------------------------------------------------
# Backend: Postgres
# ---------------------------------------------------------------------------


def _postgres_driver():
    try:
        import psycopg  # noqa: PLC0415
    except ImportError as error:  # pragma: no cover - depende do ambiente
        raise StorageError(
            "O driver psycopg não está instalado. Reinstale as dependências do projeto "
            "(requirements.txt) e faça um novo deploy."
        ) from error
    return psycopg


def _looks_like_supabase_direct_host(dsn: str) -> bool:
    """A conexão direta do Supabase (db.<projeto>.supabase.co) só tem IPv6, e o
    runtime serverless da Vercel não tem saída IPv6 — a conexão sempre falha
    ali, mesmo com credenciais corretas. É preciso usar o pooler (Supavisor)."""
    try:
        host = urlparse(dsn.replace("postgres://", "postgresql://", 1)).hostname or ""
    except ValueError:
        return False
    return host.startswith("db.") and host.endswith(".supabase.co")


def _sanitize_postgres_error(dsn: str, error: Exception) -> str:
    """Detalhe técnico seguro para exibir em /api/health: sem senha nenhuma."""
    text = f"{type(error).__name__}: {error}".strip()
    text = " ".join(text.split())
    try:
        password = urlparse(dsn.replace("postgres://", "postgresql://", 1)).password
    except ValueError:
        password = None
    if password:
        text = text.replace(password, "***")
    return text[:300]


def _postgres_connect():
    dsn = _postgres_dsn()
    if not dsn:
        raise StorageError("Nenhuma URL de Postgres foi configurada. " + SETUP_HINT)
    psycopg = _postgres_driver()
    try:
        return psycopg.connect(dsn, connect_timeout=10, autocommit=True)
    except Exception as error:
        detail = _sanitize_postgres_error(dsn, error)
        if _looks_like_supabase_direct_host(dsn):
            raise StorageError(
                "Não foi possível conectar ao banco Postgres das contas. Você está usando a "
                "conexão direta do Supabase (db.<projeto>.supabase.co), que só tem endereço "
                "IPv6 — funções serverless da Vercel não alcançam esse host. Abra o Supabase → "
                "Project Settings → Database → Connection string → aba \"Transaction pooler\" "
                "e use essa URL (host aws-0-<região>.pooler.supabase.com, porta 6543) em "
                f"POSTGRES_URL/DATABASE_URL na Vercel. Detalhe técnico: {detail}"
            ) from error
        raise StorageError(
            "Não foi possível conectar ao banco Postgres das contas. Confira a variável "
            f"POSTGRES_URL/DATABASE_URL do projeto. Detalhe técnico: {detail}"
        ) from error


def _read_postgres() -> list[dict]:
    with _postgres_connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"CREATE TABLE IF NOT EXISTS {POSTGRES_TABLE} ("
                "key text PRIMARY KEY, "
                "value jsonb NOT NULL, "
                "updated_at timestamptz NOT NULL DEFAULT now())"
            )
            cursor.execute(f"SELECT value FROM {POSTGRES_TABLE} WHERE key = %s", (STORE_KEY,))
            row = cursor.fetchone()
    if not row or row[0] is None:
        return []
    data = row[0]
    if isinstance(data, (str, bytes, bytearray)):
        try:
            data = json.loads(data)
        except (TypeError, ValueError) as error:
            raise StorageError("A base de contas no Postgres contém dados inválidos.") from error
    if not isinstance(data, list):
        raise StorageError("A base de contas no Postgres está inválida.")
    return [item for item in data if isinstance(item, dict)]


def _write_postgres(users: list[dict]) -> None:
    payload = json.dumps(users, ensure_ascii=False, separators=(",", ":"))
    with _postgres_connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"CREATE TABLE IF NOT EXISTS {POSTGRES_TABLE} ("
                "key text PRIMARY KEY, "
                "value jsonb NOT NULL, "
                "updated_at timestamptz NOT NULL DEFAULT now())"
            )
            cursor.execute(
                f"INSERT INTO {POSTGRES_TABLE} (key, value, updated_at) "
                "VALUES (%s, %s::jsonb, now()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                (STORE_KEY, payload),
            )


# ---------------------------------------------------------------------------
# Backend: Vercel Blob
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Backend: Redis REST
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Backend: arquivo local (desenvolvimento)
# ---------------------------------------------------------------------------


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
    if _is_serverless():
        raise StorageError(
            "Este deploy não tem um banco de contas conectado, e o disco das funções "
            "serverless não guarda dados entre requisições. " + SETUP_HINT
        )
    path = _local_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(users, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError as error:
        raise StorageError(
            "Não foi possível gravar a base local de contas em "
            f"{path}. Verifique a permissão de escrita da pasta."
        ) from error


# ---------------------------------------------------------------------------
# Leitura e escrita
# ---------------------------------------------------------------------------


def load_users() -> list[dict]:
    mode = storage_mode()
    with _LOCK:
        if mode == "postgres":
            return _read_postgres()
        if mode == "vercel-blob":
            return _read_blob()
        if mode == "redis-rest":
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
    mode = storage_mode()
    with _LOCK:
        if mode == "postgres":
            _write_postgres(users)
            return
        if mode == "vercel-blob":
            _write_blob(users)
            return
        if mode == "redis-rest":
            _kv_command("SET", STORE_KEY, json.dumps(users, ensure_ascii=False, separators=(",", ":")))
            return
        _write_local(users)


# ---------------------------------------------------------------------------
# Regras de negócio
# ---------------------------------------------------------------------------


def find_user(email: str, users: list[dict] | None = None) -> dict | None:
    normalized = normalize_email(email)
    records = users if users is not None else load_users()
    return next((user for user in records if normalize_email(str(user.get("email", ""))) == normalized), None)


def find_user_by_id(user_id: str, users: list[dict] | None = None) -> dict | None:
    records = users if users is not None else load_users()
    return next((user for user in records if str(user.get("id")) == str(user_id)), None)


def _user_role(user: dict) -> str:
    role = str(user.get("role", "user")).lower()
    return role if role in VALID_ROLES else "user"


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
            "role": _user_role(existing) if existing else "user",
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


def create_user(email: str, password: str, status: str = "approved", role: str = "user") -> dict:
    """Criação direta pelo painel administrativo, já liberada."""
    normalized = validate_registration(email, password)
    if status not in VALID_STATUSES:
        raise ValueError("Status de acesso inválido.")
    if role not in VALID_ROLES:
        raise ValueError("Papel de acesso inválido.")
    with _LOCK:
        users = load_users()
        if find_user(normalized, users):
            raise FileExistsError("Já existe uma conta com este e-mail.")
        now = _now()
        record = {
            "id": secrets.token_urlsafe(16),
            "email": normalized,
            "password_hash": hash_password(password),
            "status": status,
            "role": role,
            "status_token_hash": "",
            "created_at": now,
            "updated_at": now,
            "approved_at": now if status == "approved" else None,
            "rejected_at": now if status == "rejected" else None,
        }
        users.append(record)
        save_users(users)
        return _public_admin_user(record)


def get_status_by_token(email: str, token: str) -> str | None:
    if not isinstance(token, str) or not token:
        return None
    user = find_user(email)
    if not user:
        return None
    stored = str(user.get("status_token_hash", ""))
    if not stored:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not hmac_compare(token_hash, stored):
        return None
    return str(user.get("status", "pending"))


def is_approved_user(email: str) -> bool:
    user = find_user(email)
    return bool(user and user.get("status") == "approved")


def is_admin_user(email: str) -> bool:
    user = find_user(email)
    return bool(user and user.get("status") == "approved" and _user_role(user) == "admin")


def _public_admin_user(user: dict) -> dict:
    subscription = subscription_of(user)
    return {
        "id": str(user.get("id", "")),
        "email": str(user.get("email", "")),
        "status": str(user.get("status", "pending")),
        "role": _user_role(user),
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
        "approved_at": user.get("approved_at"),
        "rejected_at": user.get("rejected_at"),
        "plan": subscription["plan"],
        "subscription_status": subscription["status"],
        "cycle_end": subscription["cycle_end"],
        "credits_used_today": credits_of(user)["used"],
        "marks_count": len(marks_of(user)),
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
        "admins": sum(_user_role(user) == "admin" for user in users),
        "subscribers": sum(subscription_of(user)["status"] == "active" for user in users),
    }


def _mutate(user_id: str, apply) -> dict:
    with _LOCK:
        users = load_users()
        target = next((user for user in users if str(user.get("id")) == str(user_id)), None)
        if not target:
            raise LookupError("Usuário não encontrado.")
        apply(target)
        target["updated_at"] = _now()
        save_users(users)
        return _public_admin_user(target)


def update_user_status(user_id: str, status: str) -> dict:
    if status not in VALID_STATUSES:
        raise ValueError("Status de acesso inválido.")

    def apply(target: dict) -> None:
        now = _now()
        target["status"] = status
        target["approved_at"] = now if status == "approved" else None
        target["rejected_at"] = now if status == "rejected" else None

    return _mutate(user_id, apply)


def update_user_role(user_id: str, role: str) -> dict:
    if role not in VALID_ROLES:
        raise ValueError("Papel de acesso inválido.")

    def apply(target: dict) -> None:
        target["role"] = role
        if role == "admin" and target.get("status") != "approved":
            target["status"] = "approved"
            target["approved_at"] = _now()
            target["rejected_at"] = None

    return _mutate(user_id, apply)


def set_user_password(user_id: str, password: str) -> dict:
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"A senha deve ter pelo menos {PASSWORD_MIN_LENGTH} caracteres.")
    if len(password) > 128:
        raise ValueError("A senha deve ter no máximo 128 caracteres.")

    def apply(target: dict) -> None:
        target["password_hash"] = hash_password(password)

    return _mutate(user_id, apply)


def delete_user(user_id: str) -> None:
    with _LOCK:
        users = load_users()
        remaining = [user for user in users if str(user.get("id")) != str(user_id)]
        if len(remaining) == len(users):
            raise LookupError("Usuário não encontrado.")
        save_users(remaining)


# ---------------------------------------------------------------------------
# Assinatura, créditos diários e marcações do usuário
# ---------------------------------------------------------------------------


EMPTY_SUBSCRIPTION = {
    "plan": "",
    "status": "none",
    "asaas_customer_id": "",
    "asaas_subscription_id": "",
    "payment_id": "",
    "invoice_url": "",
    "cycle_start": None,
    "cycle_end": None,
    "confirmed_at": None,
    "updated_at": None,
}


def _slugify(value: str) -> str:
    """Chave estável para uma obra: sem acento, sem pontuação, em minúsculas."""
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_only = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")


def mark_key(title_original: str, title_pt: str = "", year: int | str = 0) -> str:
    """Identidade da obra marcada. O título original manda; o ano desempata."""
    base = _slugify(title_original) or _slugify(title_pt)
    if not base:
        return ""
    try:
        parsed_year = int(year or 0)
    except (TypeError, ValueError):
        parsed_year = 0
    return f"{base}:{parsed_year}" if parsed_year else base


def subscription_of(user: dict | None) -> dict:
    """Assinatura gravada na conta, sempre com todas as chaves preenchidas."""
    stored = (user or {}).get("subscription")
    subscription = dict(EMPTY_SUBSCRIPTION)
    if isinstance(stored, dict):
        subscription.update({key: stored.get(key, subscription[key]) for key in subscription})
    return subscription


def credits_of(user: dict | None, today: str | None = None) -> dict:
    """Consumo do dia corrente. Um dia novo zera a contagem sem precisar gravar."""
    reference = today or brazil_today()
    stored = (user or {}).get("credits")
    if isinstance(stored, dict) and str(stored.get("date", "")) == reference:
        try:
            used = max(0, int(stored.get("used", 0) or 0))
        except (TypeError, ValueError):
            used = 0
        return {"date": reference, "used": used}
    return {"date": reference, "used": 0}


def marks_of(user: dict | None) -> list[dict]:
    stored = (user or {}).get("marks")
    return [item for item in stored if isinstance(item, dict)] if isinstance(stored, list) else []


def _mutate_by_email(email: str, apply):
    """Aplica uma mudança na conta do e-mail informado e devolve o retorno de `apply`."""
    normalized = normalize_email(email)
    with _LOCK:
        users = load_users()
        target = find_user(normalized, users)
        if not target:
            raise LookupError("Conta não encontrada.")
        result = apply(target)
        target["updated_at"] = _now()
        save_users(users)
        return result


def get_account(email: str) -> dict | None:
    return find_user(email)


def save_subscription(email: str, patch: dict) -> dict:
    """Grava campos da assinatura preservando os que não vieram no patch."""

    def apply(target: dict) -> dict:
        subscription = subscription_of(target)
        for key, value in patch.items():
            if key in EMPTY_SUBSCRIPTION:
                subscription[key] = value
        subscription["updated_at"] = _now()
        target["subscription"] = subscription
        return subscription

    return _mutate_by_email(email, apply)


def consume_credit(email: str, daily_limit: int | None, today: str | None = None) -> dict:
    """Debita um crédito do dia. `daily_limit` None significa ilimitado.

    A leitura, a checagem e a gravação acontecem sob o mesmo lock para que duas
    buscas simultâneas não gastem o mesmo crédito duas vezes.
    """
    reference = today or brazil_today()

    def apply(target: dict) -> dict:
        credits = credits_of(target, reference)
        if daily_limit is not None and credits["used"] >= daily_limit:
            raise CreditsExhaustedError("Seus créditos de hoje acabaram.")
        credits["used"] += 1
        target["credits"] = credits
        return dict(credits)

    return _mutate_by_email(email, apply)


def refund_credit(email: str, today: str | None = None) -> dict:
    """Devolve o crédito quando a consulta não chegou a produzir recomendações."""
    reference = today or brazil_today()

    def apply(target: dict) -> dict:
        credits = credits_of(target, reference)
        credits["used"] = max(0, credits["used"] - 1)
        target["credits"] = credits
        return dict(credits)

    return _mutate_by_email(email, apply)


def list_marks(email: str) -> list[dict]:
    """Marcações do usuário, da mais recente para a mais antiga."""
    user = find_user(email)
    if not user:
        raise LookupError("Conta não encontrada.")
    marks = marks_of(user)
    return sorted(marks, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)


def save_mark(email: str, mark: dict) -> dict:
    """Cria ou atualiza a marcação de uma obra. A chave da obra evita duplicatas.

    `opinion` e `watched` são independentes: quem só marcou "já assisti" continua
    sem opinião registrada, e vice-versa.
    """
    title_original = str(mark.get("title_original", "") or "").strip()[:200]
    title_pt = str(mark.get("title_pt", "") or "").strip()[:200]
    if not title_original and not title_pt:
        raise ValueError("Informe o título da obra marcada.")

    opinion = str(mark.get("opinion", "") or "").strip().lower()
    if opinion not in VALID_OPINIONS:
        raise ValueError("Marcação inválida: use 'liked', 'disliked' ou vazio.")
    watched = bool(mark.get("watched", False))

    try:
        year = int(mark.get("year", 0) or 0)
    except (TypeError, ValueError):
        year = 0
    key = mark_key(title_original, title_pt, year)
    if not key:
        raise ValueError("Informe o título da obra marcada.")

    def apply(target: dict) -> dict:
        marks = marks_of(target)
        now = _now()
        existing = next((item for item in marks if str(item.get("key")) == key), None)
        if existing:
            existing.update(
                {
                    "title_original": title_original or existing.get("title_original", ""),
                    "title_pt": title_pt or existing.get("title_pt", ""),
                    "year": year or existing.get("year", 0),
                    "opinion": opinion,
                    "watched": watched,
                    "updated_at": now,
                }
            )
            record = existing
        else:
            record = {
                "id": secrets.token_urlsafe(12),
                "key": key,
                "title_original": title_original,
                "title_pt": title_pt,
                "year": year,
                "opinion": opinion,
                "watched": watched,
                "created_at": now,
                "updated_at": now,
            }
            marks.append(record)
        # Uma marcação sem opinião e sem "já assisti" não é marcação nenhuma:
        # apagá-la é o que devolve a obra às indicações futuras.
        if not opinion and not watched:
            marks = [item for item in marks if str(item.get("key")) != key]
            target["marks"] = marks
            return {}
        if len(marks) > MAX_MARKS_PER_USER:
            marks = sorted(marks, key=lambda item: str(item.get("updated_at") or ""), reverse=True)[:MAX_MARKS_PER_USER]
        target["marks"] = marks
        return dict(record)

    return _mutate_by_email(email, apply)


def delete_mark(email: str, mark_id: str) -> None:
    """Remove uma marcação individual: a obra volta a aparecer nas indicações."""
    wanted = str(mark_id or "").strip()
    if not wanted:
        raise ValueError("Informe a marcação que deve ser removida.")

    def apply(target: dict) -> None:
        marks = marks_of(target)
        remaining = [item for item in marks if str(item.get("id")) != wanted]
        if len(remaining) == len(marks):
            raise LookupError("Marcação não encontrada.")
        target["marks"] = remaining

    _mutate_by_email(email, apply)


def clear_marks(email: str) -> None:
    def apply(target: dict) -> None:
        target["marks"] = []

    _mutate_by_email(email, apply)
