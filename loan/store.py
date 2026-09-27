"""内存仓储与缓存登记：所有领域状态的唯一入口。"""

from __future__ import annotations

import hashlib

from .models import STATUS_ACTIVE, AuditEvent, MasterScan, PageOrder, Release


class DomainError(Exception):
    """领域规则被拒绝。"""


class NotFound(DomainError):
    """引用的实体不存在。"""


class ChecksumMismatch(DomainError):
    """交付文件与校验值不符。"""


class LicenseError(DomainError):
    """许可缺失、未批准或已失效。"""


class SealedError(DomainError):
    """高清母版仍处于封闭期。"""


class AuditContentError(DomainError):
    """审计日志试图记录图像内容。"""


class CacheRegistry:
    """按发布版本登记缓存与下载令牌；新决定一生效即整体失效。"""

    def __init__(self):
        self._entries = {}  # (release_id, derivative_id) -> 是否有效
        self._tokens = {}   # token -> (release_id, derivative_id)

    def register(self, release_id, derivative_ids):
        for derivative_id in derivative_ids:
            self._entries[(release_id, derivative_id)] = True

    def invalidate_release(self, release_id):
        for rid, did in list(self._entries):
            if rid == release_id:
                self._entries[(rid, did)] = False

    def is_valid(self, release_id, derivative_id):
        return self._entries.get((release_id, derivative_id), False)

    def issue_token(self, release_id, derivative_id):
        # 签发前由调用方完成许可核对；此处重新激活缓存项
        self._entries[(release_id, derivative_id)] = True
        seed = f"{release_id}:{derivative_id}:{len(self._tokens)}"
        token = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
        self._tokens[token] = (release_id, derivative_id)
        return token

    def resolve(self, token):
        pair = self._tokens.get(token)
        if pair is None:
            raise DomainError("下载链接无效")
        if not self.is_valid(*pair):
            raise DomainError("下载链接已按新决定失效")
        return pair


class Store:
    def __init__(self):
        self.institutions = {}
        self.works = {}
        self.masters = {}
        self.master_bytes = {}  # master_id -> bytes，仅供校验，不进入日志与导出
        self.derivatives = {}
        self.pages = {}
        self.page_orders = {}   # work_id -> [PageOrder]，按版本递增
        self.licenses = {}
        self.approvals = {}
        self.releases = {}
        self.audit = []
        self.cache = CacheRegistry()
        self._seq = {}

    # ---- 标识与查询 ----

    def new_id(self, prefix):
        n = self._seq.get(prefix, 0) + 1
        self._seq[prefix] = n
        return f"{prefix}-{n}"

    def institution(self, institution_id):
        try:
            return self.institutions[institution_id]
        except KeyError:
            raise NotFound(f"馆藏机构不存在：{institution_id}")

    def work(self, work_id):
        try:
            return self.works[work_id]
        except KeyError:
            raise NotFound(f"古籍不存在：{work_id}")

    def master(self, master_id):
        try:
            return self.masters[master_id]
        except KeyError:
            raise NotFound(f"母版不存在：{master_id}")

    def license(self, license_id):
        try:
            return self.licenses[license_id]
        except KeyError:
            raise NotFound(f"许可不存在：{license_id}")

    def release(self, release_id):
        try:
            return self.releases[release_id]
        except KeyError:
            raise NotFound(f"发布版本不存在：{release_id}")

    def current_master(self, work_id):
        """现行母版：未被替换的那一份。"""
        for master in self.masters.values():
            if master.work_id == work_id and master.replaced_by is None:
                return master
        return None

    def current_page_order(self, work_id):
        orders = self.page_orders.get(work_id)
        return orders[-1] if orders else None

    def active_releases(self, work_id):
        return [
            release
            for release in self.releases.values()
            if release.work_id == work_id and release.status == STATUS_ACTIVE
        ]

    # ---- 审计 ----

    def record(self, event: AuditEvent):
        """审计只记录标识符；任何内容载荷都直接拒绝。"""
        for value in event.detail.values():
            if isinstance(value, (bytes, bytearray)):
                raise AuditContentError("审计日志不得包含图像内容")
        self.audit.append(event)


def withdraw_release(store: Store, release: Release, *, at, reason, actor):
    """撤回一次发布：状态、缓存与下载令牌同步失效。"""
    release.status = "withdrawn"
    release.withdrawn_at = at
    release.withdraw_reason = reason
    store.cache.invalidate_release(release.id)
    work = store.work(release.work_id)
    store.record(
        AuditEvent(
            at=at,
            actor=actor,
            action="release-withdrawn",
            institution_id=work.institution_id,
            work_id=release.work_id,
            detail={"release_id": release.id, "reason": reason},
        )
    )
