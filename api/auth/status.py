from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from serverless_utils import send_json  # noqa: E402
from user_store import StorageError, get_status_by_token  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            params = parse_qs(urlparse(self.path).query)
            email = params.get("email", [""])[0]
            token = params.get("token", [""])[0]
            status = get_status_by_token(email, token)
            if not status:
                send_json(self, HTTPStatus.NOT_FOUND, {"error": "Pedido de acesso não encontrado."})
                return
            send_json(self, HTTPStatus.OK, {"email": email.strip().lower(), "status": status})
        except StorageError as error:
            send_json(self, HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})

    def do_POST(self):
        send_json(self, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Use GET nesta rota."})
