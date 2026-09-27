"""许可与批准：约定地域、日期窗口（按馆藏方当地时区换算 UTC）与署名要求。"""

from __future__ import annotations

from .models import HIGH_TIERS, LICENSE_SUBJECTS, Approval, AuditEvent, License
from .store import LicenseError, Store, withdraw_release
from .timeutil import local_date_window, require_utc


def grant_license(
    store: Store,
    work_id,
    *,
    subject,
    regions,
    start_date,
    end_date,
    tz_name,
    attribution,
    granted_by,
    at,
) -> License:
    """登记馆藏方授权：特定地域、当地日期区间与署名要求。"""
    work = store.work(work_id)
    at = require_utc(at)
    if subject not in LICENSE_SUBJECTS:
        raise LicenseError(f"未知许可对象：{subject}")
    if not attribution or not attribution.strip():
        raise LicenseError("许可必须包含署名要求")
    if not regions:
        raise LicenseError("许可必须约定地域")

    start_utc, end_utc = local_date_window(start_date, end_date, tz_name)
    license_ = License(
        id=store.new_id("lic"),
        work_id=work_id,
        subject=subject,
        regions=frozenset(regions),
        start_utc=start_utc,
        end_utc=end_utc,
        attribution=attribution.strip(),
        granted_by=granted_by,
    )
    store.licenses[license_.id] = license_
    store.record(
        AuditEvent(
            at=at,
            actor=granted_by,
            action="license-granted",
            institution_id=work.institution_id,
            work_id=work_id,
            detail={
                "license_id": license_.id,
                "subject": subject,
                "regions": sorted(license_.regions),
                "start_utc": start_utc.isoformat(),
                "end_utc": end_utc.isoformat(),
            },
        )
    )
    return license_


def current_license(store: Store, license_id) -> License:
    """沿替换链找到现行许可版本。"""
    license_ = store.license(license_id)
    while license_.replaced_by is not None:
        license_ = store.license(license_.replaced_by)
    return license_


def license_allows(license_: License, *, at, region) -> bool:
    """半开区间判断：起始前不算生效，截止时刻起即失效。"""
    at = require_utc(at)
    if not (license_.start_utc <= at < license_.end_utc):
        return False
    return "*" in license_.regions or region in license_.regions


def find_active_license(store: Store, work_id, subject, *, at, region):
    """在现行许可中找窗口最迟的一份有效许可。"""
    best = None
    for license_ in store.licenses.values():
        if license_.work_id != work_id or license_.subject != subject:
            continue
        if license_.replaced_by is not None:
            continue
        if license_allows(license_, at=at, region=region):
            if best is None or license_.end_utc > best.end_utc:
                best = license_
    return best


def approve(store: Store, license_id, *, by, at, note="") -> Approval:
    """对现行许可作出一次批准；发布以此作为“哪次批准”的依据。"""
    license_ = current_license(store, license_id)
    at = require_utc(at)
    approval = Approval(
        id=store.new_id("app"),
        license_id=license_.id,
        decided_by=by,
        decided_at=at,
        note=note,
    )
    store.approvals[approval.id] = approval
    work = store.work(license_.work_id)
    store.record(
        AuditEvent(
            at=at,
            actor=by,
            action="license-approved",
            institution_id=work.institution_id,
            work_id=license_.work_id,
            detail={"approval_id": approval.id, "license_id": license_.id},
        )
    )
    return approval


def shorten_license(store: Store, license_id, *, new_end_date, tz_name, at, by) -> License:
    """缩短授权：生成新的更短窗口；依赖旧窗口的现行发布与缓存按新决定失效。"""
    old = current_license(store, license_id)
    at = require_utc(at)
    _, new_end = local_date_window(new_end_date, new_end_date, tz_name)
    if new_end >= old.end_utc:
        raise LicenseError("新截止时间必须早于原窗口")

    shortened = License(
        id=store.new_id("lic"),
        work_id=old.work_id,
        subject=old.subject,
        regions=old.regions,
        start_utc=old.start_utc,
        end_utc=new_end,
        attribution=old.attribution,
        granted_by=old.granted_by,
    )
    store.licenses[shortened.id] = shortened
    old.replaced_by = shortened.id

    work = store.work(old.work_id)
    store.record(
        AuditEvent(
            at=at,
            actor=by,
            action="license-shortened",
            institution_id=work.institution_id,
            work_id=old.work_id,
            detail={
                "license_id": shortened.id,
                "previous_license_id": old.id,
                "end_utc": new_end.isoformat(),
            },
        )
    )

    # 已不合规的发布立即撤回；仍合规的发布缓存作废，下载将在新窗口截止时自然关闭
    for release in store.active_releases(old.work_id):
        if not release_compliant(store, release, at=at):
            withdraw_release(store, release, at=at, reason="license-shortened", actor=by)
        else:
            store.cache.invalidate_release(release.id)
    return shortened


def release_compliant(store: Store, release, *, at) -> bool:
    """按现行许可与封闭状态复核一次历史发布是否仍可展示。"""
    for approval_id in release.approval_ids:
        approval = store.approvals[approval_id]
        license_ = current_license(store, approval.license_id)
        if not license_allows(license_, at=at, region=release.region):
            return False
    master = store.master(release.master_id)
    if master.sealed_until is not None and at < master.sealed_until:
        if any(kind in HIGH_TIERS for _, kind, _ in release.derivatives):
            return False
    return True
