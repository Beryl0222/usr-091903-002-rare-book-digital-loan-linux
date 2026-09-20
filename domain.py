"""古籍数字借展管控的领域核心。

本模块不依赖任何 Web 框架，所有时间判断都以 ``now(UTC)`` 为基准，
授权窗口在授予时按合作馆藏方所在时区换算成绝对时间存储，
因此跨国时区差不会让授权提前开始或逾期仍可访问。

关键不变量
----------
* 母版（高清）交付后只保存元数据与校验值，不提供任何对外读取路径；
  系统内任何序列化结果（公开页、馆藏方报告、审计、导出清单）都不包含
  母版文件内容，也不包含母版直链。
* 任何衍生图必须从已核验的母版派生；公开使用前必须同时具备
  图像许可、文字解说许可与署名要求核对（一次“批准”）。
* 替换扫描件、缩短授权、临时下架、修正页序都只追加“决策台账”，
  从不覆盖历史；已展示过的版本可以按快照复原。
* 决策变化（替换/缩短/下架/页序修正）会使旧令牌与缓存键整体失效，
  下载链接与 CDN 缓存按新决定作废，必须用当前版本重新批准、重新申领。
* 馆藏方账号只能看到自己机构资料的访问与使用记录。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class DomainError(Exception):
    """所有可预期的业务拒绝，HTTP 层映射为 4xx。"""


class ValidationError(DomainError):
    """输入不完整或自相矛盾。"""


class ChecksumMismatch(DomainError):
    """交付文件的实际校验值与馆藏方申报值不一致。"""


class PermissionDenied(DomainError):
    """当前身份无权执行该操作（跨馆访问等）。"""


class NotFound(DomainError):
    """资源不存在（对无权访问者也统一报不存在，避免侧信道泄露）。"""


class ApprovalError(DomainError):
    """发布前核对未通过（许可不全、署名缺失、封闭期内等）。"""


class AccessDenied(DomainError):
    """展示窗口、封闭状态或令牌不允许本次访问。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

WINDOW_START = "start"
WINDOW_END = "end"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(local_value: str, tz_name: str, which: str) -> datetime:
    """把“当地时间 + 时区名”换算成带时区的 UTC 绝对时间。

    例如东京窗口不会因为服务器位于北京而提前一小时开始；只给日期时
    按当地 00:00 起算（开窗）或当日 00:00（收窗按精确时刻另行指定）。
    ``which`` 仅用于错误语义，时间解释两者一致。
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:  # pragma: no cover - 防御性
        raise ValidationError(f"未知时区: {tz_name}") from exc
    text = local_value.strip().replace("/", "-")
    parsed = None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValidationError(f"无法解析时间: {local_value!r}")
    return parsed.replace(tzinfo=tz).astimezone(timezone.utc)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def sha256_hex(payload: bytes, declared: str | None = None) -> str:
    digest = hashlib.sha256(payload).hexdigest()
    if declared is not None and not hmac.compare_digest(digest, declared.strip().lower()):
        raise ChecksumMismatch("文件校验值与馆藏方交付清单不一致，拒绝入库")
    return digest


def new_id(prefix: str) -> str:
    return f"{prefix}_{hashlib.sha1(os.urandom(16)).hexdigest()[:12]}"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Institution:
    code: str
    name: str
    tz_name: str


@dataclass
class User:
    username: str
    role: str  # "curator"（出版社策展人）或 "lender"（馆藏方）
    institution_code: str | None = None

    @property
    def is_curator(self) -> bool:
        return self.role == "curator"


@dataclass
class DefectNote:
    page_label: str
    note: str


@dataclass
class ColorCalibration:
    profile: str
    target: str  # 色靶，例如 X-Rite ColorChecker
    white_point: str
    measured_by: str
    measured_at: str


@dataclass
class MasterFile:
    """高清母版：系统只保存元数据与校验值，绝不保存/回传文件内容。"""

    master_id: str
    edition_id: str
    owner_code: str
    checksum_sha256: str
    media_type: str
    resolution_ppi: int
    byte_size: int
    embargo_until: datetime | None  # 开幕前保持封闭
    delivered_at: datetime
    replaced_by: str | None = None  # 被哪个新母版替换（形成谱系）


@dataclass
class DerivativeImage:
    derivative_id: str
    master_id: str
    edition_id: str
    kind: str  # "web" | "thumb" | "tile" | "download"
    checksum_sha256: str
    byte_size: int
    max_long_edge_px: int


@dataclass
class Edition:
    edition_id: str
    work_title: str
    holding_institution_code: str
    version: int
    page_order: list[str]  # 有序的页标签
    defects: list[DefectNote]
    color: ColorCalibration | None
    derivatives: dict[str, DerivativeImage] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    superseded_by: str | None = None  # 页序修正后指向新版本


@dataclass
class License:
    """许可条款。缩短与下架都是向台账追加事件，从不就地改写历史。"""

    license_id: str
    edition_id: str
    kind: str  # "image" | "text"
    grantee: str  # 授权方（馆藏方/解说作者）
    territories: set[str]
    start_utc: datetime
    end_utc: datetime
    revoked_at: datetime | None = None
    superseded_by: str | None = None  # 被“缩短”后的新许可取代


@dataclass
class Approval:
    """发布前三重核对：图像许可 + 文字解说许可 + 署名要求。"""

    approval_id: str
    edition_id: str
    derivative_id: str
    text_id: str
    credit_line_required: str
    decided_at: datetime
    decision_epoch: int  # 批准时的决策纪元
    snapshot: dict
    revoked: bool = False


@dataclass
class AccessToken:
    token_id: str
    edition_id: str
    derivative_id: str
    approval_id: str
    territory: str
    issued_epoch: int
    issued_at: datetime
    expires_at: datetime
    purpose: str  # "view" | "download"
    revoked_at: datetime | None = None
    use_count: int = 0
    last_used: datetime | None = None


@dataclass
class UsageRecord:
    at: datetime
    actor: str
    action: str
    institution_code: str | None
    edition_id: str | None
    detail: dict


@dataclass
class LedgerEvent:
    """决策台账：只追加。任何已发布版本都可凭此复原。"""

    seq: int
    at: datetime
    actor: str
    kind: str  # deliver/replace/relicense/shorten/takedown/page-correction/...
    edition_id: str | None
    summary: str
    before_ref: dict | None  # 变更前关键状态的可复原引用
    payload: dict


# ---------------------------------------------------------------------------
# 领域服务
# ---------------------------------------------------------------------------


class LoanService:
    def __init__(self, now_fn=utc_now):
        self._now_fn = now_fn
        self._lock = threading.RLock()
        self.institutions: dict[str, Institution] = {}
        self.users: dict[str, User] = {}
        self.editions: dict[str, Edition] = {}
        self.masters: dict[str, MasterFile] = {}
        self.derivatives: dict[str, DerivativeImage] = {}
        self.licenses: dict[str, License] = {}
        self.texts: dict[str, dict] = {}  # text_id -> 解说元数据（不含受限原文）
        self.approvals: dict[str, Approval] = {}
        self.tokens: dict[str, AccessToken] = {}
        self.usage: list[UsageRecord] = []
        self.ledger: list[LedgerEvent] = []
        # 每个展卷（同一书名归并为 work）一个决策纪元；任一限制性决定 +1，
        # 使此前签发的令牌与 CDN 缓存键全部作废。
        self._epoch: dict[str, int] = {}
        self._seq = 0

    # -- 身份与机构 -------------------------------------------------------

    def _now(self) -> datetime:
        """可注入的时钟；生产为真实 UTC，测试可钉住以验证时区边界。"""
        return self._now_fn()

    def register_institution(self, code: str, name: str, tz_name: str) -> Institution:
        ZoneInfo(tz_name)  # 提前校验时区
        inst = Institution(code=code, name=name, tz_name=tz_name)
        self.institutions[code] = inst
        return inst

    def register_user(self, username: str, role: str, institution_code: str | None = None) -> User:
        if role == "lender":
            if not institution_code or institution_code not in self.institutions:
                raise ValidationError("馆藏方账号必须归属已登记机构")
        elif role != "curator":
            raise ValidationError(f"未知角色: {role}")
        user = User(username=username, role=role, institution_code=institution_code)
        self.users[username] = user
        return user

    def _user(self, actor: str) -> User:
        user = self.users.get(actor)
        if not user:
            raise PermissionDenied(f"未知身份: {actor}")
        return user

    def _require_curator(self, actor: str) -> User:
        user = self._user(actor)
        if not user.is_curator:
            raise PermissionDenied("仅出版社策展人可执行该操作")
        return user

    def _require_owner(self, actor: str, institution_code: str) -> User:
        user = self._user(actor)
        if user.is_curator:
            raise PermissionDenied("仅持有该资料的馆藏方可执行该操作")
        if user.institution_code != institution_code:
            # 跨馆访问统一报不存在，避免透露他馆书目是否存在
            raise NotFound("资源不存在")
        return user

    def _epoch_for(self, work_title: str) -> int:
        return self._epoch.setdefault(work_title, 1)

    def _bump_epoch(self, work_title: str) -> int:
        self._epoch[work_title] = self._epoch.get(work_title, 1) + 1
        return self._epoch[work_title]

    def _log(self, actor: str, action: str, inst: str | None, edition: str | None, **detail):
        self.usage.append(
            UsageRecord(
                at=self._now(),
                actor=actor,
                action=action,
                institution_code=inst,
                edition_id=edition,
                detail=detail,
            )
        )

    def _append_ledger(self, actor, kind, edition_id, summary, before_ref=None, **payload):
        self._seq += 1
        event = LedgerEvent(
            seq=self._seq,
            at=self._now(),
            actor=actor,
            kind=kind,
            edition_id=edition_id,
            summary=summary,
            before_ref=before_ref,
            payload=payload,
        )
        self.ledger.append(event)
        return event

    # -- 书目版本、页序、缺损、色彩 --------------------------------------

    def create_edition(
        self,
        actor: str,
        work_title: str,
        holding_institution_code: str,
        page_order: list[str],
        defects: list[dict] | None = None,
        color: dict | None = None,
    ) -> str:
        """登记一个书目版本（含页序、缺损说明、色彩校准）。

        策展人可为任一合作馆建版本；馆藏方只能登记本馆书目。
        """
        user = self._user(actor)
        if holding_institution_code not in self.institutions:
            raise ValidationError("馆藏机构未登记")
        if user.role == "lender" and user.institution_code != holding_institution_code:
            raise PermissionDenied("馆藏方只能登记本机构书目")
        if not page_order or len(page_order) != len(set(page_order)):
            raise ValidationError("页序不能为空且页标签不得重复")
        edition_id = new_id("ed")
        defect_notes = [
            DefectNote(page_label=d["page_label"], note=d["note"])
            for d in (defects or [])
        ]
        unknown = {d.page_label for d in defect_notes} - set(page_order)
        if unknown:
            raise ValidationError(f"缺损说明指向不存在的页: {sorted(unknown)}")
        calibration = None
        if color:
            required = {"profile", "target", "white_point", "measured_by", "measured_at"}
            missing = required - color.keys()
            if missing:
                raise ValidationError(f"色彩校准缺少字段: {sorted(missing)}")
            calibration = ColorCalibration(**{k: color[k] for k in required})
        edition = Edition(
            edition_id=edition_id,
            work_title=work_title,
            holding_institution_code=holding_institution_code,
            version=1,
            page_order=list(page_order),
            defects=defect_notes,
            color=calibration,
            created_at=self._now(),
        )
        self.editions[edition_id] = edition
        self._epoch.setdefault(work_title, 1)
        self._append_ledger(
            actor, "edition-create", edition_id,
            f"《{work_title}》版本 v1 建档，共 {len(page_order)} 页",
            work_title=work_title,
            holding_institution_code=holding_institution_code,
            page_order=list(page_order),
        )
        self._log(actor, "edition.create", holding_institution_code, edition_id)
        return edition_id

    def correct_page_order(self, actor: str, edition_id: str, new_page_order: list[str]) -> str:
        """出版社修正页序：旧版冻结保留（仍可复原），返回新版本 id。

        页序修正属于新决定，旧批准快照与旧链接一律失效。
        """
        self._require_curator(actor)
        old = self._edition(edition_id)
        if set(new_page_order) != set(old.page_order):
            raise ValidationError("页序修正只能重排现有页，不得增删页面")
        if new_page_order == old.page_order:
            raise ValidationError("新页序与当前版本相同，无需修正")
        new_id_ = new_id("ed")
        new_edition = Edition(
            edition_id=new_id_,
            work_title=old.work_title,
            holding_institution_code=old.holding_institution_code,
            version=old.version + 1,
            page_order=list(new_page_order),
            defects=list(old.defects),
            color=old.color,
            derivatives=dict(old.derivatives),
        )
        old.superseded_by = new_id_
        self.editions[new_id_] = new_edition
        epoch = self._bump_epoch(old.work_title)
        self._append_ledger(
            actor, "page-correction", new_id_,
            f"《{old.work_title}》页序修正 v{old.version} → v{new_edition.version}",
            before_ref={"edition_id": old.edition_id, "page_order": list(old.page_order)},
            page_order=list(new_page_order),
            epoch=epoch,
        )
        self._log(actor, "edition.page_correction", old.holding_institution_code,
                  new_id_, supersedes=old.edition_id, epoch=epoch)
        return new_id_

    def _edition(self, edition_id: str) -> Edition:
        edition = self.editions.get(edition_id)
        if not edition:
            raise NotFound("版本不存在")
        return edition

    # -- 母版交付与替换 ---------------------------------------------------

    def deliver_master(
        self,
        actor: str,
        edition_id: str,
        payload: bytes,
        declared_checksum: str,
        media_type: str,
        resolution_ppi: int,
        embargo_until_local: str | None = None,
    ) -> str:
        """馆藏方交付高清母版与校验值；核验不符立即拒收。

        payload 仅用于当场计算校验值，方法返回后系统不再保留其内容。
        """
        edition = self._edition(edition_id)
        self._require_owner(actor, edition.holding_institution_code)
        checksum = sha256_hex(payload, declared_checksum)  # 不符即抛异常
        embargo = None
        if embargo_until_local:
            inst = self.institutions[edition.holding_institution_code]
            embargo = to_utc(embargo_until_local, inst.tz_name, WINDOW_END)
        master_id = new_id("master")
        master = MasterFile(
            master_id=master_id,
            edition_id=edition_id,
            owner_code=edition.holding_institution_code,
            checksum_sha256=checksum,
            media_type=media_type,
            resolution_ppi=resolution_ppi,
            byte_size=len(payload),
            embargo_until=embargo,
            delivered_at=self._now(),
        )
        self.masters[master_id] = master
        self._append_ledger(
            actor, "deliver", edition_id,
            f"高清母版 {master_id} 交付并核验通过"
            + ("（开幕前封闭）" if embargo else ""),
            master_id=master_id, checksum=checksum, embargo_until=iso(embargo),
        )
        self._log(actor, "master.deliver", edition.holding_institution_code, edition_id,
                  master_id=master_id, bytes=len(payload))
        return master_id

    def replace_master(
        self,
        actor: str,
        current_master_id: str,
        payload: bytes,
        declared_checksum: str,
        media_type: str,
        resolution_ppi: int,
        embargo_until_local: str | None = None,
    ) -> str:
        """馆方替换扫描件：旧母版谱系保留、可复原；衍生图需重新关联、重新批准。"""
        old = self.masters.get(current_master_id)
        if not old:
            raise NotFound("母版不存在")
        self._require_owner(actor, old.owner_code)
        checksum = sha256_hex(payload, declared_checksum)
        edition = self._edition(old.edition_id)
        embargo = None
        if embargo_until_local:
            inst = self.institutions[old.owner_code]
            embargo = to_utc(embargo_until_local, inst.tz_name, WINDOW_END)
        new_master_id = new_id("master")
        master = MasterFile(
            master_id=new_master_id,
            edition_id=edition.edition_id,
            owner_code=old.owner_code,
            checksum_sha256=checksum,
            media_type=media_type,
            resolution_ppi=resolution_ppi,
            byte_size=len(payload),
            embargo_until=embargo,
            delivered_at=self._now(),
            replaced_by=None,
        )
        old.replaced_by = new_master_id
        self.masters[new_master_id] = master
        # 旧衍生图与旧母版的关联随即失效
        edition.derivatives = {}
        epoch = self._bump_epoch(edition.work_title)
        self._append_ledger(
            actor, "replace", edition.edition_id,
            f"母版替换：{current_master_id} → {new_master_id}；旧衍生图全部停用",
            before_ref={"master_id": current_master_id, "checksum": old.checksum_sha256},
            master_id=new_master_id, checksum=checksum,
            embargo_until=iso(embargo), epoch=epoch,
        )
        self._log(actor, "master.replace", old.owner_code, edition.edition_id,
                  old_master_id=current_master_id, new_master_id=new_master_id, epoch=epoch)
        return new_master_id

    def register_derivative(
        self,
        actor: str,
        master_id: str,
        kind: str,
        payload: bytes,
        declared_checksum: str,
        max_long_edge_px: int,
    ) -> str:
        """登记母版的衍生图（网页图/缩略图/瓦片/低清下载件）。

        高清母版本身永不作为衍生图登记：分辨率必须显著低于母版。
        """
        master = self.masters.get(master_id)
        if not master:
            raise NotFound("母版不存在")
        self._require_owner(actor, master.owner_code)
        if kind not in {"web", "thumb", "tile", "download"}:
            raise ValidationError(f"未知衍生图类型: {kind}")
        checksum = sha256_hex(payload, declared_checksum)
        if master.checksum_sha256 == checksum:
            raise ValidationError("禁止把高清母版本身登记为衍生图")
        edition = self._edition(master.edition_id)
        derivative_id = new_id("deriv")
        deriv = DerivativeImage(
            derivative_id=derivative_id,
            master_id=master_id,
            edition_id=edition.edition_id,
            kind=kind,
            checksum_sha256=checksum,
            byte_size=len(payload),
            max_long_edge_px=max_long_edge_px,
        )
        self.derivatives[derivative_id] = deriv
        edition.derivatives[derivative_id] = deriv
        self._log(actor, "derivative.register", master.owner_code, edition.edition_id,
                  derivative_id=derivative_id, master_id=master_id, kind=kind)
        return derivative_id

    # -- 许可：授予、缩短、下架 ------------------------------------------

    def grant_license(
        self,
        actor: str,
        edition_id: str,
        kind: str,
        grantee: str,
        territories: list[str],
        start_local: str,
        end_local: str,
    ) -> str:
        if kind not in {"image", "text"}:
            raise ValidationError("许可类型必须是 image 或 text")
        edition = self._edition(edition_id)
        # 图像许可由馆藏方授予；文字解说许可可由策展人代为登记解说作者授权
        user = self._user(actor)
        if kind == "image" and not (
            user.role == "lender" and user.institution_code == edition.holding_institution_code
        ):
            raise PermissionDenied("图像许可只能由持有该古籍的馆藏方授予")
        inst = self.institutions[edition.holding_institution_code]
        start = to_utc(start_local, inst.tz_name, WINDOW_START)
        end = to_utc(end_local, inst.tz_name, WINDOW_END)
        if not territories:
            raise ValidationError("许可必须限定地域")
        if end <= start:
            raise ValidationError("授权结束时间必须晚于开始时间")
        license_id = new_id("lic")
        lic = License(
            license_id=license_id,
            edition_id=edition_id,
            kind=kind,
            grantee=grantee,
            territories=set(territories),
            start_utc=start,
            end_utc=end,
        )
        self.licenses[license_id] = lic
        self._append_ledger(
            actor, "license-grant", edition_id,
            f"{('图像' if kind == 'image' else '文字解说')}许可授予：{grantee}，"
            f"地域 {sorted(lic.territories)}",
            before_ref=None,
            license_id=license_id, license_kind=kind, territories=sorted(lic.territories),
            start_utc=iso(start), end_utc=iso(end),
        )
        self._log(actor, "license.grant", edition.holding_institution_code, edition_id,
                  license_id=license_id, kind=kind)
        return license_id

    def _find_active_license(self, edition_id, kind, at: datetime) -> License | None:
        candidates = [
            lic for lic in self.licenses.values()
            if lic.edition_id == edition_id and lic.kind == kind
            and lic.revoked_at is None and lic.superseded_by is None
        ]
        valid = [
            lic for lic in candidates
            if lic.start_utc <= at < lic.end_utc
        ]
        return max(valid, key=lambda l: l.end_utc, default=None)

    def shorten_license(self, actor: str, license_id: str, new_end_local: str) -> str:
        """馆方缩短授权：旧许可保留为历史，产生更短的新许可；旧链接失效。"""
        old = self.licenses.get(license_id)
        if not old:
            raise NotFound("许可不存在")
        edition = self._edition(old.edition_id)
        self._require_owner(actor, edition.holding_institution_code)
        inst = self.institutions[edition.holding_institution_code]
        new_end = to_utc(new_end_local, inst.tz_name, WINDOW_END)
        if new_end < self._now():
            raise ValidationError("新的到期时间不得早于当前时间（如需立即停止请使用下架）")
        if new_end >= old.end_utc:
            raise ValidationError("缩短授权只能给出更早的到期时间")
        new_license_id = new_id("lic")
        new_lic = License(
            license_id=new_license_id,
            edition_id=old.edition_id,
            kind=old.kind,
            grantee=old.grantee,
            territories=set(old.territories),
            start_utc=old.start_utc,
            end_utc=new_end,
        )
        old.superseded_by = new_license_id
        self.licenses[new_license_id] = new_lic
        epoch = self._bump_epoch(edition.work_title)
        self._append_ledger(
            actor, "shorten", edition.edition_id,
            f"{('图像' if old.kind == 'image' else '文字')}许可缩短至 {new_end_local}"
            f"（{inst.tz_name} 当地时间）",
            before_ref={"license_id": license_id, "end_utc": iso(old.end_utc)},
            license_id=new_license_id, new_end_utc=iso(new_end), epoch=epoch,
        )
        self._log(actor, "license.shorten", edition.holding_institution_code, edition.edition_id,
                  old_license_id=license_id, new_license_id=new_license_id, epoch=epoch)
        return new_license_id

    def takedown(self, actor: str, edition_id: str, reason: str) -> None:
        """馆方要求临时下架：吊销该展卷全部现行许可，批准与令牌立即失效。"""
        edition = self._edition(edition_id)
        self._require_owner(actor, edition.holding_institution_code)
        at = self._now()
        revoked = []
        for lic in self.licenses.values():
            if lic.edition_id == edition_id and lic.revoked_at is None:
                lic.revoked_at = at
                revoked.append(lic.license_id)
        for approval in self.approvals.values():
            if approval.edition_id == edition_id and not approval.revoked:
                approval.revoked = True
        epoch = self._bump_epoch(edition.work_title)
        self._append_ledger(
            actor, "takedown", edition_id,
            f"临时下架：{reason}；吊销 {len(revoked)} 项许可",
            before_ref={"revoked_licenses": revoked},
            reason=reason, epoch=epoch,
        )
        self._log(actor, "edition.takedown", edition.holding_institution_code, edition_id,
                  reason=reason, revoked=len(revoked), epoch=epoch)

    def register_text(self, actor: str, edition_id: str, credit_author: str) -> str:
        """登记文字解说（只存元数据与署名作者；受限解说原文不进系统导出）。"""
        self._require_curator(actor)
        edition = self._edition(edition_id)
        text_id = new_id("text")
        self.texts[text_id] = {
            "text_id": text_id,
            "edition_id": edition_id,
            "credit_author": credit_author,
            "registered_at": self._now(),
        }
        self._log(actor, "text.register", edition.holding_institution_code, edition_id,
                  text_id=text_id)
        return text_id

    # -- 发布前三重核对与批准 --------------------------------------------

    def approve_publication(
        self, actor: str, edition_id: str, derivative_id: str, text_id: str,
        territory: str, credit_line: str,
    ) -> str:
        """发布前同时核对：图像许可、文字解说许可、署名要求、封闭期。"""
        self._require_curator(actor)
        edition = self._edition(edition_id)
        deriv = self.derivatives.get(derivative_id)
        if not deriv or deriv.edition_id != edition_id:
            raise ApprovalError("衍生图不存在或不属于该版本")
        master = self.masters[deriv.master_id]
        if master.replaced_by is not None:
            raise ApprovalError("母版已被替换，必须基于新扫描件重新制作并关联衍生图")
        if text_id not in self.texts or self.texts[text_id]["edition_id"] != edition_id:
            raise ApprovalError("文字解说不存在或不属于该版本")
        if not credit_line or not credit_line.strip():
            raise ApprovalError("缺少署名要求（credit line），不予批准")
        at = self._now()
        if master.embargo_until and at < master.embargo_until:
            raise ApprovalError(
                f"高清母版尚在封闭期，直至 {master.embargo_until.isoformat()}（UTC）"
            )
        image_lic = self._find_active_license(edition_id, "image", at)
        text_lic = self._find_active_license(edition_id, "text", at)
        missing = []
        if not image_lic:
            missing.append("图像许可")
        elif territory not in image_lic.territories:
            missing.append(f"图像许可未覆盖地域 {territory}")
        if not text_lic:
            missing.append("文字解说许可")
        elif territory not in text_lic.territories:
            missing.append(f"文字解说许可未覆盖地域 {territory}")
        if missing:
            raise ApprovalError("发布核对未通过：" + "、".join(missing))
        epoch = self._epoch_for(edition.work_title)
        approval_id = new_id("appr")
        snapshot = {
            "work_title": edition.work_title,
            "edition_id": edition_id,
            "edition_version": edition.version,
            "page_order": list(edition.page_order),
            "master_id": master.master_id,
            "master_checksum": master.checksum_sha256,
            "derivative_id": derivative_id,
            "derivative_checksum": deriv.checksum_sha256,
            "text_id": text_id,
            "image_license_id": image_lic.license_id,
            "text_license_id": text_lic.license_id,
            "territory": territory,
            "credit_line": credit_line.strip(),
            "color_profile": edition.color.profile if edition.color else None,
            "decided_at_utc": iso(at),
        }
        approval = Approval(
            approval_id=approval_id,
            edition_id=edition_id,
            derivative_id=derivative_id,
            text_id=text_id,
            credit_line_required=credit_line.strip(),
            decided_at=at,
            decision_epoch=epoch,
            snapshot=snapshot,
        )
        self.approvals[approval_id] = approval
        self._log(actor, "publication.approve", edition.holding_institution_code, edition_id,
                  approval_id=approval_id, territory=territory, epoch=epoch)
        return approval_id

    # -- 访问令牌（公开链接 / 下载链接 / 缓存键） ------------------------

    def issue_token(
        self, actor: str, approval_id: str, territory: str, purpose: str = "view"
    ) -> dict:
        self._require_curator(actor)
        approval = self.approvals.get(approval_id)
        if not approval:
            raise NotFound("批准不存在")
        if approval.revoked:
            raise AccessDenied("该批准已被下架决定吊销")
        edition = self._edition(approval.edition_id)
        at = self._now()
        self._guard_window(edition, approval, territory, at, check_epoch=True)
        token_id = new_id("tok")
        token = AccessToken(
            token_id=token_id,
            edition_id=edition.edition_id,
            derivative_id=approval.derivative_id,
            approval_id=approval_id,
            territory=territory,
            issued_epoch=self._epoch_for(edition.work_title),
            issued_at=at,
            expires_at=self._current_window_end(edition, at),
            purpose=purpose,
        )
        self.tokens[token_id] = token
        self._log(actor, "token.issue", edition.holding_institution_code, edition.edition_id,
                  token_id=token_id, approval_id=approval_id, purpose=purpose)
        return {
            "token": token_id,
            # 缓存键携带决策纪元：CDN/边缘缓存在纪元变化后必然 miss
            "cache_key": f"v{token.issued_epoch}:{edition.edition_id}:{approval.derivative_id}:{territory}",
            "expires_at": iso(token.expires_at),
        }

    def _current_window_end(self, edition: Edition, at: datetime) -> datetime:
        image_lic = self._find_active_license(edition.edition_id, "image", at)
        text_lic = self._find_active_license(edition.edition_id, "text", at)
        ends = [lic.end_utc for lic in (image_lic, text_lic) if lic]
        return min(ends) if ends else at

    def _guard_window(self, edition, approval, territory, at, check_epoch):
        if approval.revoked:
            raise AccessDenied("内容已被馆藏方要求下架")
        if check_epoch and approval.decision_epoch != self._epoch_for(edition.work_title):
            raise AccessDenied("许可状态已变更（替换/缩短/下架/页序修正），链接与缓存已失效")
        master = self.masters[self.derivatives[approval.derivative_id].master_id]
        if master.embargo_until and at < master.embargo_until:
            raise AccessDenied("母版尚在开幕前封闭期")
        for kind in ("image", "text"):
            lic = self._find_active_license(edition.edition_id, kind, at)
            label = "图像" if kind == "image" else "文字解说"
            if not lic:
                raise AccessDenied(f"{label}许可当前无效")
            if territory not in lic.territories:
                raise AccessDenied(f"{label}许可不覆盖该地域")

    def serve(self, token_id: str, territory: str, at: datetime | None = None) -> dict:
        """公众通过链接访问。返回安全的服务载荷（仅校验值与元数据，无文件字节）。"""
        at = at or self._now()
        token = self.tokens.get(token_id)
        if not token:
            raise AccessDenied("链接无效")
        with self._lock:
            if token.revoked_at:
                raise AccessDenied("链接已被撤销")
            approval = self.approvals.get(token.approval_id)
            edition = self._edition(token.edition_id)
            self._guard_window(edition, approval, territory, at, check_epoch=True)
            if at >= token.expires_at:
                raise AccessDenied("链接已过授权期")
            deriv = self.derivatives[token.derivative_id]
            token.use_count += 1
            token.last_used = at
            self._log("public", "asset.serve", edition.holding_institution_code,
                      edition.edition_id, token_id=token_id, territory=territory,
                      purpose=token.purpose, derivative_id=deriv.derivative_id)
            # 载荷中只出现衍生图，母版仅有“归属 id + 校验值”用于溯源
            return {
                "kind": "derivative",
                "derivative_id": deriv.derivative_id,
                "derivative_kind": deriv.kind,
                "checksum": deriv.checksum_sha256,
                "credit_line": approval.credit_line_required,
                "provenance": f"/api/public/editions/{edition.edition_id}/provenance",
            }

    # -- 公开溯源 ---------------------------------------------------------

    def public_provenance(self, edition_id: str, territory: str, at: datetime | None = None) -> dict:
        """任一公开页面都能说明：它用了哪份母版、哪次批准、当前许可窗口。"""
        at = at or self._now()
        edition = self._edition(edition_id)
        live = [
            a for a in self.approvals.values()
            if a.edition_id == edition_id and not a.revoked
            and a.decision_epoch == self._epoch_for(edition.work_title)
        ]
        visible = []
        for approval in live:
            try:
                self._guard_window(edition, approval, territory, at, check_epoch=True)
                visible.append(approval)
            except AccessDenied:
                continue
        if not visible:
            # 即便当前无有效展示，仍给出书目级溯源与历史说明，但不暴露母版细节
            return {
                "edition_id": edition_id,
                "work_title": edition.work_title,
                "status": "unavailable",
                "reason": "当前地域/时间无有效展示授权，或内容已下架",
                "holding_institution": self.institutions[edition.holding_institution_code].name,
            }
        approval = max(visible, key=lambda a: a.decided_at)
        snap = dict(approval.snapshot)
        return {
            "edition_id": edition_id,
            "work_title": snap["work_title"],
            "status": "available",
            "holding_institution": self.institutions[edition.holding_institution_code].name,
            "version": snap["edition_version"],
            "master": {
                "master_id": snap["master_id"],
                "checksum_sha256": snap["master_checksum"],
            },
            "derivative": {
                "derivative_id": snap["derivative_id"],
                "checksum_sha256": snap["derivative_checksum"],
            },
            "approval": {
                "approval_id": approval.approval_id,
                "decided_at_utc": snap["decided_at_utc"],
                "image_license_id": snap["image_license_id"],
                "text_license_id": snap["text_license_id"],
                "territory": snap["territory"],
            },
            "credit_line": snap["credit_line"],
            "page_order": snap["page_order"],
            "color_profile": snap["color_profile"],
        }

    # -- 馆藏方：只看本馆的访问与使用记录 --------------------------------

    def lender_report(self, actor: str) -> dict:
        user = self._user(actor)
        if user.role != "lender":
            raise PermissionDenied("该报告仅面向馆藏方")
        code = user.institution_code
        records = [r for r in self.usage if r.institution_code == code]
        return {
            "institution": self.institutions[code].name,
            "records": [
                {
                    "at": iso(r.at),
                    "actor": r.actor,
                    "action": r.action,
                    "edition_id": r.edition_id,
                    "detail": r.detail,
                }
                for r in records
            ],
            "editions_visible": sorted(
                e.edition_id for e in self.editions.values()
                if e.holding_institution_code == code
            ),
        }

    # -- 复原：已展示过的版本 --------------------------------------------

    def restore_view(self, actor: str, edition_id: str, approval_id: str | None = None) -> dict:
        """按台账/批准快照复原某个历史版本在批准时的完整展示状态。

        不传 ``approval_id`` 时复原该版本“首次对外发布”的快照；
        替换扫描件后有多次批准时，可指定具体批准 id 精确复原任一次展示。
        """
        self._require_curator(actor)
        edition = self._edition(edition_id)
        approvals = sorted(
            (a for a in self.approvals.values() if a.edition_id == edition_id),
            key=lambda a: a.decided_at,
        )
        if not approvals:
            raise NotFound("该版本没有可复原的批准记录")
        if approval_id is not None:
            chosen = next((a for a in approvals if a.approval_id == approval_id), None)
            if chosen is None:
                raise NotFound("指定的批准记录不属于该版本")
        else:
            chosen = approvals[0]
        return {
            "restored": True,
            "note": "以下为批准时的快照；不代表当前可公开展示",
            "restored_approval_id": chosen.approval_id,
            "restorable_approvals": [
                {"approval_id": a.approval_id, "decided_at_utc": iso(a.decided_at),
                 "master_id": a.snapshot["master_id"]}
                for a in approvals
            ],
            "snapshot": chosen.snapshot,
            "ledger_tail": [
                {"seq": e.seq, "kind": e.kind, "summary": e.summary, "at": iso(e.at)}
                for e in self.ledger
                if e.edition_id == edition_id
            ],
        }

    def ledger_view(self, actor: str, edition_id: str | None = None) -> list[dict]:
        self._user(actor)  # 任何登记身份都可查台账（馆藏方下面再过滤）
        user = self.users[actor]
        events = self.ledger
        if edition_id:
            events = [e for e in events if e.edition_id == edition_id]
        if user.role == "lender":
            code = user.institution_code
            events = [
                e for e in events
                if e.edition_id in {
                    ed.edition_id for ed in self.editions.values()
                    if ed.holding_institution_code == code
                }
            ]
        return [
            {
                "seq": e.seq, "at": iso(e.at), "actor": e.actor, "kind": e.kind,
                "edition_id": e.edition_id, "summary": e.summary,
                "before_ref": e.before_ref,
            }
            for e in events
        ]

    # -- 导出包：绝不包含未授权高清内容 ----------------------------------

    def export_bundle(self, actor: str, edition_id: str, territory: str,
                      at: datetime | None = None) -> dict:
        """生成对外导出清单。

        安全保证：导出包只包含“当前窗口内、经批准的低清衍生图”的元数据与
        校验值；母版没有下载地址、没有字节、没有直链；一旦授权状态变化，
        旧导出清单因纪元号不再匹配而作废。
        """
        self._require_curator(actor)
        at = at or self._now()
        edition = self._edition(edition_id)
        epoch = self._epoch_for(edition.work_title)
        live = [
            a for a in self.approvals.values()
            if a.edition_id == edition_id and not a.revoked and a.decision_epoch == epoch
        ]
        items = []
        for approval in live:
            try:
                self._guard_window(edition, approval, territory, at, check_epoch=True)
            except AccessDenied:
                continue
            deriv = self.derivatives[approval.derivative_id]
            items.append({
                "derivative_id": deriv.derivative_id,
                "kind": deriv.kind,
                "checksum_sha256": deriv.checksum_sha256,
                "max_long_edge_px": deriv.max_long_edge_px,
                "credit_line": approval.credit_line_required,
                "approval_id": approval.approval_id,
                # 下载链接带当前纪元；纪元一变即失效
                "download_ref": f"/api/tokens/serve/{self._token_for(approval, territory, 'download', at)}",
            })
        return {
            "bundle_epoch": epoch,
            "generated_at_utc": iso(at),
            "territory": territory,
            "work_title": edition.work_title,
            "holding_institution": self.institutions[edition.holding_institution_code].name,
            "items": items,
            "contains_master": False,
            "notice": "本包不含高清母版；授权变更后 bundle_epoch 失效，须重新生成",
        }

    def _token_for(self, approval, territory, purpose, at: datetime) -> str:
        edition = self._edition(approval.edition_id)
        token = AccessToken(
            token_id=new_id("tok"),
            edition_id=edition.edition_id,
            derivative_id=approval.derivative_id,
            approval_id=approval.approval_id,
            territory=territory,
            issued_epoch=self._epoch_for(edition.work_title),
            issued_at=at,
            expires_at=self._current_window_end(edition, at),
            purpose=purpose,
        )
        self.tokens[token.token_id] = token
        return token.token_id
