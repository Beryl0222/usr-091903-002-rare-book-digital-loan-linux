"""数字借展领域规则测试：校验值、时区窗口、三重核对、封闭期、
替换/缩短/下架/复原、缓存失效、审计隔离与导出过滤。"""

import hashlib
import unittest
from datetime import datetime

from loan import audit, intake, licensing, publishing
from loan.models import SUBJECT_IMAGE, SUBJECT_TEXT, AuditEvent
from loan.store import (
    AuditContentError,
    ChecksumMismatch,
    DomainError,
    LicenseError,
    SealedError,
    Store,
)
from loan.api import LoanApi

OPENING = "2026-10-01T00:00:00+00:00"


def dt(text):
    return datetime.fromisoformat(text)


def sha(data):
    return hashlib.sha256(data).hexdigest()


MASTER_BYTES = b"master-scan-001"
PREVIEW_BYTES = b"preview-image"
HIGHRES_BYTES = b"highres-image"
REPLACEMENT_BYTES = b"replaced-master-scan"


def build_world(*, sealed_until=None, regions=("CN",), tz="Asia/Tokyo",
                start="2026-09-01", end="2026-12-31"):
    """搭出一套已交付、已授权、已批准、但尚未发布的完整世界。"""
    world = {}
    store = Store()
    world["store"] = store
    world["inst"] = intake.register_institution(store, name="东瀛馆", country="JP")
    world["other_inst"] = intake.register_institution(store, name="西洋馆", country="GB")
    world["work"] = intake.register_work(
        store,
        institution_id=world["inst"].id,
        title="唐诗画谱",
        edition="明万历集雅斋刻本",
        bibliography={"author": "黄凤池", "year": "1620"},
    )
    wid = world["work"].id
    world["master"] = intake.deliver_master(
        store,
        wid,
        data=MASTER_BYTES,
        sha256=sha(MASTER_BYTES),
        received_at=dt("2026-08-01T00:00:00+00:00"),
        sealed_until=dt(sealed_until) if sealed_until else None,
    )
    world["highres"] = intake.deliver_derivative(
        store, world["master"].id, kind="highres",
        data=HIGHRES_BYTES, sha256=sha(HIGHRES_BYTES),
        received_at=dt("2026-08-01T01:00:00+00:00"),
    )
    world["preview"] = intake.deliver_derivative(
        store, world["master"].id, kind="preview",
        data=PREVIEW_BYTES, sha256=sha(PREVIEW_BYTES),
        received_at=dt("2026-08-01T01:00:00+00:00"),
    )
    world["order"] = intake.set_pages(
        store, wid,
        [
            {"label": "甲一", "damage_note": "天头虫蛀",
             "color_calibration": {"profile": "FOGRA39", "delta_e": 1.2}},
            {"label": "甲二", "damage_note": "", "color_calibration": {"profile": "FOGRA39"}},
        ],
        at=dt("2026-08-02T00:00:00+00:00"),
    )
    world["lic_img"] = licensing.grant_license(
        store, wid, subject=SUBJECT_IMAGE, regions=list(regions),
        start_date=start, end_date=end, tz_name=tz,
        attribution="图片版权：东瀛馆", granted_by="jp-curator",
        at=dt("2026-08-03T00:00:00+00:00"),
    )
    world["lic_txt"] = licensing.grant_license(
        store, wid, subject=SUBJECT_TEXT, regions=list(regions),
        start_date=start, end_date=end, tz_name=tz,
        attribution="解说版权：编委会", granted_by="editor",
        at=dt("2026-08-03T01:00:00+00:00"),
    )
    world["app_img"] = licensing.approve(
        store, world["lic_img"].id, by="jp-curator", at=dt("2026-08-04T00:00:00+00:00"))
    world["app_txt"] = licensing.approve(
        store, world["lic_txt"].id, by="editor", at=dt("2026-08-04T01:00:00+00:00"))
    return world


def publish_world(world, *, at="2026-09-15T00:00:00+00:00", region="CN",
                  include_highres=True):
    return publishing.publish(
        world["store"], world["work"].id, region=region,
        at=dt(at), by="publisher", include_highres=include_highres,
    )


class IntakeTest(unittest.TestCase):
    def test_checksum_mismatch_is_rejected(self):
        world = build_world()
        with self.assertRaises(ChecksumMismatch):
            intake.deliver_master(
                world["store"], world["work"].id,
                data=b"tampered", sha256=sha(MASTER_BYTES),
                received_at=dt("2026-08-05T00:00:00+00:00"),
            )
        # 被拒收的文件不进入登记
        self.assertIsNone(world["store"].current_master(world["work"].id).replaced_by)

    def test_pages_carry_damage_notes_and_color_calibration(self):
        world = build_world()
        order = world["order"]
        page1 = world["store"].pages[order.page_ids[0]]
        self.assertEqual(page1.damage_note, "天头虫蛀")
        self.assertEqual(page1.color_calibration["profile"], "FOGRA39")
        self.assertEqual(order.version, 1)


class TimeWindowTest(unittest.TestCase):
    def test_japan_local_dates_do_not_start_early_or_run_overdue(self):
        # 仅授权 2026-10-01（东京）：窗口为 09-30T15:00Z 至 10-01T15:00Z
        world = build_world(start="2026-10-01", end="2026-10-01")
        store, wid = world["store"], world["work"].id

        with self.assertRaises(LicenseError):  # 开幕前一刻仍未授权
            publishing.publish(store, wid, region="CN",
                               at=dt("2026-09-30T14:59:59+00:00"), by="p")
        release = publishing.publish(  # 当地零点整生效
            store, wid, region="CN", at=dt("2026-09-30T15:00:00+00:00"), by="p")
        self.assertEqual(len(release.approval_ids), 2)

        token = publishing.issue_download(
            store, release.id, world["highres"].id,
            at=dt("2026-10-01T14:59:59+00:00"), region="CN")
        # 当地 10-02 零点（=10-01T15:00Z）起立即逾期，半开区间不含右端点
        with self.assertRaises(LicenseError):
            publishing.resolve_download(
                store, token, at=dt("2026-10-01T15:00:00+00:00"), region="CN")

    def test_region_outside_agreement_is_denied(self):
        world = build_world(regions=("CN",))
        with self.assertRaises(LicenseError):
            publish_world(world, region="US")

    def test_naive_datetime_is_rejected(self):
        world = build_world()
        with self.assertRaises(ValueError):
            publishing.publish(
                world["store"], world["work"].id, region="CN",
                at=datetime(2026, 9, 15, 0, 0), by="p")


class PublishGateTest(unittest.TestCase):
    def test_requires_image_text_licenses_and_approvals(self):
        store = Store()
        inst = intake.register_institution(store, name="馆", country="FR")
        work = intake.register_work(
            store, institution_id=inst.id, title="书", edition="刻本")
        master = intake.deliver_master(
            store, work.id, data=MASTER_BYTES, sha256=sha(MASTER_BYTES),
            received_at=dt("2026-08-01T00:00:00+00:00"))
        intake.deliver_derivative(
            store, master.id, kind="preview", data=PREVIEW_BYTES,
            sha256=sha(PREVIEW_BYTES), received_at=dt("2026-08-01T00:00:00+00:00"))
        intake.set_pages(store, work.id,
                         [{"label": "1", "damage_note": "", "color_calibration": {}}],
                         at=dt("2026-08-01T00:00:00+00:00"))
        at = dt("2026-09-15T00:00:00+00:00")
        with self.assertRaises(LicenseError):  # 两份许可都没有
            publishing.publish(store, work.id, region="CN", at=at, by="p")

        img = licensing.grant_license(
            store, work.id, subject=SUBJECT_IMAGE, regions=["CN"],
            start_date="2026-09-01", end_date="2026-09-30", tz_name="Europe/Paris",
            attribution="图：馆藏", granted_by="g", at=at)
        with self.assertRaises(LicenseError):  # 缺文字解说许可
            publishing.publish(store, work.id, region="CN", at=at, by="p")
        licensing.approve(store, img.id, by="g", at=at)
        txt = licensing.grant_license(
            store, work.id, subject=SUBJECT_TEXT, regions=["CN"],
            start_date="2026-09-01", end_date="2026-09-30", tz_name="Europe/Paris",
            attribution="文：编者", granted_by="g", at=at)
        with self.assertRaises(LicenseError):  # 文字许可未批准
            publishing.publish(store, work.id, region="CN", at=at, by="p")
        licensing.approve(store, txt.id, by="g", at=at)
        release = publishing.publish(store, work.id, region="CN", at=at, by="p")
        self.assertEqual(release.attributions, ("图：馆藏", "文：编者"))


class SealedMasterTest(unittest.TestCase):
    def test_highres_blocked_before_opening_preview_allowed(self):
        world = build_world(sealed_until=OPENING)
        store, wid = world["store"], world["work"].id
        before = dt("2026-09-15T00:00:00+00:00")
        with self.assertRaises(SealedError):
            publishing.publish(store, wid, region="CN", at=before, by="p")
        preview_release = publishing.publish(
            store, wid, region="CN", at=before, by="p", include_highres=False)
        kinds = {kind for _, kind, _ in preview_release.derivatives}
        self.assertEqual(kinds, {"preview"})

        after = dt("2026-10-02T00:00:00+00:00")
        full = publishing.publish(store, wid, region="CN", at=after, by="p")
        self.assertIn("highres", {kind for _, kind, _ in full.derivatives})


class ChangeLifecycleTest(unittest.TestCase):
    def test_master_replacement_withdraws_cache_but_old_version_restorable(self):
        world = build_world()
        store, wid = world["store"], world["work"].id
        release = publish_world(world, at="2026-10-02T00:00:00+00:00")
        token = publishing.issue_download(
            store, release.id, world["highres"].id,
            at=dt("2026-10-02T01:00:00+00:00"), region="CN")

        intake.deliver_master(
            store, wid, data=REPLACEMENT_BYTES, sha256=sha(REPLACEMENT_BYTES),
            received_at=dt("2026-10-03T00:00:00+00:00"))
        self.assertEqual(store.release(release.id).status, "withdrawn")
        with self.assertRaises(DomainError):  # 旧下载链接立即失效
            publishing.resolve_download(
                store, token, at=dt("2026-10-03T01:00:00+00:00"), region="CN")

        restored = publishing.rollback(
            store, wid, release.id, at=dt("2026-10-04T00:00:00+00:00"), by="publisher")
        self.assertNotEqual(restored.id, release.id)
        self.assertEqual(restored.master_sha256, sha(MASTER_BYTES))
        new_token = publishing.issue_download(
            store, restored.id, world["highres"].id,
            at=dt("2026-10-04T01:00:00+00:00"), region="CN")
        info = publishing.resolve_download(
            store, new_token, at=dt("2026-10-04T01:00:00+00:00"), region="CN")
        self.assertEqual(info["sha256"], sha(HIGHRES_BYTES))

    def test_shortened_license_withdraws_overdue_release_and_kills_links(self):
        world = build_world(end="2026-12-31")
        store, wid = world["store"], world["work"].id
        release = publish_world(world, at="2026-09-15T00:00:00+00:00")
        token = publishing.issue_download(
            store, release.id, world["preview"].id,
            at=dt("2026-09-15T00:00:00+00:00"), region="CN")

        # 缩短到 2026-09-10（东京），决定时刻为 09-20，该发布已逾期
        licensing.shorten_license(
            store, world["lic_img"].id, new_end_date="2026-09-10",
            tz_name="Asia/Tokyo", at=dt("2026-09-20T00:00:00+00:00"), by="jp-curator")
        self.assertEqual(store.release(release.id).status, "withdrawn")
        with self.assertRaises(DomainError):
            publishing.resolve_download(
                store, token, at=dt("2026-09-20T00:00:00+00:00"), region="CN")
        with self.assertRaises(LicenseError):  # 许可已失效，历史版本也不能复原
            publishing.rollback(
                store, wid, release.id, at=dt("2026-09-20T00:00:00+00:00"), by="p")

    def test_shortened_but_still_valid_release_links_must_be_reissued(self):
        world = build_world(end="2026-12-31")
        store = world["store"]
        release = publish_world(world, at="2026-09-15T00:00:00+00:00")
        token = publishing.issue_download(
            store, release.id, world["preview"].id,
            at=dt("2026-09-15T00:00:00+00:00"), region="CN")
        licensing.shorten_license(
            store, world["lic_img"].id, new_end_date="2026-10-31",
            tz_name="Asia/Tokyo", at=dt("2026-09-20T00:00:00+00:00"), by="jp-curator")
        self.assertEqual(store.release(release.id).status, "active")
        with self.assertRaises(DomainError):  # 缓存按新决定失效
            publishing.resolve_download(
                store, token, at=dt("2026-09-20T00:00:00+00:00"), region="CN")
        fresh = publishing.issue_download(  # 重新核对后签发的新链接可用
            store, release.id, world["preview"].id,
            at=dt("2026-09-20T00:00:00+00:00"), region="CN")
        publishing.resolve_download(
            store, fresh, at=dt("2026-09-20T00:00:00+00:00"), region="CN")
        # 新窗口截止后链接自然关闭
        with self.assertRaises(LicenseError):
            publishing.resolve_download(
                store, fresh, at=dt("2026-11-01T00:00:00+00:00"), region="CN")

    def test_takedown_and_restore(self):
        world = build_world()
        store, wid = world["store"], world["work"].id
        release = publish_world(world, at="2026-10-02T00:00:00+00:00")
        publishing.takedown(
            store, release.id, at=dt("2026-10-05T00:00:00+00:00"),
            by="jp-curator", reason="权利核查")
        with self.assertRaises(DomainError):
            publishing.takedown(
                store, release.id, at=dt("2026-10-05T01:00:00+00:00"), by="jp-curator")
        restored = publishing.rollback(
            store, wid, release.id, at=dt("2026-10-06T00:00:00+00:00"), by="publisher")
        self.assertEqual(restored.master_sha256, release.master_sha256)
        self.assertEqual(restored.approval_ids, release.approval_ids)

    def test_page_order_fix_versions_and_rollback_keeps_old_order(self):
        world = build_world()
        store, wid = world["store"], world["work"].id
        first = publish_world(world, at="2026-10-02T00:00:00+00:00")
        self.assertEqual(first.page_order_version, 1)

        a, b = world["order"].page_ids
        intake.fix_page_order(
            store, wid, [b, a], at=dt("2026-10-03T00:00:00+00:00"), by="publisher")
        self.assertEqual(store.release(first.id).status, "withdrawn")

        republished = publish_world(world, at="2026-10-04T00:00:00+00:00")
        self.assertEqual(republished.page_order_version, 2)

        restored = publishing.rollback(
            store, wid, first.id, at=dt("2026-10-05T00:00:00+00:00"), by="publisher")
        self.assertEqual(restored.page_order_version, 1)

        with self.assertRaises(DomainError):  # 修正必须覆盖原有全部页面
            intake.fix_page_order(
                store, wid, [a], at=dt("2026-10-05T01:00:00+00:00"), by="publisher")


class ProvenanceAuditExportTest(unittest.TestCase):
    def test_provenance_names_master_and_approvals(self):
        world = build_world()
        release = publish_world(world, at="2026-10-02T00:00:00+00:00")
        info = publishing.provenance(world["store"], release.id)
        self.assertEqual(info["master_id"], world["master"].id)
        self.assertEqual(info["master_sha256"], sha(MASTER_BYTES))
        deciders = {a["decided_by"] for a in info["approvals"]}
        self.assertEqual(deciders, {"jp-curator", "editor"})
        self.assertIn("图片版权：东瀛馆", info["attributions"])

    def test_institution_sees_only_own_records(self):
        world = build_world()
        store = world["store"]
        other_work = intake.register_work(
            store, institution_id=world["other_inst"].id,
            title="英伦本", edition="十八世纪印本")
        intake.deliver_master(
            store, other_work.id, data=b"other", sha256=sha(b"other"),
            received_at=dt("2026-08-10T00:00:00+00:00"))

        mine = audit.events_for_institution(store, world["inst"].id)
        theirs = audit.events_for_institution(store, world["other_inst"].id)
        self.assertTrue(mine)
        self.assertTrue(theirs)
        self.assertTrue(all(e.work_id != other_work.id for e in mine))
        self.assertTrue(all(e.work_id == other_work.id for e in theirs))

    def test_audit_never_carries_image_bytes(self):
        store = Store()
        with self.assertRaises(AuditContentError):
            store.record(AuditEvent(
                at=dt("2026-09-01T00:00:00+00:00"), actor="x", action="bad",
                institution_id=None, work_id=None, detail={"payload": b"\x00\x01"}))

    def test_export_excludes_highres_while_sealed_or_unlicensed(self):
        world = build_world(sealed_until=OPENING)
        store, wid = world["store"], world["work"].id
        sealed_pkg = audit.build_export(
            store, wid, region="CN", at=dt("2026-09-15T00:00:00+00:00"))
        self.assertEqual({d["kind"] for d in sealed_pkg["derivatives"]}, {"preview"})

        opened_pkg = audit.build_export(
            store, wid, region="CN", at=dt("2026-10-02T00:00:00+00:00"))
        self.assertEqual({d["kind"] for d in opened_pkg["derivatives"]},
                         {"preview", "highres"})

        foreign_pkg = audit.build_export(
            store, wid, region="US", at=dt("2026-10-02T00:00:00+00:00"))
        self.assertEqual(foreign_pkg["derivatives"], [])
        self.assertFalse(foreign_pkg["licensed"]["image"])
        # 导出包只含书目与校验值，不含图像字节
        self.assertNotIn(b"master", repr(sealed_pkg).encode("utf-8", "ignore"))


class ApiDispatchTest(unittest.TestCase):
    def test_http_flow_and_errors(self):
        api = LoanApi()
        status, inst = api.dispatch("POST", "/v1/institutions", {},
                                    {"name": "柏林馆", "country": "DE"})
        self.assertEqual(status, 200)
        status, work = api.dispatch("POST", "/v1/works", {}, {
            "institution_id": inst["id"], "title": "水浒叶子",
            "edition": "明末刻本", "bibliography": {}})
        self.assertEqual(status, 200)
        status, payload = api.dispatch(
            "POST", f"/v1/works/{work['id']}/publish", {},
            {"region": "CN", "at": "2026-09-15T00:00:00+00:00", "by": "p"})
        self.assertEqual(status, 409)  # 尚无母版与许可，拒绝发布

        status, payload = api.dispatch(
            "GET", "/v1/works/unknown/export", {"region": ["CN"],
            "at": ["2026-09-15T00:00:00+00:00"]}, {})
        self.assertEqual(status, 404)

        status, payload = api.dispatch("GET", "/v1/unknown", {}, {})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
