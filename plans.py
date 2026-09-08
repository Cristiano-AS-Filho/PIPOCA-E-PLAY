"""Catálogo de planos de assinatura do Pipoca & Play.

1 crédito = 1 consulta de filme, série ou novela. Os créditos diários dos
planos Silver e Gold não acumulam: viram à meia-noite (horário de Brasília).
O plano Diamante libera consultas ilimitadas dentro do ciclo de 30 dias da
assinatura.
"""

from __future__ import annotations

CYCLE_DAYS = 30

PLAN_CATALOG = {
    "silver": {
        "id": "silver",
        "name": "Silver",
        "price_cents": 1_500,
        "credits_per_day": 2,
        "description": "2 recomendações por dia.",
    },
    "gold": {
        "id": "gold",
        "name": "Gold",
        "price_cents": 2_500,
        "credits_per_day": 5,
        "description": "5 recomendações por dia.",
    },
    "diamond": {
        "id": "diamond",
        "name": "Diamante",
        "price_cents": 3_000,
        "credits_per_day": None,
        "description": "Recomendações ilimitadas durante os 30 dias da assinatura.",
    },
}

VALID_PLAN_IDS = tuple(PLAN_CATALOG.keys())


def is_valid_plan(plan_id: str) -> bool:
    return plan_id in PLAN_CATALOG


def _price_display(price_cents: int) -> str:
    return "R$ " + f"{price_cents / 100:.2f}".replace(".", ",")


def public_plans() -> list[dict]:
    """Formato exposto ao frontend e à rota pública de planos."""
    return [
        {
            "id": plan["id"],
            "name": plan["name"],
            "price_cents": plan["price_cents"],
            "price_display": _price_display(plan["price_cents"]),
            "credits_per_day": plan["credits_per_day"],
            "unlimited": plan["credits_per_day"] is None,
            "description": plan["description"],
            "cycle_days": CYCLE_DAYS,
        }
        for plan in PLAN_CATALOG.values()
    ]
