from http.server import BaseHTTPRequestHandler
from http import HTTPStatus
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import api_core  # noqa: E402
from serverless_utils import send_json, send_result  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        send_result(
            self,
            api_core.registration_status(params.get("email", [""])[0], params.get("token", [""])[0]),
        )

    def do_POST(self):
        send_json(self, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Use GET nesta rota."})
