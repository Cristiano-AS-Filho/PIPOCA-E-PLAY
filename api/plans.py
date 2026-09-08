"""Vitrine pública dos planos de assinatura."""

from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_core  # noqa: E402
from serverless_utils import send_result  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        send_result(self, api_core.plans_catalog())
