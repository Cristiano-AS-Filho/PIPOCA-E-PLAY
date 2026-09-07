"""Fonte única de verdade dos planos de assinatura do Pipoca & Play.

Mantenha em sincronia com a vitrine em `public/index.html` (const PLANS) —
lá é só apresentação; aqui é o que o backend realmente cobra e libera.
1 crédito = 1 consulta (filme, série ou novela). `daily_credits: None`
significa consultas ilimitadas dentro do ciclo de 30 dias da assinatura.
"""

PLANS = {
    "silver": {"name": "Silver", "price": 15.00, "daily_credits": 2},
    "gold": {"name": "Gold", "price": 25.00, "daily_credits": 5},
    "diamante": {"name": "Diamante", "price": 30.00, "daily_credits": None},
}


def plan_exists(plan_id: str) -> bool:
    return plan_id in PLANS


def daily_credits_for(plan_id: str):
    plan = PLANS.get(plan_id)
    return plan["daily_credits"] if plan else 0
