"""发布管控：许可核对、版本快照、下架、复原与缓存失效。"""

from __future__ import annotations

from .licensing import current_license, find_active_license, license_allows, release_compliant
from .models import (
    HIGH_TIERS,
    STATUS_ACTIVE,
    SUBJECT_IMAGE,
    SUBJECT_TEXT,
    AuditEvent,
    Release,
)
from .store import DomainError, LicenseError, SealedError, Store, withdraw_release
from .timeutil import require_utc


def publish(store: Store, work_id, *, region, at, by, include_highres=True) -> Release:
    """发布前同时核对图像许可、文字解说许可与署名要求，并生成不可变快照。"""
    work = store.work(work_id)
    at = require_utc(at)

    master = store.current_master(work_id)
    if master is None:
        raise DomainError("尚无母版，无法发布")
    order = store.current_page_order(work_id)
    if order is None:
        raise DomainError("尚未建立页序，无法发布")

    sealed = master.sealed_until is not None and at < master.sealed_until
    if include_highres and sealed:
        raise SealedError("高清母版在开幕前保持封闭")

    image_license = find_active_license(store, work_id, SUBJECT_IMAGE, at=at, region=region)
    if image_license is None:
        raise LicenseError("缺少该地域当前有效的图像许可")
    text_license = find_active_license(store, work_id, SUBJECT_TEXT, at=at, region=region)
    if text_license is None:
        raise LicenseError("缺少该地域当前有效的文字解说许可")

    attributions = tuple(
        text for text in (image_license.attribution, text_license.attribution) if text
    )
    if len(attributions) < 2:
        raise LicenseError("图像与文字解说许可都必须给出署名要求")

    needed = {image_license.id, text_license.id}
    approvals = [a for a in store.approvals.values() if a.license_id in needed]
    approved = {a.license_id for a in approvals}
    if needed - approved:
        raise LicenseError("许可尚未批准，无法发布")

    derivatives = []
    for derivative in store.derivatives.values():
        if derivative.master_id != master.id:
            continue
        if derivative.kind in HIGH_TIERS and (sealed or not include_highres):
            continue
        derivatives.append((derivative.id, derivative.kind, derivative.sha256))
    if not derivatives:
        raise DomainError("没有可发布的衍生图")

    supersedes = None
    for old in store.active_releases(work_id):
        supersedes = old.id
        withdraw_release(store, old, at=at, reason="superseded", actor=by)

    release = Release(
        id=store.new_id("rel"),
        work_id=work_id,
        region=region,
        created_at=at,
        created_by=by,
        master_id=master.id,
        master_sha256=master.sha256,
        page_order_version=order.version,
        derivatives=tuple(derivatives),
        approval_ids=tuple(a.id for a in approvals),
        attributions=attributions,
        supersedes=supersedes,
    )
    store.releases[release.id] = release
    store.cache.register(release.id, [d[0] for d in derivatives])
    store.record(
        AuditEvent(
            at=at,
            actor=by,
            action="release-published",
            institution_id=work.institution_id,
            work_id=work_id,
            detail={
                "release_id": release.id,
                "master_sha256": master.sha256,
                "approval_ids": list(release.approval_ids),
                "region": region,
            },
        )
    )
    return release


def takedown(store: Store, release_id, *, at, by, reason="临时下架"):
    """馆藏方要求临时下架：发布撤回，缓存与下载链接同步失效。"""
    release = store.release(release_id)
    at = require_utc(at)
    if release.status != STATUS_ACTIVE:
        raise DomainError("该发布版本已撤回")
    withdraw_release(store, release, at=at, reason=reason, actor=by)
    return release


def rollback(store: Store, work_id, release_id, *, at, by) -> Release:
    """复原任一已展示过的版本：按当前许可复核后，以原快照重新发布。"""
    store.work(work_id)
    target = store.release(release_id)
    at = require_utc(at)
    if target.work_id != work_id:
        raise DomainError("该版本不属于此古籍")
    if not release_compliant(store, target, at=at):
        raise LicenseError("原许可已失效或母版仍封闭，无法复原该版本")

    supersedes = None
    for old in store.active_releases(work_id):
        supersedes = old.id
        withdraw_release(store, old, at=at, reason="superseded", actor=by)

    restored = Release(
        id=store.new_id("rel"),
        work_id=work_id,
        region=target.region,
        created_at=at,
        created_by=by,
        master_id=target.master_id,
        master_sha256=target.master_sha256,
        page_order_version=target.page_order_version,
        derivatives=target.derivatives,
        approval_ids=target.approval_ids,
        attributions=target.attributions,
        supersedes=supersedes,
    )
    store.releases[restored.id] = restored
    store.cache.register(restored.id, [d[0] for d in restored.derivatives])
    work = store.work(work_id)
    store.record(
        AuditEvent(
            at=at,
            actor=by,
            action="release-restored",
            institution_id=work.institution_id,
            work_id=work_id,
            detail={"release_id": restored.id, "source_release_id": target.id},
        )
    )
    return restored


def _derivative_in_release(release, derivative_id):
    for did, kind, sha256 in release.derivatives:
        if did == derivative_id:
            return kind, sha256
    raise DomainError("该衍生图不在此发布版本中")


def _recheck_licenses(store: Store, release, *, at, region):
    for approval_id in release.approval_ids:
        approval = store.approvals[approval_id]
        license_ = current_license(store, approval.license_id)
        if not license_allows(license_, at=at, region=region):
            raise LicenseError("许可在该时刻或地域不再有效")


def issue_download(store: Store, release_id, derivative_id, *, at, region) -> str:
    """签发下载令牌：逐次核对许可、封闭状态与发布状态。"""
    release = store.release(release_id)
    at = require_utc(at)
    if release.status != STATUS_ACTIVE:
        raise DomainError("发布已撤回，无法签发下载链接")
    kind, _ = _derivative_in_release(release, derivative_id)
    _recheck_licenses(store, release, at=at, region=region)
    master = store.master(release.master_id)
    if kind in HIGH_TIERS and master.sealed_until is not None and at < master.sealed_until:
        raise SealedError("高清母版在开幕前保持封闭")
    return store.cache.issue_token(release_id, derivative_id)


def resolve_download(store: Store, token, *, at, region) -> dict:
    """解析下载令牌：只返回标识信息，未经许可的高清内容不出现在接口中。"""
    at = require_utc(at)
    release_id, derivative_id = store.cache.resolve(token)
    release = store.release(release_id)
    if release.status != STATUS_ACTIVE:
        raise DomainError("发布已撤回，下载链接已失效")
    kind, sha256 = _derivative_in_release(release, derivative_id)
    _recheck_licenses(store, release, at=at, region=region)
    master = store.master(release.master_id)
    if kind in HIGH_TIERS and master.sealed_until is not None and at < master.sealed_until:
        raise SealedError("高清母版在开幕前保持封闭")
    return {
        "release_id": release_id,
        "derivative_id": derivative_id,
        "kind": kind,
        "sha256": sha256,
    }


def provenance(store: Store, release_id) -> dict:
    """策展人可据任一公开页面说明它使用了哪份母版和哪次批准。"""
    release = store.release(release_id)
    work = store.work(release.work_id)
    return {
        "release_id": release.id,
        "status": release.status,
        "work": {"id": work.id, "title": work.title, "edition": work.edition},
        "master_id": release.master_id,
        "master_sha256": release.master_sha256,
        "page_order_version": release.page_order_version,
        "approvals": [
            {
                "approval_id": approval.id,
                "license_id": approval.license_id,
                "decided_by": approval.decided_by,
                "decided_at": approval.decided_at,
            }
            for approval in (store.approvals[a] for a in release.approval_ids)
        ],
        "attributions": list(release.attributions),
        "derivatives": [
            {"id": did, "kind": kind, "sha256": sha256}
            for did, kind, sha256 in release.derivatives
        ],
    }
