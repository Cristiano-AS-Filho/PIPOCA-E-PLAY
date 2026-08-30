from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from auth import config_status, public_user, read_session  # noqa: E402
from serverless_utils import send_json  # noqa: E402
from user_store import StorageError, admin_summary, admin_users  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        user = read_session(self.headers.get("Cookie"))
        if not user or user.get("role") != "admin":
            send_json(self, HTTPStatus.FORBIDDEN, {"error": "Acesso reservado ao administrador."})
            return
        try:
            send_json(
                self,
                HTTPStatus.OK,
                {
                    "user": public_user(user),
                    "config": config_status(),
                    "summary": admin_summary(),
                    "users": admin_users(),
                },
                {"Cache-Control": "no-store"},
            )
        except StorageError as error:
            send_json(self, HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})

    def do_POST(self):
        send_json(self, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Use GET nesta rota."})
