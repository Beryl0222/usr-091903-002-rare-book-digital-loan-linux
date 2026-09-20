"""领域核心的契约测试：覆盖全部数字借展合规场景。"""

import json
import unittest
from datetime import datetime, timezone

from domain import (
    AccessDenied,
    ApprovalError,
    ChecksumMismatch,
    LoanService,
    NotFound,
    PermissionDenied,
    ValidationError,
)


class Clock:
    """可钉住、可拨快的 UTC 时钟，用于精确验证时区边界。"""

    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def set(self, t: datetime):
        self.t = t


def utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


MASTER_BYTES = b"SECRET-HIGH-RES-MASTER-PAYLOAD-400DPI-TIFF"
DERIV_BYTES = b"low-res-web-jpeg-derivative"
OTHER_DERIV = b"thumb-derivative"


def sha(payload: bytes) -> str:
    import hashlib
    return hashlib.sha256(payload).hexdigest()


class LoanServiceTestBase(unittest.TestCase):
    clock: Clock

    def setUp(self):
        # 固定在一个窗口内的“当前时间”：2026-10-01 12:00 UTC
        self.clock = Clock(utc(2026, 10, 1, 12))
        self.svc = LoanService(now_fn=self.clock)
        # 四家合作馆：英、法、日、德（本测试主要用日、英两馆验证隔离）
        self.svc.register_institution("JP", "日本机构", "Asia/Tokyo")
        self.svc.register_institution("UK", "英国机构", "Europe/London")
        self.svc.register_institution("FR", "法国机构", "Europe/Paris")
        self.svc.register_institution("DE", "德国机构", "Europe/Berlin")
        self.svc.register_user("curator", "curator")
        self.svc.register_user("jp", "lender", "JP")
        self.svc.register_user("uk", "lender", "UK")

    def _edition(self, title="唐诗画谱", inst="JP", pages=None):
        return self.svc.create_edition(
            "curator", title, inst,
            pages or ["p1", "p2", "p3", "p4"],
            defects=[{"page_label": "p2", "note": "右下角虫蛀约 3mm"}],
            color={
                "profile": "ISO 12646 / sRGB",
                "target": "X-Rite ColorChecker SG",
                "white_point": "D50",
                "measured_by": "JP-scan-lab",
                "measured_at": "2026-09-01",
            },
        )

    def _full_publication(self, edition_id, territory="CN", embargo=None,
                          image_by="jp", start="2026-09-01 09:00", end="2026-12-31 23:59"):
        """建母版→衍生图→解说→双重许可→批准→令牌，返回各 id。"""
        master_id = self.svc.deliver_master(
            image_by, edition_id, MASTER_BYTES, sha(MASTER_BYTES),
            "image/tiff", 400, embargo_until_local=embargo,
        )
        deriv_id = self.svc.register_derivative(
            image_by, master_id, "web", DERIV_BYTES, sha(DERIV_BYTES), 1600,
        )
        text_id = self.svc.register_text("curator", edition_id, "解说作者某")
        self.svc.grant_license(
            image_by, edition_id, "image", "日本机构",
            [territory], start, end,
        )
        self.svc.grant_license(
            "curator", edition_id, "text", "解说作者某",
            [territory], start, end,
        )
        approval_id = self.svc.approve_publication(
            "curator", edition_id, deriv_id, text_id,
            territory, "©日本机构藏 / 摄影：某某 / 解说：解说作者某",
        )
        token = self.svc.issue_token("curator", approval_id, territory)
        return {
            "master_id": master_id,
            "deriv_id": deriv_id,
            "text_id": text_id,
            "approval_id": approval_id,
            "token": token["token"],
            "cache_key": token["cache_key"],
        }


class DeliveryTests(LoanServiceTestBase):
    def test_checksum_mismatch_is_rejected_and_nothing_stored(self):
        edition_id = self._edition()
        with self.assertRaises(ChecksumMismatch):
            self.svc.deliver_master(
                "jp", edition_id, MASTER_BYTES, "deadbeef" * 8,
                "image/tiff", 400,
            )
        self.assertEqual(self.svc.masters, {})

    def test_only_owner_may_deliver(self):
        edition_id = self._edition()
        with self.assertRaises(NotFound):  # 跨馆统一报不存在，避免侧信道
            self.svc.deliver_master(
                "uk", edition_id, MASTER_BYTES, sha(MASTER_BYTES),
                "image/tiff", 400,
            )

    def test_master_bytes_are_never_retained(self):
        edition_id = self._edition()
        self.svc.deliver_master(
            "jp", edition_id, MASTER_BYTES, sha(MASTER_BYTES), "image/tiff", 400,
        )

        def scan(obj):
            if isinstance(obj, (bytes, bytearray)):
                self.assertNotIn(MASTER_BYTES, bytes(obj))
            elif isinstance(obj, dict):
                for v in obj.values():
                    scan(v)
            elif isinstance(obj, (list, tuple, set)):
                for v in obj:
                    scan(v)

        scan(self.svc.__dict__)


class PublicationGateTests(LoanServiceTestBase):
    def test_embargo_blocks_approval_until_opening(self):
        edition_id = self._edition()
        # 母版封闭至东京时间 2026-10-02 09:00（= UTC 10-02 00:00）
        with self.assertRaises(ApprovalError):
            self._full_publication(edition_id, embargo="2026-10-02 09:00")
        # 封闭期一过即可批准
        self.clock.set(utc(2026, 10, 2, 0, 1))
        self._full_publication(edition_id, embargo="2026-10-02 09:00")

    def test_missing_image_license_rejected(self):
        edition_id = self._edition()
        master_id = self.svc.deliver_master(
            "jp", edition_id, MASTER_BYTES, sha(MASTER_BYTES), "image/tiff", 400)
        deriv_id = self.svc.register_derivative(
            "jp", master_id, "web", DERIV_BYTES, sha(DERIV_BYTES), 1600)
        text_id = self.svc.register_text("curator", edition_id, "解说作者某")
        self.svc.grant_license("curator", edition_id, "text", "解说作者某",
                               ["CN"], "2026-09-01", "2026-12-31")
        with self.assertRaises(ApprovalError) as ctx:
            self.svc.approve_publication(
                "curator", edition_id, deriv_id, text_id, "CN", "署名")
        self.assertIn("图像许可", str(ctx.exception))

    def test_missing_text_license_rejected(self):
        edition_id = self._edition()
        master_id = self.svc.deliver_master(
            "jp", edition_id, MASTER_BYTES, sha(MASTER_BYTES), "image/tiff", 400)
        deriv_id = self.svc.register_derivative(
            "jp", master_id, "web", DERIV_BYTES, sha(DERIV_BYTES), 1600)
        text_id = self.svc.register_text("curator", edition_id, "解说作者某")
        self.svc.grant_license("jp", edition_id, "image", "日本机构",
                               ["CN"], "2026-09-01", "2026-12-31")
        with self.assertRaises(ApprovalError) as ctx:
            self.svc.approve_publication(
                "curator", edition_id, deriv_id, text_id, "CN", "署名")
        self.assertIn("文字解说许可", str(ctx.exception))

    def test_missing_credit_line_rejected(self):
        edition_id = self._edition()
        master_id = self.svc.deliver_master(
            "jp", edition_id, MASTER_BYTES, sha(MASTER_BYTES), "image/tiff", 400)
        deriv_id = self.svc.register_derivative(
            "jp", master_id, "web", DERIV_BYTES, sha(DERIV_BYTES), 1600)
        text_id = self.svc.register_text("curator", edition_id, "解说作者某")
        self.svc.grant_license("jp", edition_id, "image", "日本机构",
                               ["CN"], "2026-09-01", "2026-12-31")
        self.svc.grant_license("curator", edition_id, "text", "解说作者某",
                               ["CN"], "2026-09-01", "2026-12-31")
        with self.assertRaises(ApprovalError):
            self.svc.approve_publication(
                "curator", edition_id, deriv_id, text_id, "CN", "  ")

    def test_territory_not_covered_rejected(self):
        edition_id = self._edition()
        info = self._full_publication(edition_id, territory="CN")
        # 同一时刻在未授权地域（US）无法签发链接
        with self.assertRaises(AccessDenied):
            self.svc.issue_token("curator", info["approval_id"], "US")

    def test_master_itself_cannot_be_registered_as_derivative(self):
        edition_id = self._edition()
        master_id = self.svc.deliver_master(
            "jp", edition_id, MASTER_BYTES, sha(MASTER_BYTES), "image/tiff", 400)
        with self.assertRaises(ValidationError):
            self.svc.register_derivative(
                "jp", master_id, "web", MASTER_BYTES, sha(MASTER_BYTES), 12000)


class TimezoneWindowTests(LoanServiceTestBase):
    """窗口以馆藏方当地时间授予，换算为绝对 UTC；服务器所在地不影响结果。"""

    def test_window_does_not_start_early_across_timezones(self):
        edition_id = self._edition()
        # 东京 2026-10-01 09:00 开窗 == UTC 2026-10-01 00:00
        info = self._full_publication(
            edition_id, start="2026-10-01 09:00", end="2026-10-31 09:00")
        # 开窗前一分钟（UTC 9-30 23:59）不得访问
        self.clock.set(utc(2026, 9, 30, 23, 59))
        with self.assertRaises(AccessDenied):
            self.svc.serve(info["token"], "CN")
        # 开窗瞬间可以访问
        self.clock.set(utc(2026, 10, 1, 0, 0))
        self.assertEqual(self.svc.serve(info["token"], "CN")["kind"], "derivative")

    def test_access_expires_exactly_at_local_end(self):
        edition_id = self._edition()
        # 东京 2026-10-31 09:00 关窗 == UTC 2026-10-31 00:00
        info = self._full_publication(
            edition_id, start="2026-10-01 09:00", end="2026-10-31 09:00")
        self.clock.set(utc(2026, 10, 30, 23, 59))
        self.svc.serve(info["token"], "CN")  # 关窗前一分钟仍可访问
        self.clock.set(utc(2026, 10, 31, 0, 0))
        with self.assertRaises(AccessDenied):
            self.svc.serve(info["token"], "CN")  # 逾期一分钟即拒

    def test_london_dst_boundary_window_uses_absolute_time(self):
        edition_id = self._edition(inst="UK")
        # 伦敦夏令时结束日：当地窗口跨 2026-10-25（英国 2026-10-25 02:00 拨回）
        # 批准要求许可在批准时刻有效，因此先把时钟拨入窗内
        self.clock.set(utc(2026, 10, 25, 12, 0))
        info = self._full_publication(
            edition_id, image_by="uk",
            start="2026-10-24 00:00", end="2026-10-26 00:00")
        # 伦敦 10-25 12:00（GMT，UTC+0）必然在窗内
        self.svc.serve(info["token"], "CN")
        # 窗边界按绝对时间：伦敦 10-26 00:00（GMT）== UTC 10-26 00:00 即失效
        self.clock.set(utc(2026, 10, 26, 0, 0))
        with self.assertRaises(AccessDenied):
            self.svc.serve(info["token"], "CN")


class DecisionChangeTests(LoanServiceTestBase):
    def test_replace_scan_kills_links_cache_but_old_version_restorable(self):
        edition_id = self._edition()
        info = self._full_publication(edition_id)
        self.assertTrue(info["cache_key"].startswith("v1:"))
        self.svc.serve(info["token"], "CN")  # 替换前可访问

        new_bytes = b"SECOND-SCAN-MASTER-PAYLOAD"
        new_master = self.svc.replace_master(
            "jp", info["master_id"], new_bytes, sha(new_bytes), "image/tiff", 400)
        # 旧链接立即失效
        with self.assertRaises(AccessDenied):
            self.svc.serve(info["token"], "CN")
        # 旧批准被挡：必须基于新母版重做衍生图并重新批准
        with self.assertRaises(ApprovalError):
            self.svc.approve_publication(
                "curator", edition_id, info["deriv_id"], info["text_id"],
                "CN", "©日本机构藏")
        new_deriv = b"new-web-derivative"
        deriv2 = self.svc.register_derivative(
            "jp", new_master, "web", new_deriv, sha(new_deriv), 1600)
        appr2 = self.svc.approve_publication(
            "curator", edition_id, deriv2, info["text_id"],
            "CN", "©日本机构藏 / 新扫描")
        token2 = self.svc.issue_token("curator", appr2, "CN")
        self.assertTrue(token2["cache_key"].startswith("v2:"))  # 缓存键纪元变化
        self.svc.serve(token2["token"], "CN")
        # 已对外展示过的旧版本仍可按其批准快照复原（需指定是哪次批准）
        restored = self.svc.restore_view(
            "curator", edition_id, approval_id=info["approval_id"])
        self.assertEqual(restored["snapshot"]["master_checksum"], sha(MASTER_BYTES))
        self.assertEqual(restored["snapshot"]["derivative_checksum"], sha(DERIV_BYTES))
        # 复原清单同时列出全部可复原的历史展示
        restored_ids = {a["approval_id"] for a in restored["restorable_approvals"]}
        self.assertIn(info["approval_id"], restored_ids)
        self.assertIn(appr2, restored_ids)

    def test_shorten_license_invalidates_links_before_original_end(self):
        edition_id = self._edition()
        info = self._full_publication(
            edition_id, start="2026-09-01 09:00", end="2026-12-31 23:59")
        # 馆方把授权缩短到东京 2026-10-02 09:00（UTC 10-02 00:00）
        image_lic = next(
            lid for lid, lic in self.svc.licenses.items()
            if lic.edition_id == edition_id and lic.kind == "image")
        self.svc.shorten_license("jp", image_lic, "2026-10-02 09:00")
        # 旧许可截止日前很久，但链接已按新决定失效
        self.clock.set(utc(2026, 10, 2, 0, 1))
        with self.assertRaises(AccessDenied):
            self.svc.serve(info["token"], "CN")
        # 缩短不能延长
        with self.assertRaises(ValidationError):
            self.svc.shorten_license("jp", image_lic, "2027-01-01 00:00")

    def test_takedown_revokes_everything_immediately_but_history_remains(self):
        edition_id = self._edition()
        info = self._full_publication(edition_id)
        self.svc.takedown("jp", edition_id, "权利复核，临时下架")
        with self.assertRaises(AccessDenied):
            self.svc.serve(info["token"], "CN")
        prov = self.svc.public_provenance(edition_id, "CN")
        self.assertEqual(prov["status"], "unavailable")
        # 历史快照仍在
        restored = self.svc.restore_view("curator", edition_id)
        self.assertTrue(restored["restored"])

    def test_page_order_correction_freezes_old_version_and_invalidates_links(self):
        edition_id = self._edition(pages=["p1", "p2", "p3", "p4"])
        info = self._full_publication(edition_id)
        new_edition = self.svc.correct_page_order(
            "curator", edition_id, ["p1", "p3", "p2", "p4"])
        self.assertNotEqual(new_edition, edition_id)
        # 旧版本链接因决策纪元推进而失效
        with self.assertRaises(AccessDenied):
            self.svc.serve(info["token"], "CN")
        # 旧版本冻结且可复原（页序是旧的）
        restored = self.svc.restore_view("curator", edition_id)
        self.assertEqual(restored["snapshot"]["page_order"], ["p1", "p2", "p3", "p4"])
        # 新版本保留谱系
        self.assertEqual(self.svc.editions[edition_id].superseded_by, new_edition)
        self.assertEqual(self.svc.editions[new_edition].version, 2)
        # 不能借“修正”增删页
        with self.assertRaises(ValidationError):
            self.svc.correct_page_order("curator", edition_id, ["p1", "p2", "p3"])


class IsolationAndLeakageTests(LoanServiceTestBase):
    def test_lender_sees_only_own_institution_records(self):
        jp_edition = self._edition(title="日藏本", inst="JP")
        uk_edition = self._edition(title="英藏本", inst="UK")
        self._full_publication(jp_edition, image_by="jp")
        # 英馆也交付自己的书
        self._full_publication(uk_edition, image_by="uk")
        jp_report = self.svc.lender_report("jp")
        jp_editions_in_report = {r["edition_id"] for r in jp_report["records"]}
        self.assertIn(jp_edition, jp_editions_in_report)
        self.assertNotIn(uk_edition, jp_editions_in_report)
        self.assertEqual(jp_report["editions_visible"], [jp_edition])
        # 日馆不能查英馆版本（统一 404）
        with self.assertRaises(NotFound):
            self.svc.deliver_master(
                "jp", uk_edition, b"x", sha(b"x"), "image/tiff", 400)
        # 馆藏方台账也只含本馆事件
        jp_ledger = self.svc.ledger_view("jp")
        self.assertTrue(all(
            e["edition_id"] in {jp_edition} for e in jp_ledger))

    def test_curator_cannot_act_as_lender(self):
        edition_id = self._edition()
        with self.assertRaises(PermissionDenied):
            self.svc.takedown("curator", edition_id, "策展人无权下架")

    def test_no_master_content_in_any_serialized_output(self):
        edition_id = self._edition(title="渗漏检查本")
        info = self._full_publication(edition_id)
        self.svc.serve(info["token"], "CN")
        bundle = self.svc.export_bundle("curator", edition_id, "CN")
        prov = self.svc.public_provenance(edition_id, "CN")
        report = self.svc.lender_report("jp")
        ledger = self.svc.ledger_view("curator")

        blob = json.dumps(
            {"bundle": bundle, "prov": prov, "report": report, "ledger": ledger},
            ensure_ascii=False, default=str,
        )
        # 高清母版字节绝不出现在任何输出中
        self.assertNotIn("SECRET-HIGH-RES", blob)
        # 导出包不含母版直链，只有衍生图项
        self.assertFalse(bundle["contains_master"])
        self.assertTrue(bundle["items"])
        for item in bundle["items"]:
            self.assertNotIn("master", item["download_ref"])
        # 公开服务载荷不直接出现母版 id（只给溯源地址）
        served = self.svc.serve(info["token"], "CN")
        self.assertNotIn(info["master_id"], json.dumps(served))
        # 溯源页只暴露母版 id 与校验值，不含任何可下载引用
        self.assertEqual(
            set(prov["master"].keys()), {"master_id", "checksum_sha256"})
        # 缓存键/令牌与批准的对应关系可在溯源页说明
        self.assertEqual(prov["approval"]["approval_id"], info["approval_id"])

    def test_export_bundle_invalidates_after_decision_change(self):
        edition_id = self._edition()
        info = self._full_publication(edition_id)
        bundle = self.svc.export_bundle("curator", edition_id, "CN")
        self.assertEqual(bundle["bundle_epoch"], 1)
        download_ref = bundle["items"][0]["download_ref"]
        self.svc.takedown("jp", edition_id, "临时下架")
        # 导出包中的下载链接随即失效
        token_id = download_ref.rsplit("/", 1)[-1]
        with self.assertRaises(AccessDenied):
            self.svc.serve(token_id, "CN")
        # 重新生成导出包时项目为空
        bundle2 = self.svc.export_bundle("curator", edition_id, "CN")
        self.assertEqual(bundle2["items"], [])


if __name__ == "__main__":
    unittest.main()
