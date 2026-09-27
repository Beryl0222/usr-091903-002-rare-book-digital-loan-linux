"""HTTP 门面：把领域操作暴露为 JSON 接口，统一异常到状态码的映射。"""

from __future__ import annotations

import base64
from dataclasses import asdict, is_dataclass
from datetime import datetime

from . import audit, intake, licensing, publishing
from .store import DomainError, NotFound, Store
from .timeutil import parse_instant


def _jsonify(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (frozenset, set)):
        return sorted(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonify(asdict(value))
    if isinstance(value, dict):
        return {key: _jsonify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(item) for item in value]
    return value


class LoanApi:
    def __init__(self, store: Store | None = None):
        self.store = store or Store()

    def dispatch(self, method, path, query, body):
        parts = [part for part in path.split("/") if part]
        try:
            return self._route(method, parts, query or {}, body or {})
        except NotFound as error:
            return 404, {"error": str(error)}
        except DomainError as error:
            return 409, {"error": str(error)}
        except (ValueError, KeyError) as error:
            return 400, {"error": str(error)}

    # ---- 路由 ----

    def _route(self, method, parts, query, body):
        store = self.store

        if method == "POST" and parts == ["v1", "institutions"]:
            institution = intake.register_institution(
                store, name=body["name"], country=body["country"]
            )
            return 200, _jsonify(institution)

        if method == "POST" and parts == ["v1", "works"]:
            work = intake.register_work(
                store,
                institution_id=body["institution_id"],
                title=body["title"],
                edition=body["edition"],
                bibliography=body.get("bibliography"),
            )
            return 200, _jsonify(work)

        if method == "POST" and len(parts) == 4 and parts[:2] == ["v1", "works"]:
            work_id, action = parts[2], parts[3]
            if action == "masters":
                master = intake.deliver_master(
                    store,
                    work_id,
                    data=base64.b64decode(body["data_b64"]),
                    sha256=body["sha256"],
                    received_at=parse_instant(body["received_at"]),
                    sealed_until=(
                        parse_instant(body["sealed_until"]) if body.get("sealed_until") else None
                    ),
                )
                return 200, _jsonify(master)
            if action == "pages":
                order = intake.set_pages(
                    store, work_id, body["pages"], at=parse_instant(body["at"])
                )
                return 200, _jsonify(order)
            if action == "page-order":
                order = intake.fix_page_order(
                    store,
                    work_id,
                    tuple(body["ordered_page_ids"]),
                    at=parse_instant(body["at"]),
                    by=body["by"],
                )
                return 200, _jsonify(order)
            if action == "licenses":
                license_ = licensing.grant_license(
                    store,
                    work_id,
                    subject=body["subject"],
                    regions=body["regions"],
                    start_date=body["start_date"],
                    end_date=body["end_date"],
                    tz_name=body["tz"],
                    attribution=body["attribution"],
                    granted_by=body["granted_by"],
                    at=parse_instant(body["at"]),
                )
                return 200, _jsonify(license_)
            if action == "publish":
                release = publishing.publish(
                    store,
                    work_id,
                    region=body["region"],
                    at=parse_instant(body["at"]),
                    by=body["by"],
                    include_highres=body.get("include_highres", True),
                )
                return 200, _jsonify(release)
            if action == "rollback":
                release = publishing.rollback(
                    store,
                    work_id,
                    body["release_id"],
                    at=parse_instant(body["at"]),
                    by=body["by"],
                )
                return 200, _jsonify(release)

        if method == "POST" and len(parts) == 4 and parts[:2] == ["v1", "masters"]:
            if parts[3] == "derivatives":
                derivative = intake.deliver_derivative(
                    store,
                    parts[2],
                    kind=body["kind"],
                    data=base64.b64decode(body["data_b64"]),
                    sha256=body["sha256"],
                    received_at=parse_instant(body["received_at"]),
                )
                return 200, _jsonify(derivative)

        if method == "POST" and len(parts) == 4 and parts[:2] == ["v1", "licenses"]:
            license_id, action = parts[2], parts[3]
            if action == "approve":
                approval = licensing.approve(
                    store, license_id, by=body["by"], at=parse_instant(body["at"])
                )
                return 200, _jsonify(approval)
            if action == "shorten":
                license_ = licensing.shorten_license(
                    store,
                    license_id,
                    new_end_date=body["new_end_date"],
                    tz_name=body["tz"],
                    at=parse_instant(body["at"]),
                    by=body["by"],
                )
                return 200, _jsonify(license_)

        if method == "POST" and len(parts) == 4 and parts[:2] == ["v1", "releases"]:
            release_id, action = parts[2], parts[3]
            if action == "takedown":
                release = publishing.takedown(
                    store,
                    release_id,
                    at=parse_instant(body["at"]),
                    by=body["by"],
                    reason=body.get("reason", "临时下架"),
                )
                return 200, _jsonify(release)
            if action == "downloads":
                token = publishing.issue_download(
                    store,
                    release_id,
                    body["derivative_id"],
                    at=parse_instant(body["at"]),
                    region=body["region"],
                )
                return 200, {"token": token}

        if method == "GET" and len(parts) == 3 and parts[:2] == ["v1", "downloads"]:
            info = publishing.resolve_download(
                store,
                parts[2],
                at=parse_instant(_first(query, "at")),
                region=_first(query, "region"),
            )
            return 200, _jsonify(info)

        if method == "GET" and len(parts) == 4 and parts[:2] == ["v1", "releases"]:
            if parts[3] == "provenance":
                return 200, _jsonify(publishing.provenance(store, parts[2]))

        if method == "GET" and len(parts) == 4 and parts[:2] == ["v1", "institutions"]:
            if parts[3] == "audit":
                events = audit.events_for_institution(store, parts[2])
                return 200, {"events": _jsonify(events)}

        if method == "GET" and len(parts) == 4 and parts[:2] == ["v1", "works"]:
            if parts[3] == "export":
                package = audit.build_export(
                    store,
                    parts[2],
                    region=_first(query, "region"),
                    at=parse_instant(_first(query, "at")),
                )
                return 200, _jsonify(package)

        raise NotFound("接口不存在")


def _first(query, key):
    values = query.get(key)
    if not values:
        raise KeyError(key)
    return values[0]
