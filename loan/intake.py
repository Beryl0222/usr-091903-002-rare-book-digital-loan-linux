"""馆藏交付：接收文件与校验值，并关联书目版本、页序、缺损说明、色彩校准与衍生图。"""

from __future__ import annotations

import hashlib

from .models import (
    DERIVATIVE_TIERS,
    AuditEvent,
    Derivative,
    Institution,
    MasterScan,
    Page,
    PageOrder,
    Work,
)
from .store import ChecksumMismatch, DomainError, Store, withdraw_release
from .timeutil import require_utc


def register_institution(store: Store, *, name, country) -> Institution:
    institution = Institution(id=store.new_id("inst"), name=name, country=country)
    store.institutions[institution.id] = institution
    return institution


def register_work(store: Store, *, institution_id, title, edition, bibliography=None) -> Work:
    store.institution(institution_id)
    work = Work(
        id=store.new_id("work"),
        institution_id=institution_id,
        title=title,
        edition=edition,
        bibliography=dict(bibliography or {}),
    )
    store.works[work.id] = work
    return work


def _verify_checksum(data: bytes, declared: str):
    actual = hashlib.sha256(data).hexdigest()
    if actual != declared.strip().lower():
        raise ChecksumMismatch("交付文件与校验值不符，已拒收")


def deliver_master(
    store: Store,
    work_id,
    *,
    data: bytes,
    sha256: str,
    received_at,
    sealed_until=None,
) -> MasterScan:
    """登记高清母版：核验校验值；替换旧母版时现行发布按新决定失效。"""
    work = store.work(work_id)
    received_at = require_utc(received_at)
    sealed_until = require_utc(sealed_until) if sealed_until is not None else None
    _verify_checksum(data, sha256)

    master = MasterScan(
        id=store.new_id("mas"),
        work_id=work_id,
        sha256=sha256.strip().lower(),
        received_at=received_at,
        sealed_until=sealed_until,
    )
    store.masters[master.id] = master
    store.master_bytes[master.id] = data

    replaced = False
    for old in store.masters.values():
        if old.work_id == work_id and old.id != master.id and old.replaced_by is None:
            old.replaced_by = master.id
            replaced = True
    if replaced:
        # 旧版本快照保留可复原，但现行展示与缓存按新扫描件失效
        for release in store.active_releases(work_id):
            withdraw_release(store, release, at=received_at, reason="master-replaced", actor="intake")

    store.record(
        AuditEvent(
            at=received_at,
            actor="intake",
            action="master-delivered",
            institution_id=work.institution_id,
            work_id=work_id,
            detail={"master_id": master.id, "sha256": master.sha256},
        )
    )
    return master


def deliver_derivative(store: Store, master_id, *, kind, data: bytes, sha256: str, received_at) -> Derivative:
    """登记一种衍生图，并与其母版关联。"""
    master = store.master(master_id)
    if kind not in DERIVATIVE_TIERS:
        raise DomainError(f"未知衍生图层级：{kind}")
    received_at = require_utc(received_at)
    _verify_checksum(data, sha256)

    derivative = Derivative(
        id=store.new_id("der"),
        master_id=master_id,
        kind=kind,
        sha256=sha256.strip().lower(),
    )
    store.derivatives[derivative.id] = derivative
    work = store.work(master.work_id)
    store.record(
        AuditEvent(
            at=received_at,
            actor="intake",
            action="derivative-delivered",
            institution_id=work.institution_id,
            work_id=master.work_id,
            detail={"derivative_id": derivative.id, "master_id": master_id, "kind": kind},
        )
    )
    return derivative


def set_pages(store: Store, work_id, pages, *, at) -> PageOrder:
    """建立页序，并逐页关联缺损说明与色彩校准。"""
    work = store.work(work_id)
    at = require_utc(at)
    if store.current_page_order(work_id) is not None:
        raise DomainError("页序已建立，请使用 fix_page_order 修正")

    page_ids = []
    for item in pages:
        page = Page(
            id=store.new_id("page"),
            work_id=work_id,
            label=item["label"],
            damage_note=item.get("damage_note", ""),
            color_calibration=dict(item.get("color_calibration") or {}),
        )
        store.pages[page.id] = page
        page_ids.append(page.id)
    if not page_ids:
        raise DomainError("页序不能为空")

    order = PageOrder(work_id=work_id, version=1, page_ids=tuple(page_ids))
    store.page_orders.setdefault(work_id, []).append(order)
    store.record(
        AuditEvent(
            at=at,
            actor="intake",
            action="pages-registered",
            institution_id=work.institution_id,
            work_id=work_id,
            detail={"page_order_version": 1, "page_count": len(page_ids)},
        )
    )
    return order


def fix_page_order(store: Store, work_id, ordered_page_ids, *, at, by) -> PageOrder:
    """出版社修正页序：产生新版本，现行发布按新决定失效，历史快照仍可复原。"""
    work = store.work(work_id)
    at = require_utc(at)
    current = store.current_page_order(work_id)
    if current is None:
        raise DomainError("尚未建立页序")
    known = set(current.page_ids)
    if set(ordered_page_ids) != known:
        raise DomainError("修正后的页序必须覆盖原有全部页面")

    order = PageOrder(work_id=work_id, version=current.version + 1, page_ids=tuple(ordered_page_ids))
    store.page_orders[work_id].append(order)
    for release in store.active_releases(work_id):
        withdraw_release(store, release, at=at, reason="page-order-fixed", actor=by)
    store.record(
        AuditEvent(
            at=at,
            actor=by,
            action="page-order-fixed",
            institution_id=work.institution_id,
            work_id=work_id,
            detail={"page_order_version": order.version},
        )
    )
    return order
