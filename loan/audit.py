"""审计与导出：馆藏方仅见自有资料记录；导出包不含未授权高清内容。"""

from __future__ import annotations

from .licensing import find_active_license
from .models import HIGH_TIERS, SUBJECT_IMAGE, SUBJECT_TEXT
from .store import Store
from .timeutil import require_utc


def events_for_institution(store: Store, institution_id):
    """馆藏方只能看到自己资料的访问与使用记录。"""
    store.institution(institution_id)
    return [event for event in store.audit if event.institution_id == institution_id]


def build_export(store: Store, work_id, *, region, at) -> dict:
    """生成导出包：书目、页序、缺损、色彩校准与获准的衍生图。

    未经许可或仍封闭的高清内容一律排除；包内只有标识与校验值，没有图像字节。
    """
    work = store.work(work_id)
    at = require_utc(at)
    master = store.current_master(work_id)
    order = store.current_page_order(work_id)

    image = find_active_license(store, work_id, SUBJECT_IMAGE, at=at, region=region)
    text = find_active_license(store, work_id, SUBJECT_TEXT, at=at, region=region)
    sealed = master is not None and master.sealed_until is not None and at < master.sealed_until

    derivatives = []
    if master is not None and image is not None:
        for derivative in store.derivatives.values():
            if derivative.master_id != master.id:
                continue
            if derivative.kind in HIGH_TIERS and sealed:
                continue
            derivatives.append(
                {"id": derivative.id, "kind": derivative.kind, "sha256": derivative.sha256}
            )
    derivatives.sort(key=lambda item: item["id"])

    pages = []
    if order is not None:
        for page_id in order.page_ids:
            page = store.pages[page_id]
            pages.append(
                {
                    "label": page.label,
                    "damage_note": page.damage_note,
                    "color_calibration": dict(page.color_calibration),
                }
            )

    attributions = []
    if image is not None and text is not None:
        attributions = [image.attribution, text.attribution]

    return {
        "work": {
            "id": work.id,
            "title": work.title,
            "edition": work.edition,
            "bibliography": dict(work.bibliography),
        },
        "page_order_version": order.version if order else None,
        "pages": pages,
        "licensed": {"image": image is not None, "text": text is not None},
        "attributions": attributions,
        "derivatives": derivatives,
    }
