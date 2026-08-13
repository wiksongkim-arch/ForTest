"""定时部署配置校验与下一触发点计算。"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from typing import Any


MAX_SCHEDULE_RANGE = timedelta(days=30)
MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 1440
SCHEDULE_MODES = frozenset({"interval_minutes", "daily_time"})


def parse_schedule_datetime(value: object) -> datetime:
    """读取 ISO 时间并统一为带时区、分钟精度的本地时间。"""

    try:
        parsed = datetime.fromisoformat(str(value or "").strip())
    except ValueError as exc:
        raise ValueError("定时部署的开始或结束时间无效") from exc
    if parsed.tzinfo is None:
        # Qt 日期时间控件提供本地时间；兼容旧数据中没有显式时区的值。
        parsed = parsed.astimezone()
    return parsed.replace(second=0, microsecond=0)


def parse_time_of_day(value: object) -> time:
    """校验每日时间点，持久化格式固定为 HH:mm。"""

    try:
        parsed = datetime.strptime(str(value or "").strip(), "%H:%M").time()
    except ValueError as exc:
        raise ValueError("请选择有效的部署时间点") from exc
    return parsed.replace(second=0, microsecond=0)


def disabled_schedule() -> dict[str, Any]:
    """为普通任务提供稳定默认值，避免 UI 到处判断字段是否存在。"""

    return {
        "enabled": False,
        "mode": "interval_minutes",
        "start_at": "",
        "end_at": "",
        "interval_minutes": 30,
        "time_of_day": "00:00",
        "next_run_at": "",
        "state": "disabled",
        "run_count": 0,
        "last_run_at": "",
    }


def normalize_schedule(
    value: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """校验用户配置并计算创建任务后的首个有效触发点。"""

    raw = dict(value or {})
    if not bool(raw.get("enabled")):
        return disabled_schedule()

    mode = str(raw.get("mode") or "interval_minutes").strip().casefold()
    aliases = {
        "minutes": "interval_minutes",
        "minute": "interval_minutes",
        "on_the_hour": "daily_time",
        "time": "daily_time",
    }
    mode = aliases.get(mode, mode)
    if mode not in SCHEDULE_MODES:
        raise ValueError("定时部署类型无效")

    start = parse_schedule_datetime(raw.get("start_at"))
    end = parse_schedule_datetime(raw.get("end_at"))
    if end <= start:
        raise ValueError("定时部署结束时间必须晚于开始时间")
    if end - start > MAX_SCHEDULE_RANGE:
        raise ValueError("定时部署日期范围最长为 30 天")

    try:
        interval = int(raw.get("interval_minutes") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("请输入有效的部署间隔分钟数") from exc
    if mode == "interval_minutes" and not (
        MIN_INTERVAL_MINUTES <= interval <= MAX_INTERVAL_MINUTES
    ):
        raise ValueError("部署间隔必须为 1 到 1440 分钟")
    if mode != "interval_minutes":
        interval = max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, interval or 30))

    daily_time = parse_time_of_day(raw.get("time_of_day") or "00:00")
    reference = now or datetime.now().astimezone()
    if reference.tzinfo is None:
        reference = reference.astimezone()
    reference = reference.astimezone(start.tzinfo)
    first = next_schedule_occurrence(
        {
            "enabled": True,
            "mode": mode,
            "start_at": start.isoformat(timespec="minutes"),
            "end_at": end.isoformat(timespec="minutes"),
            "interval_minutes": interval,
            "time_of_day": daily_time.strftime("%H:%M"),
        },
        not_before=max(start, reference),
    )
    if first is None:
        raise ValueError("所选日期范围内没有可执行的定时部署时间点")

    return {
        "enabled": True,
        "mode": mode,
        "start_at": start.isoformat(timespec="minutes"),
        "end_at": end.isoformat(timespec="minutes"),
        "interval_minutes": interval,
        "time_of_day": daily_time.strftime("%H:%M"),
        "next_run_at": first.isoformat(timespec="minutes"),
        "state": "waiting",
        "run_count": 0,
        "last_run_at": "",
    }


def next_schedule_occurrence(
    schedule: dict[str, Any],
    *,
    not_before: datetime | None = None,
    after: datetime | None = None,
) -> datetime | None:
    """返回范围内的下一触发点；``after`` 使用严格大于语义。"""

    if not bool(schedule.get("enabled")):
        return None
    start = parse_schedule_datetime(schedule.get("start_at"))
    end = parse_schedule_datetime(schedule.get("end_at"))
    mode = str(schedule.get("mode") or "interval_minutes")
    threshold = start
    if not_before is not None:
        normalized = _same_timezone(not_before, start)
        threshold = max(threshold, normalized)
    strict_after = _same_timezone(after, start) if after is not None else None

    if mode == "interval_minutes":
        try:
            interval = int(schedule.get("interval_minutes") or 0)
        except (TypeError, ValueError):
            return None
        if interval < MIN_INTERVAL_MINUTES:
            return None
        step = timedelta(minutes=interval)
        elapsed = max(0.0, (threshold - start).total_seconds())
        candidate = start + step * math.ceil(elapsed / step.total_seconds())
        if strict_after is not None and candidate <= strict_after:
            behind = (strict_after - candidate).total_seconds()
            candidate += step * (math.floor(behind / step.total_seconds()) + 1)
        return candidate if candidate <= end else None

    if mode != "daily_time":
        return None
    daily = parse_time_of_day(schedule.get("time_of_day"))
    current_date = threshold.date()
    end_date = end.date()
    while current_date <= end_date:
        candidate = datetime.combine(current_date, daily, tzinfo=start.tzinfo)
        if (
            start <= candidate <= end
            and candidate >= threshold
            and (strict_after is None or candidate > strict_after)
        ):
            return candidate
        current_date = _next_date(current_date)
    return None


def _same_timezone(value: datetime, reference: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(reference.tzinfo).replace(second=0, microsecond=0)


def _next_date(value: date) -> date:
    return value + timedelta(days=1)
