"""Cliente HTTP mínimo para a API de cobrança do Asaas.

Sem dependências externas — usa urllib como o resto do backend. Dois pontos
que a API do Asaas faz diferente da maioria: autentica com o header
`access_token` puro (não é `Authorization: Bearer`), e produção/sandbox são
hosts diferentes.

Aviso de implementação: o formato exato do corpo de POST /v3/checkouts foi
montado a partir da documentação pública do Asaas (não foi possível buscar
a referência completa a partir deste ambiente). Os nomes de campo abaixo têm
confiança alta (billingTypes, chargeTypes, callback, items, customer,
subscription.cycle, externalReference), mas o primeiro teste real (mesmo que
com valor simbólico) é o que confirma o contrato antes de ligar em produção
de verdade.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

PRODUCTION_BASE_URL = "https://api.asaas.com/v3"
SANDBOX_BASE_URL = "https://api-sandbox.asaas.com/v3"


class AsaasError(RuntimeError):
    """Erro de comunicação ou validação retornado pelo Asaas."""


def _base_url() -> str:
    if os.environ.get("ASAAS_ENVIRONMENT", "production").strip().lower() == "sandbox":
        return SANDBOX_BASE_URL
    return PRODUCTION_BASE_URL


def _api_key() -> str:
    key = os.environ.get("ASAAS_API_KEY", "").strip()
    if not key:
        raise AsaasError("ASAAS_API_KEY não foi configurada no servidor.")
    return key


def is_configured() -> bool:
    return bool(os.environ.get("ASAAS_API_KEY", "").strip())


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    url = _base_url() + path
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
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise AsaasError(f"Asaas respondeu HTTP {error.code}: {details}") from error
    except urllib.error.URLError as error:
        raise AsaasError("Não foi possível conectar ao Asaas.") from error


def find_customer_by_email(email: str) -> dict | None:
    query = urllib.parse.urlencode({"email": email})
    result = _request("GET", f"/customers?{query}")
    data = result.get("data") or []
    return data[0] if data else None


def create_customer(name: str, email: str, external_reference: str) -> dict:
    payload = {"name": name, "email": email, "externalReference": external_reference}
    return _request("POST", "/customers", payload)


def get_or_create_customer(name: str, email: str, external_reference: str) -> dict:
    existing = find_customer_by_email(email)
    if existing:
        return existing
    return create_customer(name, email, external_reference)


def create_subscription_checkout(
    customer_id: str,
    plan_name: str,
    price: float,
    external_reference: str,
    success_url: str,
    cancel_url: str,
    billing_types=("PIX", "CREDIT_CARD"),
) -> dict:
    """Cria uma sessão de checkout hospedada pelo Asaas para uma assinatura mensal.

    O usuário é redirecionado pra `link`/`checkoutUrl` retornado; o Asaas
    coleta a forma de pagamento e, a partir daí, gera a assinatura e as
    cobranças recorrentes — nós só reagimos aos eventos via webhook.
    """
    payload = {
        "billingTypes": list(billing_types),
        "chargeTypes": ["RECURRENT"],
        "callback": {
            "successUrl": success_url,
            "cancelUrl": cancel_url,
            "autoRedirect": True,
        },
        "items": [
            {
                "name": f"Pipoca & Play — Plano {plan_name}",
                "description": f"Assinatura mensal — plano {plan_name}",
                "quantity": 1,
                "value": price,
            }
        ],
        "customer": customer_id,
        "subscription": {"cycle": "MONTHLY"},
        "externalReference": external_reference,
    }
    return _request("POST", "/checkouts", payload)


def checkout_redirect_url(checkout_response: dict) -> str:
    """O nome do campo com a URL hospedada varia entre versões da API do
    Asaas (`link`, `url` ou `checkoutUrl` já foram observados por
    integradores) — tenta os candidatos conhecidos em vez de travar em um só.
    """
    for key in ("link", "url", "checkoutUrl", "invoiceUrl"):
        value = checkout_response.get(key)
        if value:
            return value
    raise AsaasError("O Asaas não retornou uma URL de checkout reconhecível.")
