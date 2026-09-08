"""Planos, checkout ASAAS e créditos diários do Pipoca & Play.

Um crédito equivale a uma consulta — um filme, uma série ou uma novela. Os
créditos são diários e zeram à meia-noite de Brasília; o plano diamante não tem
limite diário e vale enquanto o ciclo de 30 dias estiver pago.

O acesso aos resultados só é liberado depois que o ASAAS confirma o pagamento.
A confirmação chega por duas vias independentes: o webhook `POST
/api/billing/webhook` e a consulta ativa feita em `GET /api/billing/status`. A
segunda existe para que a plataforma não fique refém da entrega do webhook.
"""

from __future__ import annotations

import hmac
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from user_store import (
    BRAZIL_TZ,
    CreditsExhaustedError,
    brazil_today,
    consume_credit,
    credits_of,
    find_user,
    list_marks,
    marks_of,
    refund_credit,
    save_subscription,
    subscription_of,
)


CYCLE_DAYS = 30

PLANS = {
    "silver": {
        "id": "silver",
        "name": "Silver",
        "price": 15.00,
        "daily_credits": 2,
        "unlimited": False,
        "tagline": "2 consultas por dia",
        "description": "2 créditos diários para descobrir o que assistir sem rolar catálogo.",
    },
    "gold": {
        "id": "gold",
        "name": "Gold",
        "price": 25.00,
        "daily_credits": 5,
        "unlimited": False,
        "tagline": "5 consultas por dia",
        "description": "5 créditos diários para quem escolhe o filme da casa toda.",
    },
    "diamante": {
        "id": "diamante",
        "name": "Diamante",
        "price": 30.00,
        "daily_credits": None,
        "unlimited": True,
        "tagline": "Consultas ilimitadas",
        "description": "Pesquisa ilimitada durante todo o ciclo de 30 dias.",
    },
}

PLAN_ORDER = ("silver", "gold", "diamante")

# Status que o ASAAS usa para dizer que o dinheiro entrou.
PAID_PAYMENT_STATUSES = {"RECEIVED", "CONFIRMED", "RECEIVED_IN_CASH"}
# Status que indicam que o pagamento existe, mas ainda não foi liquidado.
PENDING_PAYMENT_STATUSES = {"PENDING", "AWAITING_RISK_ANALYSIS"}


class BillingError(RuntimeError):
    """Falha ao falar com o ASAAS ou configuração ausente."""


class PaymentRequiredError(RuntimeError):
    """Não há assinatura paga e ativa para liberar os resultados."""


def plans_catalog() -> list[dict]:
    return [dict(PLANS[plan_id]) for plan_id in PLAN_ORDER]


def get_plan(plan_id: str) -> dict:
    plan = PLANS.get(str(plan_id or "").strip().lower())
    if not plan:
        raise ValueError("Plano inválido. Escolha entre silver, gold ou diamante.")
    return plan


def daily_limit(plan_id: str) -> int | None:
    """Créditos por dia do plano. `None` significa ilimitado."""
    return PLANS.get(str(plan_id or "").lower(), {}).get("daily_credits")


# ---------------------------------------------------------------------------
# Cliente HTTP do ASAAS
# ---------------------------------------------------------------------------


def api_key() -> str:
    return os.environ.get("ASAAS_API_KEY", "").strip()


def api_base() -> str:
    configured = os.environ.get("ASAAS_API_URL", "").strip().rstrip("/")
    if configured:
        return configured
    sandbox = os.environ.get("ASAAS_ENVIRONMENT", "").strip().lower() in {"sandbox", "homolog", "homologacao"}
    return "https://api-sandbox.asaas.com/v3" if sandbox else "https://api.asaas.com/v3"


def is_configured() -> bool:
    return bool(api_key())


def webhook_token() -> str:
    return os.environ.get("ASAAS_WEBHOOK_TOKEN", "").strip()


def _request(method: str, path: str, payload: dict | None = None, timeout: int = 15) -> dict:
    key = api_key()
    if not key:
        raise BillingError(
            "O checkout não está configurado neste deploy. Defina ASAAS_API_KEY nas "
            "variáveis de ambiente do projeto e faça um novo deploy."
        )
    url = api_base() + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "access_token": key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "pipoca-e-play",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        detail = _asaas_error_detail(error)
        raise BillingError(f"O ASAAS recusou a solicitação (HTTP {error.code}). {detail}".strip()) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise BillingError("Não foi possível conectar ao ASAAS para processar o pagamento.") from error
    try:
        return json.loads(body or "{}")
    except json.JSONDecodeError as error:
        raise BillingError("O ASAAS devolveu uma resposta que não pôde ser interpretada.") from error


def _asaas_error_detail(error: urllib.error.HTTPError) -> str:
    """Mensagem do ASAAS repassada ao cliente — sem eco da chave de API."""
    try:
        payload = json.loads(error.read().decode("utf-8", errors="replace"))
    except (ValueError, AttributeError):
        return ""
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if isinstance(errors, list):
        messages = [str(item.get("description", "")).strip() for item in errors if isinstance(item, dict)]
        return " ".join(message for message in messages if message)[:300]
    return ""


def ensure_customer(email: str, name: str = "", cpf_cnpj: str = "", existing_id: str = "") -> str:
    """Reaproveita o cliente ASAAS já vinculado à conta; cria um novo se faltar."""
    if existing_id:
        return existing_id
    payload = {"name": (name or email.split("@", 1)[0]).strip()[:100], "email": email}
    digits = "".join(char for char in str(cpf_cnpj or "") if char.isdigit())
    if digits:
        payload["cpfCnpj"] = digits
    created = _request("POST", "/customers", payload)
    customer_id = str(created.get("id") or "")
    if not customer_id:
        raise BillingError("O ASAAS não devolveu o identificador do cliente.")
    return customer_id


def create_subscription(customer_id: str, plan: dict, email: str) -> dict:
    """Assinatura mensal recorrente no ASAAS, com a primeira cobrança para hoje."""
    today = datetime.now(BRAZIL_TZ).date()
    payload = {
        "customer": customer_id,
        "billingType": os.environ.get("ASAAS_BILLING_TYPE", "UNDEFINED").strip() or "UNDEFINED",
        "value": plan["price"],
        "nextDueDate": today.isoformat(),
        "cycle": "MONTHLY",
        "description": f"Pipoca & Play — plano {plan['name']}",
        "externalReference": f"pipoca-play:{email}:{plan['id']}",
    }
    return _request("POST", "/subscriptions", payload)


def subscription_payments(subscription_id: str) -> list[dict]:
    result = _request("GET", f"/subscriptions/{subscription_id}/payments")
    return [item for item in (result.get("data") or []) if isinstance(item, dict)]


def get_payment(payment_id: str) -> dict:
    return _request("GET", f"/payments/{payment_id}")


def cancel_subscription(subscription_id: str) -> dict:
    return _request("DELETE", f"/subscriptions/{subscription_id}")


# ---------------------------------------------------------------------------
# Estado da assinatura do usuário
# ---------------------------------------------------------------------------


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def cycle_bounds(confirmed_at: datetime | None = None) -> tuple[str, str]:
    """Início e fim do ciclo de 30 dias a partir da confirmação do pagamento."""
    start = confirmed_at or datetime.now(timezone.utc)
    end = start + timedelta(days=CYCLE_DAYS)
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def is_active(subscription: dict) -> bool:
    """Ativa é a assinatura paga cujo ciclo de 30 dias ainda não venceu."""
    if subscription.get("status") != "active":
        return False
    end = _parse_iso(subscription.get("cycle_end"))
    return bool(end and datetime.now(timezone.utc) < end)


def account_state(email: str) -> dict:
    """Retrato completo do acesso pago: plano, ciclo, créditos e marcações."""
    user = find_user(email)
    if not user:
        raise LookupError("Conta não encontrada.")
    subscription = subscription_of(user)
    today = brazil_today()
    credits = credits_of(user, today)
    active = is_active(subscription)
    plan_id = subscription.get("plan") or ""
    plan = PLANS.get(plan_id)
    limit = plan["daily_credits"] if plan else 0
    unlimited = bool(plan and plan["unlimited"])
    remaining = None if unlimited else max(0, (limit or 0) - credits["used"])
    return {
        "plan": plan_id,
        "plan_name": plan["name"] if plan else "",
        "plan_price": plan["price"] if plan else 0,
        "status": subscription.get("status", "none"),
        "active": active,
        "cycle_start": subscription.get("cycle_start"),
        "cycle_end": subscription.get("cycle_end"),
        "invoice_url": subscription.get("invoice_url") or "",
        "payment_confirmed": active,
        "unlimited": unlimited,
        "daily_credits": limit,
        "credits_used_today": credits["used"],
        "credits_remaining": remaining,
        "credits_date": today,
        "marks_count": len(marks_of(user)),
        "checkout_configured": is_configured(),
    }


def start_checkout(email: str, plan_id: str, name: str = "", cpf_cnpj: str = "") -> dict:
    """Cria (ou reaproveita) a assinatura no ASAAS e devolve a fatura para pagar."""
    plan = get_plan(plan_id)
    user = find_user(email)
    if not user:
        raise LookupError("Conta não encontrada.")
    stored = subscription_of(user)

    customer_id = ensure_customer(email, name, cpf_cnpj, stored.get("asaas_customer_id") or "")
    created = create_subscription(customer_id, plan, email)
    subscription_id = str(created.get("id") or "")
    if not subscription_id:
        raise BillingError("O ASAAS não devolveu o identificador da assinatura.")

    payments = subscription_payments(subscription_id)
    first = payments[0] if payments else {}
    save_subscription(
        email,
        {
            "plan": plan["id"],
            "status": "pending",
            "asaas_customer_id": customer_id,
            "asaas_subscription_id": subscription_id,
            "payment_id": str(first.get("id") or ""),
            "invoice_url": str(first.get("invoiceUrl") or created.get("invoiceUrl") or ""),
            "cycle_start": None,
            "cycle_end": None,
            "confirmed_at": None,
        },
    )
    return {
        "plan": plan["id"],
        "plan_name": plan["name"],
        "price": plan["price"],
        "subscription_id": subscription_id,
        "payment_id": str(first.get("id") or ""),
        "invoice_url": str(first.get("invoiceUrl") or created.get("invoiceUrl") or ""),
        "status": "pending",
    }


def _activate(email: str, payment: dict, plan_id: str) -> dict:
    confirmed = _parse_iso(payment.get("confirmedDate") or payment.get("paymentDate")) or datetime.now(timezone.utc)
    start, end = cycle_bounds(confirmed)
    return save_subscription(
        email,
        {
            "plan": plan_id,
            "status": "active",
            "payment_id": str(payment.get("id") or ""),
            "invoice_url": str(payment.get("invoiceUrl") or ""),
            "cycle_start": start,
            "cycle_end": end,
            "confirmed_at": confirmed.isoformat(timespec="seconds"),
        },
    )


def sync_subscription(email: str) -> dict:
    """Pergunta ao ASAAS se o pagamento entrou e atualiza a conta.

    Chamada sempre que o cliente abre a plataforma: é o caminho que libera o
    acesso mesmo quando o webhook não chega.
    """
    user = find_user(email)
    if not user:
        raise LookupError("Conta não encontrada.")
    subscription = subscription_of(user)
    subscription_id = subscription.get("asaas_subscription_id") or ""
    plan_id = subscription.get("plan") or ""
    if not subscription_id or not plan_id or not is_configured():
        return account_state(email)

    # Um ciclo pago que venceu volta a pendente até a próxima cobrança entrar.
    if subscription.get("status") == "active" and not is_active(subscription):
        save_subscription(email, {"status": "pending"})

    try:
        payments = subscription_payments(subscription_id)
    except BillingError:
        # Indisponibilidade do ASAAS não pode derrubar quem já pagou.
        return account_state(email)

    paid = [item for item in payments if str(item.get("status", "")).upper() in PAID_PAYMENT_STATUSES]
    if paid:
        latest = max(paid, key=lambda item: str(item.get("confirmedDate") or item.get("paymentDate") or ""))
        confirmed = _parse_iso(latest.get("confirmedDate") or latest.get("paymentDate"))
        current_end = _parse_iso(subscription.get("cycle_end"))
        # Só reescreve o ciclo quando o pagamento é mais novo que o já registrado.
        if not current_end or not confirmed or confirmed + timedelta(days=CYCLE_DAYS) > current_end:
            _activate(email, latest, plan_id)
        return account_state(email)

    pending = [item for item in payments if str(item.get("status", "")).upper() in PENDING_PAYMENT_STATUSES]
    if pending and not is_active(subscription_of(find_user(email))):
        first = pending[0]
        save_subscription(
            email,
            {
                "status": "pending",
                "payment_id": str(first.get("id") or ""),
                "invoice_url": str(first.get("invoiceUrl") or ""),
            },
        )
    return account_state(email)


def apply_webhook(event: dict) -> dict:
    """Processa um evento de pagamento do ASAAS e devolve o que foi feito."""
    payment = event.get("payment") if isinstance(event.get("payment"), dict) else {}
    reference = str(payment.get("externalReference") or "")
    email = ""
    plan_id = ""
    if reference.startswith("pipoca-play:"):
        parts = reference.split(":")
        if len(parts) >= 3:
            email, plan_id = parts[1], parts[2]

    subscription_id = str(payment.get("subscription") or "")
    if not email and subscription_id:
        email, plan_id = _account_by_subscription(subscription_id)
    if not email:
        return {"handled": False, "reason": "Pagamento sem conta correspondente."}

    user = find_user(email)
    if not user:
        return {"handled": False, "reason": "Conta não encontrada."}
    stored = subscription_of(user)
    plan_id = plan_id or stored.get("plan") or ""
    if plan_id not in PLANS:
        return {"handled": False, "reason": "Plano desconhecido."}

    status = str(payment.get("status", "")).upper()
    if status in PAID_PAYMENT_STATUSES:
        _activate(email, payment, plan_id)
        return {"handled": True, "email": email, "status": "active"}
    if status in {"OVERDUE"}:
        save_subscription(email, {"status": "overdue"})
        return {"handled": True, "email": email, "status": "overdue"}
    if status in {"REFUNDED", "CHARGEBACK_REQUESTED", "PAYMENT_DELETED", "DELETED"}:
        save_subscription(email, {"status": "canceled", "cycle_end": None})
        return {"handled": True, "email": email, "status": "canceled"}
    return {"handled": False, "reason": f"Evento ignorado ({status or 'sem status'})."}


def _account_by_subscription(subscription_id: str) -> tuple[str, str]:
    from user_store import load_users  # noqa: PLC0415 - evita ciclo na importação do módulo

    for user in load_users():
        stored = subscription_of(user)
        if stored.get("asaas_subscription_id") == subscription_id:
            return str(user.get("email", "")), str(stored.get("plan") or "")
    return "", ""


def webhook_token_matches(received: str | None) -> bool:
    """O ASAAS envia o token configurado no painel em `asaas-access-token`."""
    expected = webhook_token()
    if not expected:
        return False
    return hmac.compare_digest(str(received or ""), expected)


# ---------------------------------------------------------------------------
# Consumo de crédito
# ---------------------------------------------------------------------------


def authorize_search(email: str) -> dict:
    """Verifica pagamento e créditos, e debita um crédito da conta.

    Levanta `PaymentRequiredError` quando não há assinatura paga ativa e
    `CreditsExhaustedError` quando o limite diário do plano já foi usado.
    """
    user = find_user(email)
    if not user:
        raise LookupError("Conta não encontrada.")
    subscription = subscription_of(user)
    if not is_active(subscription):
        raise PaymentRequiredError(
            "Escolha um plano e conclua o pagamento para liberar as indicações."
            if subscription.get("status") in {"none", ""}
            else "Estamos aguardando a confirmação do seu pagamento para liberar as indicações."
        )
    plan_id = subscription.get("plan") or ""
    if plan_id not in PLANS:
        raise PaymentRequiredError("O plano da sua assinatura não é mais válido. Escolha um plano.")
    consume_credit(email, daily_limit(plan_id))
    return account_state(email)


def release_search(email: str) -> None:
    """Estorna o crédito quando a consulta falhou antes de entregar resultados."""
    try:
        refund_credit(email)
    except (LookupError, RuntimeError):
        pass


# ---------------------------------------------------------------------------
# Preferências enviadas ao modelo
# ---------------------------------------------------------------------------


PREFERENCE_LIMIT = 40


def preferences_for_prompt(email: str) -> dict:
    """Marcações do usuário no formato que o prompt consome.

    Uma obra marcada como "já assisti" não deve voltar; uma marcada com
    "não gostei" orienta o modelo a evitar títulos parecidos.
    """
    try:
        marks = list_marks(email)
    except (LookupError, RuntimeError):
        return {"watched": [], "liked": [], "disliked": []}
    watched, liked, disliked = [], [], []
    for mark in marks[:PREFERENCE_LIMIT]:
        label = str(mark.get("title_pt") or mark.get("title_original") or "").strip()
        if not label:
            continue
        year = mark.get("year") or 0
        if year:
            label = f"{label} ({year})"
        if mark.get("watched"):
            watched.append(label)
        if mark.get("opinion") == "liked":
            liked.append(label)
        elif mark.get("opinion") == "disliked":
            disliked.append(label)
    return {"watched": watched, "liked": liked, "disliked": disliked}


__all__ = [
    "BillingError",
    "CreditsExhaustedError",
    "PaymentRequiredError",
    "PLANS",
    "account_state",
    "apply_webhook",
    "authorize_search",
    "cancel_subscription",
    "daily_limit",
    "get_plan",
    "is_active",
    "is_configured",
    "plans_catalog",
    "preferences_for_prompt",
    "release_search",
    "start_checkout",
    "sync_subscription",
    "webhook_token_matches",
]
