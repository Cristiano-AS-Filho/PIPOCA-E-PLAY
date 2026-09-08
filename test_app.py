import json
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


TEST_STORE = Path(tempfile.gettempdir()) / "pipoca-play-test-users.json"
os.environ["AUTH_SECRET"] = "test-secret"
os.environ["ADMIN_EMAIL"] = "admin@test.local"
os.environ["ADMIN_PASSWORD"] = "admin-password"
os.environ["ENVIRONMENT"] = "test"
os.environ["TMDB_API_KEY"] = ""
os.environ["USER_STORE_FILE"] = str(TEST_STORE)

import billing  # noqa: E402
from auth import authenticate, create_session, read_session  # noqa: E402
from plans import public_plans  # noqa: E402
from recommender import build_feedback_section  # noqa: E402
from metadata import NullMetadataProvider  # noqa: E402
from server import buildRecommendationPrompt, clean_filters, validate_recommendation_payload  # noqa: E402
import api_core  # noqa: E402
from user_store import (  # noqa: E402
    CreditError,
    StorageError,
    SubscriptionRequired,
    activate_subscription,
    consume_credit,
    get_account,
    list_feedback,
    remove_feedback,
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


FILTERS = {
    "genre": "Suspense / Thriller",
    "mood": "Sombrio / Assustador",
    "duration": "Livre (Qualquer)",
    "era": "Livre (Qualquer)",
    "platform": "Livre (Qualquer)",
    "companionship": "Sozinho(a)",
    "popularity": "Aclamados pela Crítica / Premiações (Oscar, Cannes)",
}


class PipocaPlayTests(unittest.TestCase):
    def setUp(self):
        TEST_STORE.unlink(missing_ok=True)

    def tearDown(self):
        TEST_STORE.unlink(missing_ok=True)

    def test_clean_filters_preserves_all_seven_dimensions(self):
        cleaned = clean_filters(FILTERS)
        self.assertEqual(cleaned, FILTERS)

    def test_invalid_filter_value_is_rejected(self):
        invalid = {**FILTERS, "genre": "Qualquer valor inventado"}
        with self.assertRaises(ValueError):
            clean_filters(invalid)

    def test_prompt_contains_independent_mood_and_platform(self):
        prompt = buildRecommendationPrompt(FILTERS)
        self.assertIn("Vibe/clima emocional desejado: Sombrio / Assustador", prompt)
        self.assertIn("Plataforma de streaming: Livre (Qualquer)", prompt)

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
        self.assertEqual(validate_recommendation_payload(payload), payload)
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

    def test_metadata_provider_is_explicitly_unconfirmed_without_key(self):
        metadata = NullMetadataProvider().lookup("Example")
        self.assertFalse(metadata["availability_verified"])
        self.assertEqual(metadata["availability"], [])

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
        self.assertEqual(catalog["silver"]["price"], 15.00)
        self.assertEqual(catalog["silver"]["daily_credits"], 2)
        self.assertEqual(catalog["gold"]["price"], 25.00)
        self.assertEqual(catalog["gold"]["daily_credits"], 5)
        self.assertEqual(catalog["diamante"]["price"], 30.00)
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


if __name__ == "__main__":
    unittest.main()
