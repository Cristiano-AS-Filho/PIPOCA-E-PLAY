"""Integração de cobrança com a Asaas.

O fluxo é: a plataforma cria (ou reaproveita) um cliente na Asaas, abre uma
assinatura mensal do plano escolhido e devolve a URL da fatura. O acesso só é
liberado quando a Asaas confirma o pagamento — pelo webhook, ou pela consulta
de conferência feita em ``/api/billing/status``.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from plans import get_plan

SANDBOX_URL = "https://api-sandbox.asaas.com/v3"
PRODUCTION_URL = "https://api.asaas.com/v3"

# Eventos que liberam o acesso e eventos que suspendem o acesso.
PAID_EVENTS = {"PAYMENT_CONFIRMED", "PAYMENT_RECEIVED", "PAYMENT_APPROVED_BY_RISK_ANALYSIS"}
SUSPEND_EVENTS = {
    "PAYMENT_OVERDUE",
    "PAYMENT_DELETED",
    "PAYMENT_REFUNDED",
    "PAYMENT_CHARGEBACK_REQUESTED",
    "PAYMENT_CHARGEBACK_DISPUTE",
    "PAYMENT_REPROVED_BY_RISK_ANALYSIS",
    "SUBSCRIPTION_DELETED",
}
PAID_PAYMENT_STATUSES = {"CONFIRMED", "RECEIVED", "RECEIVED_IN_CASH"}


class BillingError(RuntimeError):
    """Falha ao falar com a Asaas ou configuração ausente."""


def api_key() -> str:
    return os.environ.get("ASAAS_API_KEY", "").strip()


def webhook_token() -> str:
    return os.environ.get("ASAAS_WEBHOOK_TOKEN", "").strip()


def is_sandbox() -> bool:
    configured = os.environ.get("ASAAS_ENV", "").strip().lower()
    if configured in {"sandbox", "homolog", "homologacao", "hml"}:
        return True
    if configured in {"production", "prod", "producao"}:
        return False
    # As chaves de homologação da Asaas carregam "hmlg" no corpo do token.
    return "hmlg" in api_key().lower()


def base_url() -> str:
    return SANDBOX_URL if is_sandbox() else PRODUCTION_URL


def is_configured() -> bool:
    return bool(api_key())


def diagnostics() -> dict:
    """Resumo sem segredos, usado pelo /api/health e pelo painel admin."""
    return {
        "configured": is_configured(),
        "environment": "sandbox" if is_sandbox() else "production",
        "webhook_token_configured": bool(webhook_token()),
        "webhook_path": "/api/billing/webhook",
    }


# ---------------------------------------------------------------------------
# Cliente HTTP
# ---------------------------------------------------------------------------


def _request(method: str, path: str, payload: dict | None = None, params: dict | None = None) -> dict:
    key = api_key()
    if not key:
        raise BillingError(
            "O checkout não está configurado neste deploy. Defina ASAAS_API_KEY nas "
            "variáveis de ambiente do projeto e faça um novo deploy."
        )
    url = base_url() + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "access_token": key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "PipocaPlay/1.0",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        raise BillingError(_describe_http_error(error)) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise BillingError("Não foi possível falar com a Asaas agora. Tente novamente em instantes.") from error
    if not body:
        return {}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as error:
        raise BillingError("A Asaas devolveu uma resposta inesperada.") from error
    return parsed if isinstance(parsed, dict) else {"data": parsed}


def _describe_http_error(error: urllib.error.HTTPError) -> str:
    """Traz a mensagem da Asaas para a tela, sem vazar credenciais."""
    raw = error.read().decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw)
        errors = parsed.get("errors") if isinstance(parsed, dict) else None
        if isinstance(errors, list) and errors:
            messages = [str(item.get("description", "")).strip() for item in errors if isinstance(item, dict)]
            detail = " ".join(message for message in messages if message)
            if detail:
                return f"A Asaas recusou a cobrança: {detail}"
    except (json.JSONDecodeError, AttributeError):
        pass
    if error.code in {401, 403}:
        return "A chave da Asaas foi recusada (HTTP %d). Confira ASAAS_API_KEY e o ambiente configurado." % error.code
    return f"A Asaas respondeu com erro HTTP {error.code}."


# ---------------------------------------------------------------------------
# CPF / CNPJ
# ---------------------------------------------------------------------------


def only_digits(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _cpf_is_valid(cpf: str) -> bool:
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    for size in (9, 10):
        total = sum(int(cpf[index]) * (size + 1 - index) for index in range(size))
        digit = (total * 10) % 11 % 10
        if digit != int(cpf[size]):
            return False
    return True


def _cnpj_is_valid(cnpj: str) -> bool:
    if len(cnpj) != 14 or cnpj == cnpj[0] * 14:
        return False
    for size in (12, 13):
        weights = list(range(size - 7, 1, -1)) + list(range(9, 1, -1))
        total = sum(int(digit) * weight for digit, weight in zip(cnpj, weights))
        remainder = total % 11
        digit = 0 if remainder < 2 else 11 - remainder
        if digit != int(cnpj[size]):
            return False
    return True


def validate_document(value: str) -> str:
    document = only_digits(value)
    if len(document) == 11 and _cpf_is_valid(document):
        return document
    if len(document) == 14 and _cnpj_is_valid(document):
        return document
    raise ValueError("Informe um CPF ou CNPJ válido para emitir a cobrança.")


def validate_name(value: str) -> str:
    name = " ".join(str(value or "").split())
    if len(name) < 3 or len(name) > 100:
        raise ValueError("Informe o seu nome completo, como consta no CPF.")
    return name


# ---------------------------------------------------------------------------
# Clientes e assinaturas
# ---------------------------------------------------------------------------


def find_customer(email: str, document: str = "") -> dict | None:
    for params in ({"cpfCnpj": document} if document else None, {"email": email}):
        if not params:
            continue
        result = _request("GET", "/customers", params={**params, "limit": 1})
        data = result.get("data") or []
        if data:
            return data[0]
    return None


def ensure_customer(email: str, name: str, document: str, external_reference: str = "") -> str:
    existing = find_customer(email, document)
    if existing and existing.get("id"):
        return str(existing["id"])
    created = _request(
        "POST",
        "/customers",
        {
            "name": name,
            "email": email,
            "cpfCnpj": document,
            "externalReference": external_reference,
            "notificationDisabled": False,
        },
    )
    customer_id = created.get("id")
    if not customer_id:
        raise BillingError("A Asaas não devolveu o identificador do cliente.")
    return str(customer_id)


def _today_in_brazil() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=3)


def create_subscription(customer_id: str, plan_id: str, external_reference: str) -> dict:
    plan = get_plan(plan_id)
    if not plan:
        raise ValueError("Plano inválido.")
    due_date = _today_in_brazil().strftime("%Y-%m-%d")
    subscription = _request(
        "POST",
        "/subscriptions",
        {
            "customer": customer_id,
            "billingType": "UNDEFINED",
            "value": float(plan["price"]),
            "nextDueDate": due_date,
            "cycle": "MONTHLY",
            "description": f"Pipoca & Play — plano {plan['name']}",
            "externalReference": external_reference,
        },
    )
    subscription_id = subscription.get("id")
    if not subscription_id:
        raise BillingError("A Asaas não devolveu o identificador da assinatura.")
    return subscription


def subscription_payments(subscription_id: str) -> list[dict]:
    result = _request("GET", f"/subscriptions/{subscription_id}/payments", params={"limit": 10})
    data = result.get("data")
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def checkout_url(subscription_id: str) -> str:
    """URL da primeira fatura em aberto: é a tela de pagamento do cliente."""
    for payment in subscription_payments(subscription_id):
        if payment.get("status") in {"PENDING", "AWAITING_RISK_ANALYSIS", "OVERDUE"}:
            url = payment.get("invoiceUrl") or payment.get("bankSlipUrl")
            if url:
                return str(url)
    for payment in subscription_payments(subscription_id):
        if payment.get("invoiceUrl"):
            return str(payment["invoiceUrl"])
    return ""


def subscription_is_paid(subscription_id: str) -> tuple[bool, str]:
    """Confere na Asaas se alguma fatura da assinatura já foi paga."""
    for payment in subscription_payments(subscription_id):
        if str(payment.get("status", "")).upper() in PAID_PAYMENT_STATUSES:
            return True, str(payment.get("id", ""))
    return False, ""


def cancel_subscription(subscription_id: str) -> None:
    _request("DELETE", f"/subscriptions/{subscription_id}")


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


def webhook_token_is_valid(received: str) -> bool:
    expected = webhook_token()
    if not expected:
        # Sem token configurado, a validação fica a cargo da própria Asaas.
        return True
    return bool(received) and _constant_time_equals(received.strip(), expected)


def _constant_time_equals(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    result = 0
    for a, b in zip(left, right):
        result |= ord(a) ^ ord(b)
    return result == 0


def read_webhook_event(body: dict) -> dict:
    """Normaliza o corpo do webhook em: evento, ids e se libera ou suspende."""
    event = str(body.get("event", "")).upper()
    payment = body.get("payment") if isinstance(body.get("payment"), dict) else {}
    subscription = body.get("subscription") if isinstance(body.get("subscription"), dict) else {}
    status = str(payment.get("status", "")).upper()
    return {
        "event": event,
        "payment_id": str(payment.get("id", "")),
        "customer_id": str(payment.get("customer", "") or subscription.get("customer", "")),
        "subscription_id": str(payment.get("subscription", "") or subscription.get("id", "")),
        "external_reference": str(
            payment.get("externalReference", "") or subscription.get("externalReference", "") or ""
        ),
        "payment_status": status,
        "grants_access": event in PAID_EVENTS or (event.startswith("PAYMENT_") and status in PAID_PAYMENT_STATUSES),
        "suspends_access": event in SUSPEND_EVENTS,
    }
