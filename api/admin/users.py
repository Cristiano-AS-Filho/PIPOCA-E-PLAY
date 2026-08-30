import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from auth import public_user, read_session  # noqa: E402
from serverless_utils import read_json, send_json  # noqa: E402
from user_store import StorageError, admin_summary, admin_users, delete_user, update_user_status  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def _admin(self):
        user = read_session(self.headers.get("Cookie"))
        if not user or user.get("role") != "admin":
            send_json(self, HTTPStatus.FORBIDDEN, {"error": "Acesso reservado ao administrador."})
            return None
        return user

    def do_GET(self):
        user = self._admin()
        if not user:
            return
        try:
            send_json(self, HTTPStatus.OK, {"user": public_user(user), "summary": admin_summary(), "users": admin_users()})
        except StorageError as error:
            send_json(self, HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})

    def do_POST(self):
        user = self._admin()
        if not user:
            return
        try:
            body = read_json(self, 4_096)
            action = body.get("action", "")
            user_id = body.get("user_id", "")
            if not isinstance(action, str) or not isinstance(user_id, str) or not user_id:
                raise ValueError("Ação administrativa inválida.")
            if action == "delete":
                delete_user(user_id)
                payload = {"deleted": True}
            elif action in {"approve", "reject"}:
                payload = {"user": update_user_status(user_id, "approved" if action == "approve" else "rejected")}
            else:
                raise ValueError("Ação administrativa inválida.")
            payload["summary"] = admin_summary()
            payload["users"] = admin_users()
            send_json(self, HTTPStatus.OK, payload)
        except LookupError as error:
            send_json(self, HTTPStatus.NOT_FOUND, {"error": str(error)})
        except StorageError as error:
            send_json(self, HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
        except (ValueError, json.JSONDecodeError) as error:
            send_json(self, HTTPStatus.BAD_REQUEST, {"error": str(error)})

    def do_PUT(self):
        self.do_POST()
