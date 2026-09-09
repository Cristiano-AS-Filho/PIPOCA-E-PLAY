"""Catálogo de planos de assinatura do Pipoca & Play.

Um crédito equivale a uma consulta (filme, série ou novela). Os planos são
mensais e o ciclo de cobrança é de 30 dias, cobrado pela Asaas.
"""

from __future__ import annotations

UNLIMITED = -1

PLANS = {
    "silver": {
        "id": "silver",
        "name": "Silver",
        "price": 10.00,
        "daily_credits": 2,
        "cycle_days": 30,
        "headline": "2 indicações por dia",
        "description": "2 créditos diários renovados todo dia à meia-noite.",
        "perks": [
            "2 consultas por dia",
            "Histórico de marcações ilimitado",
            "Indicações que aprendem com o que você marca",
        ],
    },
    "gold": {
        "id": "gold",
        "name": "Gold",
        "price": 15.00,
        "daily_credits": 5,
        "cycle_days": 30,
        "headline": "5 indicações por dia",
        "description": "5 créditos diários renovados todo dia à meia-noite.",
        "perks": [
            "5 consultas por dia",
            "Histórico de marcações ilimitado",
            "Indicações que aprendem com o que você marca",
        ],
    },
    "diamante": {
        "id": "diamante",
        "name": "Diamante",
        "price": 20.00,
        "daily_credits": UNLIMITED,
        "cycle_days": 30,
        "headline": "Indicações ilimitadas",
        "description": "Consultas ilimitadas durante todo o ciclo de 30 dias.",
        "perks": [
            "Consultas ilimitadas",
            "Histórico de marcações ilimitado",
            "Indicações que aprendem com o que você marca",
        ],
    },
}

PLAN_ORDER = ("silver", "gold", "diamante")


def get_plan(plan_id: str) -> dict | None:
    return PLANS.get(str(plan_id or "").strip().lower())


def public_plans() -> list[dict]:
    """Lista ordenada para a vitrine de planos do front-end."""
    return [
        {
            "id": PLANS[plan_id]["id"],
            "name": PLANS[plan_id]["name"],
            "price": PLANS[plan_id]["price"],
            "price_label": format_price(PLANS[plan_id]["price"]),
            "daily_credits": PLANS[plan_id]["daily_credits"],
            "unlimited": PLANS[plan_id]["daily_credits"] == UNLIMITED,
            "cycle_days": PLANS[plan_id]["cycle_days"],
            "headline": PLANS[plan_id]["headline"],
            "description": PLANS[plan_id]["description"],
            "perks": list(PLANS[plan_id]["perks"]),
        }
        for plan_id in PLAN_ORDER
    ]


def format_price(value: float) -> str:
    return "R$ " + f"{float(value):.2f}".replace(".", ",")


def daily_credits(plan_id: str) -> int:
    plan = get_plan(plan_id)
    return int(plan["daily_credits"]) if plan else 0


def is_unlimited(plan_id: str) -> bool:
    return daily_credits(plan_id) == UNLIMITED
