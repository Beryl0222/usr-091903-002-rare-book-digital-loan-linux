"""领域模型：母版、页序、许可、批准与发布快照等核心实体。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# 衍生图层级：母版与高清衍生图属于受限的高清内容
TIER_HIGHRES = "highres"
TIER_PREVIEW = "preview"
TIER_THUMBNAIL = "thumbnail"
DERIVATIVE_TIERS = frozenset({TIER_HIGHRES, TIER_PREVIEW, TIER_THUMBNAIL})
HIGH_TIERS = frozenset({TIER_HIGHRES})

# 许可对象：图像许可与文字解说许可分别核对
SUBJECT_IMAGE = "image"
SUBJECT_TEXT = "text"
LICENSE_SUBJECTS = frozenset({SUBJECT_IMAGE, SUBJECT_TEXT})

# 发布状态
STATUS_ACTIVE = "active"
STATUS_WITHDRAWN = "withdrawn"


@dataclass
class Institution:
    id: str
    name: str
    country: str


@dataclass
class Work:
    id: str
    institution_id: str
    title: str
    edition: str          # 书目版本，如“明万历刻本”
    bibliography: dict    # 其余书目信息（作者、年代、版刻等）


@dataclass
class MasterScan:
    id: str
    work_id: str
    sha256: str
    received_at: datetime
    sealed_until: datetime | None = None   # 开幕前保持封闭的高清母版
    replaced_by: str | None = None         # 馆方替换扫描件后指向新母版


@dataclass
class Derivative:
    id: str
    master_id: str
    kind: str        # highres / preview / thumbnail
    sha256: str


@dataclass
class Page:
    id: str
    work_id: str
    label: str              # 页码
    damage_note: str        # 缺损说明
    color_calibration: dict  # 色彩校准信息


@dataclass
class PageOrder:
    work_id: str
    version: int
    page_ids: tuple


@dataclass
class License:
    id: str
    work_id: str
    subject: str             # image / text
    regions: frozenset       # 约定地域，"*" 表示不限
    start_utc: datetime      # 半开区间 [start_utc, end_utc)
    end_utc: datetime
    attribution: str         # 署名要求
    granted_by: str
    replaced_by: str | None = None  # 授权被缩短后指向新许可


@dataclass
class Approval:
    id: str
    license_id: str
    decided_by: str
    decided_at: datetime
    note: str = ""


@dataclass
class Release:
    """一次对外展示的不可变快照：母版校验值、页序版本、衍生图与批准齐备。"""

    id: str
    work_id: str
    region: str
    created_at: datetime
    created_by: str
    master_id: str
    master_sha256: str
    page_order_version: int
    derivatives: tuple       # (derivative_id, kind, sha256)
    approval_ids: tuple
    attributions: tuple
    status: str = STATUS_ACTIVE
    supersedes: str | None = None
    withdrawn_at: datetime | None = None
    withdraw_reason: str = ""


@dataclass
class AuditEvent:
    """审计事件只记录标识符，绝不记录图像内容。"""

    at: datetime
    actor: str
    action: str
    institution_id: str | None
    work_id: str | None
    detail: dict = field(default_factory=dict)
