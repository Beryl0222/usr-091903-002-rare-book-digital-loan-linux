"""HTTP 端到端契约测试：从交付到公开展示、下架失效的完整链路。

时间窗口使用覆盖真实当前日期的长期授权；时区边界的精确判定在
``test_domain.py`` 中用可注入时钟覆盖。
"""

import base64
import hashlib
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from service import Handler
from domain import LoanService

MASTER_BYTES = b"SECRET-HIGH-RES-MASTER-TIFF-BYTES"
DERIV_BYTES = b"public-web-derivative-jpeg"


def b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # 每个测试类共享一个全新的领域服务
        cls.server.service = LoanService()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method: str, path: str, payload: dict | None = None):
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = Request(f"{self.base}{path}", data=data, headers=headers, method=method)
        with urlopen(req, timeout=3) as response:
            return response.status, json.load(response)

    def call_expect_error(self, method: str, path: str, payload: dict | None = None):
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(
            f"{self.base}{path}", data=data,
            headers={"Content-Type": "application/json; charset=utf-8"}, method=method,
        )
        with self.assertRaises(HTTPError) as ctx:
            urlopen(req, timeout=3)
        error = ctx.exception
        body = json.load(error)
        return error.code, body["error"]

    # ------------------------------------------------------------------

    def test_01_full_publish_flow_and_provenance(self):
        # 四家合作馆登记
        for code, name, tz in [
            ("JP", "日本机构", "Asia/Tokyo"),
            ("UK", "英国机构", "Europe/London"),
            ("FR", "法国机构", "Europe/Paris"),
            ("DE", "德国机构", "Europe/Berlin"),
        ]:
            status, body = self.call("POST", "/api/institutions",
                                     {"code": code, "name": name, "tz_name": tz})
            self.assertEqual(status, 201)
        self.call("POST", "/api/users", {"username": "curator", "role": "curator"})
        self.call("POST", "/api/users",
                  {"username": "jp", "role": "lender", "institution_code": "JP"})
        self.call("POST", "/api/users",
                  {"username": "uk", "role": "lender", "institution_code": "UK"})

        _, edition = self.call("POST", "/api/editions", {
            "actor": "curator",
            "work_title": "唐诗画谱",
            "holding_institution_code": "JP",
            "page_order": ["p1", "p2", "p3"],
            "defects": [{"page_label": "p2", "note": "虫蛀"}],
            "color": {
                "profile": "sRGB", "target": "ColorChecker",
                "white_point": "D50", "measured_by": "lab", "measured_at": "2026-09-01",
            },
        })
        self.edition_id = edition["edition_id"]

        # 校验值不符 → 422，且无母版入库
        code, error = self.call_expect_error("POST", "/api/masters/deliver", {
            "actor": "jp", "edition_id": self.edition_id,
            "payload_b64": b64(MASTER_BYTES), "checksum": "00" * 32,
            "media_type": "image/tiff", "resolution_ppi": 400,
        })
        self.assertEqual((code, error), (422, "checksum_mismatch"))

        _, master = self.call("POST", "/api/masters/deliver", {
            "actor": "jp", "edition_id": self.edition_id,
            "payload_b64": b64(MASTER_BYTES), "checksum": sha(MASTER_BYTES),
            "media_type": "image/tiff", "resolution_ppi": 400,
        })
        master_id = master["master_id"]

        # 跨馆交付 → 404（不透露他馆书目）
        code, error = self.call_expect_error("POST", "/api/masters/deliver", {
            "actor": "uk", "edition_id": self.edition_id,
            "payload_b64": b64(b"x"), "checksum": sha(b"x"),
            "media_type": "image/tiff", "resolution_ppi": 400,
        })
        self.assertEqual((code, error), (404, "not_found"))

        _, deriv = self.call("POST", f"/api/masters/{master_id}/derivatives", {
            "actor": "jp", "kind": "web",
            "payload_b64": b64(DERIV_BYTES), "checksum": sha(DERIV_BYTES),
            "max_long_edge_px": 1600,
        })
        deriv_id = deriv["derivative_id"]

        _, text = self.call("POST", f"/api/editions/{self.edition_id}/texts",
                            {"actor": "curator", "credit_author": "解说作者某"})
        text_id = text["text_id"]

        # 窗口覆盖当前真实日期，地域 CN
        for kind, actor, grantee in [
            ("image", "jp", "日本机构"),
            ("text", "curator", "解说作者某"),
        ]:
            self.call("POST", f"/api/editions/{self.edition_id}/licenses", {
                "actor": actor, "kind": kind, "grantee": grantee,
                "territories": ["CN"],
                "start_local": "2026-01-01 00:00",
                "end_local": "2030-12-31 23:59",
            })

        # 缺署名 → 批准拒绝
        code, error = self.call_expect_error(
            "POST", f"/api/editions/{self.edition_id}/approvals", {
                "actor": "curator", "derivative_id": deriv_id,
                "text_id": text_id, "territory": "CN", "credit_line": " ",
            })
        self.assertEqual((code, error), (409, "approval_rejected"))

        _, approval = self.call(
            "POST", f"/api/editions/{self.edition_id}/approvals", {
                "actor": "curator", "derivative_id": deriv_id,
                "text_id": text_id, "territory": "CN",
                "credit_line": "©日本机构藏 / 解说：解说作者某",
            })
        approval_id = approval["approval_id"]

        _, token = self.call("POST", f"/api/approvals/{approval_id}/tokens",
                             {"actor": "curator", "territory": "CN"})
        token_id, cache_key = token["token"], token["cache_key"]
        self.assertTrue(cache_key.startswith("v1:"))
        HttpApiTest.edition_id = self.edition_id
        HttpApiTest.token_id = token_id
        HttpApiTest.master_id = master_id

        # 公众访问
        status, served = self.call(
            "GET", f"/api/tokens/serve/{token_id}?territory=CN")
        self.assertEqual(status, 200)
        self.assertEqual(served["checksum"], sha(DERIV_BYTES))
        self.assertIn("credit_line", served)

        # 公开溯源：说清母版与批准
        _, prov = self.call(
            "GET", f"/api/public/editions/{self.edition_id}/provenance?territory=CN")
        self.assertEqual(prov["status"], "available")
        self.assertEqual(prov["master"]["checksum_sha256"], sha(MASTER_BYTES))
        self.assertEqual(prov["approval"]["approval_id"], approval_id)

        # 任何响应都不得携带母版字节或母版直链
        served_blob = json.dumps(served, ensure_ascii=False)
        prov_blob = json.dumps(prov, ensure_ascii=False)
        self.assertNotIn("SECRET-HIGH-RES", served_blob)
        self.assertNotIn("SECRET-HIGH-RES", prov_blob)
        self.assertNotIn(master_id, served_blob)

    def test_02_takedown_kills_public_link(self):
        edition_id = HttpApiTest.edition_id

        # 临时下架
        status, _ = self.call(
            "POST", f"/api/editions/{edition_id}/takedown",
            {"actor": "jp", "reason": "权利复核"})
        self.assertEqual(status, 200)

        # 旧公开链接立即失效
        code, error = self.call_expect_error(
            "GET", f"/api/tokens/serve/{HttpApiTest.token_id}?territory=CN")
        self.assertEqual((code, error), (403, "access_denied"))

        # 溯源页显示不可用
        _, prov = self.call(
            "GET", f"/api/public/editions/{edition_id}/provenance?territory=CN")
        self.assertEqual(prov["status"], "unavailable")

        # 馆藏方报告（下架后获取）保留完整历史记录，但不含母版字节
        _, report = self.call("GET", "/api/lender/report?actor=jp")
        blob = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("SECRET-HIGH-RES", blob)
        # 下架后报告里记录了 takedown 事件
        actions = [r["action"] for r in report["records"]]
        self.assertIn("edition.takedown", actions)

    def test_03_lender_isolation(self):
        # 日馆馆藏报告只能看到日馆版本；为英馆单独建一个版本验证隔离
        _, uk_edition = self.call("POST", "/api/editions", {
            "actor": "uk", "work_title": "英藏本",
            "holding_institution_code": "UK", "page_order": ["a", "b"],
        })
        uk_id = uk_edition["edition_id"]
        _, jp_report = self.call("GET", "/api/lender/report?actor=jp")
        self.assertNotIn(uk_id, json.dumps(jp_report))
        _, uk_report = self.call("GET", "/api/lender/report?actor=uk")
        self.assertIn(uk_id, uk_report["editions_visible"])
        # 策展人不能取馆藏方报告
        code, error = self.call_expect_error(
            "GET", "/api/lender/report?actor=curator")
        self.assertEqual((code, error), (403, "forbidden"))

    def test_04_export_bundle_contains_no_master(self):
        # 日馆版本已下架：导出包应为空且明确不含母版
        _, report = self.call("GET", "/api/lender/report?actor=jp")
        jp_editions = [
            r["edition_id"] for r in report["records"]
            if r["action"] == "edition.create"
        ]
        edition_id = jp_editions[0]
        _, bundle = self.call(
            "POST", f"/api/editions/{edition_id}/export",
            {"actor": "curator", "territory": "CN"})
        self.assertFalse(bundle["contains_master"])
        self.assertEqual(bundle["items"], [])
        self.assertNotIn("SECRET-HIGH-RES", json.dumps(bundle))

    def test_05_health_contract_remains_compatible(self):
        with urlopen(f"{self.base}/health", timeout=2) as response:
            self.assertEqual(response.status, 200)
            payload = json.load(response)
        self.assertEqual(payload["service"], "rare-book-digital-loan")
        self.assertEqual(payload["name"], "古籍数字借展管控")


if __name__ == "__main__":
    unittest.main()
