# -*- coding: utf-8 -*-
"""soul/schedule.py —— 作息课表：「她此刻在做什么」（生活感与时间感）。

解决两个真实问题：
- 她被问「在干嘛」只能现编，且同一时刻跨轮答案互相矛盾——课表命中是纯函数
  （同一时刻 → 同一活动），一致性天然成立，不用任何状态落盘；
- 她的时间感只有日期时刻，没有「星期几/这个点本来该在干嘛」的语义。

配置面（vl.toml，键名归 config.ScheduleConfig）::

    [schedule]
    enabled = true
    [[schedule.entries]]
    days = "1-5"        # 契约星期：0=周日 1=周一 … 6=周六；支持 "1-5"/"0,6"/"3"
    start = "08:00"     # "HH:MM" 24 小时制；end <= start 视为跨天区间（如 22:00-02:00）
    end = "09:40"
    activity = "在上早八课"

公开面（brain/persona 的调用方按此接线；本模块是纯函数，无 IO、无状态）：

- ``current_activity(now, entries, enabled=True)``：命中条目的 activity 文本；
- ``render_line(now, entries, enabled=True)``：注入行「【她此刻：在上早八课
  （8:00 开始）】」；无命中给兜底行（在宿舍休息）；
- ``enabled=False`` 或 entries 空：两者一律 None（调用方跳过注入）。

命中语义：契约星期命中 且 start <= t < end（左闭右开，边界时刻归「刚开始」
的一侧）；条目按配置顺序取第一个命中；缺键/解析失败的条目跳过不炸。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Sequence

logger = logging.getLogger("virtuallover.soul.schedule")

# 契约星期名（下标 = days 契约值：0=周日 … 6=周六；与 persona 侧 weekday()
# 0=周一的排列不同，勿混用）
WEEKDAY_NAMES_CN: "tuple[str, ...]" = (
    "星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六",
)

# 无命中（课表没排到的时段）兜底活动：总得有个去处
DEFAULT_ACTIVITY = "在宿舍休息"

# 注入行模板（start 显示去掉小时前导零：08:00 → 8:00，更像人口吻）
_LINE_TPL = "【她此刻：{activity}（{start} 开始）】"
_DEFAULT_LINE = f"【她此刻：{DEFAULT_ACTIVITY}】"

# 一天的分钟数（跨天区间判定用）
_MINUTES_PER_DAY = 24 * 60


# ---------------------------------------------------------------------------
# 解析（days / HH:MM）
# ---------------------------------------------------------------------------


def parse_days(spec: Any) -> "set[int]":
    """解析课表 days 字段 → 契约星期集合（0=周日 … 6=周六）。

    - ``"1-5"`` → {1,2,3,4,5}；``"0,6"`` → {0,6}；``"3"`` → {3}；
    - 混写 ``"1,3-5"`` 合法（逗号分段，段内可为区间）；
    - 越界（8）/负数/非数字段一律丢弃（不炸、不猜测），全非法返回空集。
    """
    if spec is None:
        return set()
    days: set[int] = set()
    for part in str(spec).replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            left, _, right = part.partition("-")
            try:
                lo, hi = int(left.strip()), int(right.strip())
            except ValueError:
                continue
            if lo > hi:
                lo, hi = hi, lo  # "5-1" 容错为 "1-5"
            days.update(d for d in range(lo, hi + 1) if 0 <= d <= 6)
        else:
            try:
                day = int(part)
            except ValueError:
                continue
            if 0 <= day <= 6:
                days.add(day)
    return days


def _hhmm_to_minutes(text: Any) -> "int | None":
    """"HH:MM" → 当日分钟数（0..1440，"24:00" 合法=当日结束）；非法返回 None。"""
    if text is None:
        return None
    parts = str(text).strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if hour == 24 and minute == 0:
        return _MINUTES_PER_DAY
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _display_hhmm(text: Any) -> str:
    """课表 start 的展示形态：小时去前导零（"08:00" → "8:00"）；解析失败原样返回。"""
    minutes = _hhmm_to_minutes(text)
    if minutes is None:
        return str(text)
    return f"{minutes // 60}:{minutes % 60:02d}"


def _as_datetime(now: "datetime | float | None") -> datetime:
    """时间基准归一为本地 naive datetime（None=当下；float=epoch 秒）。"""
    if now is None:
        return datetime.now()
    if isinstance(now, datetime):
        return now
    return datetime.fromtimestamp(float(now))


def _contract_weekday(now_dt: datetime) -> int:
    """days 契约值：Python weekday() 是周一=0，契约是周日=0，差一位换算。"""
    return (now_dt.weekday() + 1) % 7


# ---------------------------------------------------------------------------
# 命中与渲染
# ---------------------------------------------------------------------------


def _match_entry(
    now: "datetime | float | None", entries: "Sequence[Any] | None"
) -> "dict | None":
    """按配置顺序找第一个命中条目（契约星期 + 时间区间）；无命中返回 None。

    - 区间左闭右开：start <= t < end，边界时刻归「刚开始」的一侧；
    - end <= start 视为跨天区间（如 22:00-02:00：t >= start 或 t < end）；
    - 非法条目（非 dict / 缺键 / 时间解析失败 / start == end 零长 /
      activity 空白——没排内容等于没排）跳过，后面的合法条目照常参与命中。
    """
    if not entries:
        return None
    now_dt = _as_datetime(now)
    day = _contract_weekday(now_dt)
    minutes = now_dt.hour * 60 + now_dt.minute
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if not str(entry.get("activity") or "").strip():
            continue
        if day not in parse_days(entry.get("days")):
            continue
        start = _hhmm_to_minutes(entry.get("start"))
        end = _hhmm_to_minutes(entry.get("end"))
        if start is None or end is None or start == end:
            continue
        if start < end:
            hit = start <= minutes < end
        else:  # 跨天区间（22:00-02:00）
            hit = minutes >= start or minutes < end
        if hit:
            return entry
    return None


def current_activity(
    now: "datetime | float | None",
    entries: "Sequence[Any] | None",
    *,
    enabled: bool = True,
) -> "str | None":
    """命中条目的 activity 文本；无命中/开关关/entries 空/活动空白 → None。

    纯函数：同一 (now, entries) 恒等——「她在干嘛」跨轮一致性的根基。
    """
    if not enabled or not entries:
        return None
    entry = _match_entry(now, entries)
    if entry is None:
        return None
    activity = str(entry.get("activity") or "").strip()
    return activity or None


def render_line(
    now: "datetime | float | None",
    entries: "Sequence[Any] | None",
    *,
    enabled: bool = True,
) -> "str | None":
    """渲染「她此刻」注入行；开关关/entries 空 → None（调用方不注入）。

    - 命中：``【她此刻：在上早八课（8:00 开始）】``；
    - 无命中（课表没排的时段）：兜底行 ``【她此刻：在宿舍休息】``。
    """
    if not enabled or not entries:
        return None
    entry = _match_entry(now, entries)
    activity = str(entry.get("activity") or "").strip() if entry else ""
    if not activity:
        return _DEFAULT_LINE
    return _LINE_TPL.format(activity=activity, start=_display_hhmm(entry.get("start")))
