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

from auth import authenticate, create_session, read_session  # noqa: E402
from metadata import NullMetadataProvider, TMDBMetadataProvider  # noqa: E402
from rate_limit import check_rate_limit  # noqa: E402
from server import (  # noqa: E402
    RECOMMENDATION_SCHEMA,
    buildRecommendationPrompt,
    clean_filters,
    duration_violations,
    validate_recommendation_payload,
)
from user_store import (  # noqa: E402
    admin_summary,
    admin_users,
    delete_user,
    get_status_by_token,
    register_user,
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


if __name__ == "__main__":
    unittest.main()
