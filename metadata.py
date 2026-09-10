"""Resolução do pôster de cada indicação.

O projeto não tem (nem terá) uma chave de catálogo como o TMDB: o pôster
nunca fica vazio, mas sem depender de nenhuma chave além da já obrigatória
da OpenAI. A ordem de tentativa é sempre esta:

1. O link que o próprio motor (ChatGPT) indicar na resposta, validado só na
   forma (precisa parecer de fato uma URL de imagem) — o modelo pode errar
   o link, então o front-end ainda testa se a imagem carrega antes de
   exibi-la.
2. Uma imagem real e gratuita da Wikipedia/Wikimedia, buscada pelo título
   (API pública, sem chave).
3. Como último recurso, uma capa ilustrativa gerada por IA, na mesma conta
   da OpenAI já usada pelo motor. Nunca é a arte oficial do título, por isso
   `poster_source` chega como "generated" para a tela rotular como tal.

Disponibilidade em streaming não tem mais uma fonte externa que a confirme:
o campo `where_to_watch` que o motor preencheu é mantido como está, apenas
marcado como não confirmado.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

POSTER_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
MAX_DATA_URI_LENGTH = 6_000_000  # ~4,5 MB de imagem em base64
MAX_HTTP_URL_LENGTH = 500


def _looks_like_image_url(value) -> str:
    """Só aceita uma URL com forma plausível de imagem; o resto vira vazio."""
    url = str(value or "").strip()
    if url.startswith("data:image/"):
        return url if len(url) <= MAX_DATA_URI_LENGTH else ""
    if url.startswith(("http://", "https://")) and len(url) <= MAX_HTTP_URL_LENGTH:
        path = url.split("?", 1)[0].split("#", 1)[0].lower()
        if path.endswith(POSTER_EXTENSIONS):
            return url
    return ""


def _wikipedia_summary_image(title: str) -> str:
    if not title:
        return ""
    url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title.replace(" ", "_"))
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "PipocaPlay/1.0 (busca de pôster)"}
    )
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return ""
    if not isinstance(data, dict) or data.get("type") == "disambiguation":
        return ""
    thumbnail = (data.get("thumbnail") or {}).get("source", "")
    original = (data.get("originalimage") or {}).get("source", "")
    return _looks_like_image_url(thumbnail) or _looks_like_image_url(original)


def _wikipedia_poster(title_original: str, title_pt: str, year, content_type: str) -> str:
    """Tenta achar uma imagem real e gratuita na Wikipedia para o título."""
    is_series = str(content_type).lower().startswith("seri")
    year = int(year or 0)
    candidates = []
    for base in (title_original, title_pt):
        base = str(base or "").strip()
        if not base:
            continue
        if is_series:
            candidates.append(f"{base} (TV series)")
        else:
            if year:
                candidates.append(f"{base} ({year} film)")
            candidates.append(f"{base} (film)")
        candidates.append(base)
    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        image = _wikipedia_summary_image(candidate)
        if image:
            return image
    return ""


def _generate_poster_image(title: str, year, genres, content_type: str) -> str:
    """Último recurso: uma capa ilustrativa gerada pela mesma conta da OpenAI
    já usada pelo motor de recomendação. Nunca é a arte oficial do título."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key or not title:
        return ""
    kind = "série de TV" if str(content_type).lower().startswith("seri") else "filme"
    genre_hint = ", ".join([str(g) for g in (genres or []) if g][:2]) or "drama"
    year_hint = f" ({int(year)})" if year else ""
    prompt = (
        f'Arte de capa ilustrativa e original, estilo pôster de cinema, inspirada no clima '
        f'do {kind} "{title}"{year_hint}, gênero {genre_hint}. Composição vertical, cores '
        "fortes, cena atmosférica. Não inclua nenhum texto, título, letra ou logotipo na imagem "
        "— represente o clima da obra, não seus personagens ou atores reais."
    )
    payload = {"model": "gpt-image-1", "prompt": prompt, "size": "1024x1536", "quality": "low", "n": 1}
    request = urllib.request.Request(
        "https://api.openai.com/v1/images/generations",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=55) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception:
        return ""
    items = result.get("data") or []
    b64 = items[0].get("b64_json", "") if items else ""
    return f"data:image/png;base64,{b64}" if b64 else ""


def resolve_poster(recommendation: dict) -> tuple[str, str]:
    """Devolve ``(poster_url, poster_source)``, tentando cada fonte na ordem
    do módulo até achar uma imagem — nunca fica vazio quando a OpenAI está
    configurada (o requisito mínimo do resto da aplicação)."""
    candidate = _looks_like_image_url(recommendation.get("poster_url"))
    if candidate:
        return candidate, "model"

    wiki = _wikipedia_poster(
        recommendation.get("title_original", ""),
        recommendation.get("title_pt", ""),
        recommendation.get("year", 0),
        recommendation.get("content_type", "filme"),
    )
    if wiki:
        return wiki, "wikipedia"

    generated = _generate_poster_image(
        recommendation.get("title_pt") or recommendation.get("title_original", ""),
        recommendation.get("year", 0),
        recommendation.get("genres"),
        recommendation.get("content_type", "filme"),
    )
    return (generated, "generated") if generated else ("", "")


def enrich_result(result: dict) -> dict:
    """Resolve o pôster de cada indicação. Sem catálogo externo (TMDB foi
    removido do projeto), o restante dos campos vem inteiramente do motor —
    disponibilidade nunca é tratada como confirmada por uma fonte externa."""
    for recommendation in result.get("recommendations", []):
        if not isinstance(recommendation.get("ratings"), dict):
            recommendation["ratings"] = {}
        if not isinstance(recommendation.get("where_to_watch"), list):
            recommendation["where_to_watch"] = []

        poster_url, poster_source = resolve_poster(recommendation)
        recommendation["poster_url"] = poster_url
        recommendation["poster_source"] = poster_source
        recommendation["availability_verified"] = False
        recommendation["availability_note"] = "Disponibilidade não confirmada no momento."
    return result
