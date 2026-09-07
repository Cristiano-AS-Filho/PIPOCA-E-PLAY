from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import api_core  # noqa: E402
from auth import read_session  # noqa: E402
from serverless_utils import read_json_or_empty, send_result  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        send_result(self, api_core.admin_overview(read_session(self.headers.get("Cookie"))))

    def do_POST(self):
        session = read_session(self.headers.get("Cookie"))
        send_result(self, api_core.admin_action(session, read_json_or_empty(self, 4_096)))

    def do_PUT(self):
        self.do_POST()
