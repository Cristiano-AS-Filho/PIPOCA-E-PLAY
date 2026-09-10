"""Resolução do pôster de cada indicação.

O projeto não tem (nem terá) uma chave de catálogo como o TMDB, mas o cartão
precisa mostrar **a arte do próprio título**. A coleta tenta, nesta ordem, só
fontes públicas e sem chave, e confirma cada endereço no servidor antes de
aceitá-lo:

1. O ``poster_url`` que o próprio motor (ChatGPT) devolveu — o prompt já obriga
   cada indicação a trazer esse campo. Normalizado (``http://`` vira ``https://``,
   senão o navegador bloquearia a imagem como conteúdo misto) e confirmado: uma
   LLM erra endereços com facilidade, e sem essa checagem um link inventado
   encerrava a coleta e o cartão ficava sem pôster nenhum.
2. A **busca do iTunes** (``itunes.apple.com/search``, pública e sem chave), na
   loja brasileira e depois na americana. É a fonte com melhor cobertura de arte
   oficial de filme e série, inclusive com o título em português; o resultado só
   é aceito quando o título (e o ano, quando conhecido) batem.
3. A **Wikipedia**, em português e depois em inglês, pela API de busca com
   ``pageimages``: acha o verbete pelo nome em vez de adivinhar o título exato
   ("Fulano (filme de 2024)"), que é como as tentativas anteriores erravam. A
   consulta precisa pedir ``pilicense=any`` e ``pilimit``: nos padrões da API
   ("free" e uma página só) o pôster de um lançamento — que no verbete é
   sempre um arquivo de uso justo, não uma imagem livre — simplesmente não
   vinha, e títulos com verbete e pôster na Wikipedia, como *Ripley*, caíam na
   capa gerada.
4. Só quando nenhuma arte oficial aparece, uma capa ilustrativa gerada por IA na
   mesma conta da OpenAI já usada pelo motor. Nunca é a arte do título, por isso
   `poster_source` chega como "generated" e a tela rotula a imagem como tal. Ela
   é pedida comprimida: uma capa de vários MB embutida na resposta atrasa e
   arrisca estourar o limite de corpo da função.

As três indicações são resolvidas em paralelo e sob um prazo comum: a coleta do
pôster nunca pode estourar o tempo da função serverless e derrubar a
recomendação que o usuário já pagou.

Disponibilidade em streaming não tem uma fonte externa que a confirme: o campo
`where_to_watch` que o motor preencheu é mantido como está, apenas marcado como
não confirmado.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

POSTER_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif")

# Hospedeiros que só servem imagem: o link vale mesmo sem extensão no caminho
# (é comum um endereço de CDN trazer parâmetros de redimensionamento).
IMAGE_HOSTS = (
    "tmdb.org", "themoviedb.org", "wikimedia.org", "wikipedia.org",
    "media-amazon.com", "mzstatic.com",
)

MAX_DATA_URI_LENGTH = 3_000_000  # ~2,2 MB de imagem embutida na resposta
MAX_HTTP_URL_LENGTH = 500

USER_AGENT = "PipocaPlay/1.0 (busca de pôster)"

# Prazos de cada etapa. São tetos: o prazo comum da requisição (``deadline``)
# encurta qualquer um deles quando o tempo restante é menor.
POSTER_PROBE_TIMEOUT = 5
CATALOG_TIMEOUT = 6
GENERATION_TIMEOUT = 40

# Usado quando ninguém informa um prazo (servidor local, testes, chamadas
# diretas): sozinho, o módulo se dá esta janela para resolver os pôsteres.
POSTER_TIME_BUDGET_SECONDS = 30


def _log(message: str) -> None:
    """Deixa o motivo da falha nos logs da função, sem quebrar a resposta.

    É por aqui que se descobre, no painel da Vercel, por que um cartão caiu na
    capa ilustrativa em vez de trazer a arte do título.
    """
    if os.environ.get("ENVIRONMENT") == "test":
        return
    try:
        print(f"[poster] {message}", file=sys.stderr, flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Prazo comum
# ---------------------------------------------------------------------------


def _time_left(deadline) -> float:
    """Segundos que ainda restam do prazo comum da requisição."""
    if deadline is None:
        return float(POSTER_PROBE_TIMEOUT + CATALOG_TIMEOUT + GENERATION_TIMEOUT)
    return max(0.0, deadline - time.monotonic())


def _budget(deadline, ceiling: float) -> float:
    """Teto da etapa, encurtado pelo que sobrou do prazo. 0 = não dá tempo."""
    remaining = _time_left(deadline)
    return min(float(ceiling), remaining) if remaining > 1 else 0.0


# ---------------------------------------------------------------------------
# Endereços de imagem
# ---------------------------------------------------------------------------


def _is_public_host(host: str) -> bool:
    """Barra endereços internos: a URL pode vir do motor, não de fonte confiável."""
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
    """Normaliza um endereço de imagem; devolve vazio quando não tem forma de imagem."""
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
    """Confirma que o endereço devolve mesmo uma imagem, sem baixar o arquivo.

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
    except Exception as error:
        _log(f"endereço não respondeu ({url[:80]}): {error}")
        return False


def _http_json(url: str, timeout: float):
    """GET de JSON em API pública. Devolve ``None`` em qualquer falha."""
    if timeout <= 0:
        return None
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as error:
        _log(f"consulta falhou ({url[:80]}): {error}")
        return None


# ---------------------------------------------------------------------------
# Comparação de títulos
# ---------------------------------------------------------------------------


def _normalize_title(value: str) -> str:
    """Compara títulos sem acento, pontuação nem maiúsculas."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _drop_numbering(title: str) -> str:
    """Tira a numeração de sequência: "tira da pesada 4" vira "tira da pesada"."""
    return " ".join(word for word in title.split() if not word.isdigit())


def _titles_match(found: str, wanted: str, loose: bool = False) -> bool:
    """Aceita variações comuns de subtítulo, recusa um título diferente.

    ``loose`` também ignora a numeração da sequência — as lojas costumam
    publicar "Um Tira da Pesada: Axel Foley" onde o motor diz "Um Tira da
    Pesada 4: Axel Foley". Só vale junto de uma conferência de ano, senão o
    quarto filme casaria com o terceiro.
    """
    a, b = _normalize_title(found), _normalize_title(wanted)
    if not a or not b:
        return False
    if a == b or a.startswith(b) or b.startswith(a):
        return True
    if not loose:
        return False
    a, b = _drop_numbering(a), _drop_numbering(b)
    return bool(a and b) and (a == b or a.startswith(b) or b.startswith(a))


def _is_series(content_type) -> bool:
    return str(content_type).lower().startswith("seri")


# ---------------------------------------------------------------------------
# 1ª fonte: a resposta do próprio motor
# ---------------------------------------------------------------------------


def _model_poster(recommendation: dict, deadline=None) -> str:
    candidate = _looks_like_image_url(recommendation.get("poster_url"))
    if not candidate:
        return ""
    if candidate.startswith("data:image/"):
        # A imagem veio embutida na própria resposta: não há o que confirmar.
        return candidate
    return candidate if _image_responds(candidate, _budget(deadline, POSTER_PROBE_TIMEOUT)) else ""


# ---------------------------------------------------------------------------
# 2ª fonte: busca pública do iTunes (arte oficial, sem chave)
# ---------------------------------------------------------------------------

ITUNES_ARTWORK_SIZE = re.compile(r"/\d+x\d+(?:bb|bf)?(?:-\d+)?\.(?:jpg|jpeg|png|webp)$", re.I)


def _pick_itunes_result(data, wanted_title: str, year: int) -> str:
    """Endereço da arte do resultado cujo título (e ano, quando há) batem.

    Um pôster do filme errado é pior que nenhum: sem correspondência de título a
    busca devolve vazio e a coleta segue para a próxima fonte.
    """
    results = (data or {}).get("results") if isinstance(data, dict) else None
    numbered = ""
    for item in results or []:
        if not isinstance(item, dict):
            continue
        name = item.get("trackName") or item.get("collectionName") or ""
        artwork = str(item.get("artworkUrl100") or item.get("artworkUrl60") or "").strip()
        if not artwork:
            continue
        released = str(item.get("releaseDate") or "")[:4]
        if not year:
            if _titles_match(name, wanted_title):
                return artwork
            continue
        # Com o ano conhecido ele é obrigatório: sem essa trava, "Um Tira da
        # Pesada 4" casaria com o original de 1984 e o cartão traria a arte
        # errada — pior que cartão sem arte.
        if not (released.isdigit() and abs(int(released) - year) <= 1):
            continue
        if _titles_match(name, wanted_title):
            return artwork
        if not numbered and _titles_match(name, wanted_title, loose=True):
            numbered = artwork
    return numbered


def _itunes_full_size(artwork: str, deadline=None) -> str:
    """A busca devolve a miniatura de 100px; o mesmo caminho serve a arte cheia.

    O tamanho ampliado é uma reescrita nossa, então precisa ser confirmado — se
    não responder, vale a miniatura original, que veio da própria API.
    """
    original = _looks_like_image_url(artwork)
    for size in ("600x900bb.jpg", "400x600bb.jpg"):
        bigger = _looks_like_image_url(ITUNES_ARTWORK_SIZE.sub("/" + size, str(artwork or "").strip()))
        if bigger and bigger != original and _image_responds(bigger, _budget(deadline, POSTER_PROBE_TIMEOUT)):
            return bigger
    return original


def _itunes_poster(title_original: str, title_pt: str, year, content_type: str, deadline=None) -> str:
    """2ª fonte: a arte oficial publicada na loja da Apple."""
    year = int(year or 0)
    series = _is_series(content_type)
    attempts = []
    for country, title in (("BR", title_pt), ("US", title_original), ("BR", title_original)):
        title = str(title or "").strip()
        if title and (country, title) not in attempts:
            attempts.append((country, title))
    for country, title in attempts:
        timeout = _budget(deadline, CATALOG_TIMEOUT)
        if timeout <= 0:
            return ""
        query = urllib.parse.urlencode({
            "term": title,
            "country": country,
            "limit": 15,
            "media": "tvShow" if series else "movie",
            "entity": "tvSeason" if series else "movie",
        })
        artwork = _pick_itunes_result(
            _http_json("https://itunes.apple.com/search?" + query, timeout), title, year
        )
        if artwork:
            full = _itunes_full_size(artwork, deadline)
            if full:
                return full
    return ""


# ---------------------------------------------------------------------------
# 3ª fonte: Wikipedia (busca pelo verbete, não pelo título exato)
# ---------------------------------------------------------------------------


ENTRY_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")

# Quantos verbetes a busca traz — e quantos recebem imagem (``pilimit``).
WIKIPEDIA_SEARCH_RESULTS = 3


def _entry_matches(entry_title: str, wanted: str) -> bool:
    """O verbete achado precisa ser do título procurado.

    A busca da Wikipedia sempre devolve *algo*; sem esta conferência o cartão
    poderia exibir o pôster de outro filme, que é pior que não exibir nenhum.
    """
    return _entry_rank(entry_title, wanted) < 2


def _entry_rank(entry_title: str, wanted: str) -> int:
    """0 = o mesmo nome, 1 = variação aceitável, 2 = outro título.

    Serve para desempatar: "Ripley" e "Ripley Under Ground" passam os dois pela
    conferência (uma é prefixo da outra), e sem esta ordem o verbete errado
    ganharia só por vir antes na busca.
    """
    stripped = ENTRY_SUFFIX.sub("", str(entry_title or ""))
    if _normalize_title(stripped) and _normalize_title(stripped) == _normalize_title(wanted):
        return 0
    return 1 if _titles_match(stripped, wanted, loose=True) else 2


def _wikipedia_page_image(language: str, search: str, wanted: str, timeout: float) -> str:
    """Imagem principal do verbete que a busca da Wikipedia achar."""
    if not search or timeout <= 0:
        return ""
    query = urllib.parse.urlencode({
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "generator": "search",
        "gsrsearch": search,
        "gsrlimit": str(WIKIPEDIA_SEARCH_RESULTS),
        "gsrnamespace": "0",
        "prop": "pageimages",
        "piprop": "original|thumbnail",
        "pithumbsize": "800",
        # Sem estes dois a consulta volta sem imagem alguma para boa parte dos
        # títulos, e era o que jogava o cartão na capa gerada:
        # - ``pilicense`` vale "free" por padrão, e o pôster de um filme ou
        #   série no verbete é um arquivo de uso justo, nunca de licença livre;
        # - ``pilimit`` vale 1 por padrão, então de todos os resultados da busca
        #   um só recebia a imagem, e não necessariamente o do título certo.
        "pilicense": "any",
        "pilimit": str(WIKIPEDIA_SEARCH_RESULTS),
    })
    data = _http_json(f"https://{language}.wikipedia.org/w/api.php?{query}", timeout)
    pages = ((data or {}).get("query") or {}).get("pages") if isinstance(data, dict) else None
    if not isinstance(pages, list):
        return ""
    candidates = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        rank = _entry_rank(page.get("title", ""), wanted)
        if rank < 2:
            index = page.get("index")
            candidates.append((rank, index if isinstance(index, int) else 99, page))
    # O verbete de mesmo nome ganha do que só começa igual; entre iguais, vale a
    # ordem da própria busca.
    for _, _, page in sorted(candidates, key=lambda item: (item[0], item[1])):
        for key in ("original", "thumbnail"):
            image = _looks_like_image_url((page.get(key) or {}).get("source", ""))
            if image:
                return image
    return ""


def _wikipedia_searches(title_original: str, title_pt: str, year, content_type: str):
    """Buscas a tentar, do título em português (público brasileiro) ao original."""
    year = int(year or 0)
    kind_pt = "série de televisão" if _is_series(content_type) else "filme"
    kind_en = "television series" if _is_series(content_type) else "film"
    year_pt = f" {year}" if year else ""
    year_en = f" {year}" if year else ""
    searches = []
    for language, title, kind, suffix in (
        ("pt", title_pt, kind_pt, year_pt),
        ("en", title_original, kind_en, year_en),
        ("pt", title_original, kind_pt, year_pt),
        ("en", title_pt, kind_en, year_en),
    ):
        title = str(title or "").strip()
        if not title:
            continue
        entry = (language, f"{title} {kind}{suffix}", title)
        if entry not in searches:
            searches.append(entry)
    return searches


def _wikipedia_poster(title_original: str, title_pt: str, year, content_type: str, deadline=None) -> str:
    """3ª fonte: uma imagem real e gratuita da Wikipedia para o título."""
    for language, search, wanted in _wikipedia_searches(title_original, title_pt, year, content_type):
        timeout = _budget(deadline, CATALOG_TIMEOUT)
        if timeout <= 0:
            return ""
        image = _wikipedia_page_image(language, search, wanted, timeout)
        if image:
            return image
    return ""


# ---------------------------------------------------------------------------
# 4ª fonte: capa ilustrativa gerada por IA (último recurso)
# ---------------------------------------------------------------------------

IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
)


def _data_uri(b64: str) -> str:
    """Monta a data URI com o tipo real dos bytes, não com o tipo esperado."""
    if not b64:
        return ""
    try:
        raw = base64.b64decode(b64[:64] + "=" * (-len(b64[:64]) % 4), validate=False)
    except (binascii.Error, ValueError):
        return ""
    mime = ""
    for signature, candidate in IMAGE_SIGNATURES:
        if raw.startswith(signature):
            mime = candidate
            break
    if not mime and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "image/webp"
    if not mime:
        _log("a capa gerada não veio em um formato de imagem reconhecido")
        return ""
    uri = f"data:{mime};base64,{b64}"
    if len(uri) > MAX_DATA_URI_LENGTH:
        # Uma capa gigante embutida na resposta arrisca estourar o limite de
        # corpo da função e derrubar a recomendação inteira.
        _log(f"capa gerada descartada por tamanho ({len(uri)} caracteres)")
        return ""
    return uri


def _generate_poster_image(title: str, year, genres, content_type: str, deadline=None) -> str:
    """Capa ilustrativa gerada pela mesma conta da OpenAI já usada pelo motor.
    Nunca é a arte oficial do título."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    timeout = _budget(deadline, GENERATION_TIMEOUT)
    if not api_key or not title or timeout <= 0:
        return ""
    kind = "série de TV" if _is_series(content_type) else "filme"
    genre_hint = ", ".join([str(g) for g in (genres or []) if g][:2]) or "drama"
    year_hint = f" ({int(year)})" if year else ""
    prompt = (
        f'Arte de capa ilustrativa e original, estilo pôster de cinema, inspirada no clima '
        f'do {kind} "{title}"{year_hint}, gênero {genre_hint}. Composição vertical, cores '
        "fortes, cena atmosférica. Não inclua nenhum texto, título, letra ou logotipo na imagem "
        "— represente o clima da obra, não seus personagens ou atores reais."
    )
    payload = {
        "model": "gpt-image-1",
        "prompt": prompt,
        "size": "1024x1536",
        "quality": "low",
        "n": 1,
        # Comprimida de propósito: a capa viaja embutida no corpo da resposta.
        "output_format": "jpeg",
        "output_compression": 60,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/images/generations",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception as error:
        _log(f"geração da capa falhou: {error}")
        return ""
    items = result.get("data") or []
    return _data_uri(items[0].get("b64_json", "") if items else "")


# ---------------------------------------------------------------------------
# Coleta
# ---------------------------------------------------------------------------

def resolve_poster(recommendation: dict, deadline=None) -> tuple[str, str]:
    """Devolve ``(poster_url, poster_source)``: a primeira arte oficial do título
    e, só quando nenhuma aparece, a capa ilustrativa gerada.

    Cada fonte já entrega um endereço confirmado — o do motor por sondagem (ele
    pode ter inventado o link), o do iTunes porque o tamanho ampliado é reescrito
    por nós. O da Wikipedia vem da própria API do verbete e não é sondado de
    novo: uma sondagem que falhasse por bloqueio do CDN descartaria arte boa e
    jogaria o cartão na capa gerada, que é justamente o que queremos evitar.
    """
    title_original = recommendation.get("title_original", "")
    title_pt = recommendation.get("title_pt", "")
    year = recommendation.get("year", 0)
    content_type = recommendation.get("content_type", "filme")

    from_model = _model_poster(recommendation, deadline)
    if from_model:
        return from_model, "model"

    from_itunes = _itunes_poster(title_original, title_pt, year, content_type, deadline)
    if from_itunes:
        return from_itunes, "itunes"

    from_wikipedia = _wikipedia_poster(title_original, title_pt, year, content_type, deadline)
    if from_wikipedia:
        return from_wikipedia, "wikipedia"

    title = title_pt or title_original
    _log(f"sem arte oficial para {title!r}: caindo na capa ilustrativa")
    generated = _generate_poster_image(
        title,
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
    except Exception as error:
        _log(f"coleta falhou: {error}")
        return "", ""


def enrich_result(result: dict, deadline=None) -> dict:
    """Resolve o pôster de cada indicação. Sem catálogo pago, o restante dos
    campos vem inteiramente do motor — disponibilidade nunca é tratada como
    confirmada por uma fonte externa.

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
