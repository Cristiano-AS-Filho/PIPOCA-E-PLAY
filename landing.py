"""Catálogo do conteúdo administrável da landing page.

A landing (``public/index.html``) é um pacote gerado no Claude Web Design: a
marcação vem com espaços de pôster identificados por ``id`` e com os cartões de
depoimento marcados por ``data-pp-depo``. Este módulo é a única lista desses
pontos; o painel administrativo a recebe pronta pela API e o script
``public/landing-content.js`` usa os mesmos identificadores para aplicar o que
foi salvo. Trocar o layout da landing significa mexer só aqui.

``title``/``meta`` guardam o texto que a própria página já traz, para o painel
mostrar do que se trata cada espaço: deixar o campo em branco mantém esse texto.
"""

from __future__ import annotations

CATALOG_GROUP = "Catálogo"
TOP3_GROUP = "Top 3 (exemplo da seção “Como funciona”)"
COST_GROUP = "O custo do tempo"

POSTER = "2 / 3"
STILL = "16 / 9"

LANDING_SLOTS = (
    {
        "id": "pp-custo-still",
        "group": COST_GROUP,
        "label": "Cena de abertura da seção",
        "aspect": STILL,
        "captioned": False,
        "title": "",
        "meta": "",
    },
    {"id": "pp-poster-1", "group": CATALOG_GROUP, "label": "Pôster 1", "aspect": POSTER, "captioned": True,
     "title": "O telefone preto", "meta": "Suspense · 2021 · 1h 52 · Netflix"},
    {"id": "pp-poster-2", "group": CATALOG_GROUP, "label": "Pôster 2", "aspect": POSTER, "captioned": True,
     "title": "O enigma de outro mundo", "meta": "Drama · 2019 · 2h 04 · Prime"},
    {"id": "pp-poster-3", "group": CATALOG_GROUP, "label": "Pôster 3", "aspect": POSTER, "captioned": True,
     "title": "Velozes & Furiosos: Desafio em Tóquio", "meta": "Comédia · 2023 · 1h 38 · Max"},
    {"id": "pp-poster-4", "group": CATALOG_GROUP, "label": "Pôster 4", "aspect": POSTER, "captioned": True,
     "title": "Ruptura", "meta": "Ficção · 2020 · 2h 21 · Disney+"},
    {"id": "pp-poster-5", "group": CATALOG_GROUP, "label": "Pôster 5", "aspect": POSTER, "captioned": True,
     "title": "Uma Noite no Museu", "meta": "Série · 3 temporadas · 48 min/ep · Netflix"},
    {"id": "pp-poster-6", "group": CATALOG_GROUP, "label": "Pôster 6", "aspect": POSTER, "captioned": True,
     "title": "Origem", "meta": "Documentário · 2024 · 1h 26 · Globoplay"},
    {"id": "pp-poster-7", "group": CATALOG_GROUP, "label": "Pôster 7", "aspect": POSTER, "captioned": True,
     "title": "O abutre", "meta": "Romance · 2018 · 1h 47 · Prime"},
    {"id": "pp-poster-8", "group": CATALOG_GROUP, "label": "Pôster 8", "aspect": POSTER, "captioned": True,
     "title": "A empregada", "meta": "Série · 2 temporadas · 32 min/ep · Apple TV+"},
    {"id": "pp-poster-9", "group": CATALOG_GROUP, "label": "Pôster 9", "aspect": POSTER, "captioned": True,
     "title": "O Auto da Compadecida", "meta": "Ação · 2017 · 1h 59 · Max"},
    {"id": "pp-top3-1", "group": TOP3_GROUP, "label": "Opção #1 (cena larga)", "aspect": STILL, "captioned": True,
     "title": "O telefone preto", "meta": "Suspense · 1h 52"},
    {"id": "pp-top3-2", "group": TOP3_GROUP, "label": "Opção #2", "aspect": POSTER, "captioned": True,
     "title": "A empregada", "meta": "Drama · 2h 04"},
    {"id": "pp-top3-3", "group": TOP3_GROUP, "label": "Opção #3", "aspect": POSTER, "captioned": True,
     "title": "Origem", "meta": "Série · 3 temporadas · 48 min/ep"},
)

SLOT_IDS = tuple(slot["id"] for slot in LANDING_SLOTS)

# Formatos que o navegador exibe sem plugin e que cabem em uma data URL.
ALLOWED_IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/avif", "image/gif")

# O corpo aceito pelas rotas é de 64 000 bytes (servidor local e função da
# Vercel). O painel comprime a imagem até caber com folga para os textos.
MAX_IMAGE_CHARS = 48_000
MAX_TITLE_LENGTH = 120
MAX_META_LENGTH = 160


def catalog() -> list[dict]:
    """Cópia do catálogo, para a API devolver sem expor a tupla do módulo."""
    return [dict(slot) for slot in LANDING_SLOTS]


def is_slot(slot_id: str) -> bool:
    return slot_id in SLOT_IDS


def get_slot(slot_id: str) -> dict | None:
    for slot in LANDING_SLOTS:
        if slot["id"] == slot_id:
            return dict(slot)
    return None


# ---------------------------------------------------------------------------
# Depoimentos da seção “O que dizem sobre o tempo”
# ---------------------------------------------------------------------------
#
# Mesma ideia dos espaços de imagem acima, para os três cartões de depoimento
# da prova social. A página monta cada cartão pelo componente ``Testimonial``
# do design system; ``public/landing-content.js`` encontra o cartão pelo
# atributo ``data-pp-depo`` da marcação e aplica o que foi salvo.
#
# ``name``/``handle``/``quote``/``context``/``placeholder`` guardam o que a
# própria página já traz: campo em branco no painel mantém esse conteúdo.

TESTIMONIAL_GROUP = "O que dizem sobre o tempo"

LANDING_TESTIMONIALS = (
    {
        "id": "pp-depo-1",
        "group": TESTIMONIAL_GROUP,
        "label": "Depoimento 1",
        "name": "Nome do cliente",
        "handle": "@usuario",
        "quote": (
            "Eu passava mais tempo escolhendo do que assistindo. "
            "Agora abro, respondo e aperto play."
        ),
        "context": "Texto de exemplo, aguardando depoimento real.",
        "placeholder": True,
    },
    {
        "id": "pp-depo-2",
        "group": TESTIMONIAL_GROUP,
        "label": "Depoimento 2",
        "name": "Nome do cliente",
        "handle": "@usuario",
        "quote": "Três opções resolvem. Dez me travavam.",
        "context": "Texto de exemplo, aguardando depoimento real.",
        "placeholder": True,
    },
    {
        "id": "pp-depo-3",
        "group": TESTIMONIAL_GROUP,
        "label": "Depoimento 3",
        "name": "Nome do cliente",
        "handle": "@usuario",
        "quote": "O que eu marco como já assisti não volta. Isso mudou tudo.",
        "context": "Texto de exemplo, aguardando depoimento real.",
        "placeholder": True,
    },
)

TESTIMONIAL_IDS = tuple(item["id"] for item in LANDING_TESTIMONIALS)

# Campos de texto do depoimento e o tamanho máximo de cada um.
TESTIMONIAL_TEXT_FIELDS = {
    "name": 80,
    "handle": 60,
    "quote": 400,
    "context": 160,
}

# A foto aparece em um círculo de 40 px: não há motivo para subir mais que isso,
# e o teto menor mantém leve a resposta pública que a landing baixa de uma vez.
MAX_AVATAR_CHARS = 24_000


def testimonials() -> list[dict]:
    """Cópia do catálogo de depoimentos, para a API devolver sem expor a tupla."""
    return [dict(item) for item in LANDING_TESTIMONIALS]


def is_testimonial(testimonial_id: str) -> bool:
    return testimonial_id in TESTIMONIAL_IDS


def get_testimonial(testimonial_id: str) -> dict | None:
    for item in LANDING_TESTIMONIALS:
        if item["id"] == testimonial_id:
            return dict(item)
    return None
