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
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode, urlparse
from datetime import datetime, timezone
from pathlib import Path


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PASSWORD_MIN_LENGTH = 8
PBKDF2_ITERATIONS = 260_000
STORE_KEY = "pipoca-play:users"
POSTGRES_TABLE = "pipoca_play_store"
BLOB_PATH = os.environ.get("USER_STORE_BLOB_PATH", "pipoca-play/users.json").strip() or "pipoca-play/users.json"
VALID_ROLES = ("user", "admin")
_LOCK = threading.RLock()

SETUP_HINT = (
    "Abra o projeto na Vercel em Storage → Create Database e conecte um banco "
    "Postgres (Neon), um Upstash Redis ou um Blob store. A Vercel injeta as "
    "variáveis automaticamente; depois faça um novo deploy."
)


class StorageError(RuntimeError):
    """Indica que a base de contas não pôde ser lida ou gravada."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def register_user(email: str, password: str, confirmation: str | None = None) -> dict:
    """Cadastro do cliente: a conta já nasce ativa.

    Não existe mais fila de aprovação — quem controla o acesso ao resultado é o
    pagamento, conferido em ``consume_credit``.
    """
    normalized = validate_registration(email, password, confirmation)
    with _LOCK:
        users = load_users()
        if find_user(normalized, users):
            raise FileExistsError("Já existe uma conta com este e-mail.")

        now = _now()
        record = {
            "id": secrets.token_urlsafe(16),
            "email": normalized,
            "password_hash": hash_password(password),
            "role": "user",
            "created_at": now,
            "updated_at": now,
        }
        users.append(record)
        save_users(users)
        return record


def create_user(email: str, password: str, role: str = "user") -> dict:
    """Criação direta pelo painel administrativo."""
    normalized = validate_registration(email, password)
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
            "role": role,
            "created_at": now,
            "updated_at": now,
        }
        users.append(record)
        save_users(users)
        return _public_admin_user(record)


def user_exists(email: str) -> bool:
    """A conta ainda está na base? É o que mantém uma sessão válida."""
    return bool(find_user(email))


def is_admin_user(email: str) -> bool:
    user = find_user(email)
    return bool(user and _user_role(user) == "admin")


def _public_grant(grant: dict) -> dict:
    """Uma linha do histórico de concessões, já no formato lido pelo painel."""
    return {
        "id": str(grant.get("id", "")),
        "origin": str(grant.get("origin", "manual")),
        "credits": int(grant.get("credits", 0) or 0),
        "balance_before": int(grant.get("balance_before", 0) or 0),
        "balance_after": int(grant.get("balance_after", 0) or 0),
        "reason": str(grant.get("reason", "")),
        "granted_by": str(grant.get("granted_by", "")),
        "created_at": grant.get("created_at"),
    }


def _public_admin_user(user: dict) -> dict:
    billing = user.get("billing") if isinstance(user.get("billing"), dict) else {}
    credits = user.get("credits") if isinstance(user.get("credits"), dict) else {}
    feedback = user.get("feedback") if isinstance(user.get("feedback"), list) else []
    grants = user.get("credit_grants") if isinstance(user.get("credit_grants"), list) else []
    wallet = user.get("credit_wallet") if isinstance(user.get("credit_wallet"), dict) else {}
    return {
        "id": str(user.get("id", "")),
        "email": str(user.get("email", "")),
        "role": _user_role(user),
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
        "plan": str(billing.get("plan", "")),
        "subscription_status": str(billing.get("status", "none")),
        "subscription_active": subscription_is_active(user),
        "expires_at": billing.get("expires_at"),
        "credits_used_today": int(credits.get("used", 0)) if credits.get("day") == brazil_day() else 0,
        # Saldo do plano (renova todo dia) e saldo avulso (não renova) lado a lado.
        "plan_credits_remaining": _plan_remaining(user),
        "credit_balance": wallet_balance(user),
        "credits_remaining": _remaining_credits(user),
        "credits_granted_total": int(wallet.get("granted", 0) or 0),
        "credit_grants": [_public_grant(grant) for grant in grants[:5]],
        "feedback_count": len(feedback),
    }


def admin_users() -> list[dict]:
    users = load_users()
    return [_public_admin_user(user) for user in sorted(users, key=lambda item: item.get("created_at", ""))]


def admin_summary() -> dict:
    users = load_users()
    return {
        "total": len(users),
        "admins": sum(_user_role(user) == "admin" for user in users),
        "subscribers": sum(subscription_is_active(user) for user in users),
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


def update_user_role(user_id: str, role: str) -> dict:
    if role not in VALID_ROLES:
        raise ValueError("Papel de acesso inválido.")

    def apply(target: dict) -> None:
        target["role"] = role

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

from datetime import timedelta  # noqa: E402
from plans import UNLIMITED, daily_credits, get_plan  # noqa: E402

# O Brasil não adota horário de verão: o fuso de Brasília é UTC-3 fixo. Usar um
# deslocamento fixo evita depender do banco de fusos do runtime serverless.
BRAZIL_OFFSET = timedelta(hours=-3)
MAX_FEEDBACK_ITEMS = 300
FEEDBACK_OPINIONS = ("liked", "disliked")
SUBSCRIPTION_STATUSES = ("none", "pending", "active", "past_due", "canceled")

# Créditos avulsos: concedidos por fora do checkout, gastos uma única vez.
# "manual" é a concessão do administrador; "trial" é o período de teste grátis.
# Nenhum dos dois cria assinatura nem se renova sozinho — quando o saldo acaba,
# só outra concessão ou um plano pago devolve créditos à conta.
CREDIT_ORIGINS = ("manual", "trial")
MAX_CREDIT_GRANT = 10_000
MAX_CREDIT_GRANTS_STORED = 50
MAX_CREDIT_REASON_LENGTH = 200


class CreditError(RuntimeError):
    """Os créditos do dia acabaram."""


class SubscriptionRequired(RuntimeError):
    """A conta não tem uma assinatura ativa e paga."""


def _now_dt():
    return datetime.now(timezone.utc)


def brazil_day(moment=None) -> str:
    """Data corrente no fuso de Brasília, no formato AAAA-MM-DD."""
    return ((moment or _now_dt()) + BRAZIL_OFFSET).strftime("%Y-%m-%d")


def _parse_iso(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _normalize_title_key(title_pt: str, title_original: str, year=0) -> str:
    base = (str(title_original or "").strip() or str(title_pt or "").strip()).lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    return f"{base}:{int(year or 0)}" if base else ""


def _billing(user: dict) -> dict:
    billing = user.get("billing")
    if not isinstance(billing, dict):
        billing = {}
        user["billing"] = billing
    billing.setdefault("plan", "")
    billing.setdefault("status", "none")
    billing.setdefault("started_at", None)
    billing.setdefault("expires_at", None)
    billing.setdefault("asaas_customer_id", "")
    billing.setdefault("asaas_subscription_id", "")
    billing.setdefault("last_payment_id", "")
    billing.setdefault("last_event", "")
    billing.setdefault("checkout_url", "")
    return billing


def _credits(user: dict) -> dict:
    credits = user.get("credits")
    if not isinstance(credits, dict):
        credits = {}
        user["credits"] = credits
    credits.setdefault("day", "")
    credits.setdefault("used", 0)
    return credits


def _feedback(user: dict) -> list[dict]:
    items = user.get("feedback")
    if not isinstance(items, list):
        items = []
        user["feedback"] = items
    return items


def _wallet(user: dict) -> dict:
    """Carteira de créditos avulsos (concessão manual e teste grátis).

    Vive fora de ``billing``: é saldo gasto uma vez, sem ciclo, sem renovação e
    sem qualquer vínculo com a assinatura da conta.
    """
    wallet = user.get("credit_wallet")
    if not isinstance(wallet, dict):
        wallet = {}
        user["credit_wallet"] = wallet
    wallet.setdefault("balance", 0)
    wallet.setdefault("granted", 0)
    wallet.setdefault("used", 0)
    return wallet


def _credit_grants(user: dict) -> list[dict]:
    """Histórico das concessões avulsas — quem concedeu, quanto, quando e por quê."""
    grants = user.get("credit_grants")
    if not isinstance(grants, list):
        grants = []
        user["credit_grants"] = grants
    return grants


def wallet_balance(user: dict) -> int:
    return max(int(_wallet(user).get("balance", 0) or 0), 0)


def subscription_is_active(user: dict) -> bool:
    billing = _billing(user)
    if billing["status"] != "active":
        return False
    expires = _parse_iso(billing.get("expires_at"))
    return bool(expires and expires > _now_dt())


def _plan_remaining(user: dict) -> int:
    """Créditos do plano que ainda restam hoje. ``UNLIMITED`` no plano ilimitado.

    Só conta com assinatura ativa: é este o saldo que se renova todo dia.
    """
    if not subscription_is_active(user):
        return 0
    allowance = daily_credits(_billing(user).get("plan", ""))
    if allowance == UNLIMITED:
        return UNLIMITED
    credits = _credits(user)
    used = int(credits.get("used", 0)) if credits.get("day") == brazil_day() else 0
    return max(allowance - used, 0)


def _remaining_credits(user: dict) -> int:
    """Saldo total disponível: o do plano de hoje mais os créditos avulsos."""
    plan_remaining = _plan_remaining(user)
    if plan_remaining == UNLIMITED:
        return UNLIMITED
    return plan_remaining + wallet_balance(user)


def account_snapshot(user: dict) -> dict:
    """Retrato da conta usado pelo front-end: plano, créditos e assinatura."""
    billing = _billing(user)
    plan = get_plan(billing.get("plan", ""))
    active = subscription_is_active(user)
    plan_remaining = _plan_remaining(user)
    balance = wallet_balance(user)
    allowance = daily_credits(billing.get("plan", "")) if active else 0
    return {
        "email": str(user.get("email", "")),
        "role": _user_role(user),
        "plan": plan["id"] if plan else "",
        "plan_name": plan["name"] if plan else "",
        "subscription_status": billing.get("status", "none"),
        "subscription_active": active,
        "expires_at": billing.get("expires_at"),
        "started_at": billing.get("started_at"),
        "checkout_url": billing.get("checkout_url", "") if not active else "",
        "unlimited": active and allowance == UNLIMITED,
        "daily_credits": allowance,
        "credits_remaining": _remaining_credits(user),
        "plan_credits_remaining": plan_remaining,
        # Saldo avulso: não renova e não vira assinatura.
        "credit_balance": balance,
        "credits_used_today": int(_credits(user).get("used", 0)) if _credits(user).get("day") == brazil_day() else 0,
        "day": brazil_day(),
        "feedback_count": len(_feedback(user)),
    }


def get_account(user_id: str) -> dict:
    user = find_user_by_id(user_id)
    if not user:
        raise LookupError("Usuário não encontrado.")
    return account_snapshot(user)


def consume_credit(user_id: str) -> dict:
    """Debita um crédito da conta. Levanta erro quando não há assinatura ou saldo.

    O crédito do **plano** sai primeiro, porque ele se renova amanhã; o crédito
    avulso (manual ou teste grátis) é o último a ser gasto, já que ninguém o
    repõe. Guardamos em ``credits["last_source"]`` de onde veio o débito, para
    que ``refund_credit`` devolva no mesmo lugar.
    """
    with _LOCK:
        users = load_users()
        target = next((user for user in users if str(user.get("id")) == str(user_id)), None)
        if not target:
            raise LookupError("Usuário não encontrado.")
        billing = _billing(target)
        allowance = daily_credits(billing.get("plan", ""))
        active = subscription_is_active(target)
        credits = _credits(target)
        today = brazil_day()
        if credits.get("day") != today:
            credits["day"] = today
            credits["used"] = 0
        plan_has_credit = active and (allowance == UNLIMITED or int(credits.get("used", 0)) < allowance)
        balance = wallet_balance(target)

        if plan_has_credit:
            credits["used"] = int(credits.get("used", 0)) + 1
            credits["last_source"] = "plan"
        elif balance > 0:
            wallet = _wallet(target)
            wallet["balance"] = balance - 1
            wallet["used"] = int(wallet.get("used", 0) or 0) + 1
            credits["last_source"] = "wallet"
        elif active:
            raise CreditError(
                f"Seus {allowance} crédito(s) de hoje acabaram. Eles voltam amanhã, ou você pode migrar de plano."
            )
        else:
            # Sem plano e sem saldo avulso: crédito avulso não se renova sozinho,
            # então a saída é contratar um plano ou receber nova concessão.
            raise SubscriptionRequired(
                "Sua assinatura não está ativa. Escolha um plano e conclua o pagamento para liberar as indicações."
            )
        target["updated_at"] = _now()
        save_users(users)
        return account_snapshot(target)


def refund_credit(user_id: str) -> None:
    """Devolve o crédito quando a consulta falha antes de entregar resultado."""
    with _LOCK:
        users = load_users()
        target = next((user for user in users if str(user.get("id")) == str(user_id)), None)
        if not target:
            return
        credits = _credits(target)
        if str(credits.get("last_source", "plan")) == "wallet":
            wallet = _wallet(target)
            if int(wallet.get("used", 0) or 0) > 0:
                wallet["balance"] = wallet_balance(target) + 1
                wallet["used"] = int(wallet["used"]) - 1
                credits["last_source"] = ""
                save_users(users)
            return
        if credits.get("day") == brazil_day() and int(credits.get("used", 0)) > 0:
            credits["used"] = int(credits["used"]) - 1
            credits["last_source"] = ""
            save_users(users)


def _validate_credit_amount(value) -> int:
    """A quantidade precisa ser um inteiro positivo dentro do teto por concessão."""
    if isinstance(value, bool) or value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError("Informe quantos créditos deseja adicionar.")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("Os créditos precisam ser um número inteiro.")
        amount = int(value)
    else:
        try:
            amount = int(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError("Os créditos precisam ser um número inteiro.") from None
    if amount <= 0:
        raise ValueError("A quantidade de créditos precisa ser maior que zero.")
    if amount > MAX_CREDIT_GRANT:
        raise ValueError(f"O máximo por concessão é de {MAX_CREDIT_GRANT} créditos.")
    return amount


def grant_credits(user_id: str, credits, origin: str = "manual", reason: str = "", granted_by: str = "") -> dict:
    """Concede créditos avulsos a uma conta, sem encostar na assinatura.

    Soma ao saldo existente e grava a concessão no histórico da conta. Não cria
    plano, não muda ``billing["status"]``, não mexe em ``expires_at`` e não gera
    renovação: quando o saldo acabar, só um plano pago ou outra concessão repõe.
    """
    amount = _validate_credit_amount(credits)
    normalized_origin = str(origin or "manual").strip().lower()
    if normalized_origin not in CREDIT_ORIGINS:
        raise ValueError("Origem de crédito inválida.")
    note = str(reason or "").strip()[:MAX_CREDIT_REASON_LENGTH]
    author = normalize_email(str(granted_by or ""))
    record: dict = {}

    def apply(target: dict) -> None:
        wallet = _wallet(target)
        previous = wallet_balance(target)
        wallet["balance"] = previous + amount
        wallet["granted"] = int(wallet.get("granted", 0) or 0) + amount
        record.update(
            {
                "id": secrets.token_urlsafe(12),
                "origin": normalized_origin,
                "credits": amount,
                "balance_before": previous,
                "balance_after": wallet["balance"],
                "reason": note,
                "granted_by": author,
                "created_at": _now(),
            }
        )
        grants = _credit_grants(target)
        grants.insert(0, dict(record))
        del grants[MAX_CREDIT_GRANTS_STORED:]

    return {"user": _mutate(user_id, apply), "grant": dict(record)}


def list_credit_grants(user_id: str, limit: int = MAX_CREDIT_GRANTS_STORED) -> list[dict]:
    """Histórico de concessões da conta, da mais recente para a mais antiga."""
    user = find_user_by_id(user_id)
    if not user:
        raise LookupError("Usuário não encontrado.")
    return [_public_grant(grant) for grant in _credit_grants(user)[: max(int(limit), 0)]]


def start_checkout(user_id: str, plan_id: str, customer_id: str, subscription_id: str, checkout_url: str) -> dict:
    """Guarda a referência da cobrança criada na Asaas, ainda sem liberar acesso."""
    plan = get_plan(plan_id)
    if not plan:
        raise ValueError("Plano inválido.")

    def apply(target: dict) -> None:
        billing = _billing(target)
        billing["plan"] = plan["id"]
        billing["asaas_customer_id"] = customer_id or billing.get("asaas_customer_id", "")
        billing["asaas_subscription_id"] = subscription_id or billing.get("asaas_subscription_id", "")
        billing["checkout_url"] = checkout_url or ""
        if billing.get("status") != "active":
            billing["status"] = "pending"

    _mutate(user_id, apply)
    return get_account(user_id)


def activate_subscription(user_id: str, plan_id: str = "", payment_id: str = "", event: str = "") -> dict:
    """Confirma o pagamento e libera o ciclo de 30 dias."""

    def apply(target: dict) -> None:
        billing = _billing(target)
        chosen = get_plan(plan_id) or get_plan(billing.get("plan", ""))
        if not chosen:
            raise ValueError("Plano inválido.")
        now = _now_dt()
        current = _parse_iso(billing.get("expires_at"))
        base = current if current and current > now and billing.get("status") == "active" else now
        billing["plan"] = chosen["id"]
        billing["status"] = "active"
        billing["started_at"] = billing.get("started_at") or now.isoformat(timespec="seconds")
        billing["expires_at"] = (base + timedelta(days=int(chosen["cycle_days"]))).isoformat(timespec="seconds")
        billing["last_payment_id"] = payment_id or billing.get("last_payment_id", "")
        billing["last_event"] = event or billing.get("last_event", "")
        billing["checkout_url"] = ""

    _mutate(user_id, apply)
    return get_account(user_id)


def set_subscription_status(user_id: str, status: str, event: str = "") -> dict:
    if status not in SUBSCRIPTION_STATUSES:
        raise ValueError("Situação de assinatura inválida.")

    def apply(target: dict) -> None:
        billing = _billing(target)
        billing["status"] = status
        billing["last_event"] = event or billing.get("last_event", "")
        if status in {"canceled", "none"}:
            billing["expires_at"] = None

    _mutate(user_id, apply)
    return get_account(user_id)


def find_user_by_billing(customer_id: str = "", subscription_id: str = "") -> dict | None:
    """Localiza a conta pela referência da Asaas, usada pelo webhook."""
    if not customer_id and not subscription_id:
        return None
    for user in load_users():
        billing = user.get("billing") if isinstance(user.get("billing"), dict) else {}
        if subscription_id and str(billing.get("asaas_subscription_id", "")) == str(subscription_id):
            return user
        if customer_id and str(billing.get("asaas_customer_id", "")) == str(customer_id):
            return user
    return None


# ---------------------------------------------------------------------------
# Marcações "gostei / não gostei / já assisti"
# ---------------------------------------------------------------------------


def _public_feedback(item: dict) -> dict:
    return {
        "id": str(item.get("id", "")),
        "title_pt": str(item.get("title_pt", "")),
        "title_original": str(item.get("title_original", "")),
        "year": int(item.get("year", 0) or 0),
        "opinion": str(item.get("opinion", "")),
        "watched": bool(item.get("watched", False)),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def list_feedback(user_id: str) -> list[dict]:
    user = find_user_by_id(user_id)
    if not user:
        raise LookupError("Usuário não encontrado.")
    items = sorted(_feedback(user), key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return [_public_feedback(item) for item in items]


def set_feedback(user_id: str, entry: dict) -> list[dict]:
    """Cria ou atualiza a marcação de um título. Marcação vazia é removida."""
    title_pt = str(entry.get("title_pt", "")).strip()[:160]
    title_original = str(entry.get("title_original", "")).strip()[:160]
    if not title_pt and not title_original:
        raise ValueError("Informe o título que você quer marcar.")
    try:
        year = int(entry.get("year", 0) or 0)
    except (TypeError, ValueError):
        year = 0
    opinion = str(entry.get("opinion", "") or "").strip().lower()
    if opinion not in FEEDBACK_OPINIONS and opinion != "":
        raise ValueError("Marcação inválida.")
    watched = bool(entry.get("watched", False))
    key = _normalize_title_key(title_pt, title_original, year)
    if not key:
        raise ValueError("Informe o título que você quer marcar.")

    def apply(target: dict) -> None:
        items = _feedback(target)
        now = _now()
        existing = next((item for item in items if item.get("key") == key), None)
        if not opinion and not watched:
            if existing:
                items.remove(existing)
            return
        if existing:
            existing.update(
                {
                    "title_pt": title_pt or existing.get("title_pt", ""),
                    "title_original": title_original or existing.get("title_original", ""),
                    "year": year or existing.get("year", 0),
                    "opinion": opinion,
                    "watched": watched,
                    "updated_at": now,
                }
            )
        else:
            items.append(
                {
                    "id": secrets.token_urlsafe(12),
                    "key": key,
                    "title_pt": title_pt,
                    "title_original": title_original,
                    "year": year,
                    "opinion": opinion,
                    "watched": watched,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        if len(items) > MAX_FEEDBACK_ITEMS:
            items.sort(key=lambda item: str(item.get("updated_at") or ""))
            del items[: len(items) - MAX_FEEDBACK_ITEMS]

    _mutate(user_id, apply)
    return list_feedback(user_id)


def remove_feedback(user_id: str, feedback_id: str) -> list[dict]:
    """Apaga uma marcação para que o título volte a aparecer nas indicações."""
    removed = {"done": False}

    def apply(target: dict) -> None:
        items = _feedback(target)
        remaining = [item for item in items if str(item.get("id")) != str(feedback_id)]
        removed["done"] = len(remaining) != len(items)
        target["feedback"] = remaining

    _mutate(user_id, apply)
    if not removed["done"]:
        raise LookupError("Marcação não encontrada.")
    return list_feedback(user_id)


def clear_feedback(user_id: str) -> list[dict]:
    def apply(target: dict) -> None:
        target["feedback"] = []

    _mutate(user_id, apply)
    return []


# ---------------------------------------------------------------------------
# Histórico de buscas
# ---------------------------------------------------------------------------
#
# Antes, o histórico só existia no localStorage do navegador: o mesmo cliente
# via listas diferentes no celular e no computador. Cada busca concluída passa
# a ficar gravada na conta, no mesmo banco usado por assinatura e marcações.

MAX_HISTORY_ITEMS = 50


def _history(user: dict) -> list[dict]:
    items = user.get("history")
    if not isinstance(items, list):
        items = []
        user["history"] = items
    return items


def _public_history(item: dict) -> dict:
    return {
        "id": str(item.get("id", "")),
        "created_at": item.get("created_at"),
        "filters": item.get("filters") if isinstance(item.get("filters"), dict) else {},
        "interpretation": str(item.get("interpretation", "")),
        "bestChoice": item.get("best_choice") if isinstance(item.get("best_choice"), dict) else None,
        "recommendations": item.get("recommendations") if isinstance(item.get("recommendations"), list) else [],
    }


def list_history(user_id: str) -> list[dict]:
    user = find_user_by_id(user_id)
    if not user:
        raise LookupError("Usuário não encontrado.")
    items = sorted(_history(user), key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return [_public_history(item) for item in items]


def add_history(user_id: str, entry: dict) -> list[dict]:
    """Registra uma consulta concluída no histórico da conta logada."""
    filters = entry.get("filters") if isinstance(entry.get("filters"), dict) else {}
    recommendations = entry.get("recommendations") if isinstance(entry.get("recommendations"), list) else []
    best_choice = entry.get("best_choice") if isinstance(entry.get("best_choice"), dict) else None
    interpretation = str(entry.get("interpretation", ""))[:2000]

    def apply(target: dict) -> None:
        items = _history(target)
        items.insert(
            0,
            {
                "id": secrets.token_urlsafe(12),
                "created_at": _now(),
                "filters": filters,
                "interpretation": interpretation,
                "best_choice": best_choice,
                "recommendations": recommendations,
            },
        )
        if len(items) > MAX_HISTORY_ITEMS:
            del items[MAX_HISTORY_ITEMS:]

    _mutate(user_id, apply)
    return list_history(user_id)


def remove_history(user_id: str, history_id: str) -> list[dict]:
    """Apaga uma busca do histórico da conta logada."""
    removed = {"done": False}

    def apply(target: dict) -> None:
        items = _history(target)
        remaining = [item for item in items if str(item.get("id")) != str(history_id)]
        removed["done"] = len(remaining) != len(items)
        target["history"] = remaining

    _mutate(user_id, apply)
    if not removed["done"]:
        raise LookupError("Busca não encontrada no histórico.")
    return list_history(user_id)


# ---------------------------------------------------------------------------
# Conteúdo da landing: pôsteres administráveis
# ---------------------------------------------------------------------------
#
# Um segundo documento JSON, gravado no mesmo backend das contas e com a mesma
# ordem de detecção. As funções abaixo reaproveitam a conexão/credencial já
# resolvida para as contas (``_postgres_connect``, ``_blob_sdk``,
# ``_kv_command``) e só mudam a chave e o formato do documento — um dicionário,
# não a lista de contas.

LANDING_KEY = "pipoca-play:landing"
LANDING_BLOB_PATH = (
    os.environ.get("LANDING_STORE_BLOB_PATH", "pipoca-play/landing.json").strip()
    or "pipoca-play/landing.json"
)
LANDING_UNAVAILABLE = "Não foi possível ler as imagens da landing."


def _landing_local_path() -> Path:
    """Arquivo irmão do de contas, para os dois modos locais não se misturarem."""
    configured = os.environ.get("LANDING_STORE_FILE", "").strip()
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else Path(__file__).parent / path
    users = _local_path()
    return users.with_name(users.stem + "-landing.json")


def _read_landing_postgres() -> dict:
    with _postgres_connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"CREATE TABLE IF NOT EXISTS {POSTGRES_TABLE} ("
                "key text PRIMARY KEY, "
                "value jsonb NOT NULL, "
                "updated_at timestamptz NOT NULL DEFAULT now())"
            )
            cursor.execute(f"SELECT value FROM {POSTGRES_TABLE} WHERE key = %s", (LANDING_KEY,))
            row = cursor.fetchone()
    if not row or row[0] is None:
        return {}
    data = row[0]
    if isinstance(data, (str, bytes, bytearray)):
        try:
            data = json.loads(data)
        except (TypeError, ValueError) as error:
            raise StorageError(LANDING_UNAVAILABLE) from error
    return data if isinstance(data, dict) else {}


def _write_landing_postgres(document: dict) -> None:
    payload = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
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
                (LANDING_KEY, payload),
            )


def _read_landing_blob() -> dict:
    token = _blob_token()
    if token:
        get, _, BlobNotFoundError = _blob_sdk()
        try:
            result = get(LANDING_BLOB_PATH, access="private", token=token, use_cache=False)
        except BlobNotFoundError:
            return {}
        except Exception as error:  # noqa: BLE001 - o SDK não expõe um tipo estável
            raise StorageError(LANDING_UNAVAILABLE) from error
        content = bytes(result)
    else:
        credentials = _blob_oidc_credentials()
        if not credentials:
            raise StorageError(LANDING_UNAVAILABLE)
        oidc_token, store_id = credentials
        object_url = (
            f"https://{store_id}.private.blob.vercel-storage.com/"
            f"{quote(LANDING_BLOB_PATH, safe='/')}"
        )
        request = urllib.request.Request(
            object_url, headers={"Authorization": f"Bearer {oidc_token}"}, method="GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                content = response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return {}
            raise StorageError(LANDING_UNAVAILABLE) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise StorageError(LANDING_UNAVAILABLE) from error
    try:
        data = json.loads(content.decode("utf-8"))
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise StorageError(LANDING_UNAVAILABLE) from error
    return data if isinstance(data, dict) else {}


def _write_landing_blob(document: dict) -> None:
    payload = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    token = _blob_token()
    if token:
        _, put, _ = _blob_sdk()
        try:
            put(
                LANDING_BLOB_PATH,
                payload,
                access="private",
                content_type="application/json",
                overwrite=True,
                cache_control_max_age=0,
                token=token,
            )
        except Exception as error:  # noqa: BLE001 - o SDK não expõe um tipo estável
            raise StorageError("Não foi possível gravar as imagens da landing no Vercel Blob.") from error
        return
    credentials = _blob_oidc_credentials()
    if not credentials:
        raise StorageError("Não foi possível gravar as imagens da landing no Vercel Blob.")
    oidc_token, store_id = credentials
    request = urllib.request.Request(
        f"https://vercel.com/api/blob/?{urlencode({'pathname': LANDING_BLOB_PATH})}",
        data=payload,
        headers={
            "Authorization": f"Bearer {oidc_token}",
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
        raise StorageError("Não foi possível gravar as imagens da landing no Vercel Blob.") from error


def _read_landing_local() -> dict:
    path = _landing_local_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError) as error:
        raise StorageError(LANDING_UNAVAILABLE) from error
    return data if isinstance(data, dict) else {}


def _write_landing_local(document: dict) -> None:
    if _is_serverless():
        raise StorageError(
            "Este deploy não tem banco conectado, e o disco das funções serverless não "
            "guarda dados entre requisições. " + SETUP_HINT
        )
    path = _landing_local_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError as error:
        raise StorageError(
            f"Não foi possível gravar as imagens da landing em {path}. "
            "Verifique a permissão de escrita da pasta."
        ) from error


def load_landing() -> dict:
    """Documento completo da landing: ``{"slots": {...}, "updated_at": ...}``."""
    mode = storage_mode()
    with _LOCK:
        if mode == "postgres":
            document = _read_landing_postgres()
        elif mode == "vercel-blob":
            document = _read_landing_blob()
        elif mode == "redis-rest":
            raw = _kv_command("GET", LANDING_KEY)
            if not raw:
                document = {}
            else:
                try:
                    parsed = json.loads(raw)
                except (TypeError, json.JSONDecodeError) as error:
                    raise StorageError(LANDING_UNAVAILABLE) from error
                document = parsed if isinstance(parsed, dict) else {}
        else:
            document = _read_landing_local()
    slots = document.get("slots")
    return {
        "slots": {
            key: value
            for key, value in (slots.items() if isinstance(slots, dict) else ())
            if isinstance(value, dict)
        },
        "updated_at": document.get("updated_at") or "",
    }


def save_landing(document: dict) -> None:
    mode = storage_mode()
    with _LOCK:
        if mode == "postgres":
            _write_landing_postgres(document)
            return
        if mode == "vercel-blob":
            _write_landing_blob(document)
            return
        if mode == "redis-rest":
            _kv_command("SET", LANDING_KEY, json.dumps(document, ensure_ascii=False, separators=(",", ":")))
            return
        _write_landing_local(document)


def landing_slots() -> dict:
    """Somente os espaços já preenchidos, na forma que a landing consome."""
    return load_landing()["slots"]


def set_landing_slot(slot_id: str, values: dict, editor: str = "") -> dict:
    """Grava (ou limpa) um espaço. Campos ausentes preservam o valor anterior."""
    document = load_landing()
    slots = document["slots"]
    current = dict(slots.get(slot_id) or {})
    for field in ("image", "title", "meta"):
        if field in values:
            new_value = values[field]
            if new_value:
                current[field] = new_value
            else:
                current.pop(field, None)
    if current:
        current["updated_at"] = _now()
        if editor:
            current["updated_by"] = editor
        slots[slot_id] = current
    else:
        slots.pop(slot_id, None)
    document["updated_at"] = _now()
    save_landing(document)
    return slots


def clear_landing_slot(slot_id: str) -> dict:
    """Remove o espaço por completo: a landing volta ao conteúdo original."""
    document = load_landing()
    document["slots"].pop(slot_id, None)
    document["updated_at"] = _now()
    save_landing(document)
    return document["slots"]
