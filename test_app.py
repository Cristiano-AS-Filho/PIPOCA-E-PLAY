import base64
import json
import os
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.parse import parse_qs, urlparse


TEST_STORE = Path(tempfile.gettempdir()) / "pipoca-play-test-users.json"
os.environ["AUTH_SECRET"] = "test-secret"
os.environ["ADMIN_EMAIL"] = "admin@test.local"
os.environ["ADMIN_PASSWORD"] = "admin-password"
os.environ["ENVIRONMENT"] = "test"
os.environ["USER_STORE_FILE"] = str(TEST_STORE)

import billing  # noqa: E402
import router  # noqa: E402
from auth import authenticate, create_session, read_session  # noqa: E402
from plans import public_plans  # noqa: E402
from recommender import (  # noqa: E402
    CONTENT_TYPE_OPTIONS,
    MAX_PLATFORMS_PER_SEARCH,
    RATING_SOURCES,
    build_feedback_section,
)
from metadata import enrich_result  # noqa: E402
from server import (  # noqa: E402
    RECOMMENDATION_SCHEMA,
    buildRecommendationPrompt,
    clean_filters,
    validate_recommendation_payload,
)
import api_core  # noqa: E402
from user_store import (  # noqa: E402
    CreditError,
    StorageError,
    SubscriptionRequired,
    activate_subscription,
    consume_credit,
    get_account,
    list_feedback,
    list_history,
    remove_feedback,
    remove_history,
    set_feedback,
    admin_summary,
    admin_users,
    create_user,
    delete_user,
    get_status_by_token,
    register_user,
    set_user_password,
    storage_diagnostics,
    storage_mode,
    update_user_role,
    update_user_status,
)


# PNG de 1x1 usado onde o teste precisa de bytes de imagem de verdade.
PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

FILTERS = {
    "content_type": "Filme",
    "genre": "Suspense / Thriller",
    "mood": "Sombrio / Assustador",
    "duration": "Livre (Qualquer)",
    "era": "Livre (Qualquer)",
    "platform": "Livre (Qualquer)",
    "companionship": "Sozinho(a)",
    "popularity": "Aclamados pela Crítica / Premiações (Oscar, Cannes)",
}


# ---------------------------------------------------------------------------
# Páginas publicadas
# ---------------------------------------------------------------------------

PUBLIC = Path(__file__).parent / "public"
BUNDLE_MARK = '<script type="__bundler/template">'


def published_document(name):
    """Devolve o documento de uma página do /public.

    A landing e o ambiente logado saem do Claude Web Design empacotados: o HTML
    inteiro viaja como uma string JSON dentro de <script type="__bundler/template">.
    Ler o arquivo cru encontraria o texto escapado, então o teste desempacota
    primeiro e verifica o documento que o navegador realmente monta.
    """
    text = (PUBLIC / name).read_text(encoding="utf-8")
    if BUNDLE_MARK not in text:
        return text
    start = text.index(BUNDLE_MARK)
    opening = text.index('"', start + len(BUNDLE_MARK))
    closing = text.rindex('"', opening, text.index("</script>", opening)) + 1
    return json.loads(text[opening:closing])


class PipocaPlayTests(unittest.TestCase):
    def setUp(self):
        TEST_STORE.unlink(missing_ok=True)

    def tearDown(self):
        TEST_STORE.unlink(missing_ok=True)

    def test_clean_filters_preserves_all_eight_dimensions(self):
        cleaned = clean_filters(FILTERS)
        self.assertEqual(cleaned, FILTERS)
        self.assertEqual(len(cleaned), 8)

    def test_content_type_accepts_only_the_three_official_answers(self):
        self.assertEqual(CONTENT_TYPE_OPTIONS, ("Filme", "Série", "Mesclar (filmes e séries)"))
        for answer in CONTENT_TYPE_OPTIONS:
            self.assertEqual(clean_filters({**FILTERS, "content_type": answer})["content_type"], answer)
        with self.assertRaises(ValueError):
            clean_filters({**FILTERS, "content_type": "Novela mexicana"})
        missing = {key: value for key, value in FILTERS.items() if key != "content_type"}
        with self.assertRaises(ValueError):
            clean_filters(missing)

    def test_prompt_states_the_requested_production_type(self):
        prompt = buildRecommendationPrompt({**FILTERS, "content_type": "Mesclar (filmes e séries)"})
        self.assertIn("Tipo de produção: Mesclar (filmes e séries)", prompt)
        self.assertIn("pelo menos um filme e pelo menos uma série", prompt)
        self.assertIn("Marque cada indicação em content_type", prompt)
        series_prompt = buildRecommendationPrompt({**FILTERS, "content_type": "Série"})
        self.assertIn("Tipo de produção: Série", series_prompt)

    def test_schema_requires_the_format_of_each_recommendation(self):
        item_schema = RECOMMENDATION_SCHEMA["properties"]["recommendations"]["items"]
        self.assertIn("content_type", item_schema["required"])
        self.assertIn("seasons", item_schema["required"])
        self.assertEqual(item_schema["properties"]["content_type"]["enum"], ["filme", "serie"])

    def test_invalid_filter_value_is_rejected(self):
        invalid = {**FILTERS, "genre": "Qualquer valor inventado"}
        with self.assertRaises(ValueError):
            clean_filters(invalid)

    def test_prompt_contains_independent_mood_and_platform(self):
        prompt = buildRecommendationPrompt(FILTERS)
        self.assertIn("Vibe/clima emocional desejado: Sombrio / Assustador", prompt)
        self.assertIn("Plataforma de streaming marcada: Livre (Qualquer)", prompt)

    def test_platform_question_accepts_more_than_one_streaming(self):
        cleaned = clean_filters({**FILTERS, "platform": ["Netflix", "Disney+", "Max (HBO)"]})
        self.assertEqual(cleaned["platform"], "Netflix, Disney+, Max (HBO)")
        # O mesmo conjunto também chega como texto quando vem do histórico salvo.
        from_text = clean_filters({**FILTERS, "platform": "Netflix, Disney+, Max (HBO)"})
        self.assertEqual(from_text, cleaned)

    def test_platform_multi_choice_drops_duplicates_and_blanks(self):
        cleaned = clean_filters({**FILTERS, "platform": ["Netflix", " Netflix ", "", "Disney+"]})
        self.assertEqual(cleaned["platform"], "Netflix, Disney+")

    def test_any_platform_wins_over_the_other_marks(self):
        cleaned = clean_filters({**FILTERS, "platform": ["Netflix", "Livre (Qualquer)"]})
        self.assertEqual(cleaned["platform"], "Livre (Qualquer)")

    def test_platform_multi_choice_still_rejects_invented_services(self):
        with self.assertRaises(ValueError):
            clean_filters({**FILTERS, "platform": ["Netflix", "Streaming do vizinho"]})
        with self.assertRaises(ValueError):
            clean_filters({**FILTERS, "platform": []})
        with self.assertRaises(ValueError):
            clean_filters({**FILTERS, "platform": ""})

    def test_platform_multi_choice_has_a_ceiling(self):
        too_many = [
            "Netflix", "Disney+", "Max (HBO)", "Apple TV+", "Paramount+", "Amazon Prime Video",
        ]
        self.assertGreater(len(too_many), MAX_PLATFORMS_PER_SEARCH)
        with self.assertRaises(ValueError):
            clean_filters({**FILTERS, "platform": too_many})

    def test_prompt_tells_the_engine_to_honour_every_marked_platform(self):
        prompt = buildRecommendationPrompt({**FILTERS, "platform": "Netflix, Disney+"})
        self.assertIn("Plataforma de streaming marcada: Netflix, Disney+", prompt)
        self.assertIn("marcou 2 plataformas ao mesmo tempo (Netflix, Disney+)", prompt)
        self.assertIn("pelo menos uma dessas", prompt)
        single = buildRecommendationPrompt({**FILTERS, "platform": "Netflix"})
        self.assertIn("marcou uma única plataforma (Netflix)", single)
        self.assertNotIn("plataformas ao mesmo tempo", single)

    def test_ratings_cover_the_published_review_platforms(self):
        ratings = RECOMMENDATION_SCHEMA["properties"]["recommendations"]["items"]["properties"]["ratings"]
        for source in ("imdb", "rotten_tomatoes_critics", "google_users", "mercado_livre_filmes"):
            self.assertIn(source, ratings["required"])
        # O modo estrito da API exige toda propriedade declarada em required.
        self.assertEqual(sorted(ratings["required"]), sorted(ratings["properties"]))
        # O catálogo de fontes é a única origem do schema, do prompt e da tela.
        self.assertEqual(ratings["required"], [source["key"] for source in RATING_SOURCES])
        self.assertEqual(ratings["properties"]["imdb"]["maximum"], 10)
        self.assertEqual(ratings["properties"]["google_users"]["maximum"], 100)
        self.assertEqual(ratings["properties"]["mercado_livre_filmes"]["maximum"], 5)

    def test_prompt_asks_the_engine_for_the_poster_image_address(self):
        """O pôster é coletado da própria resposta do motor: o prompt pede o link."""
        prompt = buildRecommendationPrompt(FILTERS)
        self.assertIn("Preencha poster_url com o link direto e público do arquivo de imagem", prompt)
        self.assertIn("sempre em https", prompt)
        self.assertIn("nunca uma página HTML", prompt)
        # O servidor confere o link, então o modelo não precisa se calar na dúvida.
        self.assertIn("em vez de deixar o campo vazio", prompt)
        self.assertIn("poster_url", RECOMMENDATION_SCHEMA["properties"]["recommendations"]["items"]["required"])

    def test_prompt_lists_every_rating_source_with_its_own_scale(self):
        prompt = buildRecommendationPrompt(FILTERS)
        for label, scale in (("IMDb", 10), ("Google (% de usuários que gostaram)", 100), ("Mercado Livre Filmes", 5)):
            self.assertIn(f"{label} de 0 a {scale}", prompt)
        self.assertIn("Use 0 em toda fonte que você não souber com segurança", prompt)

    def test_ratings_pass_through_unchanged_without_an_external_catalog(self):
        """Sem TMDB (removido do projeto), a nota que o motor deu é a que fica."""
        result = {"recommendations": [{"title_original": "Example", "ratings": {"imdb": 8.1, "tmdb": 6.5}}]}
        with patch("metadata._itunes_poster", return_value=""), \
             patch("metadata._wikipedia_poster", return_value=""), \
             patch("metadata._generate_poster_image", return_value=""):
            enriched = enrich_result(result)
        self.assertEqual(enriched["recommendations"][0]["ratings"], {"imdb": 8.1, "tmdb": 6.5})

    def test_engine_poster_is_used_when_it_looks_like_a_real_image(self):
        """1ª tentativa: o link que o próprio motor (ChatGPT) indicou."""
        result = {"recommendations": [{
            "title_original": "Example",
            "poster_url": "https://image.tmdb.org/t/p/w500/abc123.jpg",
        }]}
        with patch("metadata._image_responds", return_value=True) as probe, \
             patch("metadata._wikipedia_poster") as wiki, \
             patch("metadata._generate_poster_image") as gen:
            enriched = enrich_result(result)
        item = enriched["recommendations"][0]
        self.assertEqual(item["poster_url"], "https://image.tmdb.org/t/p/w500/abc123.jpg")
        self.assertEqual(item["poster_source"], "model")
        # A coleta confirma o endereço antes de aceitá-lo como pôster.
        self.assertEqual(probe.call_args[0][0], "https://image.tmdb.org/t/p/w500/abc123.jpg")
        wiki.assert_not_called()
        gen.assert_not_called()

    def test_engine_poster_that_does_not_answer_gives_way_to_the_next_source(self):
        """O link do motor tem forma de imagem mas está morto: a coleta segue.

        Sem esta checagem no servidor, um link inventado vencia as fontes que
        funcionam e o cartão do usuário ficava sem pôster nenhum.
        """
        result = {"recommendations": [{
            "title_original": "Example",
            "poster_url": "https://image.tmdb.org/t/p/w500/inventado.jpg",
        }]}
        with patch("metadata._image_responds", return_value=False), \
             patch("metadata._itunes_poster", return_value="https://is1-ssl.mzstatic.com/a/600x900bb.jpg"), \
             patch("metadata._wikipedia_poster") as wiki, \
             patch("metadata._generate_poster_image") as gen:
            enriched = enrich_result(result)
        item = enriched["recommendations"][0]
        self.assertEqual(item["poster_url"], "https://is1-ssl.mzstatic.com/a/600x900bb.jpg")
        self.assertEqual(item["poster_source"], "itunes")
        wiki.assert_not_called()
        gen.assert_not_called()

    def test_itunes_art_is_preferred_over_wikipedia(self):
        """A arte oficial da loja vem antes da imagem do verbete."""
        result = {"recommendations": [{"title_original": "Example", "poster_url": ""}]}
        with patch("metadata._itunes_poster", return_value="https://is1-ssl.mzstatic.com/a/600x900bb.jpg"), \
             patch("metadata._wikipedia_poster") as wiki, \
             patch("metadata._generate_poster_image") as gen:
            enriched = enrich_result(result)
        item = enriched["recommendations"][0]
        self.assertEqual(item["poster_source"], "itunes")
        wiki.assert_not_called()
        gen.assert_not_called()

    def test_engine_poster_in_plain_http_is_collected_as_https(self):
        """A página é servida por HTTPS: em http:// o navegador bloquearia a imagem."""
        result = {"recommendations": [{
            "title_original": "Example",
            "poster_url": "http://image.tmdb.org/t/p/w500/abc123.jpg",
        }]}
        with patch("metadata._image_responds", return_value=True), \
             patch("metadata._wikipedia_poster") as wiki, \
             patch("metadata._generate_poster_image") as gen:
            enriched = enrich_result(result)
        item = enriched["recommendations"][0]
        self.assertEqual(item["poster_url"], "https://image.tmdb.org/t/p/w500/abc123.jpg")
        self.assertEqual(item["poster_source"], "model")
        wiki.assert_not_called()
        gen.assert_not_called()

    def test_engine_poster_is_accepted_on_an_image_host_without_extension(self):
        """Endereço de CDN de imagem com parâmetros continua sendo um pôster."""
        import metadata as metadata_module

        self.assertEqual(
            metadata_module._looks_like_image_url("https://image.tmdb.org/t/p/w500/abc123?size=500"),
            "https://image.tmdb.org/t/p/w500/abc123?size=500",
        )
        # Fora de um hospedeiro de imagem, sem extensão não é pôster.
        self.assertEqual(metadata_module._looks_like_image_url("https://exemplo.com/filme"), "")

    def test_engine_poster_on_an_internal_address_is_discarded(self):
        """A URL vem do motor: um endereço interno nunca vira requisição do servidor."""
        import metadata as metadata_module

        for url in ("https://localhost/poster.jpg", "https://127.0.0.1/poster.jpg",
                    "https://10.0.0.5/poster.jpg", "https://192.168.0.9/poster.jpg",
                    "https://172.16.3.2/poster.jpg"):
            self.assertEqual(metadata_module._looks_like_image_url(url), "", url)

    def test_engine_poster_embedded_as_data_uri_skips_the_network_check(self):
        result = {"recommendations": [{"title_original": "Example", "poster_url": "data:image/png;base64,abc"}]}
        with patch("metadata._image_responds") as probe, \
             patch("metadata._wikipedia_poster") as wiki, \
             patch("metadata._generate_poster_image") as gen:
            enriched = enrich_result(result)
        item = enriched["recommendations"][0]
        self.assertEqual(item["poster_url"], "data:image/png;base64,abc")
        self.assertEqual(item["poster_source"], "model")
        probe.assert_not_called()
        wiki.assert_not_called()
        gen.assert_not_called()

    def test_wikipedia_poster_is_used_when_the_earlier_sources_fail(self):
        """3ª tentativa: uma imagem real e gratuita da Wikipedia."""
        result = {"recommendations": [{"title_original": "Example", "poster_url": "não sei o link"}]}
        with patch("metadata._itunes_poster", return_value=""), \
             patch("metadata._wikipedia_poster", return_value="https://upload.wikimedia.org/real.jpg"), \
             patch("metadata._generate_poster_image") as gen:
            enriched = enrich_result(result)
        item = enriched["recommendations"][0]
        self.assertEqual(item["poster_url"], "https://upload.wikimedia.org/real.jpg")
        self.assertEqual(item["poster_source"], "wikipedia")
        gen.assert_not_called()

    def test_generated_poster_is_the_last_resort(self):
        """3ª tentativa: nunca fica sem imagem — gera uma capa ilustrativa por IA."""
        result = {"recommendations": [{"title_original": "Example", "poster_url": ""}]}
        with patch("metadata._itunes_poster", return_value=""), \
             patch("metadata._wikipedia_poster", return_value=""), \
             patch("metadata._generate_poster_image", return_value="data:image/png;base64,abc"):
            enriched = enrich_result(result)
        item = enriched["recommendations"][0]
        self.assertEqual(item["poster_url"], "data:image/png;base64,abc")
        self.assertEqual(item["poster_source"], "generated")

    def test_poster_ends_up_empty_only_when_every_source_fails(self):
        result = {"recommendations": [{"title_original": "Example", "poster_url": ""}]}
        with patch("metadata._itunes_poster", return_value=""), \
             patch("metadata._wikipedia_poster", return_value=""), \
             patch("metadata._generate_poster_image", return_value=""):
            enriched = enrich_result(result)
        item = enriched["recommendations"][0]
        self.assertEqual(item["poster_url"], "")
        self.assertEqual(item["poster_source"], "")

    def test_poster_collection_stops_when_the_request_deadline_has_passed(self):
        """A coleta do pôster nunca pode estourar o tempo da função e derrubar
        a recomendação que o usuário já pagou."""
        import metadata as metadata_module

        result = {"recommendations": [{
            "title_original": "Example",
            "poster_url": "https://image.tmdb.org/t/p/w500/abc123.jpg",
        }]}
        with patch("metadata.urllib.request.urlopen") as urlopen:
            enriched = enrich_result(result, deadline=time.monotonic() - 1)
        item = enriched["recommendations"][0]
        self.assertEqual(item["poster_url"], "")
        self.assertEqual(item["poster_source"], "")
        # Nenhuma das três fontes chega a abrir conexão fora do prazo.
        urlopen.assert_not_called()
        self.assertEqual(metadata_module._budget(time.monotonic() - 1, 5), 0.0)

    def test_the_three_posters_are_collected_in_parallel(self):
        """Três esperas de rede em sequência não cabem no tempo da função."""
        running = []
        started = threading.Event()

        def slow_wikipedia(*args, **kwargs):
            running.append(1)
            if len(running) == 3:
                started.set()
            # Só devolve depois que as três indicações estiverem em voo.
            started.wait(timeout=5)
            return "https://upload.wikimedia.org/real.jpg"

        result = {"recommendations": [{"title_original": f"Example {i}", "poster_url": ""} for i in range(3)]}
        with patch("metadata._itunes_poster", return_value=""), \
             patch("metadata._wikipedia_poster", side_effect=slow_wikipedia), \
             patch("metadata._generate_poster_image") as gen:
            enriched = enrich_result(result)
        self.assertTrue(started.is_set(), "as três indicações deveriam ser resolvidas ao mesmo tempo")
        for item in enriched["recommendations"]:
            self.assertEqual(item["poster_source"], "wikipedia")
        gen.assert_not_called()

    def test_a_failing_poster_never_brings_down_the_recommendation(self):
        result = {"recommendations": [{"title_original": "Example", "poster_url": ""}]}
        with patch("metadata._itunes_poster", return_value=""), \
             patch("metadata._wikipedia_poster", side_effect=RuntimeError("rede caiu")):
            enriched = enrich_result(result)
        item = enriched["recommendations"][0]
        self.assertEqual(item["poster_url"], "")
        self.assertEqual(item["poster_source"], "")

    def test_validate_requires_exactly_three_ordered_recommendations(self):
        item = {
            "rank": 1,
            "title_original": "Example",
            "title_pt": "Exemplo",
            "year": 2020,
            "runtime_minutes": 100,
            "genres": ["Drama"],
            "synopsis": "Sinopse",
            "why_it_matches": "Combina",
            "match_score": 90,
        }
        payload = {"recommendations": [item, {**item, "rank": 2}, {**item, "rank": 3}]}
        validated = validate_recommendation_payload(payload)
        self.assertEqual(validated, payload)
        # Sem content_type declarado, a indicação é tratada como filme.
        self.assertEqual([entry["content_type"] for entry in validated["recommendations"]], ["filme"] * 3)
        serie = validate_recommendation_payload(
            {"recommendations": [{**item, "content_type": "serie"}, {**item, "rank": 2}, {**item, "rank": 3}]}
        )
        self.assertEqual(serie["recommendations"][0]["content_type"], "serie")
        with self.assertRaises(RuntimeError):
            validate_recommendation_payload({"recommendations": [item]})

    def test_registration_starts_pending_and_login_is_blocked(self):
        record, token, reopened = register_user("User@Test.Local", "user-password")
        self.assertFalse(reopened)
        self.assertEqual(record["status"], "pending")
        self.assertEqual(get_status_by_token("user@test.local", token), "pending")
        self.assertIsNone(authenticate("user@test.local", "user-password"))
        self.assertEqual(
            admin_summary(),
            {"total": 1, "pending": 1, "approved": 0, "rejected": 0, "admins": 0, "subscribers": 0},
        )

    def test_admin_can_approve_reject_and_delete_user(self):
        record, token, _ = register_user("user@test.local", "user-password")
        approved = update_user_status(record["id"], "approved")
        self.assertEqual(approved["status"], "approved")
        user = authenticate("user@test.local", "user-password")
        self.assertEqual(user["role"], "user")
        cookie = "pipoca_session=" + create_session(user["email"], user["role"], user["user_id"])
        self.assertEqual(read_session(cookie)["role"], "user")
        self.assertEqual(get_status_by_token("user@test.local", token), "approved")

        rejected = update_user_status(record["id"], "rejected")
        self.assertEqual(rejected["status"], "rejected")
        self.assertIsNone(authenticate("user@test.local", "user-password"))
        delete_user(record["id"])
        self.assertEqual(admin_users(), [])

    def test_duplicate_pending_registration_is_rejected(self):
        register_user("user@test.local", "user-password")
        with self.assertRaises(FileExistsError):
            register_user("USER@test.local", "another-password")

    def test_vercel_blob_is_used_when_token_is_configured(self):
        blob_payload = b"[]"
        calls = []

        class FakeBlobNotFoundError(Exception):
            pass

        class FakeBlobResult:
            def __bytes__(self):
                return blob_payload

        def fake_get(path, **kwargs):
            calls.append(("get", path, kwargs))
            return FakeBlobResult()

        def fake_put(path, body, **kwargs):
            nonlocal blob_payload
            calls.append(("put", path, body, kwargs))
            blob_payload = body

        with patch.dict(os.environ, {"BLOB_READ_WRITE_TOKEN": "test-blob-token"}, clear=False):
            with patch("user_store._blob_sdk", return_value=(fake_get, fake_put, FakeBlobNotFoundError)):
                record, _, _ = register_user("blob@test.local", "blob-password")
                self.assertEqual(record["status"], "pending")
                self.assertEqual(storage_mode(), "vercel-blob")
                self.assertEqual([call[0] for call in calls], ["get", "put"])
                self.assertEqual(json.loads(blob_payload.decode("utf-8"))[0]["email"], "blob@test.local")

    def test_vercel_oidc_blob_fallback_uses_store_header(self):
        blob_payload = b"[]"
        requests = []

        class FakeResponse:
            def __init__(self, body=b""):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return self.body

        def fake_urlopen(request, timeout):
            nonlocal blob_payload
            requests.append(request)
            if request.get_method() == "PUT":
                blob_payload = request.data
                return FakeResponse()
            return FakeResponse(blob_payload)

        with patch.dict(
            os.environ,
            {
                "BLOB_READ_WRITE_TOKEN": "",
                "VERCEL_OIDC_TOKEN": "test-oidc-token",
                "BLOB_STORE_ID": "store_test-store",
            },
            clear=False,
        ):
            with patch("user_store.urllib.request.urlopen", side_effect=fake_urlopen):
                record, _, _ = register_user("oidc@test.local", "oidc-password")
                self.assertEqual(record["status"], "pending")
                self.assertEqual(storage_mode(), "vercel-blob")
                self.assertEqual([request.get_method() for request in requests], ["GET", "PUT"])
                self.assertEqual(
                    requests[1].headers["X-vercel-blob-store-id"],
                    "test-store",
                )
                self.assertEqual(json.loads(blob_payload.decode("utf-8"))[0]["email"], "oidc@test.local")

    def test_signed_admin_session_and_unknown_user(self):
        admin = authenticate("admin@test.local", "admin-password")
        self.assertEqual(admin["role"], "admin")
        cookie = "pipoca_session=" + create_session(admin["email"], admin["role"])
        self.assertEqual(read_session(cookie)["role"], "admin")
        self.assertIsNone(authenticate("nobody@test.local", "wrong"))


    # ------------------------------------------------------------------
    # Armazenamento
    # ------------------------------------------------------------------

    def test_postgres_backend_is_preferred_and_round_trips(self):
        stored = {}
        statements = []

        class FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, statement, params=None):
                statements.append(statement.split()[0].upper())
                self._row = None
                if statement.lstrip().upper().startswith("SELECT"):
                    value = stored.get(params[0])
                    self._row = (value,) if value is not None else None
                elif statement.lstrip().upper().startswith("INSERT"):
                    stored[params[0]] = params[1]

            def fetchone(self):
                return self._row

        class FakeConnection:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def cursor(self):
                return FakeCursor()

        class FakeDriver:
            @staticmethod
            def connect(dsn, **kwargs):
                return FakeConnection()

        with patch.dict(os.environ, {"DATABASE_URL": "postgres://user:pass@host/db"}, clear=False):
            with patch("user_store._postgres_driver", return_value=FakeDriver):
                self.assertEqual(storage_mode(), "postgres")
                record, _, _ = register_user("pg@test.local", "pg-password-123")
                self.assertEqual(record["status"], "pending")
                self.assertEqual(admin_summary()["pending"], 1)
                self.assertEqual(json.loads(stored["pipoca-play:users"])[0]["email"], "pg@test.local")
                self.assertIn("CREATE", statements)

    def test_health_reveals_the_postgres_source_and_a_password_free_preview(self):
        dsn = "postgresql://postgres.abc:senha-secreta@ep-example.neon.tech/neondb?sslmode=require"
        with patch.dict(os.environ, {"DATABASE_URL": dsn}, clear=False):
            status, payload, _ = api_core.health()
            self.assertEqual(payload["storage"]["postgres_source_env_var"], "DATABASE_URL")
            preview = payload["storage"]["postgres_dsn_preview"]
            self.assertNotIn("senha-secreta", preview)
            self.assertIn("ep-example.neon.tech", preview)
            self.assertIn("sslmode=require", preview)

    def test_postgres_connect_failure_reports_a_safe_technical_detail(self):
        class FakeDriver:
            @staticmethod
            def connect(dsn, **kwargs):
                raise RuntimeError("timeout expired")

        dsn = "postgresql://postgres:senha-secreta-123@ep-example.neon.tech:5432/postgres"
        with patch.dict(os.environ, {"DATABASE_URL": dsn}, clear=False):
            with patch("user_store._postgres_driver", return_value=FakeDriver):
                with self.assertRaises(StorageError) as raised:
                    register_user("neon@test.local", "senha-do-cliente")
                message = str(raised.exception)
                self.assertIn("timeout expired", message)
                self.assertNotIn("senha-secreta-123", message)

    def test_supabase_direct_host_gets_a_pooler_hint(self):
        class FakeDriver:
            @staticmethod
            def connect(dsn, **kwargs):
                raise RuntimeError("connection refused")

        dsn = "postgresql://postgres:secret@db.qfttycqymfpvmcnmqfpx.supabase.co:5432/postgres"
        with patch.dict(os.environ, {"DATABASE_URL": dsn}, clear=False):
            with patch("user_store._postgres_driver", return_value=FakeDriver):
                with self.assertRaises(StorageError) as raised:
                    register_user("supa@test.local", "senha-do-supabase")
                message = str(raised.exception)
                self.assertIn("IPv6", message)
                self.assertIn("Transaction pooler", message)

    def test_postgres_url_without_scheme_is_ignored(self):
        with patch.dict(os.environ, {"DATABASE_URL": "  "}, clear=False):
            self.assertEqual(storage_mode(), "arquivo-local")

    def test_serverless_without_storage_explains_the_setup(self):
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}, clear=False):
            diagnostics = storage_diagnostics()
            self.assertFalse(diagnostics["persistent"])
            self.assertIn("Storage", diagnostics["error"])
            with self.assertRaises(StorageError) as raised:
                register_user("nostore@test.local", "some-password")
            message = str(raised.exception)
            self.assertIn("serverless", message)
            self.assertIn("Storage", message)

    def test_storage_diagnostics_reports_the_active_backend(self):
        diagnostics = storage_diagnostics(probe=True)
        self.assertEqual(diagnostics["mode"], "arquivo-local")
        self.assertTrue(diagnostics["persistent"])
        self.assertTrue(diagnostics["healthy"])

    # ------------------------------------------------------------------
    # Administração
    # ------------------------------------------------------------------

    def test_stored_admin_can_sign_in_and_hold_an_admin_session(self):
        created = create_user("boss@test.local", "boss-password", role="admin")
        self.assertEqual(created["role"], "admin")
        session = authenticate("boss@test.local", "boss-password")
        self.assertEqual(session["role"], "admin")
        cookie = "pipoca_session=" + create_session(session["email"], "admin", session["user_id"])
        self.assertEqual(read_session(cookie)["role"], "admin")

    def test_demoted_admin_loses_the_admin_session(self):
        created = create_user("boss@test.local", "boss-password", role="admin")
        cookie = "pipoca_session=" + create_session("boss@test.local", "admin", created["id"])
        self.assertIsNotNone(read_session(cookie))
        update_user_role(created["id"], "user")
        self.assertIsNone(read_session(cookie))

    def test_password_reset_replaces_the_stored_hash(self):
        created = create_user("client@test.local", "first-password")
        set_user_password(created["id"], "second-password")
        self.assertIsNone(authenticate("client@test.local", "first-password"))
        self.assertEqual(authenticate("client@test.local", "second-password")["role"], "user")

    def test_admin_routes_require_an_admin_session(self):
        for session in (None, {"email": "client@test.local", "role": "user"}):
            status, payload, _ = api_core.admin_overview(session)
            self.assertEqual(status, 403)
            self.assertIn("administrador", payload["error"])
            status, _, _ = api_core.admin_action(session, {"action": "delete", "user_id": "x"})
            self.assertEqual(status, 403)

    def test_admin_action_covers_the_full_lifecycle(self):
        admin = {"email": "admin@test.local", "role": "admin"}
        status, payload, _ = api_core.admin_action(
            admin, {"action": "create", "email": "novo@test.local", "password": "senha-inicial"}
        )
        self.assertEqual(status, 200)
        user_id = payload["user"]["id"]
        self.assertEqual(payload["user"]["status"], "approved")

        status, payload, _ = api_core.admin_action(admin, {"action": "pending", "user_id": user_id})
        self.assertEqual(payload["user"]["status"], "pending")
        status, payload, _ = api_core.admin_action(admin, {"action": "approve", "user_id": user_id})
        self.assertEqual(payload["user"]["status"], "approved")
        status, payload, _ = api_core.admin_action(admin, {"action": "promote", "user_id": user_id})
        self.assertEqual(payload["user"]["role"], "admin")
        self.assertEqual(payload["summary"]["admins"], 1)
        status, payload, _ = api_core.admin_action(admin, {"action": "demote", "user_id": user_id})
        self.assertEqual(payload["user"]["role"], "user")
        status, payload, _ = api_core.admin_action(
            admin, {"action": "set_password", "user_id": user_id, "password": "nova-senha-1"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(authenticate("novo@test.local", "nova-senha-1")["role"], "user")
        status, payload, _ = api_core.admin_action(admin, {"action": "delete", "user_id": user_id})
        self.assertTrue(payload["deleted"])
        self.assertEqual(payload["users"], [])

    def test_admin_action_rejects_unknown_and_duplicated_input(self):
        admin = {"email": "admin@test.local", "role": "admin"}
        status, _, _ = api_core.admin_action(admin, {"action": "inventada", "user_id": "abc"})
        self.assertEqual(status, 400)
        status, _, _ = api_core.admin_action(admin, {"action": "approve", "user_id": "nao-existe"})
        self.assertEqual(status, 404)
        api_core.admin_action(admin, {"action": "create", "email": "dup@test.local", "password": "senha-inicial"})
        status, _, _ = api_core.admin_action(
            admin, {"action": "create", "email": "dup@test.local", "password": "outra-senha"}
        )
        self.assertEqual(status, 409)

    def test_admin_cannot_remove_their_own_access(self):
        created = create_user("boss@test.local", "boss-password", role="admin")
        session = {"email": "boss@test.local", "role": "admin"}
        for action in ("delete", "reject", "pending", "demote"):
            status, payload, _ = api_core.admin_action(
                session, {"action": action, "user_id": created["id"]}
            )
            self.assertEqual(status, 400, action)
            self.assertIn("seu próprio acesso", payload["error"])
        self.assertEqual(authenticate("boss@test.local", "boss-password")["role"], "admin")

    def test_admin_can_still_remove_another_admin(self):
        mine = create_user("boss@test.local", "boss-password", role="admin")
        other = create_user("other@test.local", "other-password", role="admin")
        session = {"email": mine["email"], "role": "admin"}
        status, payload, _ = api_core.admin_action(
            session, {"action": "demote", "user_id": other["id"]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["user"]["role"], "user")

    # ------------------------------------------------------------------
    # Rotas públicas
    # ------------------------------------------------------------------

    def test_health_reports_storage_without_leaking_secrets(self):
        status, payload, _ = api_core.health()
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["admin_panel"], "/admin")
        self.assertTrue(payload["admin_credentials_configured"])
        self.assertEqual(payload["storage"]["mode"], "arquivo-local")
        serialized = json.dumps(payload)
        self.assertNotIn("admin-password", serialized)
        self.assertNotIn("test-secret", serialized)

    def test_login_explains_a_missing_admin_configuration(self):
        with patch.dict(os.environ, {"ADMIN_PASSWORD": "", "ENVIRONMENT": "production"}, clear=False):
            status, payload, _ = api_core.login(
                {"email": "admin@test.local", "password": "qualquer"}, secure=True
            )
            self.assertEqual(status, 503)
            self.assertIn("ADMIN_PASSWORD", payload["error"])

    def test_login_sets_an_httponly_session_cookie(self):
        create_user("client@test.local", "client-password")
        status, payload, headers = api_core.login(
            {"email": "client@test.local", "password": "client-password"}, secure=True
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["user"]["role"], "user")
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("Secure", headers["Set-Cookie"])

    def test_pending_account_cannot_log_in_through_the_route(self):
        register_user("wait@test.local", "wait-password")
        status, payload, _ = api_core.login(
            {"email": "wait@test.local", "password": "wait-password"}, secure=False
        )
        self.assertEqual(status, 403)
        self.assertIn("aguardando", payload["error"])

    def test_generate_poster_image_calls_the_images_endpoint(self):
        """Último recurso do pôster: gera uma capa pela mesma conta da OpenAI."""
        import metadata as metadata_module

        seen = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"data": [{"b64_json": PNG_BASE64}]}).encode("utf-8")

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            seen["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            with patch("metadata.urllib.request.urlopen", fake_urlopen):
                data_uri = metadata_module._generate_poster_image("O Farol", 2019, ["Suspense"], "filme")
        self.assertEqual(seen["url"], "https://api.openai.com/v1/images/generations")
        self.assertEqual(seen["body"]["model"], "gpt-image-1")
        self.assertIn("O Farol", seen["body"]["prompt"])
        # A capa viaja embutida na resposta: é pedida comprimida de propósito.
        self.assertEqual(seen["body"]["output_format"], "jpeg")
        self.assertLessEqual(seen["body"]["output_compression"], 80)
        self.assertEqual(data_uri, "data:image/png;base64," + PNG_BASE64)

    def test_generated_cover_declares_the_real_image_type_and_has_a_ceiling(self):
        """O tipo sai dos bytes, não do formato pedido; e uma capa gigante é
        descartada antes de estourar o corpo da resposta da função."""
        import metadata as metadata_module

        self.assertTrue(metadata_module._data_uri(PNG_BASE64).startswith("data:image/png;base64,"))
        jpeg = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 40).decode()
        self.assertTrue(metadata_module._data_uri(jpeg).startswith("data:image/jpeg;base64,"))
        # Bytes que não são imagem nenhuma não viram pôster.
        self.assertEqual(metadata_module._data_uri(base64.b64encode(b"nao sou imagem").decode()), "")
        with patch.object(metadata_module, "MAX_DATA_URI_LENGTH", 40):
            self.assertEqual(metadata_module._data_uri(PNG_BASE64), "")

    def test_generate_poster_image_is_empty_without_an_openai_key(self):
        import metadata as metadata_module

        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertEqual(metadata_module._generate_poster_image("Título", 2020, [], "filme"), "")

    def test_the_three_titles_that_came_back_without_art_now_get_the_real_poster(self):
        """Regressão do relato do usuário: os três cartões vinham só com a capa
        gerada por IA. Aqui a cadeia inteira roda contra o formato real das
        respostas das APIs públicas — nada do módulo é simulado, só o transporte.
        """
        import metadata as metadata_module

        itunes_br = {
            "De Volta à Ação": {"results": [{
                "trackName": "De Volta à Ação", "releaseDate": "2025-01-17T08:00:00Z",
                "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/v4/volta/source/100x100bb.jpg"}]},
            # A loja publica sem o "4" que o motor colocou no título.
            "Um Tira da Pesada 4: Axel Foley": {"results": [
                {"trackName": "Um Tira da Pesada", "releaseDate": "1984-12-05T08:00:00Z",
                 "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/v4/1984/source/100x100bb.jpg"},
                {"trackName": "Um Tira da Pesada: Axel Foley", "releaseDate": "2024-07-03T07:00:00Z",
                 "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/v4/axel/source/100x100bb.jpg"}]},
            "Lift: Roubo nas Alturas": {"results": []},
        }
        wiki_pt = {"Lift: Roubo nas Alturas filme 2024": {"query": {"pages": [{
            "index": 1, "title": "Lift: Roubo nas Alturas",
            "original": {"source": "https://upload.wikimedia.org/wikipedia/pt/9/9a/Lift.jpg"}}]}}}

        class FakeResponse:
            def __init__(self, body, status=200, content_type="application/json"):
                self._body, self.status = body, status
                self.headers = {"Content-Type": content_type}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(self._body).encode("utf-8")

        def fake_urlopen(request, timeout=None):
            url = request.full_url
            query = parse_qs(urlparse(url).query)
            if url.startswith("https://itunes.apple.com/search"):
                if query.get("country", [""])[0] != "BR":
                    return FakeResponse({"results": []})
                return FakeResponse(itunes_br.get(query.get("term", [""])[0], {"results": []}))
            if "wikipedia.org/w/api.php" in url:
                body = wiki_pt.get(query.get("gsrsearch", [""])[0]) if "pt.wikipedia" in url else None
                return FakeResponse(body or {"query": {"pages": []}})
            if "mzstatic.com" in url or "upload.wikimedia.org" in url:
                return FakeResponse({}, 206, "image/jpeg")
            return FakeResponse({}, 404, "text/html")

        result = {"recommendations": [
            {"rank": 1, "content_type": "filme", "title_original": "Back in Action",
             "title_pt": "De Volta à Ação", "year": 2025, "genres": ["Ação"], "poster_url": ""},
            {"rank": 2, "content_type": "filme", "title_original": "Beverly Hills Cop: Axel F",
             "title_pt": "Um Tira da Pesada 4: Axel Foley", "year": 2024, "genres": ["Ação"], "poster_url": ""},
            {"rank": 3, "content_type": "filme", "title_original": "Lift",
             "title_pt": "Lift: Roubo nas Alturas", "year": 2024, "genres": ["Ação"], "poster_url": ""},
        ]}
        with patch("metadata.urllib.request.urlopen", fake_urlopen), \
             patch("metadata._generate_poster_image") as gen:
            enriched = metadata_module.enrich_result(result)

        items = enriched["recommendations"]
        self.assertEqual([item["poster_source"] for item in items], ["itunes", "itunes", "wikipedia"])
        # Nenhum cartão pode sobrar sem imagem, e nenhum cai na capa gerada.
        self.assertTrue(all(item["poster_url"] for item in items))
        gen.assert_not_called()
        # A miniatura de 100px vira a arte cheia.
        self.assertTrue(items[0]["poster_url"].endswith("/600x900bb.jpg"))
        # O quarto filme não pode trazer a arte do original de 1984.
        self.assertIn("axel", items[1]["poster_url"])

    def test_wikipedia_finds_the_entry_by_search_instead_of_guessing_the_title(self):
        """Adivinhar "Título (filme de 2024)" era o que fazia a Wikipedia errar."""
        import metadata as metadata_module

        asked = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"query": {"pages": [
                    {"index": 1, "title": "O Farol (filme)",
                     "original": {"source": "https://upload.wikimedia.org/poster.jpg"}},
                ]}}).encode("utf-8")

        def fake_urlopen(request, timeout=None):
            asked.append(request.full_url)
            return FakeResponse()

        with patch("metadata.urllib.request.urlopen", fake_urlopen):
            image = metadata_module._wikipedia_poster("The Lighthouse", "O Farol", 2019, "filme")
        self.assertEqual(image, "https://upload.wikimedia.org/poster.jpg")
        # O público é brasileiro: o verbete em português vem primeiro.
        self.assertIn("pt.wikipedia.org", asked[0])
        self.assertIn("generator=search", asked[0])
        self.assertIn("prop=pageimages", asked[0])
        self.assertIn("O+Farol+filme+2019", asked[0])

    def test_wikipedia_falls_back_to_english_and_to_the_thumbnail(self):
        import metadata as metadata_module

        asked = []

        class FakeResponse:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(self.body).encode("utf-8")

        def fake_urlopen(request, timeout=None):
            asked.append(request.full_url)
            if "pt.wikipedia.org" in request.full_url:
                return FakeResponse({"query": {"pages": [{"index": 1, "title": "O Farol"}]}})
            return FakeResponse({"query": {"pages": [
                {"index": 1, "title": "The Lighthouse (film)",
                 "thumbnail": {"source": "https://upload.wikimedia.org/thumb.jpg"}},
            ]}})

        with patch("metadata.urllib.request.urlopen", fake_urlopen):
            image = metadata_module._wikipedia_poster("The Lighthouse", "O Farol", 2019, "filme")
        self.assertEqual(image, "https://upload.wikimedia.org/thumb.jpg")
        self.assertTrue(any("en.wikipedia.org" in url and "The+Lighthouse" in url for url in asked))

    def test_wikipedia_query_asks_for_the_non_free_poster_of_every_result(self):
        """O caso Ripley: verbete e pôster existem, mas a consulta não os pedia.

        ``pageimages`` só devolve imagem de licença livre (``pilicense=free``,
        o padrão) e só para uma página (``pilimit=1``, o padrão). O pôster de um
        filme ou série no verbete é sempre um arquivo de uso justo, então a
        resposta vinha sem imagem nenhuma e o cartão caía na capa gerada.
        """
        import metadata as metadata_module

        asked = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"query": {"pages": [
                    {"index": 1, "title": "Ripley (TV series)",
                     "original": {"source": "https://upload.wikimedia.org/wikipedia/en/ripley.jpg"}},
                ]}}).encode("utf-8")

        def fake_urlopen(request, timeout=None):
            asked.append(request.full_url)
            return FakeResponse()

        with patch("metadata.urllib.request.urlopen", fake_urlopen):
            image = metadata_module._wikipedia_poster("Ripley", "Ripley", 2024, "serie")
        self.assertEqual(image, "https://upload.wikimedia.org/wikipedia/en/ripley.jpg")
        for url in asked:
            self.assertIn("pilicense=any", url)
            self.assertIn("pilimit=3", url)

    def test_wikipedia_prefers_the_entry_with_the_very_same_name(self):
        """"Ripley Under Ground" começa igual; o verbete de mesmo nome ganha."""
        import metadata as metadata_module

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"query": {"pages": [
                    {"index": 1, "title": "Ripley Under Ground",
                     "original": {"source": "https://upload.wikimedia.org/outro-livro.jpg"}},
                    {"index": 2, "title": "Ripley (TV series)",
                     "original": {"source": "https://upload.wikimedia.org/wikipedia/en/ripley.jpg"}},
                ]}}).encode("utf-8")

        with patch("metadata.urllib.request.urlopen", lambda request, timeout=None: FakeResponse()):
            image = metadata_module._wikipedia_poster("Ripley", "Ripley", 2024, "serie")
        self.assertEqual(image, "https://upload.wikimedia.org/wikipedia/en/ripley.jpg")

    def test_wikipedia_searches_use_the_series_wording_for_series(self):
        import metadata as metadata_module

        searches = metadata_module._wikipedia_searches("Dark", "Dark", 2017, "serie")
        self.assertEqual(searches[0], ("pt", "Dark série de televisão 2017", "Dark"))
        self.assertIn(("en", "Dark television series 2017", "Dark"), searches)

    def test_wikipedia_ignores_an_entry_about_a_different_title(self):
        """A busca sempre devolve algo: o pôster de outro filme é pior que nenhum."""
        import metadata as metadata_module

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"query": {"pages": [
                    {"index": 1, "title": "Farol de Alexandria",
                     "original": {"source": "https://upload.wikimedia.org/outro.jpg"}},
                ]}}).encode("utf-8")

        with patch("metadata.urllib.request.urlopen", lambda request, timeout=None: FakeResponse()):
            self.assertEqual(metadata_module._wikipedia_poster("The Lighthouse", "O Farol", 2019, "filme"), "")
        # Um verbete com sufixo entre parênteses continua sendo o mesmo título.
        self.assertTrue(metadata_module._entry_matches("O Farol (filme de 2019)", "O Farol"))
        self.assertFalse(metadata_module._entry_matches("Farol de Alexandria", "O Farol"))

    def test_itunes_search_takes_the_art_of_the_matching_title_and_year(self):
        """A loja da Apple é a fonte de arte oficial sem chave."""
        import metadata as metadata_module

        asked = []
        catalog = {"results": [
            {"trackName": "Outro Filme", "releaseDate": "2024-01-01",
             "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/x/source/100x100bb.jpg"},
            {"trackName": "De Volta à Ação", "releaseDate": "2025-01-17",
             "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/certo/source/100x100bb.jpg"},
        ]}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(catalog).encode("utf-8")

        def fake_urlopen(request, timeout=None):
            asked.append(request.full_url)
            return FakeResponse()

        with patch("metadata.urllib.request.urlopen", fake_urlopen), \
             patch("metadata._image_responds", return_value=True):
            art = metadata_module._itunes_poster("Back in Action", "De Volta à Ação", 2025, "filme")
        self.assertEqual(art, "https://is1-ssl.mzstatic.com/image/thumb/certo/source/600x900bb.jpg")
        self.assertIn("itunes.apple.com/search", asked[0])
        self.assertIn("country=BR", asked[0])
        self.assertIn("media=movie", asked[0])

    def test_itunes_search_asks_for_seasons_when_the_title_is_a_series(self):
        import metadata as metadata_module

        asked = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"results": []}).encode("utf-8")

        def fake_urlopen(request, timeout=None):
            asked.append(request.full_url)
            return FakeResponse()

        with patch("metadata.urllib.request.urlopen", fake_urlopen):
            self.assertEqual(metadata_module._itunes_poster("Dark", "Dark", 2017, "serie"), "")
        self.assertIn("media=tvShow", asked[0])
        self.assertIn("entity=tvSeason", asked[0])

    def test_itunes_never_returns_the_art_of_a_different_title(self):
        """Um pôster do filme errado é pior que nenhum."""
        import metadata as metadata_module

        catalog = {"results": [
            {"trackName": "Outro Filme Qualquer", "releaseDate": "2024-01-01",
             "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/x/source/100x100bb.jpg"},
        ]}
        self.assertEqual(metadata_module._pick_itunes_result(catalog, "De Volta à Ação", 2025), "")
        # Acento, caixa e subtítulo não impedem a correspondência.
        combinado = {"results": [
            {"trackName": "Um Tira da Pesada: Axel Foley", "releaseDate": "2024-07-03",
             "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/ok/source/100x100bb.jpg"},
        ]}
        self.assertTrue(metadata_module._pick_itunes_result(combinado, "Um Tira da Pesada", 2024))

    def test_itunes_keeps_the_original_art_when_the_bigger_size_is_missing(self):
        """O tamanho ampliado é reescrita nossa: sem resposta, vale a miniatura."""
        import metadata as metadata_module

        small = "https://is1-ssl.mzstatic.com/image/thumb/x/source/100x100bb.jpg"
        with patch("metadata._image_responds", return_value=False):
            self.assertEqual(metadata_module._itunes_full_size(small), small)

    def test_image_probe_accepts_only_an_answer_that_is_really_an_image(self):
        """A confirmação do link do motor olha o tipo de conteúdo, não a extensão."""
        import metadata as metadata_module

        seen = {}

        class FakeResponse:
            def __init__(self, status, content_type):
                self.status = status
                self.headers = {"Content-Type": content_type}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def probe(status, content_type):
            def fake_urlopen(request, timeout=None):
                seen["headers"] = dict(request.headers)
                seen["timeout"] = timeout
                return FakeResponse(status, content_type)

            with patch("metadata.urllib.request.urlopen", fake_urlopen):
                return metadata_module._image_responds("https://image.tmdb.org/t/p/w500/abc.jpg", 5)

        self.assertTrue(probe(206, "image/jpeg"))
        self.assertTrue(probe(200, "image/webp; charset=binary"))
        # Uma página HTML respondendo em vez da imagem não vira pôster.
        self.assertFalse(probe(200, "text/html; charset=utf-8"))
        self.assertFalse(probe(404, "image/jpeg"))
        # Só o primeiro byte é pedido: a imagem não é baixada no servidor.
        self.assertEqual(seen["headers"].get("Range"), "bytes=0-0")
        self.assertEqual(seen["timeout"], 5)

    def test_image_probe_answers_no_when_the_address_fails(self):
        import metadata as metadata_module

        def boom(request, timeout=None):
            raise OSError("host inexistente")

        with patch("metadata.urllib.request.urlopen", boom):
            self.assertFalse(metadata_module._image_responds("https://image.tmdb.org/x.jpg", 5))
        # Fora do prazo nem chega a tentar.
        self.assertFalse(metadata_module._image_responds("https://image.tmdb.org/x.jpg", 0))

    # -----------------------------------------------------------------
    # Roteador único (uma função serverless serve todo o /api)
    # -----------------------------------------------------------------

    def test_router_answers_every_published_route(self):
        """Cada rota da documentação precisa existir no roteador único."""
        routes = [
            ("GET", "/api/health"),
            ("GET", "/api/plans"),
            ("POST", "/api/auth/register"),
            ("POST", "/api/auth/login"),
            ("POST", "/api/auth/logout"),
            ("GET", "/api/auth/me"),
            ("GET", "/api/auth/status"),
            ("GET", "/api/admin/status"),
            ("GET", "/api/admin/users"),
            ("POST", "/api/admin/users"),
            ("GET", "/api/account"),
            ("GET", "/api/feedback"),
            ("POST", "/api/feedback"),
            ("DELETE", "/api/feedback"),
            ("GET", "/api/history"),
            ("POST", "/api/history"),
            ("DELETE", "/api/history"),
            ("POST", "/api/billing/checkout"),
            ("GET", "/api/billing/status"),
            ("GET", "/api/billing/webhook"),
            ("POST", "/api/billing/webhook"),
            ("POST", "/api/recommend"),
        ]
        for method, path in routes:
            status, payload, _ = router.handle(method, path, {}, {}, {})
            # Só o 404 de roteamento carrega a chave "path"; um 404 de regra de
            # negócio (pedido inexistente, por exemplo) significa que a rota existe.
            self.assertNotIn("path", payload, f"{method} {path} não está roteado")
            self.assertNotEqual(status, 405, f"{method} {path} recusou o próprio método")

    def test_router_rejects_unknown_paths_and_wrong_methods(self):
        status, payload, _ = router.handle("GET", "/api/inventada", {}, {}, {})
        self.assertEqual(status, 404)
        self.assertEqual(payload["path"], "/api/inventada")
        self.assertEqual(router.handle("GET", "/api/recommend", {}, {}, {})[0], 405)
        self.assertEqual(router.handle("POST", "/api/health", {}, {}, {})[0], 405)
        self.assertEqual(router.handle("GET", "/nao-e-api", {}, {}, {})[0], 404)

    def test_router_ignores_the_trailing_slash(self):
        self.assertEqual(router.handle("GET", "/api/plans/", {}, {}, {})[0], 200)

    def test_router_reads_the_session_from_the_cookie(self):
        record = create_user("router@test.local", "client-password")
        cookie = "pipoca_session=" + create_session(record["email"], "user", record["id"])
        status, payload, _ = router.handle("GET", "/api/auth/me", {}, {}, {"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertTrue(payload["authenticated"])
        self.assertEqual(payload["user"]["email"], "router@test.local")
        anonymous = router.handle("GET", "/api/auth/me", {}, {}, {})
        self.assertFalse(anonymous[1]["authenticated"])

    def test_router_reads_the_query_string_of_the_registration_status(self):
        record, token, _ = register_user("query@test.local", "client-password")
        status, payload, _ = router.handle(
            "GET", "/api/auth/status", {"email": ["query@test.local"], "token": [token]}, {}, {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "pending")

    def test_deploy_stays_within_the_serverless_function_limit(self):
        """O plano Hobby da Vercel aceita no máximo 12 funções por deploy."""
        functions = sorted(Path(__file__).parent.glob("api/**/*.py"))
        self.assertEqual([item.name for item in functions], ["index.py"])
        config = json.loads((Path(__file__).parent / "vercel.json").read_text(encoding="utf-8"))
        self.assertEqual(config["rewrites"][0]["destination"], "/api/index")

    # -----------------------------------------------------------------
    # Assinatura, créditos diários e marcações
    # -----------------------------------------------------------------

    def _client_session(self, plan=""):
        """Cria um cliente aprovado e devolve (sessão, id da conta)."""
        record = create_user("plan@test.local", "client-password")
        if plan:
            activate_subscription(record["id"], plan, "pay_test", "TEST")
        return {"email": record["email"], "role": "user", "user_id": record["id"]}, record["id"]

    def test_plans_catalog_has_the_three_published_prices(self):
        catalog = {plan["id"]: plan for plan in public_plans()}
        self.assertEqual(catalog["silver"]["price"], 10.00)
        self.assertEqual(catalog["silver"]["price_label"], "R$ 10,00")
        self.assertEqual(catalog["silver"]["daily_credits"], 2)
        self.assertEqual(catalog["gold"]["price"], 15.00)
        self.assertEqual(catalog["gold"]["price_label"], "R$ 15,00")
        self.assertEqual(catalog["gold"]["daily_credits"], 5)
        self.assertEqual(catalog["diamante"]["price"], 20.00)
        self.assertEqual(catalog["diamante"]["price_label"], "R$ 20,00")
        self.assertTrue(catalog["diamante"]["unlimited"])

    def test_recommendation_is_blocked_without_a_confirmed_payment(self):
        session, _ = self._client_session()
        status, payload, _ = api_core.recommend(session, {"filters": FILTERS})
        self.assertEqual(status, 402)
        self.assertEqual(payload["code"], "subscription_required")
        self.assertEqual(len(payload["plans"]), 3)

    def test_silver_plan_spends_two_daily_credits_and_then_stops(self):
        _, user_id = self._client_session("silver")
        self.assertEqual(consume_credit(user_id)["credits_remaining"], 1)
        self.assertEqual(consume_credit(user_id)["credits_remaining"], 0)
        with self.assertRaises(CreditError):
            consume_credit(user_id)

    def test_diamante_plan_never_runs_out_of_credits(self):
        _, user_id = self._client_session("diamante")
        for _ in range(12):
            snapshot = consume_credit(user_id)
        self.assertTrue(snapshot["unlimited"])
        self.assertEqual(snapshot["credits_remaining"], -1)

    def test_credits_reset_on_the_next_day_in_brasilia(self):
        _, user_id = self._client_session("silver")
        consume_credit(user_id)
        consume_credit(user_id)
        with patch("user_store.brazil_day", return_value="2999-01-01"):
            self.assertEqual(consume_credit(user_id)["credits_remaining"], 1)

    def test_expired_cycle_requires_a_new_payment(self):
        _, user_id = self._client_session("gold")
        past = "2020-01-01T00:00:00+00:00"
        users = __import__("user_store").load_users()
        for user in users:
            if str(user["id"]) == user_id:
                user["billing"]["expires_at"] = past
        __import__("user_store").save_users(users)
        with self.assertRaises(SubscriptionRequired):
            consume_credit(user_id)

    def test_marks_are_stored_per_user_and_removed_one_by_one(self):
        session, user_id = self._client_session("gold")
        set_feedback(user_id, {"title_pt": "Matrix", "title_original": "The Matrix", "year": 1999, "opinion": "liked", "watched": True})
        set_feedback(user_id, {"title_pt": "Crepúsculo", "title_original": "Twilight", "year": 2008, "opinion": "disliked"})
        self.assertEqual(len(list_feedback(user_id)), 2)

        # Remarcar o mesmo título atualiza o registro em vez de duplicar.
        set_feedback(user_id, {"title_pt": "Matrix", "title_original": "The Matrix", "year": 1999, "opinion": "disliked", "watched": True})
        stored = {item["title_original"]: item for item in list_feedback(user_id)}
        self.assertEqual(stored["The Matrix"]["opinion"], "disliked")

        status, payload, _ = api_core.feedback_delete(session, {"id": stored["Twilight"]["id"]})
        self.assertEqual(status, 200)
        self.assertEqual([item["title_original"] for item in payload["feedback"]], ["The Matrix"])
        with self.assertRaises(LookupError):
            remove_feedback(user_id, "nao-existe")

    def test_clearing_both_marks_removes_the_title_from_the_history(self):
        _, user_id = self._client_session("gold")
        set_feedback(user_id, {"title_pt": "Matrix", "title_original": "The Matrix", "year": 1999, "opinion": "liked"})
        set_feedback(user_id, {"title_pt": "Matrix", "title_original": "The Matrix", "year": 1999, "opinion": "", "watched": False})
        self.assertEqual(list_feedback(user_id), [])

    def test_search_history_is_stored_on_the_account_not_the_browser(self):
        """A busca fica gravada na conta: qualquer aparelho logado enxerga a mesma lista."""
        session, user_id = self._client_session("gold")
        self.assertEqual(list_history(user_id), [])

        def fake_call(filters, feedback=None):
            return json.dumps(
                {
                    "interpretation": "Você quer algo tenso e curto.",
                    "best_choice": {"title_pt": "O Farol", "reason": "Combina com a vibe."},
                    "recommendations": [
                        {"rank": 1, "title_pt": "O Farol", "title_original": "The Lighthouse", "year": 2019,
                         "synopsis": "Dois faroleiros isolados enlouquecem aos poucos.",
                         "awards": {"oscars_won": 0, "highlight": "Indicado ao Oscar de Fotografia"}}
                    ],
                }
            )

        with patch("recommender.call_openai", fake_call):
            status, _, _ = api_core.recommend(session, {"filters": FILTERS})
        self.assertEqual(status, 200)

        stored = list_history(user_id)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["bestChoice"]["title_pt"], "O Farol")
        self.assertEqual(stored[0]["recommendations"][0]["synopsis"], "Dois faroleiros isolados enlouquecem aos poucos.")

        # Uma segunda "sessão" (outro aparelho) lendo a mesma conta enxerga a mesma busca.
        status, payload, _ = api_core.history_overview(session)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["history"]), 1)

        status, payload, _ = api_core.history_delete(session, {"id": stored[0]["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["history"], [])
        with self.assertRaises(LookupError):
            remove_history(user_id, "nao-existe")

    def test_history_route_requires_login(self):
        status, payload, _ = router.handle("GET", "/api/history", {}, {}, {})
        self.assertEqual(status, 401)
        status, _, _ = router.handle("POST", "/api/history", {}, {"id": "x"}, {})
        self.assertEqual(status, 401)

    def test_prompt_carries_the_user_marks_to_the_engine(self):
        section = build_feedback_section([
            {"title_pt": "Matrix", "title_original": "The Matrix", "year": 1999, "watched": True, "opinion": ""},
            {"title_pt": "Crepúsculo", "title_original": "Twilight", "year": 2008, "opinion": "disliked", "watched": False},
            {"title_pt": "Cidade de Deus", "title_original": "Cidade de Deus", "year": 2002, "opinion": "liked", "watched": False},
        ])
        self.assertIn("JÁ ASSISTIU", section)
        self.assertIn("Matrix (The Matrix, 1999)", section)
        self.assertIn("NÃO GOSTOU", section)
        self.assertIn("Twilight", section)
        self.assertIn("GOSTOU", section)
        self.assertIn("Cidade de Deus (2002)", section)
        self.assertEqual(build_feedback_section([]), "")

    def test_recommendation_spends_a_credit_and_forwards_the_marks(self):
        session, user_id = self._client_session("silver")
        set_feedback(user_id, {"title_pt": "Matrix", "title_original": "The Matrix", "year": 1999, "watched": True})
        seen = {}

        def fake_call(filters, feedback=None):
            seen["feedback"] = feedback
            return json.dumps({"recommendations": []})

        with patch("recommender.call_openai", fake_call):
            status, payload, _ = api_core.recommend(session, {"filters": FILTERS})
        self.assertEqual(status, 200)
        self.assertEqual(payload["account"]["credits_remaining"], 1)
        self.assertEqual(seen["feedback"][0]["title_original"], "The Matrix")

    def test_request_carries_every_marked_streaming_into_the_prompt(self):
        session, _ = self._client_session("silver")
        seen = {}

        def fake_call(filters, feedback=None):
            seen["prompt"] = buildRecommendationPrompt(filters, feedback)
            seen["platform"] = filters["platform"]
            return json.dumps({"recommendations": []})

        body = {"filters": {**FILTERS, "platform": ["Netflix", "Disney+", "Max (HBO)"]}}
        with patch("recommender.call_openai", fake_call):
            status, _, _ = api_core.recommend(session, body)
        self.assertEqual(status, 200)
        self.assertEqual(seen["platform"], "Netflix, Disney+, Max (HBO)")
        self.assertIn("marcou 3 plataformas ao mesmo tempo", seen["prompt"])
        self.assertIn("Netflix, Disney+, Max (HBO)", seen["prompt"])

    def test_request_with_an_unofficial_streaming_never_reaches_the_engine(self):
        session, user_id = self._client_session("silver")
        body = {"filters": {**FILTERS, "platform": ["Netflix", "Streaming do vizinho"]}}

        def must_not_run(filters, feedback=None):  # pragma: no cover - só falha se chamada
            raise AssertionError("O motor não pode ser chamado com filtro inválido.")

        with patch("recommender.call_openai", must_not_run):
            status, payload, _ = api_core.recommend(session, body)
        self.assertEqual(status, 400)
        self.assertIn("opções oficiais", payload["error"])
        # Filtro recusado antes do débito: o crédito do dia continua intacto.
        self.assertEqual(get_account(user_id)["credits_remaining"], 2)

    def test_missing_engine_key_is_reported_without_naming_the_provider(self):
        from recommender import call_openai

        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False):
            with self.assertRaises(RuntimeError) as raised:
                call_openai(FILTERS)
        message = str(raised.exception)
        self.assertIn("motor de recomendação", message)
        for secret in ("OPENAI", "OpenAI", "ChatGPT"):
            self.assertNotIn(secret, message)

    def test_engine_failures_never_name_the_provider_to_the_client(self):
        session, _ = self._client_session("silver")

        def broken(filters, feedback=None):
            raise RuntimeError("Não foi possível conectar ao motor de recomendação.")

        with patch("recommender.call_openai", broken):
            status, payload, _ = api_core.recommend(session, {"filters": FILTERS})
        self.assertEqual(status, 502)
        # Qual motor está por trás da curadoria é informação interna.
        for secret in ("OpenAI", "openai", "ChatGPT", "GPT"):
            self.assertNotIn(secret, payload["error"])

    def test_credit_comes_back_when_the_engine_fails(self):
        session, user_id = self._client_session("silver")

        def broken(filters, feedback=None):
            raise RuntimeError("A consulta à OpenAI falhou.")

        with patch("recommender.call_openai", broken):
            status, _, _ = api_core.recommend(session, {"filters": FILTERS})
        self.assertEqual(status, 502)
        self.assertEqual(get_account(user_id)["credits_remaining"], 2)

    def test_exhausted_credits_answer_with_the_dedicated_code(self):
        session, user_id = self._client_session("silver")
        consume_credit(user_id)
        consume_credit(user_id)
        status, payload, _ = api_core.recommend(session, {"filters": FILTERS})
        self.assertEqual(status, 429)
        self.assertEqual(payload["code"], "no_credits")

    # -----------------------------------------------------------------
    # Checkout Asaas
    # -----------------------------------------------------------------

    def test_checkout_creates_the_subscription_and_returns_the_invoice(self):
        session, user_id = self._client_session()
        calls = []

        def fake_request(method, path, payload=None, params=None):
            calls.append((method, path))
            if path == "/customers" and method == "GET":
                return {"data": []}
            if path == "/customers":
                return {"id": "cus_123"}
            if path == "/subscriptions":
                return {"id": "sub_123"}
            if path == "/subscriptions/sub_123/payments":
                return {"data": [{"id": "pay_1", "status": "PENDING", "invoiceUrl": "https://asaas.test/i/pay_1"}]}
            return {}

        with patch.dict(os.environ, {"ASAAS_API_KEY": "$aact_hmlg_test"}), patch("billing._request", fake_request):
            status, payload, _ = api_core.billing_checkout(
                session, {"plan": "gold", "name": "Cliente Teste", "document": "529.982.247-25"}
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["checkout_url"], "https://asaas.test/i/pay_1")
        self.assertIn(("POST", "/subscriptions"), calls)
        # A cobrança fica pendente: o acesso só abre com a confirmação da Asaas.
        account = get_account(user_id)
        self.assertFalse(account["subscription_active"])
        self.assertEqual(account["subscription_status"], "pending")

    def test_billing_diagnostics_explain_a_missing_key(self):
        for name in billing.API_KEY_ENV_VARS:
            os.environ.pop(name, None)
        report = billing.diagnostics()
        self.assertFalse(report["configured"])
        self.assertEqual(report["api_key_source_env_var"], "")
        self.assertIn("Environment Variables", report["error"])
        self.assertIn("ASAAS_API_KEY", report["accepted_env_vars"])

    def test_billing_accepts_the_usual_alternative_variable_names(self):
        for name in billing.API_KEY_ENV_VARS:
            os.environ.pop(name, None)
        with patch.dict(os.environ, {"ASAAS_TOKEN": "$aact_prod_alternativa"}):
            report = billing.diagnostics()
            self.assertTrue(report["configured"])
            self.assertEqual(report["api_key_source_env_var"], "ASAAS_TOKEN")
            self.assertNotIn("error", report)

    def test_billing_flags_a_key_that_lost_the_dollar_sign(self):
        for name in billing.API_KEY_ENV_VARS:
            os.environ.pop(name, None)
        with patch.dict(os.environ, {"ASAAS_API_KEY": "aact_prod_sem_cifrao"}):
            report = billing.diagnostics()
            self.assertTrue(report["configured"])
            self.assertIn("$aact_", report["error"])

    def test_unconfigured_checkout_answers_with_the_setup_steps(self):
        session, _ = self._client_session()
        for name in billing.API_KEY_ENV_VARS:
            os.environ.pop(name, None)
        status, payload, _ = api_core.billing_checkout(
            session, {"plan": "gold", "name": "Cliente Teste", "document": "529.982.247-25"}
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "billing_unconfigured")

    def test_admin_config_reports_the_billing_setup(self):
        from auth import config_status

        self.assertIn("billing", config_status())

    def test_checkout_rejects_an_invalid_document(self):
        session, _ = self._client_session()
        with patch.dict(os.environ, {"ASAAS_API_KEY": "$aact_hmlg_test"}):
            status, payload, _ = api_core.billing_checkout(
                session, {"plan": "gold", "name": "Cliente Teste", "document": "111.111.111-11"}
            )
        self.assertEqual(status, 400)
        self.assertIn("CPF", payload["error"])

    def test_webhook_confirms_the_payment_and_unlocks_the_platform(self):
        session, user_id = self._client_session()
        with patch.dict(os.environ, {"ASAAS_API_KEY": "$aact_hmlg_test"}), patch(
            "billing._request",
            lambda method, path, payload=None, params=None: {"data": []}
            if (path == "/customers" and method == "GET")
            else {"id": "cus_1"}
            if path == "/customers"
            else {"id": "sub_1"}
            if path == "/subscriptions"
            else {"data": [{"id": "pay_1", "status": "PENDING", "invoiceUrl": "https://asaas.test/i/1"}]},
        ):
            api_core.billing_checkout(session, {"plan": "silver", "name": "Cliente Teste", "document": "529.982.247-25"})

        status, payload, _ = api_core.billing_webhook(
            {
                "event": "PAYMENT_CONFIRMED",
                "payment": {"id": "pay_1", "customer": "cus_1", "subscription": "sub_1", "status": "CONFIRMED"},
            },
            token_header="",
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["access"], "granted")
        account = get_account(user_id)
        self.assertTrue(account["subscription_active"])
        self.assertEqual(account["plan"], "silver")
        self.assertEqual(account["credits_remaining"], 2)

    def test_webhook_rejects_a_wrong_token(self):
        with patch.dict(os.environ, {"ASAAS_WEBHOOK_TOKEN": "segredo"}):
            status, _, _ = api_core.billing_webhook({"event": "PAYMENT_CONFIRMED"}, token_header="errado")
            self.assertEqual(status, 401)
            ok_status, _, _ = api_core.billing_webhook({"event": "PAYMENT_CONFIRMED"}, token_header="segredo")
            self.assertEqual(ok_status, 200)

    def test_overdue_payment_suspends_the_access(self):
        _, user_id = self._client_session("gold")
        activate_subscription(user_id, "gold", "pay_9", "TEST")
        users = __import__("user_store").load_users()
        for user in users:
            if str(user["id"]) == user_id:
                user["billing"]["asaas_subscription_id"] = "sub_9"
        __import__("user_store").save_users(users)
        status, payload, _ = api_core.billing_webhook(
            {"event": "PAYMENT_OVERDUE", "payment": {"id": "pay_9", "subscription": "sub_9", "status": "OVERDUE"}},
            token_header="",
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["access"], "past_due")
        self.assertFalse(get_account(user_id)["subscription_active"])

    def test_webhook_event_reader_understands_the_asaas_payload(self):
        event = billing.read_webhook_event(
            {"event": "PAYMENT_RECEIVED", "payment": {"id": "p", "customer": "c", "status": "RECEIVED"}}
        )
        self.assertTrue(event["grants_access"])
        self.assertFalse(event["suspends_access"])
        self.assertTrue(billing.read_webhook_event({"event": "SUBSCRIPTION_DELETED"})["suspends_access"])

    def test_session_cookie_expires_when_the_browser_closes(self):
        create_user("leave@test.local", "client-password")
        _, _, headers = api_core.login({"email": "leave@test.local", "password": "client-password"}, secure=True)
        cookie = headers["Set-Cookie"]
        self.assertNotIn("Max-Age", cookie)
        self.assertNotIn("Expires", cookie)

    def test_admin_can_grant_and_revoke_a_plan_by_hand(self):
        record = create_user("manual@test.local", "client-password")
        admin = {"email": "admin@test.local", "role": "admin"}
        status, payload, _ = api_core.admin_action(admin, {"action": "grant_plan", "user_id": record["id"], "plan": "diamante"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["account"]["unlimited"])
        status, payload, _ = api_core.admin_action(admin, {"action": "revoke_plan", "user_id": record["id"]})
        self.assertEqual(status, 200)
        self.assertFalse(payload["account"]["subscription_active"])

    def test_admin_listing_shows_the_subscription_of_each_client(self):
        record = create_user("listed@test.local", "client-password")
        activate_subscription(record["id"], "gold", "pay_x", "TEST")
        listed = {user["email"]: user for user in admin_users()}["listed@test.local"]
        self.assertEqual(listed["plan"], "gold")
        self.assertTrue(listed["subscription_active"])
        self.assertEqual(admin_summary()["subscribers"], 1)


    # -----------------------------------------------------------------
    # O painel do administrador precisa estar alcançável no deploy
    # -----------------------------------------------------------------

    def test_the_landing_page_sends_an_administrator_to_the_panel(self):
        """Regressão: o site publicado mandava todo mundo para /app, inclusive o
        administrador, e /app não tem nenhuma tela de controle de acesso — o
        painel existia no deploy mas ninguém chegava nele pela porta da frente."""
        doc = published_document("index.html")
        self.assertIn('href="/admin"', doc, "a landing precisa linkar o painel")
        self.assertIn("goToAdmin", doc, "falta o desvio para o painel")
        self.assertIn(
            "logged.role === 'admin'",
            doc,
            "o login precisa separar administrador de cliente antes de navegar",
        )
        self.assertIn("this.adminUrl()", doc)

    def test_the_logged_in_environment_keeps_a_way_back_to_the_panel(self):
        """Um administrador que caia em /app não pode ficar sem caminho de volta."""
        doc = published_document("app.html")
        self.assertIn("adminHref", doc)
        self.assertIn('href="{{ adminHref }}"', doc, "falta o link para /admin no cabeçalho")
        self.assertIn("adminDisplay", doc, "o atalho precisa sumir para quem não é admin")
        self.assertIn("role === 'admin'", doc, "o atalho precisa depender do papel da sessão")

    def test_the_admin_page_only_calls_routes_the_router_publishes(self):
        """Toda rota citada em admin.html precisa existir: um 404 aqui deixaria o
        painel aberto e inerte, que é pior do que não abrir."""
        page = published_document("admin.html")
        called = sorted(set(re.findall(r'"(/api/[a-z/]+)"', page)))
        self.assertIn("/api/admin/status", called)
        self.assertIn("/api/admin/users", called)
        for path in called:
            for method in ("GET", "POST"):
                status = router.handle(method, path, {}, {}, {})[0]
                self.assertNotEqual(status, 404, f"{method} {path} não existe no roteador")

    def test_the_admin_panel_is_reachable_by_the_same_url_in_both_runtimes(self):
        """A Vercel serve /admin e /app por `cleanUrls`. O servidor local precisa
        fazer o mesmo, senão as duas páginas só existem em produção."""
        config = json.loads((Path(__file__).parent / "vercel.json").read_text(encoding="utf-8"))
        self.assertTrue(config["cleanUrls"], "sem cleanUrls a Vercel não serve /admin")
        self.assertTrue((PUBLIC / "admin.html").is_file())
        self.assertTrue((PUBLIC / "app.html").is_file())

        import server

        clean = server.AppHandler.clean_url
        self.assertEqual(clean(None, "/admin"), "/admin.html")
        self.assertEqual(clean(None, "/admin/"), "/admin.html")
        self.assertEqual(clean(None, "/app"), "/app.html")
        self.assertEqual(clean(None, "/"), "/")
        self.assertEqual(clean(None, "/admin.html"), "/admin.html")
        self.assertEqual(clean(None, "/nao-existe"), "/nao-existe")

    def test_an_account_promoted_to_admin_opens_the_panel(self):
        """O painel não é só do admin de ambiente: quem é promovido na base entra
        pelo mesmo /admin, e quem não é admin continua barrado."""
        record = create_user("promovido@test.local", "client-password")
        client = {"email": record["email"], "role": "user", "user_id": record["id"]}
        self.assertEqual(router.handle("GET", "/api/admin/status", {}, {}, {})[0], 403)
        self.assertEqual(api_core.admin_overview(client)[0], 403)

        update_user_role(record["id"], "admin")
        session = read_session("pipoca_session=" + create_session(record["email"], "admin", record["id"]))
        self.assertIsNotNone(session, "a sessão do admin promovido precisa sobreviver")
        status, payload, _ = api_core.admin_overview(session)
        self.assertEqual(status, 200)
        self.assertEqual(payload["user"]["role"], "admin")
        self.assertIn("promovido@test.local", [user["email"] for user in payload["users"]])


if __name__ == "__main__":
    unittest.main()
