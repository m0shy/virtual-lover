# -*- coding: utf-8 -*-
"""语音质感（语速链 + 首次语音纪念）契约测试。

覆盖四条链路：
- ``voice/mapping.py``：mood_speed 档位（happy/excited 1.10~1.15、sad/lonely
  0.90~0.95、calm/其余 1.0；intensity 线性放大偏移、越界收敛、round 两位）与
  speed_for_snapshot 快照口径（与 emotion_for_snapshot 同源取数）；
- ``voice/gsvi.py``：speed_factor 写进 /tts 请求（None 不传，键缺位=服务端
  默认 1.0）；
- ``voice/cache.py``：make_key 的 speed 维度（两位小数等价类，不同语速不共
  缓存，None 与既有键兼容）；
- ``voice/manager.py``：speed 经 speed_factor 关键字透传引擎（签名探测兼容旧
  两参契约，绝不试错出网）+ first_voice.json 首次合成只写一次 + setup 落盘
  路径装配。

运行：python -m pytest virtuallover/tests/test_voice_speed.py -q
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from fake_gsvi import MIN_WAV
from virtuallover.voice.cache import VoiceCache
from virtuallover.voice.gsvi import GsviClient
from virtuallover.voice.manager import VoiceManager
from virtuallover.voice.mapping import mood_speed, speed_for_snapshot


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# mapping.mood_speed：档位 / 强度 / 取整
# ---------------------------------------------------------------------------


class TestMoodSpeed:
    def test_happy_excited_band(self):
        """happy/excited：任意强度下都落在 1.10~1.15（开心语速略快）。"""
        for mood in ("happy", "excited"):
            for intensity in (0.0, 0.3, 0.5, 0.8, 1.0):
                got = mood_speed(mood, intensity)
                assert 1.10 <= got <= 1.15, (mood, intensity, got)

    def test_sad_lonely_band(self):
        """sad/lonely：任意强度下都落在 0.90~0.95（低落语速略慢）。"""
        for mood in ("sad", "lonely"):
            for intensity in (0.0, 0.3, 0.5, 0.8, 1.0):
                got = mood_speed(mood, intensity)
                assert 0.90 <= got <= 0.95, (mood, intensity, got)

    def test_pinned_values(self):
        """档位锚点：强度 0/1 取档位两极，0.5 取中点（round 两位后）。"""
        assert mood_speed("happy", 0.0) == 1.10
        assert mood_speed("happy", 0.5) == 1.12
        assert mood_speed("happy", 1.0) == 1.15
        assert mood_speed("sad", 0.0) == 0.95
        assert mood_speed("sad", 0.5) == 0.92
        assert mood_speed("sad", 1.0) == 0.90

    def test_neutral_moods_constant_one(self):
        """calm/其余 7 态（angry/shy）/表外情绪/空串：恒 1.0（语速是增益项）。"""
        for mood in ("calm", "angry", "shy", "惊恐", ""):
            assert mood_speed(mood, 1.0) == 1.0
            assert mood_speed(mood, 0.0) == 1.0

    def test_intensity_amplifies_offset_linearly(self):
        """强度线性放大偏移：0 → 档位近原速端，1 → 极值端，单调不减。"""
        ascending = [mood_speed("happy", i) for i in (0.0, 0.25, 0.5, 0.75, 1.0)]
        assert ascending == sorted(ascending)
        assert ascending[0] < ascending[-1]  # 确有放大而非恒值
        descending = [mood_speed("sad", i) for i in (0.0, 0.25, 0.5, 0.75, 1.0)]
        assert descending == sorted(descending, reverse=True)

    def test_intensity_out_of_range_clamped(self):
        """越界强度收敛到 [0,1]：不外溢档位区间。"""
        assert mood_speed("happy", 5.0) == 1.15
        assert mood_speed("happy", -3.0) == 1.10

    def test_default_intensity_is_half(self):
        """缺省强度 0.5：mood_speed(mood) ≡ mood_speed(mood, 0.5)。"""
        for mood in ("happy", "sad", "calm", "lonely"):
            assert mood_speed(mood) == mood_speed(mood, 0.5)

    def test_result_rounded_to_two_decimals(self):
        """round 两位：缓存键与 /tts 参数共用该值，等价类稳定。"""
        for mood in ("happy", "excited", "sad", "lonely", "calm"):
            for intensity in (0.0, 0.1, 0.37, 0.73, 1.0):
                got = mood_speed(mood, intensity)
                assert got == round(got, 2)


class TestSpeedForSnapshot:
    def test_none_snapshot_returns_none(self):
        """无情绪上下文 → None（调用方不传 speed_factor，服务端默认 1.0）。"""
        assert speed_for_snapshot(None) is None

    def test_tuple_snapshot_uses_intensity(self):
        """(mood, intensity) 元组（effective_mood 规范输入）：强度参与。"""
        assert speed_for_snapshot(("happy", 0.8)) == mood_speed("happy", 0.8) == 1.14

    def test_tuple_without_intensity_uses_default(self):
        """缺强度的元组：按缺省强度取档位（落在档位区间内）。"""
        got = speed_for_snapshot(("sad",))
        assert 0.90 <= got <= 0.95

    def test_string_snapshot_treated_as_mood_key(self):
        """字符串快照（7 态键形态）：1.0（calm 恒值）。"""
        assert speed_for_snapshot("calm") == 1.0

    def test_bad_intensity_falls_back_to_default(self):
        """强度不可解析：按缺省强度处理（档位区间内），绝不抛异常。"""
        got = speed_for_snapshot(("happy", "不是数字"))
        assert 1.10 <= got <= 1.15


# ---------------------------------------------------------------------------
# gsvi：speed_factor 写进 /tts 请求
# ---------------------------------------------------------------------------


class TestGsviSpeedFactor:
    def test_without_speed_factor_param_absent(self, fake_gsvi):
        """None 不传：query 里无 speed_factor 键（服务端按默认 1.0，旧行为）。"""
        client = GsviClient(base_url=fake_gsvi.base_url, retries=1)
        assert _run(client.synthesize("你好")) == MIN_WAV
        assert "speed_factor" not in fake_gsvi.tts_requests[-1]["params"]

    def test_speed_factor_written_to_request(self, fake_gsvi):
        """给定语速：原样写进 /tts（服务端 app.py:76,124 非经典模式消费）。"""
        client = GsviClient(base_url=fake_gsvi.base_url, character="Huohua",
                            emotion="傻白甜", retries=1)
        assert _run(client.synthesize("你好", speed_factor=1.12)) == MIN_WAV
        params = fake_gsvi.tts_requests[-1]["params"]
        assert params["speed_factor"] == "1.12"
        # 情绪/文本等既有参数不受影响（别名成对携带契约不变）
        assert params["character_emotion"] == params["emotion"] == "傻白甜"

    def test_speed_factor_kept_across_retries(self, fake_gsvi):
        """失败重试的每次尝试都携带同一语速（不因重试退化原速）。"""
        fake_gsvi.set_mode("503")
        client = GsviClient(base_url=fake_gsvi.base_url, retries=3, backoff_base=0.0)
        assert _run(client.synthesize("慢速句", speed_factor=0.92)) is None
        assert len(fake_gsvi.tts_requests) == 3
        assert all(r["params"]["speed_factor"] == "0.92"
                   for r in fake_gsvi.tts_requests)


# ---------------------------------------------------------------------------
# cache：speed 维度纳入缓存键
# ---------------------------------------------------------------------------


def _key(cache: VoiceCache, **kw) -> str:
    base = dict(character="测试音色", emotion="傻白甜", text_language="zh",
                audio_format="wav", text="一句测试文本")
    base.update(kw)
    return cache.make_key(**base)


class TestCacheKeySpeed:
    def setup_method(self):
        self.cache = VoiceCache.__new__(VoiceCache)  # make_key 纯函数，免初始化

    def test_different_speeds_different_keys(self):
        """不同语速不共缓存：1.10 / 1.12 / 1.15 三键互异。"""
        keys = {_key(self.cache, speed=s) for s in (1.10, 1.12, 1.15)}
        assert len(keys) == 3

    def test_speed_rounded_to_two_decimals(self):
        """两位小数等价类：1.10 与 1.0999 同键（round 后参与）。"""
        assert _key(self.cache, speed=1.1) == _key(self.cache, speed=1.0999)
        assert _key(self.cache, speed=0.9) == _key(self.cache, speed=0.904)

    def test_no_speed_is_legacy_key(self):
        """speed 缺省（None）：键与既有条目兼容——与新显式传入的键不同维度。"""
        legacy = _key(self.cache)
        assert legacy != _key(self.cache, speed=1.0)

    def test_other_dimensions_still_discriminate(self):
        """speed 相同、其余维度不同仍不同键（情绪/文本维度不被稀释）。"""
        assert _key(self.cache, speed=1.12, emotion="疯批") != \
            _key(self.cache, speed=1.12, emotion="傻白甜")
        assert _key(self.cache, speed=1.12, text="另一句") != \
            _key(self.cache, speed=1.12, text="一句测试文本")


# ---------------------------------------------------------------------------
# manager：speed 透传引擎（签名探测兼容旧契约）
# ---------------------------------------------------------------------------


class _SpeedRecordingEngine:
    """收 speed_factor 的新契约引擎替身：记录 (text, mood, speed) 调用。"""

    def __init__(self, audio: "bytes | None" = MIN_WAV, fail: bool = False):
        self.calls: "list[tuple[str, object, float | None]]" = []
        self.audio = audio
        self.fail = fail

    async def synthesize(self, text, mood_snapshot=None, *, speed_factor=None):
        self.calls.append((text, mood_snapshot, speed_factor))
        if self.fail:
            raise RuntimeError("模拟引擎故障")
        return self.audio


class _LegacyEngine:
    """旧两参契约引擎替身（现役 voice.engine 形态：不收 speed_factor）。"""

    def __init__(self, audio: "bytes | None" = MIN_WAV):
        self.calls: "list[tuple[str, object]]" = []
        self.audio = audio

    async def synthesize(self, text, mood_snapshot=None):
        self.calls.append((text, mood_snapshot))
        return self.audio


def _manager(engine, **kw) -> VoiceManager:
    async def _send(user_id: str, audio: bytes) -> bool:
        return True

    kw.setdefault("engine_provider", lambda: engine)
    kw.setdefault("send_voice", _send)
    return VoiceManager(**kw)


class TestManagerSpeedChain:
    def test_speed_passed_to_engine(self, tmp_path):
        """mood 快照经 speed_for_snapshot 产出语速，speed_factor 关键字透传。"""
        engine = _SpeedRecordingEngine()
        manager = _manager(engine)
        got = _run(manager.synthesize("夸夸你", ("happy", 0.8)))
        assert got == MIN_WAV
        assert engine.calls == [("夸夸你", ("happy", 0.8), 1.14)]

    def test_none_snapshot_passes_no_speed(self, tmp_path):
        """无快照：不传 speed_factor（引擎内部 mood_getter 兜底口径不变）。"""
        engine = _SpeedRecordingEngine()
        manager = _manager(engine)
        _run(manager.synthesize("平平一句"))
        assert engine.calls[0][2] is None

    def test_legacy_engine_contract_still_works(self, tmp_path):
        """旧两参契约引擎：按签名探测退回两参调用，语音不因语速透传失败。"""
        engine = _LegacyEngine()
        manager = _manager(engine)
        got = _run(manager.synthesize("你好", ("sad", 0.9)))
        assert got == MIN_WAV
        assert len(engine.calls) == 1  # 绝不试错二次出网
        assert engine.calls[0] == ("你好", ("sad", 0.9))

    def test_engine_failure_still_degrades(self, tmp_path):
        """引擎故障按失败降级（语速链不改变降级语义）。"""
        engine = _SpeedRecordingEngine(fail=True)
        manager = _manager(engine)
        assert _run(manager.synthesize("坏句", ("happy", 0.8))) is None


# ---------------------------------------------------------------------------
# manager：first_voice.json 首次合成只写一次
# ---------------------------------------------------------------------------


class _FakeClock:
    """可手动推进的 time.time 替身（first_voice 时间戳断言）。"""

    def __init__(self, start: float = 1700.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


def _bare_manager(first_voice_path, clock=None, audio: "bytes | None" = MIN_WAV):
    """裸形态门面：注入 synth 直返脚本化结果（绕过引擎栈，单测记录逻辑）。"""

    async def _synth(text, mood_snapshot=None):
        return audio

    async def _send(user_id: str, audio: bytes) -> bool:
        return True

    return VoiceManager(
        synth=_synth, send_voice=_send,
        first_voice_path=first_voice_path, clock=clock,
    )


class TestFirstVoiceRecord:
    def test_first_success_writes_once(self, tmp_path):
        """首次合成成功落 {ts, text 截断 50 字}；后续合成不覆盖（只记第一次）。"""
        clock = _FakeClock(1700.5)
        path = tmp_path / "vl_data" / "first_voice.json"
        manager = _bare_manager(path, clock)
        assert _run(manager.synthesize("这是她第一次开口说的话")) == MIN_WAV
        clock.now = 9999.0
        _run(manager.synthesize("第二次合成的内容"))
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == {"ts": 1700.5, "text": "这是她第一次开口说的话"}
        assert not list(tmp_path.rglob("*.json.tmp"))  # 原子写无 tmp 残留

    def test_existing_file_never_overwritten(self, tmp_path):
        """已存在不覆盖：重启后首语音时刻仍是事实（文件优先于本次合成）。"""
        path = tmp_path / "first_voice.json"
        path.write_text('{"ts": 42.0, "text": "最早的纪念"}', encoding="utf-8")
        manager = _bare_manager(path, _FakeClock(777.0))
        _run(manager.synthesize("新的一句话"))
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "ts": 42.0, "text": "最早的纪念"}

    def test_long_text_truncated_to_50_chars(self, tmp_path):
        """text 截断 50 字（纪念数据只留引子，不复制整段对话）。"""
        path = tmp_path / "first_voice.json"
        manager = _bare_manager(path)
        _run(manager.synthesize("字" * 80))
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["text"] == "字" * 50 and len(data["text"]) == 50

    def test_failed_synth_writes_nothing(self, tmp_path):
        """合成失败（None）：不记录——纪念的是「第一次成功开口」。"""
        path = tmp_path / "first_voice.json"
        manager = _bare_manager(path, audio=None)
        assert _run(manager.synthesize("失败句")) is None
        assert not path.exists()

    def test_none_path_is_noop(self, tmp_path):
        """first_voice_path=None（未装配/测试形态）：不写盘、不报错。"""
        manager = _bare_manager(None)
        assert _run(manager.synthesize("无落盘路径")) == MIN_WAV
        assert not (tmp_path / "first_voice.json").exists()


# ---------------------------------------------------------------------------
# setup：first_voice 落盘路径按 vl_data 模式装配
# ---------------------------------------------------------------------------


class TestSetupWiring:
    def test_setup_passes_first_voice_path(self, tmp_path):
        """setup(ctx)：first_voice_path = <repo_root>/<storage.data_dir>/first_voice.json
        （与 voice-engine.pid 等运行态同一 vl_data 目录模式）。"""
        from virtuallover.config import TtsConfig
        from virtuallover.voice import manager as manager_mod

        ctx = types.SimpleNamespace(
            repo_root=tmp_path,
            cfg=types.SimpleNamespace(
                tts=TtsConfig(enabled=True),
                storage=types.SimpleNamespace(data_dir="local/vl_data"),
                reply=types.SimpleNamespace(
                    segmented=types.SimpleNamespace(typing_cps=0.08)),
            ),
        )
        cleanup = manager_mod.setup(ctx)
        try:
            manager = ctx.voice_manager
            assert manager._first_voice_path == \
                tmp_path / "local" / "vl_data" / "first_voice.json"
        finally:
            if cleanup is not None:
                cleanup()

    def test_setup_skips_when_tts_disabled(self, tmp_path):
        """tts.enabled=false：不装配（现状语义，first_voice 也不落）。"""
        from virtuallover.config import TtsConfig
        from virtuallover.voice import manager as manager_mod

        ctx = types.SimpleNamespace(
            repo_root=tmp_path,
            cfg=types.SimpleNamespace(tts=TtsConfig(enabled=False)),
        )
        assert manager_mod.setup(ctx) is None
        assert not hasattr(ctx, "voice_manager")
