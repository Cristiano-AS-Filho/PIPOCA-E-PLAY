"""Limitador de taxa para a rota de recomendação.

Cada busca dispara uma chamada paga à OpenAI, então essa rota precisa de
limite tanto no servidor local quanto na função serverless da Vercel. Uma
função serverless roda em um processo novo a cada invocação (ou é reciclada
sem aviso), então um contador em memória não protege nada em produção.

Por isso este módulo usa o mesmo Redis REST já configurado para as contas
(KV_REST_API_URL/TOKEN ou UPSTASH_REDIS_REST_URL/TOKEN) quando disponível —
o contador fica compartilhado entre invocações e instâncias. Sem essas
variáveis configuradas, cai para um contador em memória por processo: ainda
funciona no servidor local (processo único de longa duração) e serve de rede
de segurança mínima em produção até o Redis ser configurado.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque


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


def _kv_command(command: str, *args: str):
    credentials = _kv_credentials()
    if not credentials:
        return None
    url, token = credentials
    payload = json.dumps([command, *args], ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        body = json.loads(response.read().decode("utf-8"))
    if isinstance(body, dict) and body.get("error"):
        raise RuntimeError("O contador de limite retornou um erro.")
    return body.get("result") if isinstance(body, dict) else None


_MEMORY_BUCKETS: dict[str, deque] = defaultdict(deque)


def _check_memory(identifier: str, max_requests: int, window_seconds: int) -> bool:
    now = time.monotonic()
    bucket = _MEMORY_BUCKETS[identifier]
    while bucket and now - bucket[0] > window_seconds:
        bucket.popleft()
    if len(bucket) >= max_requests:
        return False
    bucket.append(now)
    return True


def check_rate_limit(identifier: str, max_requests: int = 12, window_seconds: int = 60) -> bool:
    """Retorna True se a requisição pode prosseguir, False se o limite foi excedido."""
    if not _kv_credentials():
        return _check_memory(identifier, max_requests, window_seconds)

    bucket_key = f"ratelimit:{identifier}:{int(time.time() // window_seconds)}"
    try:
        count = _kv_command("INCR", bucket_key)
        if count == 1:
            _kv_command("EXPIRE", bucket_key, str(window_seconds))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError, ValueError, OSError):
        return _check_memory(identifier, max_requests, window_seconds)

    if not isinstance(count, int):
        return _check_memory(identifier, max_requests, window_seconds)
    return count <= max_requests
