import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from serverless_utils import read_json, send_json  # noqa: E402
from user_store import StorageError, register_user  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            body = read_json(self, 4_096)
            email = body.get("email", "")
            password = body.get("password", "")
            confirmation = body.get("confirmation", body.get("password_confirmation"))
            if not isinstance(email, str) or not isinstance(password, str) or (confirmation is not None and not isinstance(confirmation, str)):
                raise ValueError("Informe e-mail e senha.")
            record, token, was_reopened = register_user(email, password, confirmation)
            send_json(
                self,
                HTTPStatus.OK if was_reopened else HTTPStatus.CREATED,
                {
                    "registered": True,
                    "email": record["email"],
                    "status": record["status"],
                    "token": token,
                    "message": "Seu acesso será liberado assim que o administrador validar. Por favor, aguarde a liberação.",
                },
            )
        except FileExistsError as error:
            send_json(self, HTTPStatus.CONFLICT, {"error": str(error)})
        except StorageError as error:
            send_json(self, HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
        except (ValueError, json.JSONDecodeError) as error:
            send_json(self, HTTPStatus.BAD_REQUEST, {"error": str(error)})

    def do_GET(self):
        send_json(self, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Use POST nesta rota."})
