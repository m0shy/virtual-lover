# -*- coding: utf-8 -*-
"""soul/schedule.py 契约测试（生活感与时间感：她此刻在做什么）。

覆盖点：
- days 解析："1-5"/"0,6"/"3"/混写 "1,3-5"；越界与非数字段丢弃；
- 区间命中：左闭右开（边界时刻归「刚开始」一侧）、跨天区间（22:00-02:00）、
  周末契约（0=周日）；条目按配置顺序第一个命中生效；
- 兜底：无命中/空白活动 → 「在宿舍休息」；开关关/entries 空 → None（不注入）；
- 防御：非 dict / 缺键 / 非法时刻的条目跳过不炸；
- 纯函数一致性：同一时刻重复判定结果恒等（跨轮不现编的根基）；
- render_line 注入行格式（小时去前导零）+ 默认课表（config.ScheduleConfig）联测。

时间一律 datetime 构造（不依赖机器时区：2026-10-14=周三、10-17=周六、
10-18=周日已核对），无状态文件、无网络。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

# sys.path 引导：virtuallover/ 是 namespace 包，仓库根必须可导入。
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from virtuallover.config import ScheduleConfig  # noqa: E402
from virtuallover.soul import schedule  # noqa: E402

# 已核对的真实日历日：2026-10-14 是周三，10-17 周六，10-18 周日
WED_0830 = datetime(2026, 10, 14, 8, 30)   # 周三早八进行中
WED_0800 = datetime(2026, 10, 14, 8, 0)    # 周三早八边界：刚开始
WED_0940 = datetime(2026, 10, 14, 9, 40)   # 周三早八边界：刚结束
SAT_1000 = datetime(2026, 10, 17, 10, 0)   # 周六
SUN_1000 = datetime(2026, 10, 18, 10, 0)   # 周日（契约值 0）

ENTRIES = [
    {"days": "1-5", "start": "08:00", "end": "09:40", "activity": "在上早八课"},
    {"days": "1-5", "start": "09:50", "end": "11:30", "activity": "在上课"},
    {"days": "0,6", "start": "09:30", "end": "12:00", "activity": "睡懒觉、窝在床上追动漫"},
]


# ---------------------------------------------------------------------------
# days 解析
# ---------------------------------------------------------------------------


def test_parse_days_range_comma_and_single():
    assert schedule.parse_days("1-5") == {1, 2, 3, 4, 5}
    assert schedule.parse_days("0,6") == {0, 6}
    assert schedule.parse_days("3") == {3}
    assert schedule.parse_days("1,3-5") == {1, 3, 4, 5}


def test_parse_days_invalid_parts_dropped():
    """越界/负数/非数字段一律丢弃；空集=条目永不命中（宁缺勿猜）。"""
    assert schedule.parse_days("x") == set()
    assert schedule.parse_days("8") == set()
    assert schedule.parse_days("-1") == set()
    assert schedule.parse_days("1,x,3") == {1, 3}
    assert schedule.parse_days("") == set()
    assert schedule.parse_days(None) == set()


def test_parse_days_reversed_range_tolerated():
    assert schedule.parse_days("5-1") == {1, 2, 3, 4, 5}


# ---------------------------------------------------------------------------
# current_activity：区间命中 / 边界 / 跨天 / 周末
# ---------------------------------------------------------------------------


def test_current_activity_interval_left_closed():
    """左闭右开：08:00 整=刚开始（命中），09:40 整=已结束（不命中）。"""
    assert schedule.current_activity(WED_0800, ENTRIES) == "在上早八课"
    assert schedule.current_activity(WED_0830, ENTRIES) == "在上早八课"
    assert schedule.current_activity(WED_0940, ENTRIES) is None
    # 09:40-09:50 课间空档不命中；09:50 整=下一节刚开始（命中）
    gap = datetime(2026, 10, 14, 9, 45)
    assert schedule.current_activity(gap, ENTRIES) is None
    assert schedule.current_activity(datetime(2026, 10, 14, 9, 50), ENTRIES) == "在上课"


def test_current_activity_first_match_wins():
    """条目按配置顺序取第一个命中（重叠时段不二义）。"""
    overlapping = [
        {"days": "1-5", "start": "08:00", "end": "12:00", "activity": "第一个"},
        {"days": "1-5", "start": "09:00", "end": "10:00", "activity": "第二个"},
    ]
    assert schedule.current_activity(WED_0830, overlapping) == "第一个"


def test_current_activity_weekday_weekend_contract():
    """周末契约：0=周日 6=周六；工作日条目周六周日都不命中。"""
    assert schedule.current_activity(SAT_1000, ENTRIES) == "睡懒觉、窝在床上追动漫"
    assert schedule.current_activity(SUN_1000, ENTRIES) == "睡懒觉、窝在床上追动漫"
    monday = datetime(2026, 10, 19, 10, 0)  # 10-19 是周一
    assert schedule.current_activity(monday, ENTRIES) == "在上课"


def test_current_activity_overnight_interval():
    """跨天区间（22:00-02:00）：深夜与凌晨两头都命中，凌晨过后不再命中。"""
    overnight = [{"days": "0-6", "start": "22:00", "end": "02:00", "activity": "在睡觉"}]
    assert schedule.current_activity(datetime(2026, 10, 14, 23, 30), overnight) == "在睡觉"
    assert schedule.current_activity(datetime(2026, 10, 15, 1, 30), overnight) == "在睡觉"
    assert schedule.current_activity(datetime(2026, 10, 15, 3, 0), overnight) is None
    assert schedule.current_activity(datetime(2026, 10, 14, 21, 59), overnight) is None


def test_current_activity_disabled_or_empty_returns_none():
    """开关关 / entries 空（含 None）一律 None——调用方据此跳过注入。"""
    assert schedule.current_activity(WED_0830, ENTRIES, enabled=False) is None
    assert schedule.current_activity(WED_0830, []) is None
    assert schedule.current_activity(WED_0830, None) is None


def test_current_activity_malformed_entries_skipped():
    """非 dict / 缺键 / 非法时刻的条目跳过不炸，后面的合法条目照常命中。"""
    messy = [
        "不是字典",
        {"days": "1-5", "start": "08:00", "end": "09:40"},           # 缺 activity
        {"days": "1-5", "start": "八点", "end": "09:40", "activity": "坏时刻"},
        {"days": "1-5", "start": "08:00", "end": "09:40", "activity": "在上课"},
    ]
    assert schedule.current_activity(WED_0830, messy) == "在上课"


def test_current_activity_pure_function_same_instant_same_answer():
    """纯函数：同一时刻重复判定恒等——「在干嘛」跨轮不现编的根基。"""
    first = schedule.current_activity(WED_0830, ENTRIES)
    second = schedule.current_activity(WED_0830, ENTRIES)
    assert first == second == "在上早八课"


def test_current_activity_accepts_epoch_float():
    """epoch 秒与等价 datetime 判定一致（时间基准两种注入形态）。"""
    ts = WED_0830.timestamp()
    assert schedule.current_activity(ts, ENTRIES) == "在上早八课"
    assert schedule.current_activity(ts, ENTRIES) == schedule.current_activity(WED_0830, ENTRIES)


# ---------------------------------------------------------------------------
# render_line：注入行格式 / 兜底 / 开关
# ---------------------------------------------------------------------------


def test_render_line_hit_format_strips_hour_leading_zero():
    """命中行：小时去前导零（08:00 → 8:00），更像人口吻。"""
    line = schedule.render_line(WED_0830, ENTRIES)
    assert line == "【她此刻：在上早八课（8:00 开始）】"


def test_render_line_no_match_falls_back_to_default():
    """课表没排到的时段给兜底行（她总得有个去处），绝不返回 None。"""
    late_night = datetime(2026, 10, 14, 23, 30)  # 周三深夜，课表外
    assert schedule.render_line(late_night, ENTRIES) == "【她此刻：在宿舍休息】"


def test_render_line_disabled_or_empty_returns_none():
    """与 current_activity 同口径：开关关 / entries 空 → None（不注入）。"""
    assert schedule.render_line(WED_0830, ENTRIES, enabled=False) is None
    assert schedule.render_line(WED_0830, []) is None


def test_render_line_blank_activity_falls_back_to_default():
    """命中条目的 activity 空白 → 视为没排，给兜底行。"""
    blank = [{"days": "3", "start": "08:00", "end": "09:40", "activity": "  "}]
    assert schedule.render_line(WED_0830, blank) == "【她此刻：在宿舍休息】"


def test_render_line_with_default_config_entries():
    """与真实出厂课表（config.ScheduleConfig）联测：早八/晚自习/周末/深夜四态。"""
    cfg = ScheduleConfig()
    assert schedule.render_line(WED_0830, cfg.entries, enabled=cfg.enabled) == \
        "【她此刻：在上早八课（8:00 开始）】"
    evening = datetime(2026, 10, 14, 21, 0)
    assert schedule.render_line(evening, cfg.entries, enabled=cfg.enabled) == \
        "【她此刻：在图书馆自习（19:00 开始）】"
    assert schedule.render_line(SAT_1000, cfg.entries, enabled=cfg.enabled) == \
        "【她此刻：睡懒觉、窝在床上追动漫（9:30 开始）】"
    late_night = datetime(2026, 10, 14, 23, 30)
    assert schedule.render_line(late_night, cfg.entries, enabled=cfg.enabled) == \
        "【她此刻：在宿舍休息】"
    # 配置总开关关闭 → None（entries 仍在也不注入）
    assert schedule.render_line(WED_0830, cfg.entries, enabled=False) is None
