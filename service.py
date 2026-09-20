"""古籍数字借展管控的运行入口与 JSON HTTP 接口。

健康检查保持向后兼容：``GET /health`` 与 ``python3 service.py --check``。

其余 ``/api/*`` 为领域接口。所有请求/响应均为 JSON；交付文件以
base64 放在 ``payload_b64`` 字段，服务端只在内存中完成校验值核验，
不把文件内容写入日志、响应或任何导出物。
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from domain import (
    AccessDenied,
    ApprovalError,
    ChecksumMismatch,
    DomainError,
    LoanService,
    NotFound,
    PermissionDenied,
    ValidationError,
)

SERVICE_ID = "rare-book-digital-loan"
SERVICE_NAME = "古籍数字借展管控"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_service() -> LoanService:
    """构造一个空白领域服务（不预置任何书目）。"""
    return LoanService()


# (method, prefix) -> handler 名，路径参数在末尾按段解析
class Handler(BaseHTTPRequestHandler):
    """提供健康检查与数字借展领域接口。"""

    server_version = "RareBookLoan/1.0"

    # -- 基础收发 ---------------------------------------------------------

    def _json_response(self, status: int, payload: dict | list):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 公开响应也不得被共享缓存意外保留：缓存键以决策纪元为准，
        # 且授权窗口到期或下架后必须重新校验。
        self.send_header("Cache-Control", "private, no-transform")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValidationError("请求体必须是 UTF-8 JSON")
        if not isinstance(data, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return data

    def _query(self) -> dict[str, str]:
        parsed = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in parsed.items()}

    def _b64_payload(self, data: dict) -> bytes:
        text = data.get("payload_b64")
        if not isinstance(text, str):
            raise ValidationError("缺少 payload_b64（交付文件的 base64）")
        try:
            return base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError("payload_b64 不是合法 base64") from exc

    # -- 路由 -------------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/health":
                return self._json_response(200, health_payload())
            if path.startswith("/api/tokens/serve/"):
                token_id = path.rsplit("/", 1)[-1]
                return self._serve(token_id)
            if path.startswith("/api/public/editions/") and path.endswith("/provenance"):
                edition_id = path.split("/")[4]
                return self._provenance(edition_id)
            if path == "/api/lender/report":
                return self._lender_report()
            if path == "/api/ledger":
                return self._ledger()
            self._json_response(404, {"error": "not_found", "message": "未知接口"})
        except DomainError as exc:
            self._domain_error(exc)

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        routes = {
            "/api/institutions": self._register_institution,
            "/api/users": self._register_user,
            "/api/editions": self._create_edition,
            "/api/masters/deliver": self._deliver_master,
        }
        try:
            if path in routes:
                return routes[path]()
            parts = path.split("/")
            # /api/editions/{id}/...
            if len(parts) >= 5 and parts[1] == "api" and parts[2] == "editions":
                edition_id = parts[3]
                action = "/".join(parts[4:])
                return self._edition_action(edition_id, action)
            # /api/masters/{id}/replace | /api/masters/{id}/derivatives
            if len(parts) == 5 and parts[1] == "api" and parts[2] == "masters":
                master_id, action = parts[3], parts[4]
                data = self._read_json()
                if action == "replace":
                    return self._replace_master(master_id, data)
                if action == "derivatives":
                    return self._register_derivative(master_id, data)
            # /api/licenses/{id}/shorten
            if len(parts) == 5 and parts[1] == "api" and parts[2] == "licenses" and parts[4] == "shorten":
                return self._shorten_license(parts[3], self._read_json())
            # /api/approvals/{id}/tokens
            if len(parts) == 5 and parts[1] == "api" and parts[2] == "approvals" and parts[4] == "tokens":
                return self._issue_token(parts[3], self._read_json())
            self._json_response(404, {"error": "not_found", "message": "未知接口"})
        except DomainError as exc:
            self._domain_error(exc)

    def _edition_action(self, edition_id: str, action: str):
        data = self._read_json()
        service: LoanService = self.server.service  # type: ignore[attr-defined]
        if action == "correct-page-order":
            new_id = service.correct_page_order(data["actor"], edition_id, data["page_order"])
            return self._json_response(200, {"edition_id": new_id})
        if action == "licenses":
            license_id = service.grant_license(
                data["actor"], edition_id, data["kind"], data["grantee"],
                data["territories"], data["start_local"], data["end_local"],
            )
            return self._json_response(200, {"license_id": license_id})
        if action == "takedown":
            service.takedown(data["actor"], edition_id, data["reason"])
            return self._json_response(200, {"status": "taken_down"})
        if action == "texts":
            text_id = service.register_text(data["actor"], edition_id, data["credit_author"])
            return self._json_response(200, {"text_id": text_id})
        if action == "approvals":
            approval_id = service.approve_publication(
                data["actor"], edition_id, data["derivative_id"], data["text_id"],
                data["territory"], data["credit_line"],
            )
            return self._json_response(200, {"approval_id": approval_id})
        if action == "restore":
            return self._json_response(
                200, service.restore_view(data["actor"], edition_id,
                                          data.get("approval_id")))
        if action == "export":
            bundle = service.export_bundle(data["actor"], edition_id, data["territory"])
            return self._json_response(200, bundle)
        self._json_response(404, {"error": "not_found", "message": "未知版本操作"})

    # -- 具体接口 ---------------------------------------------------------

    def _register_institution(self):
        data = self._read_json()
        service: LoanService = self.server.service  # type: ignore[attr-defined]
        inst = service.register_institution(data["code"], data["name"], data["tz_name"])
        self._json_response(201, {"code": inst.code, "name": inst.name, "tz_name": inst.tz_name})

    def _register_user(self):
        data = self._read_json()
        service: LoanService = self.server.service  # type: ignore[attr-defined]
        user = service.register_user(data["username"], data["role"], data.get("institution_code"))
        self._json_response(201, {"username": user.username, "role": user.role})

    def _create_edition(self):
        data = self._read_json()
        service: LoanService = self.server.service  # type: ignore[attr-defined]
        edition_id = service.create_edition(
            data["actor"], data["work_title"], data["holding_institution_code"],
            data["page_order"], data.get("defects"), data.get("color"),
        )
        self._json_response(201, {"edition_id": edition_id})

    def _deliver_master(self):
        data = self._read_json()
        service: LoanService = self.server.service  # type: ignore[attr-defined]
        master_id = service.deliver_master(
            data["actor"], data["edition_id"], self._b64_payload(data),
            data["checksum"], data["media_type"], int(data["resolution_ppi"]),
            data.get("embargo_until_local"),
        )
        self._json_response(201, {"master_id": master_id})

    def _replace_master(self, master_id: str, data: dict):
        service: LoanService = self.server.service  # type: ignore[attr-defined]
        new_id = service.replace_master(
            data["actor"], master_id, self._b64_payload(data),
            data["checksum"], data["media_type"], int(data["resolution_ppi"]),
            data.get("embargo_until_local"),
        )
        self._json_response(200, {"master_id": new_id})

    def _register_derivative(self, master_id: str, data: dict):
        service: LoanService = self.server.service  # type: ignore[attr-defined]
        derivative_id = service.register_derivative(
            data["actor"], master_id, data["kind"], self._b64_payload(data),
            data["checksum"], int(data["max_long_edge_px"]),
        )
        self._json_response(201, {"derivative_id": derivative_id})

    def _shorten_license(self, license_id: str, data: dict):
        service: LoanService = self.server.service  # type: ignore[attr-defined]
        new_license_id = service.shorten_license(data["actor"], license_id, data["new_end_local"])
        self._json_response(200, {"license_id": new_license_id})

    def _issue_token(self, approval_id: str, data: dict):
        service: LoanService = self.server.service  # type: ignore[attr-defined]
        token = service.issue_token(
            data["actor"], approval_id, data["territory"], data.get("purpose", "view"),
        )
        self._json_response(200, token)

    def _serve(self, token_id: str):
        query = self._query()
        service: LoanService = self.server.service  # type: ignore[attr-defined]
        payload = service.serve(token_id, query.get("territory", ""))
        self._json_response(200, payload)

    def _provenance(self, edition_id: str):
        query = self._query()
        service: LoanService = self.server.service  # type: ignore[attr-defined]
        payload = service.public_provenance(edition_id, query.get("territory", ""))
        self._json_response(200, payload)

    def _lender_report(self):
        query = self._query()
        service: LoanService = self.server.service  # type: ignore[attr-defined]
        self._json_response(200, service.lender_report(query.get("actor", "")))

    def _ledger(self):
        query = self._query()
        service: LoanService = self.server.service  # type: ignore[attr-defined]
        events = service.ledger_view(query.get("actor", ""), query.get("edition_id"))
        self._json_response(200, {"events": events})

    def _domain_error(self, exc: DomainError):
        # 注意：错误响应只含分类与人工消息，绝不回显提交内容，避免高清数据落日志。
        status_map = {
            ValidationError: (400, "invalid_request"),
            ChecksumMismatch: (422, "checksum_mismatch"),
            ApprovalError: (409, "approval_rejected"),
            PermissionDenied: (403, "forbidden"),
            NotFound: (404, "not_found"),
            AccessDenied: (403, "access_denied"),
        }
        status, code = status_map.get(type(exc), (400, "domain_error"))
        self._json_response(status, {"error": code, "message": str(exc)})

    def log_message(self, *_args):
        # 访问日志关闭：防止请求内容（即便只含 id）进入未分级日志通道。
        return


def make_server(port: int = 0, service: LoanService | None = None) -> ThreadingHTTPServer:
    """创建服务器（测试可注入独立的领域服务）。"""
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.service = service or build_service()  # type: ignore[attr-defined]
    return server


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        # 同时自检领域服务可构造
        build_service()
        print("基础检查通过")
        return
    make_server(args.port).serve_forever()


if __name__ == "__main__":
    main()
