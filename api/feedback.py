"""Marcações do usuário: gostei, não gostei e já assisti."""

from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_core  # noqa: E402
from auth import read_session  # noqa: E402
from serverless_utils import read_json_or_empty, send_result  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def _session(self):
        return read_session(self.headers.get("Cookie"))

    def do_GET(self):
        send_result(self, api_core.feedback_overview(self._session()))

    def do_POST(self):
        body = read_json_or_empty(self, 4_096)
        session = self._session()
        # "remove" também chega por POST: alguns proxies descartam corpo em DELETE.
        if str(body.get("action", "")).lower() in {"remove", "delete"}:
            send_result(self, api_core.feedback_delete(session, body))
            return
        send_result(self, api_core.feedback_save(session, body))

    def do_DELETE(self):
        send_result(self, api_core.feedback_delete(self._session(), read_json_or_empty(self, 4_096)))
