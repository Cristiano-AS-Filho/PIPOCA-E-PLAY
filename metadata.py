"""Resolução do pôster de cada indicação.

O projeto não tem (nem terá) uma chave de catálogo como o TMDB. O prompt do
motor já obriga cada indicação a trazer um ``poster_url``, então **a resposta
do próprio motor é o ponto de coleta principal** — as outras fontes só existem
para que o cartão nunca fique sem imagem. A ordem é sempre esta:

1. O link que o próprio motor (ChatGPT) devolveu, normalizado (``http://`` vira
   ``https://``, senão o navegador bloquearia a imagem como conteúdo misto num
   deploy servido por HTTPS) e **confirmado aqui no servidor**: uma requisição
   mínima checa se o endereço responde mesmo com uma imagem. Sem essa
   confirmação um link inventado vencia as fontes que funcionam — a coleta
   parava nele e o cartão ficava sem pôster nenhum.
2. Uma imagem real e gratuita da Wikipedia/Wikimedia, buscada pelo título (API
   pública, sem chave), primeiro em português e depois em inglês.
3. Como último recurso, uma capa ilustrativa gerada por IA, na mesma conta da
   OpenAI já usada pelo motor. Nunca é a arte oficial do título, por isso
   `poster_source` chega como "generated" para a tela rotular como tal.

As três indicações são resolvidas em paralelo e sob um prazo comum: a coleta do
pôster nunca pode estourar o tempo da função serverless e derrubar a
recomendação que o usuário já pagou.

Disponibilidade em streaming não tem mais uma fonte externa que a confirme:
o campo `where_to_watch` que o motor preencheu é mantido como está, apenas
marcado como não confirmado.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

POSTER_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif")

# Hospedeiros que só servem imagem: o link vale mesmo sem extensão no caminho
# (é comum o motor citar um endereço com parâmetros de redimensionamento).
IMAGE_HOSTS = ("tmdb.org", "themoviedb.org", "wikimedia.org", "wikipedia.org", "media-amazon.com")

MAX_DATA_URI_LENGTH = 6_000_000  # ~4,5 MB de imagem em base64
MAX_HTTP_URL_LENGTH = 500

USER_AGENT = "PipocaPlay/1.0 (busca de pôster)"

# Prazos de cada etapa. São tetos: o prazo comum da requisição (``deadline``)
# encurta qualquer um deles quando o tempo restante é menor.
POSTER_PROBE_TIMEOUT = 5
WIKIPEDIA_TIMEOUT = 5
GENERATION_TIMEOUT = 45
MAX_WIKIPEDIA_CANDIDATES = 6

# Usado quando ninguém informa um prazo (servidor local, testes, chamadas
# diretas): sozinho, o módulo se dá esta janela para resolver os pôsteres.
POSTER_TIME_BUDGET_SECONDS = 30


def _time_left(deadline) -> float:
    """Segundos que ainda restam do prazo comum da requisição."""
    if deadline is None:
        return float(POSTER_PROBE_TIMEOUT + WIKIPEDIA_TIMEOUT + GENERATION_TIMEOUT)
    return max(0.0, deadline - time.monotonic())


def _budget(deadline, ceiling: float) -> float:
    """Teto da etapa, encurtado pelo que sobrou do prazo. 0 = não dá tempo."""
    remaining = _time_left(deadline)
    return min(float(ceiling), remaining) if remaining > 1 else 0.0


def _is_public_host(host: str) -> bool:
    """Barra endereços internos: a URL vem do motor, não de uma fonte confiável."""
    host = host.split("@")[-1].split(":")[0].strip("[]").lower()
    if not host or host in {"localhost", "::1"} or host.endswith((".local", ".internal")):
        return False
    if host.startswith(("127.", "10.", "192.168.", "169.254.", "0.")):
        return False
    if host.startswith("172."):
        second = host.split(".")[1] if host.count(".") >= 1 else ""
        if second.isdigit() and 16 <= int(second) <= 31:
            return False
    return True


def _looks_like_image_url(value) -> str:
    """Normaliza o link do motor; devolve vazio quando não tem forma de imagem."""
    url = str(value or "").strip()
    if url.startswith("data:image/"):
        return url if len(url) <= MAX_DATA_URI_LENGTH else ""
    if url.startswith("http://"):
        # A aplicação é servida por HTTPS: uma imagem em http:// é bloqueada
        # pelo navegador como conteúdo misto e nunca chegaria a aparecer.
        url = "https://" + url[len("http://") :]
    if not url.startswith("https://") or len(url) > MAX_HTTP_URL_LENGTH:
        return ""
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    host = path[len("https://") :].split("/", 1)[0]
    if not _is_public_host(host):
        return ""
    if path.endswith(POSTER_EXTENSIONS):
        return url
    bare = host.split(":")[0]
    if any(bare == known or bare.endswith("." + known) for known in IMAGE_HOSTS):
        return url
    return ""


def _image_responds(url: str, timeout: float) -> bool:
    """Confirma que o link devolve mesmo uma imagem, sem baixar o arquivo.

    Pede só o primeiro byte (``Range``) e olha o tipo de conteúdo. O corpo da
    resposta nunca é lido nem repassado: daqui só sai um sim ou não.
    """
    if timeout <= 0:
        return False
    request = urllib.request.Request(
        url,
        headers={"Accept": "image/*", "Range": "bytes=0-0", "User-Agent": USER_AGENT},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status not in (200, 206):
                return False
            return response.headers.get("Content-Type", "").split(";")[0].strip().lower().startswith("image/")
    except Exception:
        return False


def _model_poster(recommendation: dict, deadline=None) -> str:
    """1ª fonte: o ``poster_url`` que o motor devolveu, já confirmado."""
    candidate = _looks_like_image_url(recommendation.get("poster_url"))
    if not candidate:
        return ""
    if candidate.startswith("data:image/"):
        # A imagem veio embutida na própria resposta: não há o que confirmar.
        return candidate
    return candidate if _image_responds(candidate, _budget(deadline, POSTER_PROBE_TIMEOUT)) else ""


def _wikipedia_summary_image(title: str, language: str = "en", timeout: float = WIKIPEDIA_TIMEOUT) -> str:
    if not title or timeout <= 0:
        return ""
    url = (
        f"https://{language}.wikipedia.org/api/rest_v1/page/summary/"
        + urllib.parse.quote(title.replace(" ", "_"))
    )
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return ""
    if not isinstance(data, dict) or data.get("type") == "disambiguation":
        return ""
    thumbnail = (data.get("thumbnail") or {}).get("source", "")
    original = (data.get("originalimage") or {}).get("source", "")
    return _looks_like_image_url(thumbnail) or _looks_like_image_url(original)


def _wikipedia_candidates(title_original: str, title_pt: str, year, content_type: str):
    """Pares ``(idioma, verbete)`` a tentar, do mais provável ao mais genérico.

    O público é brasileiro: o título em português vai primeiro, na Wikipedia em
    português, onde costuma estar a capa do lançamento nacional.
    """
    is_series = str(content_type).lower().startswith("seri")
    year = int(year or 0)
    original = str(title_original or "").strip()
    pt = str(title_pt or "").strip()
    candidates = []
    if pt:
        if is_series:
            candidates.append(("pt", f"{pt} (série de televisão)"))
        else:
            if year:
                candidates.append(("pt", f"{pt} (filme de {year})"))
            candidates.append(("pt", f"{pt} (filme)"))
        candidates.append(("pt", pt))
    if original:
        if is_series:
            candidates.append(("en", f"{original} (TV series)"))
        else:
            if year:
                candidates.append(("en", f"{original} ({year} film)"))
            candidates.append(("en", f"{original} (film)"))
        candidates.append(("en", original))
    ordered = []
    for candidate in candidates:
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered[:MAX_WIKIPEDIA_CANDIDATES]


def _wikipedia_poster(title_original: str, title_pt: str, year, content_type: str, deadline=None) -> str:
    """2ª fonte: uma imagem real e gratuita na Wikipedia para o título."""
    for language, candidate in _wikipedia_candidates(title_original, title_pt, year, content_type):
        timeout = _budget(deadline, WIKIPEDIA_TIMEOUT)
        if timeout <= 0:
            return ""
        image = _wikipedia_summary_image(candidate, language, timeout)
        if image:
            return image
    return ""


def _generate_poster_image(title: str, year, genres, content_type: str, deadline=None) -> str:
    """3ª fonte: uma capa ilustrativa gerada pela mesma conta da OpenAI já usada
    pelo motor de recomendação. Nunca é a arte oficial do título."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    timeout = _budget(deadline, GENERATION_TIMEOUT)
    if not api_key or not title or timeout <= 0:
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
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception:
        return ""
    items = result.get("data") or []
    b64 = items[0].get("b64_json", "") if items else ""
    return f"data:image/png;base64,{b64}" if b64 else ""


def resolve_poster(recommendation: dict, deadline=None) -> tuple[str, str]:
    """Devolve ``(poster_url, poster_source)``, tentando cada fonte na ordem do
    módulo até achar uma imagem que responda de verdade — nunca fica vazio
    quando a OpenAI está configurada e o prazo permite."""
    from_model = _model_poster(recommendation, deadline)
    if from_model:
        return from_model, "model"

    wiki = _wikipedia_poster(
        recommendation.get("title_original", ""),
        recommendation.get("title_pt", ""),
        recommendation.get("year", 0),
        recommendation.get("content_type", "filme"),
        deadline,
    )
    if wiki:
        return wiki, "wikipedia"

    generated = _generate_poster_image(
        recommendation.get("title_pt") or recommendation.get("title_original", ""),
        recommendation.get("year", 0),
        recommendation.get("genres"),
        recommendation.get("content_type", "filme"),
        deadline,
    )
    return (generated, "generated") if generated else ("", "")


def _safe_resolve_poster(recommendation: dict, deadline=None) -> tuple[str, str]:
    """Um pôster que falha nunca derruba a recomendação inteira."""
    try:
        return resolve_poster(recommendation, deadline)
    except Exception:
        return "", ""


def enrich_result(result: dict, deadline=None) -> dict:
    """Resolve o pôster de cada indicação. Sem catálogo externo (TMDB foi
    removido do projeto), o restante dos campos vem inteiramente do motor —
    disponibilidade nunca é tratada como confirmada por uma fonte externa.

    ``deadline`` é o instante (``time.monotonic``) em que a coleta precisa ter
    terminado; quem chama conhece o tempo que a função ainda tem.
    """
    recommendations = result.get("recommendations", [])
    for recommendation in recommendations:
        if not isinstance(recommendation.get("ratings"), dict):
            recommendation["ratings"] = {}
        if not isinstance(recommendation.get("where_to_watch"), list):
            recommendation["where_to_watch"] = []

    if deadline is None:
        deadline = time.monotonic() + POSTER_TIME_BUDGET_SECONDS

    # Em paralelo: as três indicações dividem o mesmo prazo em vez de somarem
    # três esperas de rede uma depois da outra.
    if recommendations:
        with ThreadPoolExecutor(max_workers=len(recommendations)) as pool:
            posters = list(pool.map(lambda item: _safe_resolve_poster(item, deadline), recommendations))
    else:
        posters = []

    for recommendation, (poster_url, poster_source) in zip(recommendations, posters):
        recommendation["poster_url"] = poster_url
        recommendation["poster_source"] = poster_source
        recommendation["availability_verified"] = False
        recommendation["availability_note"] = "Disponibilidade não confirmada no momento."
    return result
