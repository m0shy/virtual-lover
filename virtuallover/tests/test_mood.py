# -*- coding: utf-8 -*-
"""情绪引擎测试（SPEC §12 行为断言）—— 沿用 plugins/vl_mood_engine/tests 的关键
断言改写到并入后的 virtuallover.soul.mood，另覆盖活配置加载器与 MoodEngine 门面。
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from virtuallover.soul import mood as mood_mod
from virtuallover.soul.mood import (
    ALL_MOODS,
    DEFAULT_RUDE_PATTERN,
    DEFAULT_SWEET_PATTERN,
    Event,
    MOOD_ENRICHED,
    MOOD_INNER_VOICE,
    MoodEngine,
    MoodState,
    MoodStorage,
    MOOD_ZH,
    VALID_EVENT_TYPES,
    VALID_MOODS,
    apply_event,
    apply_events,
    affection_level,
    classify_keyword,
    compile_keyword_pattern,
    is_crisis,
    is_new_day,
    load_mood_config,
    parse_mood_tag,
    render_stats_reply,
    render_system_fragment,
    render_user_reply,
)
from virtuallover.soul.mood import (
    EVENT_DAILY_FIRST,
    EVENT_KEYWORD_PRAISE,
    EVENT_KEYWORD_RUDE,
    EVENT_KEYWORD_SWEET,
    EVENT_MESSAGE,
    EVENT_MOOD_LLM,
    EVENT_SILENCE_HOURS,
)


def ts(*args) -> float:
    """本地时区时间戳（与引擎的本地自然日判定一致）。"""
    return datetime(*args).timestamp()


# ---------------------------------------------------------------------------
# 数据模型（沿用 test_engine.py TestMoodState 关键断言）
# ---------------------------------------------------------------------------


class TestMoodState:
    def test_default_state(self):
        s = MoodState()
        assert s.affection == 50.0
        assert s.mood == "calm"
        assert s.mood_intensity == pytest.approx(0.3)
        assert s.last_interaction == 0.0
        assert s.daily_streak == 0
        assert s.total_messages == 0

    def test_from_dict_tolerates_bad_data(self):
        s = MoodState.from_dict({"affection": "abc", "mood": "???", "daily_streak": -1})
        assert s.affection == 50.0
        assert s.mood == "calm"
        assert s.daily_streak == 0
        s2 = MoodState.from_dict({"affection": 120.0, "mood_intensity": 2.0})
        assert s2.affection == 100.0
        assert s2.mood_intensity == 1.0
        assert MoodState.from_dict(None) == MoodState()

    def test_from_dict_legacy_data_without_counters(self):
        """旧格式数据（无防刷分计数）加载后计数为 0，不崩溃。"""
        s = MoodState.from_dict({"affection": 50.0, "mood": "calm"})
        assert s.sweet_today == 0 and s.praise_today == 0 and s.rude_today == 0
        assert s.counters_date == ""
        s2 = MoodState.from_dict({"counters_date": "2026-10-10"})
        assert s2.sweet_today == 0
        assert s2.counters_date == "2026-10-10"

    def test_roundtrip_dict(self):
        s = MoodState(affection=66.6, mood="shy", mood_intensity=0.55,
                      daily_streak=3, total_messages=10,
                      sweet_today=1, counters_date="2026-10-10")
        assert MoodState.from_dict(s.to_dict()) == s


# ---------------------------------------------------------------------------
# 事件推进（沿用 test_engine.py 关键断言）
# ---------------------------------------------------------------------------


class TestEvents:
    def test_message_event(self):
        s0 = MoodState(mood_intensity=0.6, total_messages=1)
        t = ts(2026, 10, 10, 8)
        s1 = apply_event(s0, Event(EVENT_MESSAGE, timestamp=t))
        assert s1.affection == pytest.approx(50.05)
        assert s1.total_messages == 2
        assert s1.last_interaction == t
        # R3 灵敏度：普通消息单次回落 0.1 → 0.04（0.6 - 0.04；可配，
        # 见 test_proactive_quality / vl.toml [mood].calm_down_per_message）
        assert s1.mood_intensity == pytest.approx(0.56)
        assert s1.updated_at == t

    def test_sweet_event(self):
        s1 = apply_event(MoodState(), Event(EVENT_KEYWORD_SWEET, timestamp=100.0))
        assert s1.affection == pytest.approx(51.0)
        assert s1.mood == "happy"
        assert s1.mood_intensity == pytest.approx(0.6)
        assert s1.total_messages == 0
        assert s1.sweet_today == 1

    def test_praise_at_60_boundary_shy(self):
        s1 = apply_event(
            MoodState(affection=60.0, mood_intensity=0.0),
            Event(EVENT_KEYWORD_PRAISE, timestamp=100.0),
        )
        assert s1.affection == pytest.approx(62.0)
        assert s1.mood == "shy"

    def test_rude_event(self):
        s1 = apply_event(MoodState(), Event(EVENT_KEYWORD_RUDE, timestamp=100.0))
        assert s1.affection == pytest.approx(48.0)
        assert s1.mood == "angry"
        assert s1.mood_intensity == pytest.approx(0.7)
        assert s1.total_messages == 0
        assert s1.rude_today == 1

    def test_silence_exactly_24h(self):
        s1 = apply_event(
            MoodState(last_interaction=ts(2026, 10, 9, 8)),
            Event(EVENT_SILENCE_HOURS, value=24.0, timestamp=ts(2026, 10, 10, 8)),
        )
        assert s1.affection == pytest.approx(49.5)
        assert s1.mood == "lonely"

    def test_silence_multiple_steps(self):
        s1 = apply_event(
            MoodState(), Event(EVENT_SILENCE_HOURS, value=48.0, timestamp=100.0)
        )
        assert s1.affection == pytest.approx(49.0)
        assert s1.mood == "lonely"

    def test_silence_under_24h_no_penalty(self):
        s1 = apply_event(
            MoodState(), Event(EVENT_SILENCE_HOURS, value=23.9, timestamp=100.0)
        )
        assert s1.affection == pytest.approx(50.0)
        assert s1.mood == "lonely"

    def test_silence_does_not_touch_interaction_fields(self):
        s0 = MoodState(last_interaction=50.0, total_messages=7)
        s1 = apply_event(s0, Event(EVENT_SILENCE_HOURS, value=25.0, timestamp=100.0))
        assert s1.total_messages == 7
        assert s1.last_interaction == 50.0
        assert s1.updated_at == 100.0


class TestDailyFirst:
    def test_first_ever_interaction(self):
        s1 = apply_event(MoodState(), Event(EVENT_DAILY_FIRST, timestamp=ts(2026, 10, 10)))
        assert s1.affection == pytest.approx(51.5)
        assert s1.daily_streak == 1

    def test_streak_continues_bonus(self):
        s0 = MoodState(daily_streak=3, last_interaction=ts(2026, 10, 9))
        s1 = apply_event(s0, Event(EVENT_DAILY_FIRST, timestamp=ts(2026, 10, 10)))
        assert s1.affection == pytest.approx(50.0 + 1.5 + 0.6)
        assert s1.daily_streak == 4

    def test_streak_bonus_capped_at_5(self):
        s0 = MoodState(daily_streak=6, last_interaction=ts(2026, 10, 9))
        s1 = apply_event(s0, Event(EVENT_DAILY_FIRST, timestamp=ts(2026, 10, 10)))
        assert s1.affection == pytest.approx(50.0 + 1.5 + 1.0)
        assert s1.daily_streak == 7

    def test_gap_clears_streak(self):
        s0 = MoodState(daily_streak=5, last_interaction=ts(2026, 10, 7))
        s1 = apply_event(s0, Event(EVENT_DAILY_FIRST, timestamp=ts(2026, 10, 10)))
        assert s1.affection == pytest.approx(50.0 + 1.5 + 1.0)  # 加成基于互动前旧值
        assert s1.daily_streak == 1

    def test_same_day_crosses_without_24h(self):
        """不足 24h 但跨自然日（昨夜 23:00 → 今晨 01:00）：连击延续。"""
        s0 = MoodState(daily_streak=2, last_interaction=ts(2026, 10, 9, 23, 0))
        s1 = apply_event(s0, Event(EVENT_DAILY_FIRST, timestamp=ts(2026, 10, 10, 1, 0)))
        assert s1.daily_streak == 3

    def test_daily_bonus_disabled(self):
        s0 = MoodState(daily_streak=3, last_interaction=ts(2026, 10, 9))
        s1 = apply_event(
            s0, Event(EVENT_DAILY_FIRST, timestamp=ts(2026, 10, 10)), daily_bonus_enabled=False
        )
        assert s1.affection == pytest.approx(51.5)
        assert s1.daily_streak == 4


class TestClampAndLevels:
    def test_affection_clamped_at_max(self):
        s1 = apply_event(MoodState(affection=99.98), Event(EVENT_KEYWORD_SWEET, timestamp=100.0))
        assert s1.affection == 100.0

    def test_affection_clamped_at_min(self):
        s1 = apply_event(MoodState(affection=0.5), Event(EVENT_KEYWORD_RUDE, timestamp=100.0))
        assert s1.affection == 0.0
        assert s1.mood == "angry"  # 唤醒后强度 0.7 >= 0.6 → angry

    def test_intensity_clamped(self):
        s = MoodState(mood_intensity=0.05)
        for _ in range(10):
            s = apply_event(s, Event(EVENT_MESSAGE, timestamp=100.0))
        assert s.mood_intensity == 0.0

    @pytest.mark.parametrize("affection,expected", [
        (0.0, "cold"), (29.99, "cold"), (30.0, "neutral"), (59.99, "neutral"),
        (60.0, "close"), (85.0, "close"), (85.01, "devoted"), (100.0, "devoted"),
    ])
    def test_affection_level_boundaries(self, affection, expected):
        assert affection_level(affection) == expected


class TestKeywordFarmingCap:
    def test_sweet_6th_time_no_affection_gain(self):
        t = ts(2026, 10, 10)
        s = MoodState()
        for _ in range(5):
            s = apply_event(s, Event(EVENT_KEYWORD_SWEET, timestamp=t))
        assert s.affection == pytest.approx(55.0)
        assert s.sweet_today == 5
        s6 = apply_event(s, Event(EVENT_KEYWORD_SWEET, timestamp=t))
        assert s6.affection == pytest.approx(55.0)
        assert s6.sweet_today == 6
        assert s6.mood == "happy"  # 行为化照常

    def test_rude_6th_time_no_affection_loss(self):
        t = ts(2026, 10, 10)
        s = MoodState()
        for _ in range(5):
            s = apply_event(s, Event(EVENT_KEYWORD_RUDE, timestamp=t))
        assert s.affection == pytest.approx(40.0)
        s6 = apply_event(s, Event(EVENT_KEYWORD_RUDE, timestamp=t))
        assert s6.affection == pytest.approx(40.0)
        assert s6.rude_today == 6
        assert s6.mood == "angry"

    def test_counters_reset_next_day(self):
        t1 = ts(2026, 10, 10)
        s = MoodState()
        for _ in range(5):
            s = apply_event(s, Event(EVENT_KEYWORD_SWEET, timestamp=t1))
        t2 = ts(2026, 10, 11)
        s2 = apply_event(s, Event(EVENT_KEYWORD_SWEET, timestamp=t2))
        assert s2.sweet_today == 1
        assert s2.praise_today == 0 and s2.rude_today == 0
        assert s2.affection == pytest.approx(56.0)  # 新的一天第 1 条重新加分

    def test_non_keyword_events_do_not_roll_counters(self):
        s0 = MoodState(counters_date="2026-10-01", sweet_today=3)
        s1 = apply_event(s0, Event(EVENT_MESSAGE, timestamp=ts(2026, 10, 10)))
        assert s1.counters_date == "2026-10-01"
        assert s1.sweet_today == 3


class TestComposition:
    def test_silence_then_daily_first_sequence(self):
        """隔夜回归完整序列：沉默结算 → 每日首条 → 关键词 → 普通消息。"""
        s0 = MoodState(daily_streak=2, last_interaction=ts(2026, 10, 9, 8))
        events = [
            Event(EVENT_SILENCE_HOURS, value=28.0),
            Event(EVENT_DAILY_FIRST),
            Event(EVENT_KEYWORD_SWEET),
            Event(EVENT_MESSAGE),
        ]
        s1 = apply_events(s0, events, now=ts(2026, 10, 10, 12))
        assert s1.affection == pytest.approx(50.0 - 0.5 + 1.9 + 1.0 + 0.05)
        assert s1.daily_streak == 3
        assert s1.mood == "happy"
        assert s1.last_interaction == ts(2026, 10, 10, 12)
        assert s1.total_messages == 1  # 仅普通消息计数

    def test_apply_event_is_pure(self):
        s0 = MoodState(affection=50.0, mood="calm", daily_streak=1)
        apply_event(s0, Event(EVENT_KEYWORD_RUDE, timestamp=100.0))
        assert s0.affection == 50.0
        assert s0.mood == "calm"
        assert s0.daily_streak == 1

    def test_unknown_event_type_raises(self):
        with pytest.raises(ValueError):
            apply_event(MoodState(), Event("hug", timestamp=100.0))

    def test_is_new_day(self):
        assert is_new_day(ts(2026, 10, 10, 8), ts(2026, 10, 10, 20)) is False
        assert is_new_day(ts(2026, 10, 9, 23), ts(2026, 10, 10, 1)) is True
        assert is_new_day(0.0, ts(2026, 10, 10)) is True


# ---------------------------------------------------------------------------
# 渲染（去数字化 + 时间衰减）
# ---------------------------------------------------------------------------


class TestRender:
    def test_system_fragment_no_numbers(self):
        s = MoodState(affection=72.4, mood="happy", mood_intensity=0.5, daily_streak=3)
        text = render_system_fragment(s)
        assert "亲密" in text and "开心" in text and "3 天" in text
        assert "72" not in text and "/100" not in text

    def test_system_fragment_pure_data_line(self):
        """R3：片段只承载数据，使用指令固化在 persona system_prompt 尾部。"""
        s = MoodState(affection=72.4, mood="happy", mood_intensity=0.5, daily_streak=3)
        text = render_system_fragment(s)
        assert text == "【当前内心状态】关系：亲密｜心情：开心（中等）｜连续互动 3 天"

    def test_system_fragment_decays_to_calm_after_10h(self):
        updated = ts(2026, 10, 10, 8)
        s = MoodState(mood="happy", mood_intensity=0.6, updated_at=updated)
        text = render_system_fragment(s, now=updated + 10 * 3600)
        assert "心情：平静（平静）" in text
        assert "开心" not in text

    def test_user_reply_no_numbers(self):
        s = MoodState(affection=85.0, mood="shy", daily_streak=2)
        text = render_user_reply(s)
        assert text == "她现在害羞，你们的关系是「亲密」哦，已经连续互动 2 天啦～"
        assert "85" not in text

    def test_stats_reply_contains_numbers_and_counters(self):
        s = MoodState(
            affection=72.4, mood="happy", mood_intensity=0.55, daily_streak=3,
            total_messages=10, sweet_today=2, praise_today=1, rude_today=4,
        )
        text = render_stats_reply(s)
        assert "72.4" in text and "0.55" in text
        assert "甜言 2 次" in text and "夸奖 1 次" in text and "负面 4 次" in text


# ---------------------------------------------------------------------------
# 关键词分类、否定窗口与危机兜底（沿用 test_keywords.py 关键断言）
# ---------------------------------------------------------------------------


def _compiled(sweet: str | None = None, praise: str | None = None, rude: str | None = None):
    import re

    return (
        re.compile(rude or DEFAULT_RUDE_PATTERN),
        re.compile(praise or r"好看|厉害|想你|爱你"),
        re.compile(sweet or DEFAULT_SWEET_PATTERN),
    )


class TestClassifyKeyword:
    def test_rude_priority_over_others(self):
        rude_re, praise_re, sweet_re = _compiled()
        assert classify_keyword("闭嘴，你真是厉害", rude_re, praise_re, sweet_re) == EVENT_KEYWORD_RUDE

    def test_negated_sweet_voided(self):
        rude_re, praise_re, sweet_re = _compiled()
        assert classify_keyword("不喜欢你", rude_re, praise_re, sweet_re) is None

    def test_negated_rude_voided(self):
        rude_re, praise_re, sweet_re = _compiled()
        # 现网活配置 rude 含裸「烦」：否定窗口内的「烦」作废
        rude_re, praise_re, sweet_re = _compiled(rude=r"闭嘴|烦|滚")
        assert classify_keyword("不烦", rude_re, praise_re, sweet_re) is None

    def test_negation_beyond_window_still_hits(self):
        rude_re, praise_re, sweet_re = _compiled(sweet=r"喜欢你")
        # 否定标记距命中超过 6 字符 → 照常命中
        text = "不确定明天有没有空，不过说真的我很喜欢你"
        assert classify_keyword(text, rude_re, praise_re, sweet_re) == EVENT_KEYWORD_SWEET

    def test_no_fallthrough_after_void(self):
        """否定作废后整条消息按普通消息处理，不再尝试其他类别。"""
        rude_re, praise_re, sweet_re = _compiled()
        assert classify_keyword("别厉害", rude_re, praise_re, sweet_re) is None

    def test_crisis_signals(self):
        assert is_crisis("我不想活了") is True
        assert is_crisis("今天天气不错") is False


class TestPatternGuard:
    def test_normal_pattern_kept(self):
        p = compile_keyword_pattern(r"亲爱的|宝贝", DEFAULT_SWEET_PATTERN, key="sweet")
        assert p.pattern == r"亲爱的|宝贝"

    def test_invalid_pattern_falls_back(self):
        p = compile_keyword_pattern(r"([unclosed", DEFAULT_SWEET_PATTERN, key="sweet")
        assert p.pattern == DEFAULT_SWEET_PATTERN

    def test_overlong_pattern_falls_back(self):
        p = compile_keyword_pattern("a" * 201, DEFAULT_SWEET_PATTERN, key="sweet")
        assert p.pattern == DEFAULT_SWEET_PATTERN

    def test_catastrophic_pattern_falls_back(self):
        """灾难性回溯正则：探针累计超时后回退默认（不挂死加载）。"""
        p = compile_keyword_pattern(r"(a+)+b", DEFAULT_SWEET_PATTERN, key="sweet")
        assert p.pattern == DEFAULT_SWEET_PATTERN


# ---------------------------------------------------------------------------
# 存储层（沿用 test_storage.py 关键断言）
# ---------------------------------------------------------------------------


class TestStorage:
    def test_set_get_roundtrip(self, tmp_path):
        st = MoodStorage(tmp_path)
        st.set("u1", MoodState(affection=61.0, mood="shy"))
        got = st.get("u1")
        assert got.affection == 61.0 and got.mood == "shy"

    def test_set_only_updates_cache_until_flush(self, tmp_path):
        st = MoodStorage(tmp_path)
        st.set("u1", MoodState(affection=61.0))
        reloaded = MoodStorage(tmp_path)
        assert reloaded.get("u1").affection == 50.0  # 未 flush 不落盘
        st.flush()
        reloaded = MoodStorage(tmp_path)
        assert reloaded.get("u1").affection == 61.0

    def test_persists_across_instances(self, tmp_path):
        st = MoodStorage(tmp_path)
        st.set("42", MoodState(affection=55.5))
        st.flush()
        st2 = MoodStorage(tmp_path)
        assert st2.get("42").affection == 55.5

    def test_delete(self, tmp_path):
        st = MoodStorage(tmp_path)
        st.set("u1", MoodState())
        st.flush()
        assert st.delete("u1") is True
        assert st.delete("u1") is False
        assert MoodStorage(tmp_path).get("u1") == MoodState()

    def test_corrupted_file_falls_back_to_empty(self, tmp_path):
        path = tmp_path / "mood_states.json"
        path.write_text("not-json{{{", encoding="utf-8")
        st = MoodStorage(tmp_path)
        assert st.get("u1") == MoodState()
        assert path.with_suffix(".json.bak").exists()  # 原文件已备份

    def test_entry_level_corruption_keeps_good_entries(self, tmp_path):
        path = tmp_path / "mood_states.json"
        path.write_text(
            json.dumps({"good": MoodState(affection=60.0).to_dict(), "bad": 123},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        st = MoodStorage(tmp_path)
        assert st.get("good").affection == 60.0  # 好条目保留
        assert path.with_suffix(".json.bak").exists()


# ---------------------------------------------------------------------------
# 活配置加载（读现网 vl_mood_engine_config.json）
# ---------------------------------------------------------------------------


class TestLoadMoodConfig:
    def test_live_config_loaded(self):
        """读现网活配置：rude=「闭嘴|烦|滚」（实测比引擎默认宽松）、双开关为 true。"""
        local_cfg = REPO_ROOT / "local/astrbot_data/config/vl_mood_engine_config.json"
        if not local_cfg.exists():
            pytest.skip("local live config not present in clean clone")
        cfg = load_mood_config(local_cfg)
        assert cfg["keyword_groups"]["rude"] == r"闭嘴|烦|滚"
        assert cfg["keyword_groups"]["sweet"] == r"亲爱的|宝贝|喜欢你"
        assert cfg["keyword_groups"]["praise"] == r"好看|厉害|想你|爱你"
        assert cfg["daily_bonus_enabled"] is True
        assert cfg["inject_enabled"] is True

    def test_missing_file_falls_back_to_defaults(self, tmp_path):
        cfg = load_mood_config(tmp_path / "nope.json")
        assert cfg["keyword_groups"] == {
            "sweet": DEFAULT_SWEET_PATTERN,
            "praise": r"好看|厉害|想你|爱你",
            "rude": DEFAULT_RUDE_PATTERN,
        }
        assert cfg["daily_bonus_enabled"] is True and cfg["inject_enabled"] is True

    def test_broken_json_falls_back(self, tmp_path):
        path = tmp_path / "cfg.json"
        path.write_text("{oops", encoding="utf-8")
        cfg = load_mood_config(path)
        assert cfg["keyword_groups"]["rude"] == DEFAULT_RUDE_PATTERN

    def test_bom_file_readable(self, tmp_path):
        """实测现网配置带 UTF-8 BOM：utf-8-sig 读取不炸。"""
        path = tmp_path / "cfg.json"
        path.write_bytes(
            b"\xef\xbb\xbf" + json.dumps({"daily_bonus_enabled": False}).encode("utf-8")
        )
        cfg = load_mood_config(path)
        assert cfg["daily_bonus_enabled"] is False


# ---------------------------------------------------------------------------
# 门面：消息推进 / 危机兜底 / 命令跳过 / 注入开关 / reset
# ---------------------------------------------------------------------------


class TestMoodEngineFacade:
    @pytest.fixture()
    def engine(self, tmp_path):
        cfg = {
            "keyword_groups": {
                "sweet": r"亲爱的|宝贝|喜欢你",
                "praise": r"好看|厉害|想你|爱你",
                "rude": r"闭嘴|烦|滚",  # 现网活配置同款（含裸「烦」）
            },
            "daily_bonus_enabled": True,
            "inject_enabled": True,
        }
        return MoodEngine(tmp_path, config=cfg)

    def test_handle_message_full_sequence(self, engine):
        """沉默补算 + 每日首条 + 关键词 + 普通消息在门面内按序推进。"""
        async def run():
            # t0→t1 间隔 25h：够 24h 沉默补算，且自然日 diff==1（连击延续）
            t0 = ts(2026, 10, 8, 20)
            await engine.handle_message("u1", "在吗", now=t0)
            s = engine.storage.get("u1")
            assert s.daily_streak == 1
            t1 = ts(2026, 10, 9, 21)
            s2 = await engine.handle_message("u1", "晚安啦", now=t1)
            return s2, t1

        s2, t1 = asyncio.run(run())
        assert s2 is not None
        assert s2.daily_streak == 2
        assert s2.mood == "lonely"  # 沉默事件后普通消息回落，情绪为孤单
        assert s2.last_interaction == t1
        assert s2.total_messages == 2

    def test_crisis_message_not_advanced(self, engine):
        """危机兜底先于一切分类：不推进状态、不计事件。"""
        async def run():
            t0 = ts(2026, 10, 10, 12)
            await engine.handle_message("u1", "今天很开心", now=t0)
            before = engine.storage.get("u1")
            result = await engine.handle_message("u1", "我不想活了", now=t0 + 60)
            return result, before

        result, before = asyncio.run(run())
        assert result is None
        assert engine.storage.get("u1") == before  # 状态原封不动

    def test_command_message_skipped(self, engine):
        """/ 开头命令不刷好感。"""
        t0 = ts(2026, 10, 10, 12)
        result = asyncio.run(engine.handle_message("u1", "/mood", now=t0))
        assert result is None
        assert engine.storage.get("u1") == MoodState()

    def test_live_rude_pattern_hits(self, engine):
        """现网活配置 rude=「闭嘴|烦|滚」：裸「烦」也命中。"""
        t0 = ts(2026, 10, 10, 12)
        s = asyncio.run(engine.handle_message("u1", "真烦", now=t0))
        assert s is not None
        assert s.mood in ("angry", "sad")
        assert s.affection == pytest.approx(50.0 + 1.5 - 2.0 + 0.05)  # daily_first +1.5、rude -2、message +0.05

    def test_render_fragment_respects_switch(self, engine):
        t0 = ts(2026, 10, 10, 12)
        engine.storage.set("u1", MoodState(mood="happy", updated_at=t0))
        assert "内心状态" in engine.render_fragment("u1", now=t0)
        engine.inject_enabled = False
        assert engine.render_fragment("u1", now=t0) == ""

    def test_reset_user(self, engine):
        async def run():
            t0 = ts(2026, 10, 10, 12)
            await engine.handle_message("u1", "在吗", now=t0)
            first = await engine.reset_user("u1")
            second = await engine.reset_user("u1")
            return first, second

        first, second = asyncio.run(run())
        assert first is True
        assert second is False
        assert engine.storage.get("u1") == MoodState()


# ---------------------------------------------------------------------------
# 情绪丰富化：新心情枚举 / 自评标签 / 小转移规则 / 重逢彩蛋
# ---------------------------------------------------------------------------


class TestEnrichedMoods:
    def test_new_mood_enums(self):
        assert MOOD_ENRICHED == {
            "wronged", "jealous", "expect", "lost", "sweet", "secure", "tsundere",
        }
        assert MOOD_ENRICHED.isdisjoint(VALID_MOODS)
        assert ALL_MOODS == VALID_MOODS | MOOD_ENRICHED
        assert len(ALL_MOODS) == 14

    def test_valid_moods_unchanged_for_voice_mapping(self):
        """VALID_MOODS 必须保持原 7 态：与 voice/mapping 音色 7 态表对账（勿扩）。"""
        assert VALID_MOODS == {
            "happy", "sad", "angry", "shy", "lonely", "calm", "excited",
        }

    def test_mood_zh_covers_all_moods(self):
        assert set(MOOD_ZH) == ALL_MOODS
        assert MOOD_ZH["wronged"] == "委屈"
        assert MOOD_ZH["jealous"] == "吃醋"
        assert MOOD_ZH["expect"] == "期待"
        assert MOOD_ZH["lost"] == "失落"
        assert MOOD_ZH["sweet"] == "甜蜜"
        assert MOOD_ZH["secure"] == "安心"
        assert MOOD_ZH["tsundere"] == "傲娇"

    def test_inner_voice_covers_enriched_only(self):
        """内心独白只收新 7 情绪：原 7 态保持 R3 纯数据行片段。"""
        assert set(MOOD_INNER_VOICE) == MOOD_ENRICHED
        for line in MOOD_INNER_VOICE.values():
            assert line.startswith("（") and line.endswith("）")

    @pytest.mark.parametrize("name", sorted(MOOD_ENRICHED))
    def test_new_mood_roundtrip(self, name):
        """新心情 to_dict/from_dict 往返无损。"""
        s = MoodState(affection=55.0, mood=name, mood_intensity=0.6, updated_at=100.0)
        assert MoodState.from_dict(s.to_dict()) == s

    def test_from_dict_illegal_mood_falls_back_calm(self):
        assert MoodState.from_dict({"mood": "ghost"}).mood == "calm"
        assert MoodState.from_dict({"mood": 123}).mood == "calm"

    def test_legacy_state_without_new_fields_loads(self):
        """旧格式数据（无 last_silence_* 键）加载为从未结算，无需迁移。"""
        s = MoodState.from_dict(
            {"affection": 66.6, "mood": "shy", "mood_intensity": 0.55,
             "counters_date": "2026-10-01"}
        )
        assert s.mood == "shy"
        assert s.last_silence_hours == 0.0
        assert s.last_silence_at == 0.0

    def test_legacy_state_file_loads_without_migration(self, tmp_path):
        """旧版 mood_states.json 直接加载：不崩溃、新键按默认值补齐。"""
        path = tmp_path / "mood_states.json"
        legacy = {"u1": {
            "affection": 50.0, "mood": "happy", "mood_intensity": 0.3,
            "last_interaction": 100.0, "daily_streak": 1, "total_messages": 2,
            "updated_at": 100.0, "sweet_today": 0, "praise_today": 0,
            "rude_today": 0, "counters_date": "",
        }}
        path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        st = MoodStorage(tmp_path)
        got = st.get("u1")
        assert got.mood == "happy"
        assert got.last_silence_hours == 0.0 and got.last_silence_at == 0.0
        # 读改写一轮后新键自然落盘——整个过程不需要任何迁移步骤
        st.set("u1", st.get("u1"))
        st.flush()
        reloaded = json.loads(path.read_text(encoding="utf-8"))
        assert reloaded["u1"]["last_silence_hours"] == 0.0
        assert reloaded["u1"]["mood"] == "happy"


class TestParseMoodTag:
    def test_tail_tag(self):
        assert parse_mood_tag("今天也要开心哦<<mood:happy:0.8>>") == (
            "今天也要开心哦", "happy", 0.8)

    def test_middle_tag(self):
        assert parse_mood_tag("他刚才<<mood:wronged:0.6>>哼") == ("他刚才哼", "wronged", 0.6)

    def test_multiple_tags_last_legal_wins_all_stripped(self):
        assert parse_mood_tag("<<mood:sad:0.2>>先这样<<mood:jealous:0.9>>") == (
            "先这样", "jealous", 0.9)

    def test_illegal_name_between_legals_is_skipped(self):
        assert parse_mood_tag("a<<mood:happy:0.1>>b<<mood:ghost:0.9>>") == (
            "ab", "happy", 0.1)

    def test_only_illegal_names_returns_none(self):
        assert parse_mood_tag("哦<<mood:ecstatic:0.9>><<MOOD:god>>") is None

    def test_no_tag_returns_none(self):
        assert parse_mood_tag("普通一句话") is None
        assert parse_mood_tag("") is None

    def test_missing_intensity_defaults_half(self):
        assert parse_mood_tag("哼<<mood:tsundere>>") == ("哼", "tsundere", 0.5)

    def test_case_insensitive_with_whitespace(self):
        assert parse_mood_tag("嗯<< MOOD : Secure : 0.4 >>") == ("嗯", "secure", 0.4)

    def test_intensity_clamped_into_unit_range(self):
        assert parse_mood_tag("<<mood:shy:1.7>>") == ("", "shy", 1.0)

    def test_negative_intensity_is_not_a_tag(self):
        # 负数不匹配严格数值形态：没有合法标签 → None（出站由 strip 兜底剥除）
        assert parse_mood_tag("x<<mood:shy:-0.5>>") is None

    def test_parsed_tag_is_fully_strippable(self):
        """parse 与出站 strip 的交叉一致性：能解析的标签必能被完整剥掉。"""
        from virtuallover.soul.textclean import strip_mood_tags

        sample = "文本<<mood:shy:0.4>>中<<MOOD:secure:1.2>>尾"
        parsed = parse_mood_tag(sample)
        assert parsed is not None
        cleaned, _, _ = parsed
        assert strip_mood_tags(sample).strip() == cleaned


class TestMoodLlmEvent:
    def test_event_type_registered(self):
        assert EVENT_MOOD_LLM in VALID_EVENT_TYPES

    def test_applies_mood_and_intensity_without_affection(self):
        s = apply_event(
            MoodState(),
            Event(EVENT_MOOD_LLM, value=0.8, mood="wronged", timestamp=100.0),
        )
        assert s.mood == "wronged"
        assert s.mood_intensity == pytest.approx(0.8)
        assert s.affection == pytest.approx(50.0)  # 自评声明不动好感度
        assert s.updated_at == 100.0
        assert s.last_interaction == 100.0

    def test_accepts_enriched_and_legacy_names(self):
        for name in ("secure", "tsundere", "happy", "angry"):
            s = apply_event(
                MoodState(), Event(EVENT_MOOD_LLM, value=0.4, mood=name, timestamp=1.0)
            )
            assert s.mood == name

    def test_missing_value_keeps_current_intensity(self):
        s = apply_event(
            MoodState(mood_intensity=0.45),
            Event(EVENT_MOOD_LLM, mood="expect", timestamp=1.0),
        )
        assert s.mood == "expect"
        assert s.mood_intensity == pytest.approx(0.45)

    def test_illegal_mood_ignored(self):
        s0 = MoodState(mood="shy", mood_intensity=0.5)
        s = apply_event(
            s0, Event(EVENT_MOOD_LLM, value=0.9, mood="ghost", timestamp=1.0)
        )
        assert s.mood == "shy"
        assert s.mood_intensity == pytest.approx(0.5)
        assert s.affection == pytest.approx(50.0)

    def test_apply_event_is_pure_for_mood_llm(self):
        s0 = MoodState(mood="calm")
        apply_event(s0, Event(EVENT_MOOD_LLM, value=0.7, mood="lost", timestamp=1.0))
        assert s0.mood == "calm"

    def test_pipeline_parse_then_apply(self):
        """管线约定用法：parse_mood_tag 输出直接喂 Event(EVENT_MOOD_LLM)。"""
        cleaned, name, intensity = parse_mood_tag("好啦好啦<<mood:tsundere:0.7>>")
        s = apply_event(
            MoodState(),
            Event(EVENT_MOOD_LLM, value=intensity, mood=name, timestamp=5.0),
        )
        assert cleaned == "好啦好啦"
        assert s.mood == "tsundere"
        assert s.mood_intensity == pytest.approx(0.7)


class TestTransitionRules:
    def test_third_party_female_signal_jealous(self):
        t = ts(2026, 10, 10, 12)
        s = apply_event(
            MoodState(mood_intensity=0.2, updated_at=t - 60),
            Event(EVENT_MESSAGE, timestamp=t, text="今天碰到那个女生了"),
        )
        assert s.mood == "jealous"

    @pytest.mark.parametrize("text", [
        "别的女生有你好看了吗",
        "他前女友的事我不想听",
        "路边小姐姐朝我笑了一下",
        "别提别的女人",
    ])
    def test_third_party_lexicon(self, text):
        t = ts(2026, 10, 10, 12)
        s = apply_event(
            MoodState(updated_at=t - 60),
            Event(EVENT_MESSAGE, timestamp=t, text=text),
        )
        assert s.mood == "jealous"

    def test_expect_keywords(self):
        t = ts(2026, 10, 10, 12)
        for text in ("好期待周末的约会", "我等着你回来", "到时候再说吧"):
            s = apply_event(
                MoodState(updated_at=t - 60),
                Event(EVENT_MESSAGE, timestamp=t, text=text),
            )
            assert s.mood == "expect", text

    def test_jealous_priority_over_expect(self):
        t = ts(2026, 10, 10, 12)
        s = apply_event(
            MoodState(updated_at=t - 60),
            Event(EVENT_MESSAGE, timestamp=t, text="到时候让那个女生看看"),
        )
        assert s.mood == "jealous"

    def test_plain_message_no_transition(self):
        t = ts(2026, 10, 10, 12)
        s = apply_event(
            MoodState(mood_intensity=0.5, updated_at=t - 60),
            Event(EVENT_MESSAGE, timestamp=t, text="今天天气不错"),
        )
        assert s.mood == "calm"

    def test_message_without_text_keeps_legacy_behavior(self):
        s = apply_event(MoodState(mood_intensity=0.6), Event(EVENT_MESSAGE, timestamp=100.0))
        assert s.mood == "calm"
        assert s.mood_intensity == pytest.approx(0.56)

    def test_angry_then_sweet_within_30min_makes_up(self):
        """吃醋后和好：angry 状态 30 分钟内的甜话 → sweet。"""
        t0 = ts(2026, 10, 10, 12)
        angry = apply_event(MoodState(mood_intensity=0.3), Event(EVENT_KEYWORD_RUDE, timestamp=t0))
        assert angry.mood == "angry"
        made_up = apply_event(angry, Event(EVENT_KEYWORD_SWEET, timestamp=t0 + 600))
        assert made_up.mood == "sweet"

    def test_angry_then_sweet_after_30min_stays_happy(self):
        t0 = ts(2026, 10, 10, 12)
        angry = apply_event(MoodState(mood_intensity=0.3), Event(EVENT_KEYWORD_RUDE, timestamp=t0))
        late = apply_event(angry, Event(EVENT_KEYWORD_SWEET, timestamp=t0 + 3600))
        assert late.mood == "happy"

    def test_angry_with_unknown_update_time_no_reconcile(self):
        """updated_at 为 0（从未更新）不参与和好窗口判定。"""
        angry = MoodState(mood="angry", mood_intensity=0.7, updated_at=0.0)
        s = apply_event(angry, Event(EVENT_KEYWORD_SWEET, timestamp=100.0))
        assert s.mood == "happy"


class TestReunionFragment:
    def _settled(self, hours: float, settled_at: float = 1000.0) -> MoodState:
        return apply_event(
            MoodState(), Event(EVENT_SILENCE_HOURS, value=hours, timestamp=settled_at)
        )

    def test_reunion_within_10_minutes(self):
        s = self._settled(hours=30.0, settled_at=1000.0)
        frag = render_system_fragment(s, now=1000.0 + 300)
        assert frag.startswith("（他一整天都没理我……哼，终于肯来找我了）\n")
        assert "【当前内心状态】" in frag

    def test_no_reunion_after_10_minutes(self):
        s = self._settled(hours=30.0, settled_at=1000.0)
        frag = render_system_fragment(s, now=1000.0 + 601)
        assert not frag.startswith("（")

    def test_tier_lines_by_hours(self):
        s72 = self._settled(hours=80.0, settled_at=1000.0)
        frag72 = render_system_fragment(s72, now=1000.0 + 60)
        assert frag72.startswith("（他好几天没理我了，又委屈又想他）\n")
        s168 = self._settled(hours=200.0, settled_at=1000.0)
        frag168 = render_system_fragment(s168, now=1000.0 + 60)
        assert frag168.startswith("（他一个多星期都没理我了")

    def test_silence_under_24h_no_reunion(self):
        s = self._settled(hours=10.0, settled_at=1000.0)
        assert not render_system_fragment(s, now=1000.0 + 30).startswith("（")

    def test_legacy_state_no_reunion(self):
        frag = render_system_fragment(
            MoodState(mood="happy", updated_at=1000.0), now=1000.0
        )
        assert frag == "【当前内心状态】关系：平和｜心情：开心（平静）｜连续互动 0 天"

    def test_reunion_without_now_uses_updated_at(self):
        """now 不传以 updated_at 为参照：刚结算完的存量状态也带彩蛋。"""
        s = self._settled(hours=30.0, settled_at=1000.0)
        assert render_system_fragment(s).startswith("（他一整天")

    def test_inner_voice_for_enriched_mood(self):
        s = MoodState(mood="tsundere", mood_intensity=0.6, updated_at=1000.0)
        frag = render_system_fragment(s, now=1000.0)
        assert "傲娇" in frag
        assert "才、才不是特意在等他" in frag

    def test_original_moods_keep_pure_data_line(self):
        """原 7 态且无重逢：R3 纯数据行输出保持逐字不变。"""
        s = MoodState(
            affection=72.4, mood="happy", mood_intensity=0.5,
            daily_streak=3, updated_at=1000.0,
        )
        assert render_system_fragment(s, now=1000.0) == (
            "【当前内心状态】关系：亲密｜心情：开心（中等）｜连续互动 3 天"
        )

    def test_engine_silence_then_reunion_fragment(self, tmp_path):
        """引擎端到端：25h 后回归消息结算沉默，紧随其后的片段带重逢文案。"""
        engine = MoodEngine(tmp_path, config={
            "keyword_groups": {}, "daily_bonus_enabled": True, "inject_enabled": True,
        })
        t0 = ts(2026, 10, 8, 20)
        asyncio.run(engine.handle_message("u1", "在吗", now=t0))
        t1 = t0 + 25 * 3600 + 60
        s = asyncio.run(engine.handle_message("u1", "在吗", now=t1))
        assert s.last_silence_hours == pytest.approx(25.0 + 60 / 3600.0)
        frag = engine.render_fragment("u1", now=t1 + 60)
        assert frag.startswith("（他一整天")

    def test_engine_no_reunion_after_window(self, tmp_path):
        engine = MoodEngine(tmp_path, config={
            "keyword_groups": {}, "daily_bonus_enabled": True, "inject_enabled": True,
        })
        t0 = ts(2026, 10, 8, 20)
        asyncio.run(engine.handle_message("u1", "在吗", now=t0))
        t1 = t0 + 25 * 3600
        asyncio.run(engine.handle_message("u1", "在吗", now=t1))
        frag = engine.render_fragment("u1", now=t1 + 3600)
        assert "他一整天" not in frag
