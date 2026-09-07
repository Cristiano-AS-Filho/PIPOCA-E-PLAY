import json
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
os.environ["ASAAS_API_KEY"] = ""
os.environ["ASAAS_WEBHOOK_TOKEN"] = ""

import asaas_client  # noqa: E402
from auth import authenticate, create_session, read_session  # noqa: E402
from metadata import NullMetadataProvider, TMDBMetadataProvider  # noqa: E402
from rate_limit import check_rate_limit  # noqa: E402
from server import (  # noqa: E402
    RECOMMENDATION_SCHEMA,
    buildRecommendationPrompt,
    clean_filters,
    create_checkout_session,
    duration_violations,
    handle_asaas_webhook_event,
    resolve_billing_user,
    validate_recommendation_payload,
    verify_asaas_webhook,
)
from user_store import (  # noqa: E402
    activate_subscription,
    admin_summary,
    admin_users,
    consume_credit,
    delete_user,
    find_user_by_token,
    get_status_by_token,
    get_user_by_id,
    register_user,
    set_subscription_status,
    update_user_status,
    storage_mode,
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
        self.assertEqual(admin_summary(), {"total": 1, "pending": 1, "approved": 0, "rejected": 0})

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

    def test_metadata_provider_is_explicitly_unconfirmed_without_key(self):
        metadata = NullMetadataProvider().lookup("Example")
        self.assertFalse(metadata["availability_verified"])
        self.assertEqual(metadata["availability"], [])
        self.assertEqual(metadata["ratings"], [])

    def test_recommendation_schema_never_asks_ai_for_ratings_or_awards(self):
        item_schema = RECOMMENDATION_SCHEMA["properties"]["recommendations"]["items"]
        self.assertNotIn("ratings", item_schema["properties"])
        self.assertNotIn("awards", item_schema["properties"])
        self.assertNotIn("ratings", item_schema["required"])
        self.assertNotIn("awards", item_schema["required"])

    def test_duration_violations_flags_out_of_range_and_ignores_unknown(self):
        short_ok = {"title_pt": "Curto ok", "runtime_minutes": 80}
        short_bad = {"title_pt": "Curto estourado", "runtime_minutes": 140}
        unknown = {"title_pt": "Sem duração conhecida", "runtime_minutes": 0}
        self.assertEqual(duration_violations("Curto (Até 90 min)", [short_ok, unknown]), [])
        self.assertEqual(duration_violations("Curto (Até 90 min)", [short_bad]), [short_bad])
        self.assertEqual(duration_violations("Livre (Qualquer)", [short_bad]), [])

    def test_rate_limit_blocks_after_max_requests_in_memory_fallback(self):
        identifier = "rate-limit-test@pipocaplay.com"
        for _ in range(3):
            self.assertTrue(check_rate_limit(identifier, max_requests=3, window_seconds=60))
        self.assertFalse(check_rate_limit(identifier, max_requests=3, window_seconds=60))

    def test_tmdb_provider_extracts_real_vote_average_as_rating(self):
        provider = TMDBMetadataProvider("fake-key")
        search_response = {"results": [{"id": 42, "poster_path": None, "backdrop_path": None}]}
        details_response = {
            "original_title": "Example",
            "title": "Exemplo",
            "release_date": "2020-01-01",
            "runtime": 100,
            "genres": [],
            "overview": "",
            "vote_average": 7.83,
            "vote_count": 500,
            "watch/providers": {"results": {}},
        }
        calls = {"n": 0}

        def fake_get(path, params):
            calls["n"] += 1
            return search_response if path == "/search/movie" else details_response

        with patch.object(provider, "_get", side_effect=fake_get):
            metadata = provider.lookup("Example", "Exemplo", 2020)
        self.assertEqual(metadata["ratings"], [
            {"source": "TMDB", "score": 7.8, "scale": "0-10", "retrieved_at": metadata["ratings"][0]["retrieved_at"]}
        ])


    def test_consume_credit_blocks_without_active_subscription(self):
        record, _, _ = register_user("no-plan@test.local", "user-password")
        ok, info = consume_credit(record["id"])
        self.assertFalse(ok)
        self.assertEqual(info["subscription_status"], "none")

    def test_activate_subscription_sets_daily_limit_and_auto_approves(self):
        record, _, _ = register_user("silver@test.local", "user-password")
        updated = activate_subscription(record["id"], "silver", asaas_customer_id="cus_1", asaas_subscription_id="sub_1")
        self.assertEqual(updated["status"], "approved")
        self.assertEqual(updated["subscription_status"], "active")
        self.assertEqual(updated["credits_daily_limit"], 2)
        self.assertEqual(updated["asaas_customer_id"], "cus_1")

    def test_consume_credit_enforces_daily_limit_then_resets_next_day(self):
        record, _, _ = register_user("gold@test.local", "user-password")
        activate_subscription(record["id"], "gold")  # 5 créditos/dia
        for _ in range(5):
            ok, _ = consume_credit(record["id"])
            self.assertTrue(ok)
        ok, info = consume_credit(record["id"])
        self.assertFalse(ok)
        self.assertEqual(info["credits_remaining_today"], 0)

        # simula a virada do dia mexendo direto na base local
        from user_store import load_users, save_users
        users = load_users()
        for user in users:
            if user["id"] == record["id"]:
                user["credits_date"] = "2000-01-01"
        save_users(users)
        ok, info = consume_credit(record["id"])
        self.assertTrue(ok)
        self.assertEqual(info["credits_remaining_today"], 4)

    def test_diamante_plan_has_unlimited_credits(self):
        record, _, _ = register_user("diamante@test.local", "user-password")
        activate_subscription(record["id"], "diamante")
        for _ in range(20):
            ok, info = consume_credit(record["id"])
            self.assertTrue(ok)
            self.assertIsNone(info["credits_remaining_today"])

    def test_set_subscription_status_cancelled_clears_plan(self):
        record, _, _ = register_user("cancel@test.local", "user-password")
        activate_subscription(record["id"], "silver")
        cancelled = set_subscription_status(record["id"], "cancelled")
        self.assertIsNone(cancelled["plan_id"])
        self.assertEqual(cancelled["subscription_status"], "cancelled")

    def test_find_user_by_token_matches_registration_token(self):
        record, token, _ = register_user("token@test.local", "user-password")
        found = find_user_by_token("token@test.local", token)
        self.assertEqual(found["id"], record["id"])
        self.assertIsNone(find_user_by_token("token@test.local", "token-errado"))

    def test_resolve_billing_user_prefers_session_over_token(self):
        record, token, _ = register_user("resolve@test.local", "user-password")
        session_user = {"role": "user", "user_id": record["id"], "email": record["email"]}
        resolved = resolve_billing_user(session_user, {})
        self.assertEqual(resolved["id"], record["id"])
        # sem sessão, cai pro par (email, token)
        resolved_by_token = resolve_billing_user(None, {"email": record["email"], "token": token})
        self.assertEqual(resolved_by_token["id"], record["id"])
        self.assertIsNone(resolve_billing_user(None, {"email": record["email"], "token": "errado"}))

    def test_asaas_client_sends_access_token_header_not_bearer(self):
        captured = {}

        class FakeResponse:
            def __init__(self, body):
                self.body = body
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return self.body

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
            captured["body"] = json.loads(request.data.decode("utf-8")) if request.data else None
            return FakeResponse(b'{"id": "cus_123"}')

        with patch.dict(os.environ, {"ASAAS_API_KEY": "test-key", "ASAAS_ENVIRONMENT": "sandbox"}, clear=False):
            with patch("asaas_client.urllib.request.urlopen", side_effect=fake_urlopen):
                result = asaas_client.create_customer("Fulano", "fulano@test.local", "user-123")

        self.assertEqual(result["id"], "cus_123")
        self.assertEqual(captured["headers"].get("access_token"), "test-key")
        self.assertNotIn("authorization", captured["headers"])
        self.assertTrue(captured["url"].startswith(asaas_client.SANDBOX_BASE_URL))
        self.assertEqual(captured["body"]["externalReference"], "user-123")

    def test_create_checkout_session_builds_composite_external_reference(self):
        record, _, _ = register_user("checkout@test.local", "user-password")
        captured = {}

        def fake_get_or_create_customer(name, email, external_reference):
            captured["customer_external_reference"] = external_reference
            return {"id": "cus_999"}

        def fake_create_subscription_checkout(**kwargs):
            captured.update(kwargs)
            return {"link": "https://asaas.example/checkout/abc"}

        with patch.dict(os.environ, {"ASAAS_API_KEY": "test-key"}, clear=False):
            with patch("server.asaas_client.get_or_create_customer", side_effect=fake_get_or_create_customer):
                with patch("server.asaas_client.create_subscription_checkout", side_effect=fake_create_subscription_checkout):
                    url = create_checkout_session(record, "gold", {"Host": "app.example"})

        self.assertEqual(url, "https://asaas.example/checkout/abc")
        self.assertEqual(captured["external_reference"], f"{record['id']}:gold")
        self.assertEqual(captured["customer_external_reference"], record["id"])
        self.assertIn("app.example", captured["success_url"])

    def test_create_checkout_session_requires_asaas_configured(self):
        record, _, _ = register_user("noasaas@test.local", "user-password")
        with patch.dict(os.environ, {"ASAAS_API_KEY": ""}, clear=False):
            with self.assertRaises(RuntimeError):
                create_checkout_session(record, "gold", {"Host": "app.example"})

    def test_webhook_signature_must_match_configured_token(self):
        with patch.dict(os.environ, {"ASAAS_WEBHOOK_TOKEN": "shared-secret"}, clear=False):
            self.assertTrue(verify_asaas_webhook({"asaas-access-token": "shared-secret"}))
            self.assertFalse(verify_asaas_webhook({"asaas-access-token": "wrong"}))
            self.assertFalse(verify_asaas_webhook({}))

    def test_webhook_payment_confirmed_activates_plan_from_external_reference(self):
        record, _, _ = register_user("webhook@test.local", "user-password")
        handle_asaas_webhook_event({
            "event": "PAYMENT_CONFIRMED",
            "payment": {"externalReference": f"{record['id']}:silver", "customer": "cus_1"},
        })
        updated = get_user_by_id(record["id"])
        self.assertEqual(updated["subscription_status"], "active")
        self.assertEqual(updated["plan_id"], "silver")
        self.assertEqual(updated["status"], "approved")

    def test_webhook_falls_back_to_asaas_customer_id_for_renewals(self):
        record, _, _ = register_user("renew@test.local", "user-password")
        activate_subscription(record["id"], "gold", asaas_customer_id="cus_42")
        handle_asaas_webhook_event({
            "event": "PAYMENT_RECEIVED",
            "payment": {"externalReference": "", "customer": "cus_42"},
        })
        updated = get_user_by_id(record["id"])
        self.assertEqual(updated["subscription_status"], "active")
        self.assertEqual(updated["plan_id"], "gold")

    def test_webhook_overdue_and_cancel_events_update_status(self):
        record, _, _ = register_user("overdue@test.local", "user-password")
        activate_subscription(record["id"], "silver", asaas_customer_id="cus_7")
        handle_asaas_webhook_event({"event": "PAYMENT_OVERDUE", "payment": {"customer": "cus_7"}})
        self.assertEqual(get_user_by_id(record["id"])["subscription_status"], "overdue")
        handle_asaas_webhook_event({"event": "SUBSCRIPTION_DELETED", "payment": {"customer": "cus_7"}})
        cancelled = get_user_by_id(record["id"])
        self.assertEqual(cancelled["subscription_status"], "cancelled")
        self.assertIsNone(cancelled["plan_id"])

    def test_webhook_unknown_user_is_ignored_without_error(self):
        handle_asaas_webhook_event({"event": "PAYMENT_CONFIRMED", "payment": {"externalReference": "nao-existe:gold"}})
        # não levanta exceção — apenas não encontra o usuário e ignora


if __name__ == "__main__":
    unittest.main()
