# -*- coding: utf-8 -*-
"""voice/engine.py（§1 引擎栈，manager.py 槽位的契约实现）测试（VOICE_SPEC §1/§4）。

最高优先：**串行化断言**——并发调用 /tts 请求时间窗零重叠（fake_gsvi 记录
start_ts/end_ts）。其余覆盖：缓存命中单请求（字节相等、不出网）、键含情绪、
非法情绪永不出网（GSVI「未注册情绪静默 200+wav」怪癖的反向防御）、未注册
角色 400 判失败、降级两型（未就绪秒败 + HANG 超时喂死）、熔断冷却跳过、
§2 门面接缝（synth/engine_gate/state_name）、控制台契约（status/preview
线程安全/setup 转发）。

不引入 pytest-asyncio：asyncio.run / run_coroutine_threadsafe 驱动。

运行：python -m pytest virtuallover/tests/test_voice_manager.py -q
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from fake_gsvi import DEFAULT_CHARACTERS, FakeGsvi, MIN_WAV
from virtuallover.voice.cache import VoiceCache
from virtuallover.voice.engine import VoiceEngine
from virtuallover.voice.gsvi import GsviClient
from virtuallover.voice.lifecycle import (
    GsviLifecycle,
    LifecycleParams,
    STATE_DEGRADED,
    STATE_NOT_STARTED,
    STATE_READY,
)
from virtuallover.voice.manager import VoiceManager


# ---------------------------------------------------------------------------
# 夹具与构造
# ---------------------------------------------------------------------------


@pytest.fixture()
def gsvi():
    """假 GSVI（OK 模式、50ms 人为耗时——串行化断言的时窗来源）。"""
    server = FakeGsvi(latency=0.05).start()
    try:
        yield server
    finally:
        server.stop()


def _engine(gsvi: FakeGsvi, tmp_path, *, character="Huohua", emotion_map=None,
            retries=2, timeout=1.0, default_emotion="聊天") -> VoiceEngine:
    """构造接假服务的引擎（lifecycle 不拉真进程：auto_start=False + 假探针）。"""
    client = GsviClient(base_url=gsvi.base_url, character=character,
                        timeout=timeout, retries=retries, backoff_base=0.0)
    cache = VoiceCache(tmp_path / "voice_cache")
    params = LifecycleParams(
        auto_start=False, host="127.0.0.1",
        port=gsvi._httpd.server_address[1],  # 探测指向假服务端口
        pid_path=tmp_path / "voice" / "voice-engine.pid",
        log_path=tmp_path / "logs" / "voice-engine.log",
    )
    lc = GsviLifecycle(params, repo_root=tmp_path, probe=lambda: False)
    return VoiceEngine(
        client=client, cache=cache, lifecycle=lc,
        emotion_map=dict(emotion_map) if emotion_map else None,
        default_emotion=default_emotion,
    )


def _mark_ready(engine: VoiceEngine) -> None:
    """测试直注就绪态（等价 adopt-first 收编：不 spawn、不探测真实进程）。"""
    engine.lifecycle.state = STATE_READY
    engine.lifecycle.ownership = "external"


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 串行化（VOICE_SPEC §4 最高优先：请求时间窗零重叠）
# ---------------------------------------------------------------------------


def _assert_disjoint(gsvi: FakeGsvi, count: int) -> None:
    """全部 /tts 请求时间窗两两零重叠（排序后相邻比较）。"""
    windows = sorted((r["start_ts"], r["end_ts"]) for r in gsvi.tts_requests)
    assert len(windows) == count
    for (_, prev_end), (next_start, _) in zip(windows, windows[1:]):
        assert next_start >= prev_end, f"时间窗重叠：{windows}"


class TestSerialization:
    def test_concurrent_synth_windows_never_overlap(self, gsvi, tmp_path):
        """并发 6 路 engine.synthesize：全局单飞锁内出网，时间窗零重叠。"""
        engine = _engine(gsvi, tmp_path)
        _mark_ready(engine)

        async def main():
            return await asyncio.gather(*[
                engine.synthesize(f"并发第{i}句") for i in range(6)])

        results = _run(main())
        assert all(r == MIN_WAV for r in results)
        _assert_disjoint(gsvi, 6)

    def test_facade_and_engine_share_one_lock(self, gsvi, tmp_path):
        """§2 门面（synth=engine.synthesize）并发调用同样零重叠（同一把锁）。"""
        engine = _engine(gsvi, tmp_path)
        _mark_ready(engine)

        async def _ok() -> bool:
            return True

        facade = VoiceManager(synth=engine.synthesize, send_voice=_ok)

        async def main():
            return await asyncio.gather(*[
                facade.synthesize(f"门面第{i}句") for i in range(4)])

        results = _run(main())
        assert all(r == MIN_WAV for r in results)
        _assert_disjoint(gsvi, 4)


# ---------------------------------------------------------------------------
# 缓存与情绪映射
# ---------------------------------------------------------------------------


class TestCacheAndMapping:
    def test_second_identical_call_hits_cache_no_new_request(self, gsvi, tmp_path):
        engine = _engine(gsvi, tmp_path)
        _mark_ready(engine)
        first = _run(engine.synthesize("重复文本", ("happy", 0.8)))
        assert first == MIN_WAV
        assert len(gsvi.tts_requests) == 1
        second = _run(engine.synthesize("重复文本", ("happy", 0.8)))
        assert second == MIN_WAV and len(gsvi.tts_requests) == 1  # 零出网

    def test_key_contains_emotion(self, gsvi, tmp_path):
        """同文本不同情绪 → 两个缓存键 → 两次出网（键含 emotion）。"""
        engine = _engine(gsvi, tmp_path)
        _mark_ready(engine)
        _run(engine.synthesize("同一句", ("happy", 0.8)))
        _run(engine.synthesize("同一句", ("calm", 0.3)))
        emotions = [r["effective"]["emotion"] for r in gsvi.tts_requests]
        assert emotions == ["傻白甜", "聊天"]  # R3-2a：calm→聊天

    def test_illegal_emotion_never_leaves_client(self, gsvi, tmp_path):
        """映射值 ∉ 注册表 → 回退链 → 只发注册情绪（GSVI 静默怪癖的反向断言）。"""
        engine = _engine(gsvi, tmp_path, emotion_map={"happy": "不存在的情绪"})
        _mark_ready(engine)
        audio = _run(engine.synthesize("夸夸你", ("happy", 0.8)))
        assert audio == MIN_WAV
        sent = [r["effective"]["emotion"] for r in gsvi.tts_requests]
        assert sent == ["聊天"]           # 回退到注册情绪（fallback 键默认）
        assert "不存在的情绪" not in sent  # 非法情绪永不出网

    def test_mood_snapshot_forms(self, gsvi, tmp_path):
        """快照形态归一化：元组 / 7 态键 / 中文情绪名 / None（calm）。"""
        engine = _engine(gsvi, tmp_path)
        _mark_ready(engine)
        cases = [(("happy", 0.5), "傻白甜"), ("happy", "傻白甜"),
                 ("开心", "傻白甜"), (None, "聊天")]  # R3-2a：None→calm→聊天
        for index, (snapshot, want) in enumerate(cases):
            _run(engine.synthesize(f"形态用例{index}", snapshot))
            assert gsvi.tts_requests[-1]["effective"]["emotion"] == want

    def test_registry_fetched_once_only(self, gsvi, tmp_path):
        """注册表拉一次后常驻（GSVI 每调写盘 character_info.json，禁轮询）。"""
        engine = _engine(gsvi, tmp_path)
        _mark_ready(engine)
        for i in range(3):
            _run(engine.synthesize(f"第{i}句"))
        registry_calls = [r for r in gsvi.requests if r["path"] == "/character_list"]
        assert len(registry_calls) == 1


# ---------------------------------------------------------------------------
# 降级两型与故障模式
# ---------------------------------------------------------------------------


class TestDegradation:
    def test_not_ready_returns_none_without_requests(self, gsvi, tmp_path):
        """引擎未就绪：该条降级纯文字（返回 None），零出网，绝不阻塞文字路径。"""
        engine = _engine(gsvi, tmp_path)  # lifecycle not_started（auto_start=False）
        assert engine.state_name() == STATE_NOT_STARTED
        assert _run(engine.synthesize("你好")) is None
        assert gsvi.tts_requests == []

    def test_degraded_cooldown_skips_retry(self, gsvi, tmp_path):
        """熔断冷却期：跳过重试直接 None（§2），零出网。"""
        engine = _engine(gsvi, tmp_path)
        engine.lifecycle.state = STATE_DEGRADED
        assert _run(engine.synthesize("你好")) is None
        assert gsvi.tts_requests == []

    def test_503_falls_back_to_none(self, gsvi, tmp_path):
        engine = _engine(gsvi, tmp_path)
        _mark_ready(engine)
        gsvi.set_mode("503")
        assert _run(engine.synthesize("失败句")) is None

    def test_non_wav_treated_as_failure(self, gsvi, tmp_path):
        engine = _engine(gsvi, tmp_path)
        _mark_ready(engine)
        gsvi.set_mode("NON_WAV")
        assert _run(engine.synthesize("假成功")) is None

    def test_conn_reset_treated_as_failure(self, gsvi, tmp_path):
        engine = _engine(gsvi, tmp_path)
        _mark_ready(engine)
        gsvi.set_mode("CONN_RESET")
        assert _run(engine.synthesize("断连句")) is None

    def test_hang_timeout_feeds_death_chain(self, tmp_path):
        """HANG（TCP 通、响应挂起）：客户端超时 → None；连续 2 次喂死 →
        auto_start=False 下进入 not_started（无自愈能力，等待外部处理）。"""
        hang = FakeGsvi(mode="HANG", hang_seconds=5.0, latency=0).start()
        try:
            engine = _engine(hang, tmp_path, timeout=0.15)
            _mark_ready(engine)
            assert _run(engine.synthesize("挂起句")) is None
            assert engine.lifecycle.state_name() == STATE_READY  # 1 次：仅计数
            assert _run(engine.synthesize("挂起句2")) is None
            assert engine.lifecycle.state_name() == STATE_NOT_STARTED  # 判死
        finally:
            hang.stop()

    def test_unregistered_character_400_is_failure(self, gsvi, tmp_path):
        """GSVI 怪癖：未注册角色 → 400 JSON（tts_backend.py:154）→ 判失败回退。"""
        engine = _engine(gsvi, tmp_path, character="不存在的角色")
        _mark_ready(engine)
        assert _run(engine.synthesize("你好")) is None

    def test_empty_text_rejected(self, gsvi, tmp_path):
        engine = _engine(gsvi, tmp_path)
        _mark_ready(engine)
        with pytest.raises(ValueError):
            _run(engine.synthesize("   "))


# ---------------------------------------------------------------------------
# §2 接缝与控制台契约
# ---------------------------------------------------------------------------


class TestContracts:
    def test_state_name_gate_for_facade(self, gsvi, tmp_path):
        """§2 门面状态门消费 state_name()：仅 ready 放行。"""
        engine = _engine(gsvi, tmp_path)
        assert engine.state_name() == STATE_NOT_STARTED
        _mark_ready(engine)
        assert engine.state_name() == STATE_READY

    def test_status_console_keys(self, gsvi, tmp_path):
        """status() 提供控制台状态卡契约键（console/voice.py 消费）。"""
        engine = _engine(gsvi, tmp_path)
        _mark_ready(engine)
        _run(engine.client.character_list())  # 注册表拉取后状态卡可给 chips
        status = engine.status()
        for key in ("lifecycle", "ownership", "character", "registered_emotions",
                    "cache_count", "cache_bytes", "cache_max_entries",
                    "cache_max_bytes", "pid", "started_at", "auto_start",
                    "base_url", "log_path", "last_error"):
            assert key in status, f"缺状态卡键 {key}"
        assert status["lifecycle"] == "ready" and status["ownership"] == "external"
        assert status["registered_emotions"] == {"Huohua": DEFAULT_CHARACTERS["Huohua"]}

    def test_reload_mapping_hot_reload(self, gsvi, tmp_path):
        """覆盖层热加载：写 emotion_map.json → reload_mapping → 生效表更新。"""
        engine = _engine(gsvi, tmp_path)
        engine.overlay_path = tmp_path / "voice" / "emotion_map.json"
        engine.overlay_path.parent.mkdir(parents=True, exist_ok=True)
        engine.overlay_path.write_text(
            '{"version": 1, "updated_at": 0, "map": {"happy": "平静的"}}',
            encoding="utf-8")
        mapping = engine.reload_mapping()
        assert mapping["happy"] == "平静的"        # 覆盖层最高优先
        assert mapping["sad"] == "慢速病娇"        # 其余保持默认表

    def test_preview_is_sync_and_shares_cache(self, gsvi, tmp_path):
        """控制台试听：同步方法（threadsafe 回主循环）、共用缓存与单飞锁。"""
        engine = _engine(gsvi, tmp_path)
        _mark_ready(engine)
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        try:
            # start_background 的 loop 捕获等价物：主循环引用注入（线程安全 + 等待生效）
            loop_ready = threading.Event()

            def _set_loop():
                engine._loop = loop
                loop_ready.set()

            loop.call_soon_threadsafe(_set_loop)
            assert loop_ready.wait(5), "主循环引用注入超时"
            audio = engine.preview("试听一句", "傻白甜")  # 控制台线程同步调用
            assert audio == MIN_WAV
            assert len(gsvi.tts_requests) == 1
            again = engine.preview("试听一句", "傻白甜")   # 命中缓存零出网
            assert again == MIN_WAV and len(gsvi.tts_requests) == 1
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            loop.close()

    def test_setup_wires_engine_and_facade_forwarding(self, gsvi, tmp_path):
        """setup(ctx)：挂 voice_engine / tts 适配器；控制台契约转发到 §2 门面。"""
        from virtuallover.config import TtsConfig
        from virtuallover.voice import engine as engine_mod

        class _Storage:
            data_dir = "vl_data"

        class _Cfg:
            tts = TtsConfig(base_url=gsvi.base_url, character="Huohua",
                            auto_start=False, retries=2, timeout=1.0,
                            cache_max_entries=16, cache_max_bytes=1 << 20)
            storage = _Storage()
            session_id = "测试:FriendMessage:10000"

        class _Ctx:
            cfg = _Cfg()
            repo_root = tmp_path

        async def _noop_synth(text, mood=None):
            return None

        async def _noop_send():
            return True

        ctx = _Ctx()
        ctx.voice_manager = VoiceManager(synth=_noop_synth, send_voice=_noop_send)
        cleanup = engine_mod.setup(ctx)
        try:
            assert callable(cleanup)
            assert getattr(ctx, "voice_engine", None) is not None
            # 控制台契约转发：门面获得 status/preview（仅补缺，不覆盖已有）
            for name in ("status", "preview", "start", "stop", "reload_mapping"):
                assert callable(getattr(ctx.voice_manager, name, None)), name
            # proactive ⑧ 兼容面：tts_client.synthesize(text) 走引擎全栈
            assert hasattr(ctx.tts_client, "synthesize")
        finally:
            cleanup()
