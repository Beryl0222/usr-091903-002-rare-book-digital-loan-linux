"""时间语义：许可窗口一律换算为 UTC 半开区间，杜绝提前开始或逾期。

馆藏方分布在英、法、日、德等不同时区，授权以“当地日期”约定。
系统在登记时就把当地日期区间换算成 UTC 半开区间 [start, end)，
之后所有判断只比较 UTC 时刻，不再依赖运行机器的时区。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def parse_instant(text: str) -> datetime:
    """解析必须显式携带时区偏移的时刻，并归一到 UTC。"""
    instant = datetime.fromisoformat(text)
    if instant.tzinfo is None:
        raise ValueError("时间必须显式携带时区偏移，拒绝朴素时间")
    return instant.astimezone(UTC)


def require_utc(instant: datetime) -> datetime:
    """校验调用方传入的是带时区的时刻，并归一到 UTC。"""
    if instant.tzinfo is None:
        raise ValueError("时间必须显式携带时区，拒绝朴素时间")
    return instant.astimezone(UTC)


def local_date_window(start_date: str, end_date: str, tz_name: str) -> tuple[datetime, datetime]:
    """把馆藏方约定的当地日期区间换算成 UTC 半开区间 [start, end)。

    起始日当地 00:00 之前一律未授权；截止日当地 24:00 起一律失效，
    跨国时区不会让授权提前开始或逾期。
    """
    tz = ZoneInfo(tz_name)
    first = date.fromisoformat(start_date)
    last = date.fromisoformat(end_date)
    if last < first:
        raise ValueError("截止日期不能早于起始日期")
    start = datetime.combine(first, time.min, tzinfo=tz).astimezone(UTC)
    end = datetime.combine(last + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC)
    return start, end
