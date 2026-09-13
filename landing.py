"""Catálogo dos espaços de imagem da landing page.

A landing (``public/index.html``) é um pacote gerado no Claude Web Design: a
marcação vem com espaços de pôster identificados por ``id``. Este módulo é a
única lista desses espaços; o painel administrativo a recebe pronta pela API e
o script ``public/landing-posters.js`` usa os mesmos ``id`` para aplicar o que
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
