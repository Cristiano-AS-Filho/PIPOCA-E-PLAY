"""Cliente mínimo para a API de assinaturas (Subscriptions) do ASAAS.

Usa apenas `urllib`, como o resto do backend, sem dependências novas.
A URL base muda conforme `ASAAS_ENV` (padrão: sandbox).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta


class AsaasError(RuntimeError):
    """Erro de comunicação ou resposta de erro da API do ASAAS."""


def _base_url() -> str:
    env = os.environ.get("ASAAS_ENV", "sandbox").strip().lower()
    return "https://api.asaas.com/v3" if env == "production" else "https://sandbox.asaas.com/api/v3"


def _api_key() -> str:
    return os.environ.get("ASAAS_API_KEY", "").strip()


def is_configured() -> bool:
    return bool(_api_key())


def next_due_date() -> str:
    """Data da primeira cobrança da assinatura: amanhã, no fuso UTC."""
    return (date.today() + timedelta(days=1)).isoformat()


def _request(method: str, path: str, payload: dict | None = None, params: dict | None = None) -> dict:
    if not _api_key():
        raise AsaasError("ASAAS_API_KEY não está configurada neste deploy.")
    url = _base_url() + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "access_token": _api_key(),
            "Content-Type": "application/json",
            "User-Agent": "PipocaPlay/1.0",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise AsaasError(f"O ASAAS respondeu HTTP {error.code}: {detail[:300]}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise AsaasError("Não foi possível conectar ao ASAAS.") from error


def find_customer_by_reference(external_reference: str) -> dict | None:
    result = _request("GET", "/customers", params={"externalReference": external_reference})
    items = result.get("data") or []
    return items[0] if items else None


def create_customer(name: str, cpf_cnpj: str, email: str, external_reference: str) -> dict:
    return _request(
        "POST",
        "/customers",
        {
            "name": name,
            "cpfCnpj": cpf_cnpj,
            "email": email,
            "externalReference": external_reference,
        },
    )


def get_or_create_customer(name: str, cpf_cnpj: str, email: str, external_reference: str) -> dict:
    existing = find_customer_by_reference(external_reference)
    if existing:
        return existing
    return create_customer(name, cpf_cnpj, email, external_reference)


def create_subscription(customer_id: str, plan: str, value: float, external_reference: str) -> dict:
    return _request(
        "POST",
        "/subscriptions",
        {
            "customer": customer_id,
            "billingType": "UNDEFINED",
            "value": value,
            "nextDueDate": next_due_date(),
            "cycle": "MONTHLY",
            "description": f"Pipoca & Play — plano {plan}",
            "externalReference": external_reference,
        },
    )


def list_subscription_payments(subscription_id: str) -> list[dict]:
    result = _request("GET", "/payments", params={"subscription": subscription_id})
    return result.get("data") or []


def cancel_subscription(subscription_id: str) -> dict:
    return _request("DELETE", f"/subscriptions/{subscription_id}")
