"""Motor de recomendação do Pipoca & Play.

O servidor local e as funções serverless usam exatamente este módulo, então o
prompt, o schema e a validação nunca divergem entre os dois ambientes.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from metadata import enrich_result


CONTENT_TYPE_OPTIONS = ("Filme", "Série", "Mesclar (filmes e séries)")

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
                    "where_to_watch",
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
            f"estar disponíveis nela, e não em outro serviço."
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

Priorize gênero, vibe e plataformas marcadas; depois duração e companhia; por fim época e popularidade. Os filtros são preferências contextuais, não generalizações rígidas. A duração curta deve favorecer títulos de até 90 minutos; a faixa padrão, 90 a 120; a longa, acima de 120 — em séries, aplique a mesma faixa à duração média do episódio. Quando houver plataforma específica, trate disponibilidade como dado a ser validado por uma fonte externa, nunca como fato conhecido apenas pela IA. Para família com crianças, evite conteúdo inadequado quando a classificação for conhecida.

Selecione exatamente três títulos reais do tipo pedido, ordenados da maior para a menor compatibilidade, e explique por que cada um combina com o perfil. O match_score é a compatibilidade própria do sistema entre 0 e 100, não é nota do IMDb, da crítica ou de qualquer outra fonte. Não escolha simplesmente os títulos mais populares.

{_ratings_prompt_line()}

Não invente avaliações, plataformas, disponibilidade, URLs, preços, datas, classificação indicativa ou premiações. Quando não tiver certeza, use 0, string vazia ou array vazio. A resposta deve obedecer exatamente ao JSON solicitado.{build_feedback_section(feedback)}"""


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


def call_openai(filters, feedback=None):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        # As mensagens de erro daqui chegam à tela do cliente: elas descrevem a
        # falha sem revelar qual motor está por trás da recomendação.
        raise RuntimeError("O motor de recomendação não está configurado no servidor.")
    payload = {
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
        "max_output_tokens": 1800,
        "reasoning": {"effort": "low"},
        "store": False,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"A consulta ao motor de recomendação falhou (HTTP {error.code})."
        ) from RuntimeError(details)
    except urllib.error.URLError as error:
        raise RuntimeError("Não foi possível conectar ao motor de recomendação.") from error

    text = extract_response_text(result)
    if not text:
        status = result.get("status", "desconhecido")
        reason = (result.get("incomplete_details") or {}).get("reason")
        suffix = f" Motivo: {reason}." if reason else ""
        raise RuntimeError(
            f"O motor de recomendação não retornou texto final (status: {status}).{suffix}"
        )
    try:
        structured = validate_recommendation_payload(json.loads(text))
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise RuntimeError("O motor de recomendação retornou um JSON inválido.") from error
    enriched = enrich_result(structured)
    return json.dumps(enriched, ensure_ascii=False, separators=(",", ":"))
