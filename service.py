"""古籍数字借展管控的基础运行入口。"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from loan.api import LoanApi

SERVICE_ID = "rare-book-digital-loan"
SERVICE_NAME = "古籍数字借展管控"

API = LoanApi()


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """提供健康检查，并把 /v1/ 下的请求交给领域接口。"""

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(200, health_payload())
            return
        if parsed.path.startswith("/v1/"):
            status, payload = API.dispatch("GET", parsed.path, parse_qs(parsed.query), None)
            self._send_json(status, payload)
            return
        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/v1/"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self._send_json(400, {"error": "请求体不是合法 JSON"})
            return
        status, payload = API.dispatch("POST", parsed.path, {}, body)
        self._send_json(status, payload)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
