import importlib.util
import io
import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from pathlib import Path


TEST_STORE = Path(tempfile.gettempdir()) / "pipoca-play-test-users.json"
os.environ["AUTH_SECRET"] = "test-secret"
os.environ["ADMIN_EMAIL"] = "admin@test.local"
os.environ["ADMIN_PASSWORD"] = "admin-password"
os.environ["ENVIRONMENT"] = "test"
os.environ["TMDB_API_KEY"] = ""
os.environ["USER_STORE_FILE"] = str(TEST_STORE)

from auth import authenticate, create_session, read_session, session_cookie  # noqa: E402
from metadata import NullMetadataProvider  # noqa: E402
from server import buildRecommendationPrompt, clean_filters, validate_recommendation_payload  # noqa: E402
import api_core  # noqa: E402
import billing  # noqa: E402
from user_store import (  # noqa: E402
    CreditsExhaustedError,
    StorageError,
    admin_summary,
    admin_users,
    consume_credit,
    create_user,
    delete_mark,
    delete_user,
    get_status_by_token,
    list_marks,
    register_user,
    save_mark,
    save_subscription,
    set_user_password,
    storage_diagnostics,
    storage_mode,
    update_user_role,
    update_user_status,
)


# Um ciclo de 30 dias que já venceu: usado para testar a expiração do acesso.
CYCLE_OVER = 31

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


class SubscriptionAndCreditTests(unittest.TestCase):
    """Assinatura paga, créditos diários e liberação dos resultados."""

    def setUp(self):
        TEST_STORE.unlink(missing_ok=True)
        record, _, _ = register_user("cliente@test.local", "cliente-password")
        update_user_status(record["id"], "approved")
        self.email = "cliente@test.local"
        self.session = {"email": self.email, "role": "user", "user_id": record["id"]}

    def tearDown(self):
        TEST_STORE.unlink(missing_ok=True)

    def activate(self, plan: str):
        start, end = billing.cycle_bounds()
        save_subscription(
            self.email,
            {"plan": plan, "status": "active", "cycle_start": start, "cycle_end": end, "confirmed_at": start},
        )

    def test_plans_match_the_published_prices_and_credits(self):
        catalog = {plan["id"]: plan for plan in billing.plans_catalog()}
        self.assertEqual(catalog["silver"]["price"], 15.00)
        self.assertEqual(catalog["silver"]["daily_credits"], 2)
        self.assertEqual(catalog["gold"]["price"], 25.00)
        self.assertEqual(catalog["gold"]["daily_credits"], 5)
        self.assertEqual(catalog["diamante"]["price"], 30.00)
        self.assertTrue(catalog["diamante"]["unlimited"])
        self.assertIsNone(catalog["diamante"]["daily_credits"])

    def test_recommendation_is_blocked_until_payment_is_confirmed(self):
        status, payload, _ = api_core.recommend(self.session, {"filters": FILTERS})
        self.assertEqual(status, 402)
        self.assertEqual(payload["reason"], "payment_required")
        self.assertFalse(billing.account_state(self.email)["payment_confirmed"])

    def test_pending_payment_still_blocks_the_results(self):
        save_subscription(self.email, {"plan": "gold", "status": "pending", "asaas_subscription_id": "sub_1"})
        status, payload, _ = api_core.recommend(self.session, {"filters": FILTERS})
        self.assertEqual(status, 402)
        self.assertIn("confirmação", payload["error"])

    def test_silver_plan_grants_exactly_two_daily_credits(self):
        self.activate("silver")
        self.assertEqual(billing.account_state(self.email)["credits_remaining"], 2)
        billing.authorize_search(self.email)
        billing.authorize_search(self.email)
        self.assertEqual(billing.account_state(self.email)["credits_remaining"], 0)
        with self.assertRaises(CreditsExhaustedError):
            billing.authorize_search(self.email)

    def test_gold_plan_grants_five_daily_credits(self):
        self.activate("gold")
        for _ in range(5):
            billing.authorize_search(self.email)
        self.assertEqual(billing.account_state(self.email)["credits_used_today"], 5)
        with self.assertRaises(CreditsExhaustedError):
            billing.authorize_search(self.email)

    def test_diamond_plan_has_no_daily_limit(self):
        self.activate("diamante")
        for _ in range(25):
            billing.authorize_search(self.email)
        state = billing.account_state(self.email)
        self.assertTrue(state["unlimited"])
        self.assertIsNone(state["credits_remaining"])
        self.assertTrue(state["active"])

    def test_credits_reset_on_the_next_brazilian_day(self):
        self.activate("silver")
        consume_credit(self.email, 2, today="2026-09-08")
        consume_credit(self.email, 2, today="2026-09-08")
        with self.assertRaises(CreditsExhaustedError):
            consume_credit(self.email, 2, today="2026-09-08")
        self.assertEqual(consume_credit(self.email, 2, today="2026-09-09")["used"], 1)

    def test_expired_cycle_stops_being_active(self):
        expired = datetime.now(timezone.utc) - timedelta(days=CYCLE_OVER)
        start, end = billing.cycle_bounds(expired)
        save_subscription(
            self.email,
            {"plan": "diamante", "status": "active", "cycle_start": start, "cycle_end": end, "confirmed_at": start},
        )
        self.assertFalse(billing.account_state(self.email)["active"])
        with self.assertRaises(billing.PaymentRequiredError):
            billing.authorize_search(self.email)

    def test_failed_search_refunds_the_credit(self):
        self.activate("silver")
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            status, _, _ = api_core.recommend(self.session, {"filters": FILTERS})
        self.assertEqual(status, 502)
        self.assertEqual(billing.account_state(self.email)["credits_used_today"], 0)

    def test_webhook_confirms_the_payment_and_releases_access(self):
        save_subscription(self.email, {"plan": "gold", "status": "pending", "asaas_subscription_id": "sub_42"})
        event = {
            "event": "PAYMENT_CONFIRMED",
            "payment": {
                "id": "pay_42",
                "status": "CONFIRMED",
                "subscription": "sub_42",
                "externalReference": f"pipoca-play:{self.email}:gold",
                "confirmedDate": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        }
        with patch.dict(os.environ, {"ASAAS_WEBHOOK_TOKEN": "token-do-asaas"}):
            self.assertEqual(api_core.billing_webhook(event, "errado")[0], 401)
            self.assertEqual(api_core.billing_webhook(event, None)[0], 401)
            status, payload, _ = api_core.billing_webhook(event, "token-do-asaas")
        self.assertEqual(status, 200)
        self.assertTrue(payload["handled"])
        state = billing.account_state(self.email)
        self.assertTrue(state["payment_confirmed"])
        self.assertEqual(state["daily_credits"], 5)

    def test_webhook_without_configured_token_is_refused(self):
        with patch.dict(os.environ, {"ASAAS_WEBHOOK_TOKEN": ""}):
            self.assertEqual(api_core.billing_webhook({}, "qualquer-coisa")[0], 401)

    def test_overdue_payment_takes_the_access_back(self):
        self.activate("silver")
        event = {"payment": {"id": "p", "status": "OVERDUE", "externalReference": f"pipoca-play:{self.email}:silver"}}
        with patch.dict(os.environ, {"ASAAS_WEBHOOK_TOKEN": "t"}):
            api_core.billing_webhook(event, "t")
        self.assertFalse(billing.account_state(self.email)["active"])

    def test_admin_summary_counts_active_subscribers(self):
        self.assertEqual(admin_summary()["subscribers"], 0)
        self.activate("gold")
        self.assertEqual(admin_summary()["subscribers"], 1)


class MarkTests(unittest.TestCase):
    """Gostei / não gostei / já assisti, e o histórico com remoção individual."""

    def setUp(self):
        TEST_STORE.unlink(missing_ok=True)
        record, _, _ = register_user("marcador@test.local", "marcador-password")
        update_user_status(record["id"], "approved")
        self.email = "marcador@test.local"
        self.session = {"email": self.email, "role": "user", "user_id": record["id"]}

    def tearDown(self):
        TEST_STORE.unlink(missing_ok=True)

    def test_marks_are_stored_per_user_and_deduplicated_by_title(self):
        save_mark(self.email, {"title_original": "Parasite", "title_pt": "Parasita", "year": 2019, "opinion": "liked"})
        save_mark(
            self.email,
            {"title_original": "Parasite", "title_pt": "Parasita", "year": 2019, "opinion": "liked", "watched": True},
        )
        marks = list_marks(self.email)
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]["opinion"], "liked")
        self.assertTrue(marks[0]["watched"])

    def test_marks_do_not_leak_between_accounts(self):
        other, _, _ = register_user("outro@test.local", "outro-password")
        update_user_status(other["id"], "approved")
        save_mark(self.email, {"title_original": "Dune", "year": 2021, "opinion": "liked"})
        self.assertEqual(len(list_marks(self.email)), 1)
        self.assertEqual(list_marks("outro@test.local"), [])

    def test_opinion_and_watched_are_independent(self):
        save_mark(self.email, {"title_original": "Dune", "year": 2021, "watched": True})
        mark = list_marks(self.email)[0]
        self.assertEqual(mark["opinion"], "")
        self.assertTrue(mark["watched"])

    def test_clearing_both_flags_removes_the_mark(self):
        save_mark(self.email, {"title_original": "Dune", "year": 2021, "opinion": "liked"})
        save_mark(self.email, {"title_original": "Dune", "year": 2021, "opinion": "", "watched": False})
        self.assertEqual(list_marks(self.email), [])

    def test_marks_reach_the_prompt_sent_to_the_model(self):
        save_mark(self.email, {"title_original": "Parasite", "title_pt": "Parasita", "year": 2019, "watched": True})
        save_mark(self.email, {"title_original": "Cats", "title_pt": "Cats", "year": 2019, "opinion": "disliked"})
        preferences = billing.preferences_for_prompt(self.email)
        self.assertEqual(preferences["watched"], ["Parasita (2019)"])
        self.assertEqual(preferences["disliked"], ["Cats (2019)"])
        prompt = buildRecommendationPrompt(FILTERS, preferences)
        self.assertIn("NÃO recomende nenhum destes títulos novamente: Parasita (2019)", prompt)
        self.assertIn("evite obras parecidas com eles: Cats (2019)", prompt)

    def test_prompt_is_unchanged_when_the_user_has_no_marks(self):
        self.assertNotIn("Histórico pessoal", buildRecommendationPrompt(FILTERS))
        self.assertNotIn(
            "Histórico pessoal",
            buildRecommendationPrompt(FILTERS, {"watched": [], "liked": [], "disliked": []}),
        )

    def test_removing_one_mark_brings_the_title_back_to_future_searches(self):
        save_mark(self.email, {"title_original": "Parasite", "title_pt": "Parasita", "year": 2019, "watched": True})
        save_mark(self.email, {"title_original": "Cats", "title_pt": "Cats", "year": 2019, "opinion": "disliked"})
        parasite = next(item for item in list_marks(self.email) if item["title_pt"] == "Parasita")
        status, payload, _ = api_core.mark_action(self.session, {"action": "delete", "id": parasite["id"]})
        self.assertEqual(status, 200)
        self.assertEqual([item["title_pt"] for item in payload["marks"]], ["Cats"])
        self.assertEqual(billing.preferences_for_prompt(self.email)["watched"], [])
        with self.assertRaises(LookupError):
            delete_mark(self.email, parasite["id"])

    def test_marks_require_an_authenticated_session(self):
        self.assertEqual(api_core.marks(None)[0], 401)
        self.assertEqual(api_core.mark_action(None, {"action": "save"})[0], 401)

    def test_mark_without_a_title_is_rejected(self):
        status, _, _ = api_core.mark_action(self.session, {"action": "save", "title_original": " "})
        self.assertEqual(status, 400)


class SessionCookieTests(unittest.TestCase):
    """A sessão termina ao sair da página: o cookie é de sessão de navegador."""

    def test_cookie_has_no_expiry_so_leaving_the_page_requires_a_new_login(self):
        cookie = session_cookie(create_session("a@test.local", "user", "1"), secure=True)
        self.assertNotIn("Max-Age", cookie)
        self.assertNotIn("Expires", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)


class ServerlessRoutingTests(unittest.TestCase):
    """As funções consolidadas em api/ e os rewrites que as alimentam.

    A Vercel limita o número de funções por deploy, então rotas irmãs dividem
    uma função só. Estes testes garantem que cada rota pública continua
    respondendo, tanto se o caminho original chegar intacto quanto se o rewrite
    entregar a ação em `?__route=`.
    """

    ROOT = Path(__file__).resolve().parent
    # Limite do plano Hobby da Vercel; estourá-lo quebra o deploy inteiro.
    MAX_FUNCTIONS = 12

    def setUp(self):
        TEST_STORE.unlink(missing_ok=True)

    def tearDown(self):
        TEST_STORE.unlink(missing_ok=True)

    # -- utilidades ------------------------------------------------------

    @staticmethod
    def load(module_name):
        path = ServerlessRoutingTests.ROOT / "api" / f"{module_name}.py"
        spec = importlib.util.spec_from_file_location(f"_api_{module_name}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.handler

    def call(self, module_name, method, path, body=None, cookie=None, extra_headers=None):
        handler_class = self.load(module_name)
        instance = handler_class.__new__(handler_class)
        instance.path = path
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        headers = {"Content-Length": str(len(raw))}
        if cookie:
            headers["Cookie"] = cookie
        headers.update(extra_headers or {})
        instance.headers = headers
        instance.rfile = io.BytesIO(raw)
        instance.wfile = io.BytesIO()
        captured = {}
        instance.send_response = lambda code, *rest: captured.__setitem__("status", int(code))
        instance.send_header = lambda key, value: None
        instance.end_headers = lambda: None
        getattr(instance, "do_" + method)()
        written = instance.wfile.getvalue().decode("utf-8")
        return captured["status"], (json.loads(written) if written else {})

    def both_paths(self, group, action, query=""):
        """O caminho original e o caminho reescrito pelo vercel.json."""
        return [f"/api/{group}/{action}{query}", f"/api/{group}?__route={action}{query.replace('?', '&', 1)}"]

    # -- limites do deploy -----------------------------------------------

    def test_deployment_stays_under_the_vercel_function_limit(self):
        functions = sorted(p.relative_to(self.ROOT).as_posix() for p in (self.ROOT / "api").rglob("*.py"))
        self.assertLessEqual(
            len(functions),
            self.MAX_FUNCTIONS,
            f"{len(functions)} funções em api/ — o deploy falha acima de {self.MAX_FUNCTIONS}: {functions}",
        )

    def test_every_grouped_route_has_a_rewrite(self):
        config = json.loads((self.ROOT / "vercel.json").read_text(encoding="utf-8"))
        sources = {rule["source"] for rule in config.get("rewrites", [])}
        for group in ("auth", "admin", "billing"):
            self.assertIn(f"/api/{group}/:action", sources)
            self.assertTrue((self.ROOT / "api" / f"{group}.py").exists())

    def test_rewrites_cover_every_route_the_frontend_calls(self):
        config = json.loads((self.ROOT / "vercel.json").read_text(encoding="utf-8"))
        rewritten = {rule["source"].rsplit("/", 1)[0] for rule in config.get("rewrites", [])}
        pages = (self.ROOT / "public" / "index.html").read_text(encoding="utf-8")
        pages += (self.ROOT / "public" / "admin.html").read_text(encoding="utf-8")
        called = set(re.findall(r'"(/api/[a-z/]+)', pages))
        self.assertTrue(called, "nenhuma chamada de API encontrada no frontend")
        for route in called:
            group = route.rsplit("/", 1)[0]
            served_by_own_file = (self.ROOT / "api" / (route[len("/api/"):] + ".py")).exists()
            self.assertTrue(
                served_by_own_file or group in rewritten,
                f"{route} não tem função própria nem rewrite",
            )

    # -- /api/auth -------------------------------------------------------

    def test_auth_me_answers_on_both_paths(self):
        for path in self.both_paths("auth", "me"):
            status, payload = self.call("auth", "GET", path)
            self.assertEqual(status, 200, path)
            self.assertFalse(payload["authenticated"], path)

    def test_auth_login_answers_on_both_paths(self):
        record, _, _ = register_user("rota@test.local", "rota-password")
        update_user_status(record["id"], "approved")
        for path in self.both_paths("auth", "login"):
            status, payload = self.call(
                "auth", "POST", path, {"email": "rota@test.local", "password": "rota-password"}
            )
            self.assertEqual(status, 200, path)
            self.assertTrue(payload["authenticated"], path)

    def test_auth_register_and_status_answer_on_both_paths(self):
        for index, path in enumerate(self.both_paths("auth", "register")):
            status, payload = self.call(
                "auth", "POST", path, {"email": f"novo{index}@test.local", "password": "nova-senha"}
            )
            self.assertEqual(status, 201, path)
            token = payload["token"]
            query = f"?email=novo{index}@test.local&token={token}"
            status, payload = self.call("auth", "GET", f"/api/auth/status{query}")
            self.assertEqual((status, payload["status"]), (200, "pending"))

    def test_auth_logout_clears_the_cookie_on_both_paths(self):
        for path in self.both_paths("auth", "logout"):
            status, payload = self.call("auth", "POST", path)
            self.assertEqual(status, 200, path)
            self.assertFalse(payload["authenticated"], path)

    def test_auth_rejects_the_wrong_method_and_unknown_actions(self):
        self.assertEqual(self.call("auth", "GET", "/api/auth/login")[0], 405)
        self.assertEqual(self.call("auth", "POST", "/api/auth/me")[0], 405)
        self.assertEqual(self.call("auth", "GET", "/api/auth?__route=inventada")[0], 404)

    # -- /api/admin ------------------------------------------------------

    def test_admin_requires_an_admin_session_on_both_paths(self):
        for group in ("status", "users"):
            for path in self.both_paths("admin", group):
                self.assertEqual(self.call("admin", "GET", path)[0], 403, path)

    def test_admin_overview_and_action_work_for_an_admin(self):
        record, _, _ = register_user("alvo@test.local", "alvo-password")
        cookie = "pipoca_session=" + create_session("admin@test.local", "admin", None)
        status, payload = self.call("admin", "GET", "/api/admin?__route=status", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"]["pending"], 1)
        status, payload = self.call(
            "admin", "POST", "/api/admin?__route=users",
            {"action": "approve", "user_id": record["id"]}, cookie=cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["user"]["status"], "approved")

    def test_admin_status_does_not_accept_post(self):
        self.assertEqual(self.call("admin", "POST", "/api/admin/status")[0], 405)

    # -- /api/billing ----------------------------------------------------

    def test_billing_plans_answer_on_both_paths(self):
        for path in self.both_paths("billing", "plans"):
            status, payload = self.call("billing", "GET", path)
            self.assertEqual(status, 200, path)
            self.assertEqual([plan["id"] for plan in payload["plans"]], ["silver", "gold", "diamante"])

    def test_billing_status_requires_a_session_on_both_paths(self):
        for path in self.both_paths("billing", "status"):
            self.assertEqual(self.call("billing", "GET", path)[0], 401, path)

    def test_billing_status_reports_the_subscription_without_touching_asaas(self):
        record, _, _ = register_user("assina@test.local", "assina-password")
        update_user_status(record["id"], "approved")
        start, end = billing.cycle_bounds()
        save_subscription("assina@test.local", {"plan": "silver", "status": "active",
                                                "cycle_start": start, "cycle_end": end})
        cookie = "pipoca_session=" + create_session("assina@test.local", "user", record["id"])
        status, payload = self.call("billing", "GET", "/api/billing?__route=status&sync=0", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(payload["subscription"]["active"])
        self.assertEqual(payload["subscription"]["credits_remaining"], 2)

    def test_billing_checkout_requires_a_session_on_both_paths(self):
        for path in self.both_paths("billing", "checkout"):
            self.assertEqual(self.call("billing", "POST", path, {"plan": "gold"})[0], 401, path)

    def test_billing_webhook_refuses_a_wrong_token_on_both_paths(self):
        event = {"payment": {"id": "p", "status": "CONFIRMED"}}
        with patch.dict(os.environ, {"ASAAS_WEBHOOK_TOKEN": "token-certo"}):
            for path in self.both_paths("billing", "webhook"):
                self.assertEqual(self.call("billing", "POST", path, event)[0], 401, path)
                status, _ = self.call("billing", "POST", path, event,
                                      extra_headers={"asaas-access-token": "token-certo"})
                self.assertEqual(status, 200, path)

    def test_billing_rejects_the_wrong_method(self):
        self.assertEqual(self.call("billing", "POST", "/api/billing/plans")[0], 405)
        self.assertEqual(self.call("billing", "GET", "/api/billing/checkout")[0], 405)

    # -- funções que continuam com arquivo próprio -----------------------

    def test_standalone_functions_still_answer(self):
        self.assertEqual(self.call("health", "GET", "/api/health")[0], 200)
        self.assertEqual(self.call("marks", "GET", "/api/marks")[0], 401)
        self.assertEqual(self.call("recommend", "POST", "/api/recommend", {})[0], 401)
        self.assertEqual(self.call("recommend", "GET", "/api/recommend")[0], 405)


if __name__ == "__main__":
    unittest.main()
