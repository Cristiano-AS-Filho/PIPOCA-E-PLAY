"""Motor de recomendação do Pipoca & Play.

O servidor local e as funções serverless usam exatamente este módulo, então o
prompt, o schema e a validação nunca divergem entre os dois ambientes.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from metadata import enrich_result


CONTENT_TYPE_OPTIONS = ("Filme", "Série", "Mesclar (filmes e séries)")

# A função serverless que serve /api morre aos 60s (``vercel.json``). Este é o
# tempo total que a chamada tem para responder o motor e coletar os pôsteres:
# o que sobrar depois da resposta do motor é o prazo da coleta de imagens.
REQUEST_BUDGET_SECONDS = 52

# Teto de saída do motor. O modo estrito precisa fechar o JSON inteiro — três
# indicações completas, com nove notas e um link de pôster cada — e os tokens de
# raciocínio saem do mesmo teto. Apertado demais, a resposta chega cortada no
# meio e o JSON não fecha: era isso que derrubava a busca "de vez em quando"
# com "JSON inválido". O teto não é cobrado, só o que o modelo realmente gera.
MAX_OUTPUT_TOKENS = 6000

# Tempo máximo de uma chamada ao motor e o mínimo que precisa sobrar do
# orçamento para valer a pena tentar de novo depois de uma resposta cortada.
ENGINE_TIMEOUT_SECONDS = 45
RETRY_MIN_SECONDS_LEFT = 18

OFFICIAL_FILTER_OPTIONS = {
    "content_type": set(CONTENT_TYPE_OPTIONS),
    "genre": {"Livre (Qualquer)", "Ação", "Comédia", "Drama", "Ficção Científica", "Terror", "Romance", "Suspense / Thriller", "Animação", "Documentário", "Aventura", "Fantasia"},
    "mood": {"Livre (Qualquer)", "Quer dar risada / Divertido", "Para chorar / Emocionante", "Tensão / Adrenalina", "Para pensar / Cabeça", "Leve / Relaxante para descansar", "Inspirador / Motivacional", "Sombrio / Assustador"},
    "duration": {"Livre (Qualquer)", "Curto (Até 90 min)", "Padrão (90 a 120 min)", "Longo (Mais de 120 min)"},
    "era": {"Livre (Qualquer)", "Lançamentos Recentes (2023-2026)", "Anos 2010s", "Anos 2000s", "Anos 90s", "Clássicos (Antes de 1990)"},
    "platform": {"Livre (Qualquer)", "Netflix", "Amazon Prime Video", "Max (HBO)", "Disney+", "Apple TV+", "Paramount+", "Cinema / Aluguel"},
    "companionship": {"Sozinho(a)", "Em Casal", "Com Amigos", "Em Família (com crianças)"},
    "popularity": {"Indiferente", "Grandes Sucessos / Blockbusters", "Filmes Cult / Menos Conhecidos", "Aclamados pela Crítica / Premiações (Oscar, Cannes)"},
}

# Fontes de avaliação exibidas no cartão de cada indicação. A ordem aqui é a
# ordem das pílulas na tela e a ordem citada no prompt, então schema, prompt e
# front-end nunca divergem sobre quais notas existem.
RATING_SOURCES = (
    {"key": "imdb", "label": "IMDb", "scale": 10, "kind": "number"},
    {"key": "rotten_tomatoes_critics", "label": "Rotten Tomatoes (crítica)", "scale": 100, "kind": "integer"},
    {"key": "rotten_tomatoes_audience", "label": "Rotten Tomatoes (público)", "scale": 100, "kind": "integer"},
    {"key": "metacritic", "label": "Metacritic", "scale": 100, "kind": "integer"},
    {"key": "google_users", "label": "Google (% de usuários que gostaram)", "scale": 100, "kind": "integer"},
    {"key": "tmdb", "label": "TMDB", "scale": 10, "kind": "number"},
    {"key": "letterboxd", "label": "Letterboxd", "scale": 5, "kind": "number"},
    {"key": "adorocinema", "label": "AdoroCinema", "scale": 5, "kind": "number"},
    {"key": "mercado_livre_filmes", "label": "Mercado Livre Filmes", "scale": 5, "kind": "number"},
)

RATING_KEYS = tuple(source["key"] for source in RATING_SOURCES)


def _ratings_schema():
    """Monta o bloco ratings a partir de RATING_SOURCES.

    O modo estrito da API exige todas as chaves em ``required``: quem não souber
    a nota devolve 0, que o front-end trata como "sem avaliação".
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(RATING_KEYS),
        "properties": {
            source["key"]: {
                "type": source["kind"],
                "minimum": 0,
                "maximum": source["scale"],
            }
            for source in RATING_SOURCES
        },
    }


def _ratings_prompt_line():
    scales = "; ".join(f"{source['label']} de 0 a {source['scale']}" for source in RATING_SOURCES)
    return (
        "Preencha o bloco ratings com as notas públicas de cada fonte, sempre na escala da "
        f"própria fonte: {scales}. Use 0 em toda fonte que você não souber com segurança — nunca "
        "estime, converta a nota de uma fonte para outra nem invente uma avaliação."
    )


RECOMMENDATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["interpretation", "best_choice", "recommendations"],
    "properties": {
        "interpretation": {"type": "string"},
        "best_choice": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title_pt", "reason"],
            "properties": {
                "title_pt": {"type": "string"},
                "reason": {"type": "string"},
            },
        },
        "recommendations": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "rank", "content_type", "title_original", "title_pt", "year",
                    "runtime_minutes", "seasons", "age_rating_br", "genres", "vibe_tags",
                    "synopsis", "why_it_matches", "match_score", "ratings", "awards",
                    "where_to_watch", "poster_url",
                ],
                "properties": {
                    "rank": {"type": "integer", "minimum": 1, "maximum": 3},
                    "content_type": {"type": "string", "enum": ["filme", "serie"]},
                    "title_original": {"type": "string"},
                    "title_pt": {"type": "string"},
                    "year": {"type": "integer", "minimum": 0},
                    "runtime_minutes": {"type": "integer", "minimum": 0},
                    "seasons": {"type": "integer", "minimum": 0},
                    "age_rating_br": {"type": "integer", "minimum": 0},
                    "genres": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                    "vibe_tags": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                    "synopsis": {"type": "string"},
                    "why_it_matches": {"type": "string"},
                    "match_score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "ratings": _ratings_schema(),
                    "awards": {
                        "type": "object", "additionalProperties": False,
                        "required": ["oscars_won", "highlight"],
                        "properties": {
                            "oscars_won": {"type": "integer", "minimum": 0},
                            "highlight": {"type": "string"},
                        },
                    },
                    "poster_url": {"type": "string"},
                    "where_to_watch": {
                        "type": "array",
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "required": ["platform", "type"],
                            "properties": {
                                "platform": {"type": "string"},
                                "type": {"type": "string", "enum": ["assinatura", "aluguel_compra", "cinema"]},
                            },
                        },
                    },
                },
            },
        },
    },
}


# Perguntas que aceitam mais de uma resposta. A plataforma é a única por
# enquanto: o usuário costuma assinar vários serviços ao mesmo tempo.
MULTI_ANSWER_KEYS = ("platform",)

# Separador usado para guardar várias respostas num único campo de texto. Nenhum
# nome oficial de plataforma contém vírgula, então a ida e a volta são seguras.
MULTI_ANSWER_SEPARATOR = ", "

ANY_PLATFORM = "Livre (Qualquer)"

MAX_PLATFORMS_PER_SEARCH = 5

# Onde assistir é o coração do produto: o cartão precisa dizer em qual serviço o
# título está. O motor às vezes escreve "HBO Max" ou "prime video"; normalizar
# aqui deixa o cartão coerente com o nome que o cliente marcou no questionário.
PLATFORM_ALIASES = {
    "netflix": "Netflix",
    "prime video": "Amazon Prime Video",
    "amazon prime": "Amazon Prime Video",
    "amazon prime video": "Amazon Prime Video",
    "prime video (amazon)": "Amazon Prime Video",
    "disney+": "Disney+",
    "disney plus": "Disney+",
    "star+": "Disney+",
    "star plus": "Disney+",
    "max": "Max (HBO)",
    "max (hbo)": "Max (HBO)",
    "hbo max": "Max (HBO)",
    "hbo": "Max (HBO)",
    "apple tv+": "Apple TV+",
    "apple tv plus": "Apple TV+",
    "apple tv": "Apple TV+",
    "paramount+": "Paramount+",
    "paramount plus": "Paramount+",
    "paramount": "Paramount+",
}
WHERE_TO_WATCH_TYPES = ("assinatura", "aluguel_compra", "cinema")
MAX_WHERE_TO_WATCH_ITEMS = 4


def canonical_platform(name) -> str:
    """Nome exibido do serviço: apelidos conhecidos viram o nome oficial."""
    cleaned = " ".join(str(name or "").split())[:60]
    if not cleaned:
        return ""
    return PLATFORM_ALIASES.get(cleaned.lower(), cleaned)


def _clean_where_entry(entry):
    if not isinstance(entry, dict):
        return None
    platform = canonical_platform(entry.get("platform"))
    if not platform:
        return None
    kind = str(entry.get("type", "") or "").strip().lower()
    if kind not in WHERE_TO_WATCH_TYPES:
        kind = "cinema" if "cinema" in platform.lower() else "assinatura"
    return {"platform": platform, "type": kind}


def _fallback_where_to_watch(filters):
    """A plataforma marcada pelo cliente, quando ele marcou uma só.

    O motor recebeu a marcação como restrição forte — as três indicações
    precisam estar nela —, então repetir esse nome é dizer o que o próprio motor
    afirmou ao escolher o título, e a tela continua exibindo a disponibilidade
    como não confirmada. Com várias marcações não dá para saber qual delas exibe
    cada título: aí o campo fica vazio em vez de chutar.
    """
    marked = [name for name in split_answers(filters.get("platform")) if name != ANY_PLATFORM]
    if len(marked) != 1:
        return []
    entry = _clean_where_entry({"platform": marked[0], "type": "assinatura"})
    return [entry] if entry else []


def _apply_where_to_watch(payload, filters):
    """Limpa ``where_to_watch`` e completa com a marcação quando o motor cala."""
    fallback = _fallback_where_to_watch(filters if isinstance(filters, dict) else {})
    for recommendation in payload.get("recommendations", []):
        raw = recommendation.get("where_to_watch")
        entries, seen = [], set()
        for entry in raw if isinstance(raw, list) else []:
            cleaned = _clean_where_entry(entry)
            if cleaned and cleaned["platform"].lower() not in seen:
                seen.add(cleaned["platform"].lower())
                entries.append(cleaned)
        recommendation["where_to_watch"] = (entries or [dict(item) for item in fallback])[:MAX_WHERE_TO_WATCH_ITEMS]
    return payload


def split_answers(value):
    """Lê um campo multivalorado como lista, aceitando texto ou lista."""
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return []
    seen = []
    for item in items:
        if not isinstance(item, str):
            return []
        item = item.strip()
        if item and item not in seen:
            seen.append(item)
    return seen


def _clean_multi_answer(key, raw):
    """Valida uma resposta com várias marcações e devolve o texto canônico."""
    answers = split_answers(raw)
    if not answers:
        raise ValueError("Marque pelo menos uma opção antes de buscar.")
    if len(answers) > MAX_PLATFORMS_PER_SEARCH:
        raise ValueError(
            f"Marque no máximo {MAX_PLATFORMS_PER_SEARCH} opções nesta pergunta."
        )
    for answer in answers:
        if len(answer) > 120 or answer not in OFFICIAL_FILTER_OPTIONS[key]:
            raise ValueError("Uma das respostas não pertence às opções oficiais.")
    # "Livre (Qualquer)" abre todo o catálogo: combinado com outras marcações ele
    # anula o filtro, então prevalece sozinho.
    if ANY_PLATFORM in answers:
        return ANY_PLATFORM
    return MULTI_ANSWER_SEPARATOR.join(answers)


def clean_filters(value):
    if not isinstance(value, dict):
        raise ValueError("Filtros inválidos.")
    allowed = set(OFFICIAL_FILTER_OPTIONS)
    cleaned = {}
    for key in allowed:
        if key in MULTI_ANSWER_KEYS:
            cleaned[key] = _clean_multi_answer(key, value.get(key, ""))
            continue
        item = value.get(key, "")
        if not isinstance(item, str) or len(item) > 120:
            raise ValueError("Um dos filtros é inválido.")
        item = item.strip()
        if item not in OFFICIAL_FILTER_OPTIONS[key]:
            raise ValueError("Uma das respostas não pertence às opções oficiais.")
        cleaned[key] = item
    if not all(cleaned.values()):
        raise ValueError("Responda às oito perguntas antes de buscar.")
    return cleaned


MAX_FEEDBACK_IN_PROMPT = 40


def _title_label(item):
    title = str(item.get("title_pt", "")).strip() or str(item.get("title_original", "")).strip()
    original = str(item.get("title_original", "")).strip()
    year = int(item.get("year", 0) or 0)
    label = title
    if original and original.lower() != title.lower():
        label += f" ({original}"
        label += f", {year})" if year else ")"
    elif year:
        label += f" ({year})"
    return label


def build_feedback_section(feedback):
    """Transforma as marcações do usuário em instruções para o modelo.

    As marcações vêm da conta logada — ``já assisti`` remove o título das
    próximas indicações, ``gostei`` e ``não gostei`` guiam o estilo da seleção.
    """
    if not feedback:
        return ""
    recent = list(feedback)[:MAX_FEEDBACK_IN_PROMPT]
    watched = [_title_label(item) for item in recent if item.get("watched")]
    liked = [_title_label(item) for item in recent if item.get("opinion") == "liked"]
    disliked = [_title_label(item) for item in recent if item.get("opinion") == "disliked"]
    if not (watched or liked or disliked):
        return ""
    blocks = ["\n\nHistórico pessoal deste usuário, registrado por ele na plataforma:"]
    if watched:
        blocks.append(
            "- JÁ ASSISTIU (proibido recomendar de novo, escolha outros títulos): "
            + "; ".join(watched)
        )
    if liked:
        blocks.append(
            "- GOSTOU (use como referência de estilo, tom e ritmo, sem repetir esses títulos): "
            + "; ".join(liked)
        )
    if disliked:
        blocks.append(
            "- NÃO GOSTOU (evite esses títulos e o que for muito parecido com eles): "
            + "; ".join(disliked)
        )
    blocks.append(
        "Trate o histórico como restrição forte: nenhuma das três indicações pode ser um título "
        "já assistido ou marcado como não gostei."
    )
    return "\n".join(blocks)


def build_platform_section(platform_value):
    """Descreve as plataformas marcadas — a pergunta aceita mais de uma marcação."""
    platforms = split_answers(platform_value)
    if not platforms or platforms == [ANY_PLATFORM]:
        return (
            "O usuário não restringiu o serviço: pode indicar títulos de qualquer plataforma, "
            "inclusive cinema e aluguel."
        )
    if len(platforms) == 1:
        return (
            f"O usuário marcou uma única plataforma ({platforms[0]}): as três indicações precisam "
            f"estar disponíveis nela, e não em outro serviço. Repita esse serviço em where_to_watch "
            f"de cada indicação."
        )
    joined = ", ".join(platforms)
    return (
        f"O usuário marcou {len(platforms)} plataformas ao mesmo tempo ({joined}). Trate a marcação "
        "como um conjunto: cada indicação precisa estar disponível em pelo menos uma dessas "
        "plataformas e nenhuma pode depender de um serviço fora dessa lista. Quando houver bons "
        "títulos em mais de uma delas, distribua as três indicações entre as plataformas marcadas "
        "em vez de concentrar tudo em uma só, e diga em where_to_watch qual das plataformas "
        "marcadas exibe cada título."
    )


def _poster_prompt_line():
    """Instrução do pôster.

    O servidor confere o endereço antes de exibi-lo e troca de fonte quando ele
    não responde (ver ``metadata.py``), então aqui pedimos o melhor link que o
    modelo conhecer em vez de pedir silêncio na dúvida: um link errado não custa
    nada, um campo vazio custa a única fonte que sabe o pôster do título.
    """
    return (
        "Preencha poster_url com o link direto e público do arquivo de imagem do pôster oficial "
        "do título, sempre em https e terminado em .jpg, .jpeg, .png ou .webp — por exemplo, um "
        "link no formato https://image.tmdb.org/t/p/w500/<caminho>.jpg ou o arquivo de capa "
        "hospedado em https://upload.wikimedia.org/<caminho>.jpg. Precisa ser o endereço da "
        "imagem em si: nunca uma página HTML, um resultado de busca, um link de vídeo ou o "
        "endereço da página do filme. O servidor testa o endereço antes de exibi-lo e busca outra "
        "fonte sozinho quando ele não responde, então indique o melhor link que você conhecer "
        "para aquele título em vez de deixar o campo vazio; use string vazia só quando não "
        "conhecer nenhum endereço de imagem para ele."
    )


def _where_to_watch_prompt_line():
    """Instrução de onde assistir.

    Saber onde assistir é o motivo de o cliente estar aqui, então o campo não
    pode voltar vazio "por precaução". A tela exibe a disponibilidade como não
    confirmada e oferece o link para conferir na fonte — por isso pedimos o
    melhor conhecimento do modelo em vez de silêncio.
    """
    return (
        "Preencha where_to_watch de cada indicação com o serviço (ou os serviços) em que o título "
        "está disponível no Brasil hoje, do melhor do seu conhecimento: platform recebe o nome do "
        "serviço como o público brasileiro o conhece (Netflix, Amazon Prime Video, Disney+, "
        "Max (HBO), Apple TV+, Paramount+, Globoplay e afins) e type recebe \"assinatura\" quando "
        "está incluído no catálogo do serviço, \"aluguel_compra\" quando só sai alugando ou "
        "comprando, e \"cinema\" quando ainda está em cartaz. Este campo não pode voltar vazio: a "
        "tela informa ao cliente que a disponibilidade não está confirmada e oferece o link para "
        "conferir na fonte, então um serviço provável é muito mais útil que um campo em branco. "
        "Liste no máximo três serviços, do mais provável para o menos provável."
    )


def buildRecommendationPrompt(filters, feedback=None):
    return f"""Atue como um especialista em cinema e séries, recomendador personalizado para o público brasileiro. Responda em pt-BR.

Estou procurando uma recomendação perfeita para assistir agora. Considere conjuntamente estas oito dimensões:
- Tipo de produção: {filters['content_type']}
- Gênero principal: {filters['genre']}
- Vibe/clima emocional desejado: {filters['mood']}
- Tempo disponível: {filters['duration']}
- Época do título: {filters['era']}
- Plataforma de streaming marcada: {filters['platform']}
- Companhia: {filters['companionship']}
- Perfil de popularidade/estilo: {filters['popularity']}

O tipo de produção é a restrição mais forte de todas: com "Filme", as três indicações são longas-metragens; com "Série", as três são séries de TV ou streaming (incluindo minisséries e novelas); com "Mesclar (filmes e séries)", entregue os dois formatos na mesma lista, com pelo menos um filme e pelo menos uma série. Marque cada indicação em content_type com "filme" ou "serie". Para séries, runtime_minutes é a duração média de um episódio e seasons é o número de temporadas já lançadas; para filmes, seasons é 0.

{build_platform_section(filters['platform'])}

Priorize gênero, vibe e plataformas marcadas; depois duração e companhia; por fim época e popularidade. Os filtros são preferências contextuais, não generalizações rígidas. A duração curta deve favorecer títulos de até 90 minutos; a faixa padrão, 90 a 120; a longa, acima de 120 — em séries, aplique a mesma faixa à duração média do episódio. Quando houver plataforma específica, a marcação é uma restrição de escolha: só indique títulos que você acredita estarem disponíveis nela. A tela sempre apresenta a disponibilidade como não confirmada, a ser conferida na fonte, então informe o serviço em where_to_watch mesmo sem certeza absoluta. Para família com crianças, evite conteúdo inadequado quando a classificação for conhecida.

Selecione exatamente três títulos reais do tipo pedido, ordenados da maior para a menor compatibilidade, e explique por que cada um combina com o perfil. O match_score é a compatibilidade própria do sistema entre 0 e 100, não é nota do IMDb, da crítica ou de qualquer outra fonte. Não escolha simplesmente os títulos mais populares.

{_ratings_prompt_line()}

{_poster_prompt_line()}

{_where_to_watch_prompt_line()}

Não invente avaliações, preços, datas, classificação indicativa ou premiações: nesses campos, sem certeza, use 0 ou string vazia. A regra é outra em where_to_watch e poster_url, onde o melhor palpite informado vale mais que o campo vazio, porque a tela já avisa o cliente de que a disponibilidade não está confirmada. A resposta deve obedecer exatamente ao JSON solicitado.{build_feedback_section(feedback)}"""


def validate_recommendation_payload(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("recommendations"), list):
        raise RuntimeError("A resposta do motor não tem o formato esperado.")
    recommendations = payload["recommendations"]
    if len(recommendations) != 3:
        raise RuntimeError("O motor deve retornar exatamente 3 recomendações.")
    required = {"rank", "title_original", "title_pt", "year", "runtime_minutes", "genres", "synopsis", "why_it_matches", "match_score"}
    for index, recommendation in enumerate(recommendations, start=1):
        if not isinstance(recommendation, dict) or not required.issubset(recommendation):
            raise RuntimeError(f"A recomendação {index} está incompleta.")
        if recommendation.get("rank") != index:
            raise RuntimeError("As recomendações devem estar ordenadas por posição.")
        if not 0 <= int(recommendation.get("match_score", 0)) <= 100:
            raise RuntimeError("A pontuação de compatibilidade é inválida.")
        # Sem content_type declarado, tratamos como filme: é o formato padrão do MVP.
        recommendation["content_type"] = "serie" if str(recommendation.get("content_type", "")).lower().startswith("seri") else "filme"
    return payload


def extract_response_text(response):
    """Extrai texto da resposta REST do provedor do motor.

    `output_text` é uma conveniência dos SDKs. A API REST retorna os blocos em
    `output[].content[]`, então essa leitura mantém o backend independente de SDK.
    """
    parts = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                parts.append(content["text"])
    return "".join(parts).strip()


def _engine_payload(filters, feedback, max_output_tokens):
    return {
        "model": os.environ.get("OPENAI_MODEL", "gpt-5.6-luna"),
        "input": buildRecommendationPrompt(filters, feedback),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "movie_recommendations",
                "strict": True,
                "schema": RECOMMENDATION_SCHEMA,
            }
        },
        "max_output_tokens": max_output_tokens,
        "reasoning": {"effort": "low"},
        "store": False,
    }


def _ask_engine(api_key, payload, timeout):
    """Uma chamada ao motor. Erros de rede e HTTP sobem já traduzidos."""
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"A consulta ao motor de recomendação falhou (HTTP {error.code})."
        ) from RuntimeError(details)
    except urllib.error.URLError as error:
        raise RuntimeError("Não foi possível conectar ao motor de recomendação.") from error


def _read_engine_result(result):
    """Devolve ``(payload, erro)``: o JSON já validado, ou o motivo da falha.

    Resposta cortada pelo teto de tokens é o caso importante: o texto chega
    incompleto e o JSON não fecha. Reconhecer isso aqui é o que permite tentar
    de novo, em vez de entregar "JSON inválido" ao cliente.
    """
    text = extract_response_text(result)
    status = str(result.get("status", "") or "desconhecido")
    reason = str((result.get("incomplete_details") or {}).get("reason") or "")

    if status == "incomplete" or not text:
        if reason == "max_output_tokens":
            return None, "A resposta do motor de recomendação veio cortada antes de terminar."
        if reason:
            return None, f"O motor de recomendação não concluiu a resposta (motivo: {reason})."
        if not text:
            return None, f"O motor de recomendação não retornou texto final (status: {status})."
    try:
        return validate_recommendation_payload(json.loads(text)), ""
    except json.JSONDecodeError:
        # JSON que não fecha é quase sempre resposta truncada.
        return None, "A resposta do motor de recomendação veio cortada antes de terminar."
    except (TypeError, ValueError, RuntimeError) as error:
        return None, f"O motor de recomendação devolveu uma resposta fora do formato esperado. {error}".strip()


def call_openai(filters, feedback=None):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        # As mensagens de erro daqui chegam à tela do cliente: elas descrevem a
        # falha sem revelar qual motor está por trás da recomendação.
        raise RuntimeError("O motor de recomendação não está configurado no servidor.")

    started = time.monotonic()
    deadline = started + REQUEST_BUDGET_SECONDS
    max_output_tokens = MAX_OUTPUT_TOKENS
    structured = None
    problem = ""

    for attempt in (1, 2):
        remaining = deadline - time.monotonic()
        result = _ask_engine(
            api_key,
            _engine_payload(filters, feedback, max_output_tokens),
            timeout=max(min(ENGINE_TIMEOUT_SECONDS, remaining), 5),
        )
        structured, problem = _read_engine_result(result)
        if structured is not None:
            break
        # Uma única segunda tentativa, e só quando ainda sobra tempo de função
        # para ela e para a coleta de pôster: entregar nada é pior que esperar.
        if attempt == 2 or (deadline - time.monotonic()) < RETRY_MIN_SECONDS_LEFT:
            raise RuntimeError(f"{problem} Tente novamente.".strip())
        max_output_tokens = min(int(max_output_tokens * 1.5), 16_000)

    _apply_where_to_watch(structured, filters)
    # O pôster é coletado com o tempo que sobrou da requisição.
    enriched = enrich_result(structured, deadline=deadline)
    return json.dumps(enriched, ensure_ascii=False, separators=(",", ":"))
