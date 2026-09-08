"""Integração com a API do ASAAS (checkout hospedado e assinaturas).

O backend cria o cliente e a assinatura mensal via API do ASAAS e redireciona
o cliente para a página de pagamento hospedada pelo próprio ASAAS
(``invoiceUrl``), que aceita cartão, PIX e boleto sem que dados de pagamento
passem pelo nosso servidor. A confirmação chega depois por um webhook público
validado por um token de assinatura configurável.
"""

from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from plans import PLAN_CATALOG


class AsaasError(RuntimeError):
    """Erro ao falar com a API do ASAAS."""


def _base_url() -> str:
    env = os.environ.get("ASAAS_ENV", "sandbox").strip().lower()
    return "https://api.asaas.com/v3" if env == "production" else "https://sandbox.asaas.com/api/v3"


def _api_key() -> str:
    return os.environ.get("ASAAS_API_KEY", "").strip()


def is_configured() -> bool:
    return bool(_api_key())


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _request(method: str, path: str, payload: dict | None = None, query: dict | None = None) -> dict:
    api_key = _api_key()
    if not api_key:
        raise AsaasError("ASAAS_API_KEY não foi configurada no servidor.")
    url = _base_url() + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "access_token": api_key,
            "Content-Type": "application/json",
            "User-Agent": "PipocaPlay/1.0",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise AsaasError(f"A API do ASAAS respondeu HTTP {error.code}: {detail[:300]}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise AsaasError("Não foi possível conectar à API do ASAAS.") from error
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except (TypeError, ValueError) as error:
        raise AsaasError("A API do ASAAS retornou uma resposta inválida.") from error


def find_customer_by_email(email: str) -> dict | None:
    result = _request("GET", "/customers", query={"email": email})
    items = result.get("data") or []
    return items[0] if items else None


def ensure_customer(email: str, name: str | None = None) -> dict:
    """Reaproveita o cliente ASAAS existente ou cria um novo pelo e-mail da conta."""
    existing = find_customer_by_email(email)
    if existing:
        return existing
    return _request("POST", "/customers", {"name": name or email, "email": email})


def create_subscription(customer_id: str, plan_id: str, external_reference: str) -> dict:
    plan = PLAN_CATALOG[plan_id]
    payload = {
        "customer": customer_id,
        "billingType": "UNDEFINED",
        "cycle": "MONTHLY",
        "value": round(plan["price_cents"] / 100, 2),
        "nextDueDate": _today_iso(),
        "description": f"Pipoca & Play - Plano {plan['name']}",
        "externalReference": external_reference,
    }
    return _request("POST", "/subscriptions", payload)


def get_first_payment_checkout_url(subscription_id: str) -> str | None:
    """Página de pagamento hospedada pelo ASAAS para a primeira cobrança da assinatura."""
    result = _request("GET", "/payments", query={"subscription": subscription_id, "limit": 1})
    items = result.get("data") or []
    return items[0].get("invoiceUrl") if items else None


def cancel_subscription(subscription_id: str) -> None:
    _request("DELETE", f"/subscriptions/{subscription_id}")


def verify_webhook_token(header_value: str | None) -> bool:
    """Compara o header ``asaas-access-token`` configurado no painel do ASAAS."""
    configured = os.environ.get("ASAAS_WEBHOOK_TOKEN", "").strip()
    if not configured or not header_value:
        return False
    return secrets.compare_digest(header_value.strip(), configured)
