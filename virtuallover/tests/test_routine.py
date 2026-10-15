# -*- coding: utf-8 -*-
"""soul/routine.py 早晚安锚点契约测试（主动陪伴升级：定点早安晚安）。

覆盖点（任务书）：
- 抖动确定性：hash(日期+键) 偏移幂等重算不漂移；落在 ±jitter 区间内；
- 锚点判定：到点返回 ["morning"]/["night"]（同刻两者），未到点安静；
- 当日幂等：routine.json 落账 {morning_sent/night_sent: "YYYY-MM-DD"}，
  同日重复 tick 一律安静；损坏状态自愈；
- prompt 渲染：早安可带天气行 + &&morning&&、晚安 &&sleep&&，无残留占位符；
- __main__._routine_tick 接缝：morning prompt 注入天气行并经
  generate_and_send(template_override=...) 发送；放弃期/开关关整段停。
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from virtuallover.soul import routine as routine_mod

DAY = datetime(2026, 10, 14).date()
TODAY = DAY.isoformat()


def make_cfg(**overrides):
    """RoutineConfig 鸭子类型假件（字段名与 config.RoutineConfig 对齐）。"""
    cfg = SimpleNamespace(
        enabled=True,
        morning_time="07:30", morning_jitter_minutes=10,
        night_time="22:50", night_jitter_minutes=5,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


# ---------------------------------------------------------------------------
# 确定性抖动
# ---------------------------------------------------------------------------

def test_jitter_offset_is_deterministic():
    """同一天同一键重算一万次结果一致（内置 hash 有进程盐，sha256 无）。"""
    first = routine_mod.jitter_offset(TODAY, "morning", 10)
    assert all(routine_mod.jitter_offset(TODAY, "morning", 10) == first
               for _ in range(10000))


def test_jitter_offset_within_bounds():
    """偏移落在 [-j, +j]；jitter=0 恒 0（关闭抖动）。"""
    for key in ("morning", "night"):
        for _ in range(50):
            offset = routine_mod.jitter_offset(TODAY, key, 10)
            assert -10 <= offset <= 10
    assert routine_mod.jitter_offset(TODAY, "morning", 0) == 0
    assert routine_mod.jitter_offset(TODAY, "night", -5) == 0


def test_anchor_datetime_applies_jitter():
    """锚点时刻 = 配置时刻 + 确定性抖动；两次计算一致（幂等不漂移）。"""
    a1 = routine_mod.anchor_datetime(DAY, "07:30", 10, "morning")
    a2 = routine_mod.anchor_datetime(DAY, "07:30", 10, "morning")
    base = datetime(DAY.year, DAY.month, DAY.day, 7, 30)
    assert a1 == a2 and abs((a1 - base).total_seconds()) <= 10 * 60


def test_anchor_datetime_invalid_time_falls_back():
    """非法时刻配置回退出厂锚点（night 22:50 / morning 07:30，零抖动）。"""
    assert routine_mod.anchor_datetime(DAY, "坏时间", 0, "night") == \
        datetime(DAY.year, DAY.month, DAY.day, 22, 50)
    assert routine_mod.anchor_datetime(DAY, "", 0, "morning") == \
        datetime(DAY.year, DAY.month, DAY.day, 7, 30)


# ---------------------------------------------------------------------------
# tick：锚点判定 + 当日幂等
# ---------------------------------------------------------------------------

def test_tick_quiet_before_anchor(tmp_path):
    """锚点前 1 分钟：安静返回空。"""
    anchor = routine_mod.anchor_datetime(DAY, "07:30", 10, "morning")
    assert routine_mod.tick(anchor - timedelta(minutes=1), make_cfg(), tmp_path) == []


def test_tick_fires_morning_once_per_day(tmp_path):
    """到点返回 ["morning"]；同日重复 tick 幂等安静；状态文件落账。"""
    anchor = routine_mod.anchor_datetime(DAY, "07:30", 10, "morning")
    assert routine_mod.tick(anchor, make_cfg(), tmp_path) == ["morning"]
    assert routine_mod.tick(anchor + timedelta(minutes=1), make_cfg(), tmp_path) == []
    state = json.loads(
        (tmp_path / routine_mod.STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["morning_sent"] == TODAY


def test_tick_fires_night_after_morning(tmp_path):
    """同一天先早安后晚安：各一次，互不干扰。"""
    morning = routine_mod.anchor_datetime(DAY, "07:30", 10, "morning")
    night = routine_mod.anchor_datetime(DAY, "22:50", 5, "night")
    assert routine_mod.tick(morning, make_cfg(), tmp_path) == ["morning"]
    assert routine_mod.tick(night, make_cfg(), tmp_path) == ["night"]
    assert routine_mod.tick(night + timedelta(minutes=1), make_cfg(), tmp_path) == []
    state = json.loads(
        (tmp_path / routine_mod.STATE_FILENAME).read_text(encoding="utf-8"))
    assert state == {"morning_sent": TODAY, "night_sent": TODAY}


def test_tick_both_due_same_moment(tmp_path):
    """同刻都到点（早晚锚点配同一时刻的极端配置）：一次 tick 返回两者。"""
    cfg = make_cfg(night_time="07:30", night_jitter_minutes=0,
                   morning_jitter_minutes=0)
    at = datetime(DAY.year, DAY.month, DAY.day, 7, 30)
    assert routine_mod.tick(at, cfg, tmp_path) == ["morning", "night"]


def test_tick_disabled_is_silent(tmp_path):
    assert routine_mod.tick(datetime(DAY.year, DAY.month, DAY.day, 12, 0),
                            make_cfg(enabled=False), tmp_path) == []
    assert not (tmp_path / routine_mod.STATE_FILENAME).exists()


def test_tick_survives_corrupt_state(tmp_path):
    """状态文件损坏：视为当日未发，锚点后照常触发（宁可补问候不静默一天）。"""
    (tmp_path / routine_mod.STATE_FILENAME).write_text("{broken", encoding="utf-8")
    anchor = routine_mod.anchor_datetime(DAY, "07:30", 10, "morning")
    assert routine_mod.tick(anchor, make_cfg(), tmp_path) == ["morning"]


def test_mark_sent_idempotent(tmp_path):
    """mark_sent 重复落账同值；与 tick 的幂等口径一致。"""
    routine_mod.mark_sent(datetime(DAY.year, DAY.month, DAY.day, 8, 0),
                          tmp_path, ["morning"])
    routine_mod.mark_sent(datetime(DAY.year, DAY.month, DAY.day, 9, 0),
                          tmp_path, ["morning"])
    state = json.loads(
        (tmp_path / routine_mod.STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["morning_sent"] == TODAY


# ---------------------------------------------------------------------------
# prompt 渲染
# ---------------------------------------------------------------------------

NOW_DT = datetime(DAY.year, DAY.month, DAY.day, 7, 30)


def test_morning_prompt_with_weather_line():
    prompt = routine_mod.render_morning_prompt(NOW_DT, "[资料·天气] 测试市 当前晴 22°C（今日 18~28°C）")
    assert "早安" in prompt
    assert "[资料·天气] 测试市" in prompt          # 天气事实在场
    assert prompt.count("&&morning&&") == 1        # 表情标记至多一次
    assert "{{" not in prompt and "}}" not in prompt


def test_morning_prompt_without_weather():
    prompt = routine_mod.render_morning_prompt(NOW_DT, None)
    assert "早安" in prompt
    assert "资料·天气" not in prompt               # 天气失败自然省略
    assert prompt.count("&&morning&&") == 1


def test_night_prompt_has_sleep_tag():
    prompt = routine_mod.render_night_prompt(datetime(DAY.year, DAY.month, DAY.day, 22, 50))
    assert "晚安" in prompt
    assert prompt.count("&&sleep&&") == 1
    assert "{{" not in prompt


# ---------------------------------------------------------------------------
# __main__._routine_tick 接缝
# ---------------------------------------------------------------------------

class _FakeProactive:
    """发送通道假件：记录 template_override 调用（含放弃期状态槽位）。"""

    def __init__(self, gave_up_date: str = ""):
        self.state = SimpleNamespace(gave_up_date=gave_up_date)
        self.overrides: "list[str]" = []

    async def generate_and_send(self, *, now_ts=None, now_dt=None,
                                subdued=False, template_override=None):
        self.overrides.append(template_override)
        return "早安呀"


def _make_ctx(tmp_path, service, weather_city=""):
    return SimpleNamespace(
        proactive=service,
        repo_root=tmp_path,
        cfg=SimpleNamespace(
            routine=make_cfg(),
            storage=SimpleNamespace(data_dir=str(tmp_path)),
            tools=SimpleNamespace(weather_city=weather_city),
        ),
    )


def test_routine_tick_sends_morning_with_weather(tmp_path, monkeypatch):
    """早安：天气行注入 prompt 并经主动消息通道发出；routine.json 落账。"""
    from virtuallover import __main__ as main_mod

    monkeypatch.setattr(
        main_mod.weather_mod, "get_weather_line",
        lambda city, *, now, cache_dir: f"[资料·天气] {city} 当前阴 20°C（今日 16~24°C）")
    service = _FakeProactive()
    ctx = _make_ctx(tmp_path, service, weather_city="测试市")
    now_ts = datetime(DAY.year, DAY.month, DAY.day, 7, 30).timestamp()
    asyncio.run(main_mod._routine_tick(ctx, now_ts))
    assert len(service.overrides) == 1
    assert "早安" in service.overrides[0]
    assert "[资料·天气] 测试市" in service.overrides[0]
    assert "&&morning&&" in service.overrides[0]
    state = json.loads(
        (tmp_path / routine_mod.STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["morning_sent"] == TODAY


def test_routine_tick_morning_without_city_omits_weather(tmp_path, monkeypatch):
    """未配置天气城市：不发起天气请求，prompt 无天气行。"""

    def _boom(city, *, now, cache_dir):
        raise AssertionError("未配置城市不应发起天气请求")

    from virtuallover import __main__ as main_mod
    monkeypatch.setattr(main_mod.weather_mod, "get_weather_line", _boom)
    service = _FakeProactive()
    ctx = _make_ctx(tmp_path, service, weather_city="")
    now_ts = datetime(DAY.year, DAY.month, DAY.day, 7, 30).timestamp()
    asyncio.run(main_mod._routine_tick(ctx, now_ts))
    assert len(service.overrides) == 1
    assert "资料·天气" not in service.overrides[0]


def test_routine_tick_gave_up_stops_greetings(tmp_path):
    """放弃期（等他回来）：连早晚安也停，且不落账（他回来当天还能补上）。"""
    from virtuallover import __main__ as main_mod

    service = _FakeProactive(gave_up_date=TODAY)
    ctx = _make_ctx(tmp_path, service)
    now_ts = datetime(DAY.year, DAY.month, DAY.day, 7, 30).timestamp()
    asyncio.run(main_mod._routine_tick(ctx, now_ts))
    assert service.overrides == []
    assert not (tmp_path / routine_mod.STATE_FILENAME).exists()


def test_routine_tick_disabled_is_silent(tmp_path):
    from virtuallover import __main__ as main_mod

    service = _FakeProactive()
    ctx = _make_ctx(tmp_path, service)
    ctx.cfg.routine = make_cfg(enabled=False)
    asyncio.run(main_mod._routine_tick(ctx, 0.0))
    assert service.overrides == []


def test_routine_tick_without_channel_is_silent(tmp_path):
    """proactive.enable=false（无发送通道）：静默返回，不落账。"""
    from virtuallover import __main__ as main_mod

    ctx = _make_ctx(tmp_path, None)
    asyncio.run(main_mod._routine_tick(ctx, 0.0))
    assert not (tmp_path / routine_mod.STATE_FILENAME).exists()


def test_wire_scheduler_hooks_routine(tmp_path):
    """EVENT_SCHEDULED 心跳会驱动 _routine_tick（照 _anniversary_tick 同款挂法）。"""
    from virtuallover import __main__ as main_mod
    from virtuallover.events import EVENT_SCHEDULED, EventBus

    bus = EventBus()
    ctx = SimpleNamespace(
        proactive=None, diary_service=None, diary_next_ts=0.0, diary_rng=None,
        repo_root=tmp_path, bus=bus,
        cfg=SimpleNamespace(
            proactive=SimpleNamespace(anniversary=False),
            routine=make_cfg(),
            storage=SimpleNamespace(data_dir=str(tmp_path)),
            tools=SimpleNamespace(weather_city=""),
        ),
    )
    main_mod._wire_scheduler(ctx)

    async def scenario():
        await bus.emit(EVENT_SCHEDULED, {"now": 0.0})
        task = getattr(ctx, "_routine_tick_task", None)
        assert task is not None
        await task                                   # 复入门任务正常收尾

    asyncio.run(scenario())
