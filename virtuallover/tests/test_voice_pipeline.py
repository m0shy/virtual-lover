# -*- coding: utf-8 -*-
"""语音管线测试（VOICE_SPEC §2，test_voice_pipeline.py）。

覆盖四个点名断言 + 管线配套行为，全部合成/发送/引擎为假件，零真实网络：
- 慢合成注入：文字先达（主流程绝不 await 合成，文字路径零改动）；
- 队列顺序：每用户 FIFO（先入先发）、段序保持、跨用户并行互不阻塞；
- dual_output 两口径：主回复「文字必发+语音附加」，proactive「成功只发语音、
  失败回退文字」（含 always_send_text=true 双发口径）；
- trigger_probability 按回复整体抽样一次（种子 rng，DEBUG 记录掷点）；
- 引擎 waiting/degraded 状态门降级纯文字（后台继续等，恢复自动回语音）；
- 裸形态单飞锁：并发合成请求时间窗零重叠（§1 串行化）；
- mood 快照同源：persona 装配时刻一次快照贯穿文字注入与语音映射
  （soul.mood.effective_mood 结构化派生，绝不解析注入字符串）。
"""

from __future__ import annotations

import asyncio
import random
import sys
import time
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from virtuallover.__main__ import AppContext, _handle_user_message
from virtuallover.brain.sender import RateLimiter
from virtuallover.config import VlConfig
from virtuallover.events import EventBus
from virtuallover.soul.mood import MoodEngine
from virtuallover.voice import manager as manager_mod
from virtuallover.voice.manager import VoiceManager

# 占位用户（敏感值规约：号码一律明显占位段）
USER_A = "10000"
USER_B = "10001"
FAKE_SESSION = "测试人格:FriendMessage:10000"


# ---------------------------------------------------------------------------
# 假件
# ---------------------------------------------------------------------------


class FakeBridge:
    """假桥：文字/语音发送全部记录进同一 order 序列（先达断言的数据面）。"""

    def __init__(self) -> None:
        self.order: "list[tuple[str, object]]" = []
        self.voices: "list[str]" = []
        self.fail_record = False

    async def send_private_text(self, user_id, text):
        self.order.append(("text", str(text)))
        return types.SimpleNamespace(ok=True, status="ok")

    async def send_record_b64(self, user_id, b64):
        if self.fail_record:
            return types.SimpleNamespace(ok=False, status="send-failed")
        self.voices.append(b64)
        self.order.append(("voice", user_id))
        return types.SimpleNamespace(ok=True, status="ok")


class FakeChat:
    """假 chat 客户端：记录装配后的 messages（mood 注入断言用）。"""

    def __init__(self, reply: str = "好呀，那我们一起玩～"):
        self.calls: "list[list[dict]]" = []
        self.reply = reply

    async def chat_async(self, messages, **kwargs):  # extra_body 等运行期参数透传
        # （O6 主管线传 prompt_cache_key；假件不校验参数面）
        self.calls.append([dict(m) for m in messages])
        return types.SimpleNamespace(content=self.reply)


class SynthRecorder:
    """可编程假合成引擎：逐段返回脚本化结果，支持事件阻塞（慢合成注入）。"""

    def __init__(self, default: "bytes | None" = b"RIFF....WAVE"):
        self.calls: "list[tuple[str, object]]" = []  # (text, mood_snapshot)
        self.script: "list[bytes | None]" = []
        self.default = default
        self.gate_event: "asyncio.Event | None" = None  # 置位则每段等待（慢合成）
        self.delay = 0.0

    async def __call__(self, text: str, mood_snapshot=None):
        self.calls.append((str(text), mood_snapshot))
        if self.gate_event is not None:
            await self.gate_event.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.script:
            return self.script.pop(0)
        return self.default


def _zero_gap_params() -> types.SimpleNamespace:
    """零间隔分段参数（sample_gap 解析 "0-0" → 恒 0，测试免真等待）。"""
    return types.SimpleNamespace(
        enable=True, words_count_threshold=0, split_mode="", regex="",
        split_words=[], interval_method="log", interval="0-0",
        log_base=2.6, min_gap=0.0, max_gap=0.0,
    )


def _make_ctx(tmp_path: Path, *, bridge: FakeBridge, chat: FakeChat | None = None,
              mood_engine: MoodEngine | None = None) -> AppContext:
    """最小可跑主管线的装配上下文（全部假件/占位值，数据收敛在 tmp）。"""
    cfg = VlConfig(session_id=FAKE_SESSION, admins=[USER_A], whitelist=[USER_A])
    cfg.persona.system_prompt = "你是「她」（测试占位人格）。"
    cfg.persona.name = "测试人格"
    cfg.memes.enable = False  # 免图片文件系统依赖
    cfg.reply.segmented.min_gap = 0.0
    cfg.reply.segmented.max_gap = 0.0
    ctx = AppContext(cfg=cfg, bus=EventBus(), repo_root=tmp_path)
    ctx.rate_limiter = RateLimiter(max_count=10, window_seconds=30)
    ctx.bridge = bridge
    ctx.chat_client = chat or FakeChat()
    ctx.mood_engine = mood_engine
    return ctx


def _make_manager(bridge: FakeBridge, synth: SynthRecorder, **kwargs) -> VoiceManager:
    """构造被测门面：send_voice 记录进 bridge.order（与文字同一先达序列）。"""

    async def _send_voice(user_id: str, audio: bytes) -> bool:
        result = await bridge.send_record_b64(user_id, "b64:" + str(len(audio)))
        return bool(getattr(result, "ok", False))

    kwargs.setdefault("gap_params", _zero_gap_params())
    kwargs.setdefault("trigger_probability", 1.0)
    kwargs.setdefault("rng", random.Random(7))
    return VoiceManager(synth=synth, send_voice=_send_voice, **kwargs)


def _mood_engine(tmp_path: Path) -> MoodEngine:
    return MoodEngine(tmp_path / "mood_data", config={"keyword_groups": {}, "daily_bonus_enabled": True, "inject_enabled": True})


def _drain(manager: VoiceManager, user_id: str) -> "asyncio.Task":
    """等待该用户队列清空（worker 处理完毕的确定性同步点）。"""

    async def _join():
        queue = manager._queues.get(user_id)
        if queue is not None:
            await queue.join()

    return asyncio.ensure_future(_join())


# ---------------------------------------------------------------------------
# 慢合成注入：文字先达（绝不 await 合成）
# ---------------------------------------------------------------------------


class TestSlowSynthTextFirst:
    def test_text_arrives_while_synth_blocked(self, tmp_path):
        """合成挂起时文字已全部到达；释放后语音才逐段补上（一段一语音）。"""
        bridge = FakeBridge()
        chat = FakeChat(reply="第一句！第二句？")
        synth = SynthRecorder()
        synth.gate_event = asyncio.Event()  # 合成永远挂起直到测试放行
        manager = _make_manager(bridge, synth)
        ctx = _make_ctx(tmp_path, bridge=bridge, chat=chat)
        ctx.voice_manager = manager

        cfg = ctx.cfg
        cfg.reply.segmented.split_mode = "words"
        cfg.reply.segmented.split_words = ["！"]  # "第一句！第二句？" → 2 段
        cfg.reply.segmented.words_count_threshold = 1  # 强制分段（默认 100 字内整条）

        async def driver():
            await _handle_user_message(ctx, {"type": "message", "user_id": USER_A,
                                             "text": "在吗"})
            # 等 worker 跑到被事件挂起的合成点（轮询，不依赖调度时序）
            deadline = time.monotonic() + 2.0
            while not synth.calls and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            # ① 文字轨已全部先行到达且绝无语音（合成挂起中）——「绝不 await 合成」
            assert [k for k, _ in bridge.order] == ["text", "text"]
            assert len(synth.calls) == 1, "worker 应已进入（被挂起的）第一段合成"
            synth.gate_event.set()          # 放行慢合成
            await _drain(manager, USER_A)   # 等 worker 清空队列

        asyncio.run(driver())
        kinds = [k for k, _ in bridge.order]
        texts = [v for k, v in bridge.order if k == "text"]
        assert texts == ["第一句！", "第二句？"]      # 文字路径零改动（分段+顺序不变）
        assert len(bridge.voices) == 2               # 一段一语音
        assert kinds.index("voice") > kinds.index("text")  # 语音全部后于文字（先文字后语音）
        # mood 快照贯穿：合成收到的快照与注入 system 的同一有效状态（见 TestMoodSnapshot）

    def test_text_path_identical_without_voice_manager(self, tmp_path):
        """voice_manager 缺位（tts.enabled=false 等价场景）：文字路径字节级不变。"""
        bridge = FakeBridge()
        chat = FakeChat(reply="只有文字")
        ctx = _make_ctx(tmp_path, bridge=bridge, chat=chat)

        asyncio.run(_handle_user_message(ctx, {"type": "message", "user_id": USER_A,
                                               "text": "嗨"}))
        assert [v for k, v in bridge.order if k == "text"] == ["只有文字"]
        assert bridge.voices == []


# ---------------------------------------------------------------------------
# 队列顺序（每用户有序 FIFO）
# ---------------------------------------------------------------------------


class TestQueueOrder:
    def test_fifo_across_replies_despite_synth_speed(self, tmp_path):
        """先入队的回复先发（即便它的合成更慢）：每用户单 worker 保序。"""
        bridge = FakeBridge()
        calls: "list[tuple[str, object]]" = []
        delays = {0: 0.15, 1: 0.0}

        async def synth_fn(text, mood=None):
            calls.append((text, mood))
            await asyncio.sleep(delays[len(calls) - 1])
            return b"RIFF-wave"

        manager = _make_manager(bridge, synth_fn)

        async def driver():
            manager.enqueue_reply(USER_A, ["第一条"], mood_snapshot=("calm", 0.3))
            manager.enqueue_reply(USER_A, ["第二条"], mood_snapshot=("calm", 0.3))
            await _drain(manager, USER_A)

        asyncio.run(driver())
        sent = [c[0] for c in calls]
        assert sent == ["第一条", "第二条"]  # FIFO：慢的第一条仍先处理完先发送
        assert bridge.order == [("voice", USER_A), ("voice", USER_A)]

    def test_segment_order_preserved_within_reply(self):
        """单条回复多段：语音逐段按原顺序发送。"""
        bridge = FakeBridge()
        synth = SynthRecorder()
        manager = _make_manager(bridge, synth)

        async def driver():
            manager.enqueue_reply(USER_A, ["一", "二", "三"])
            await _drain(manager, USER_A)

        asyncio.run(driver())
        assert [c[0] for c in synth.calls] == ["一", "二", "三"]
        assert len(bridge.voices) == 3

    def test_cross_users_single_flight_and_each_fifo(self):
        """跨用户：每用户各自 FIFO；合成全局单飞——B 的快合成也绝不插入
        A 的 in-flight 时间窗（§1「任一时刻至多一个 in-flight /tts」）。"""
        bridge = FakeBridge()
        windows: "list[tuple[float, float]]" = []

        async def synth_fn(text, mood=None):
            start = time.monotonic()
            await asyncio.sleep(0.15 if text.startswith(USER_A) else 0.0)
            windows.append((start, time.monotonic()))
            return b"RIFF-wave"

        async def _send_voice(user_id, audio):
            bridge.order.append(("voice", user_id))
            return True

        manager = VoiceManager(synth=synth_fn, send_voice=_send_voice,
                               gap_params=_zero_gap_params(), rng=random.Random(1))

        async def driver():
            manager.enqueue_reply(USER_A, [USER_A + ":一"])
            manager.enqueue_reply(USER_B, [USER_B + ":一"])
            await asyncio.gather(_drain(manager, USER_A), _drain(manager, USER_B))

        asyncio.run(driver())
        voice_users = [v for k, v in bridge.order if k == "voice"]
        assert sorted(voice_users) == sorted([USER_A, USER_B])  # 两用户的语音都送达
        windows.sort()
        assert windows[1][0] >= windows[0][1]  # 合成时间窗零重叠（全局单飞）

    def test_gap_reuses_sample_gap_between_segments(self, monkeypatch):
        """段间间隔复用 brain.sender.sample_gap（每回复 N 段调 N-1 次，参数透传）。"""
        calls: "list[tuple[object, object]]" = []
        gap_params = _zero_gap_params()

        def fake_sample_gap(params, rng=None):
            calls.append((params, rng))
            return 0.0

        monkeypatch.setattr(manager_mod.sender_mod, "sample_gap", fake_sample_gap)
        bridge = FakeBridge()
        synth = SynthRecorder()
        rng = random.Random(3)
        manager = _make_manager(bridge, synth, gap_params=gap_params, rng=rng)

        async def driver():
            manager.enqueue_reply(USER_A, ["一", "二", "三"])
            await _drain(manager, USER_A)

        asyncio.run(driver())
        assert len(calls) == 2                    # 3 段 → 2 次间隔采样
        assert all(p is gap_params and r is rng for p, r in calls)  # 同一参数与随机源


# ---------------------------------------------------------------------------
# dual_output 两口径
# ---------------------------------------------------------------------------


class TestDualOutput:
    def test_reply_track_appends_voice_only(self):
        """主回复轨：manager 只补语音（文字由管线先行发出，manager 绝不发文字）。"""
        bridge = FakeBridge()
        synth = SynthRecorder()
        manager = _make_manager(bridge, synth)

        async def driver():
            manager.enqueue_reply(USER_A, ["甲", "乙"])
            await _drain(manager, USER_A)

        asyncio.run(driver())
        assert len(bridge.voices) == 2
        assert not any(k == "text" for k, _ in bridge.order)  # manager 不碰文字轨

    def test_reply_track_synth_failure_degrades_silently(self):
        """主回复轨合成失败：该段降级纯文字（文字已先行），无异常无语音。"""
        bridge = FakeBridge()
        synth = SynthRecorder(default=None)
        manager = _make_manager(bridge, synth)

        async def driver():
            manager.enqueue_reply(USER_A, ["甲", "乙"])
            await _drain(manager, USER_A)

        asyncio.run(driver())
        assert bridge.voices == []
        assert manager.stats["segments_failed"] == 2

    def test_proactive_success_sends_voice_only(self):
        """proactive 轨（always_send_text=false）：合成成功只发语音，不发文字。"""
        bridge = FakeBridge()
        synth = SynthRecorder()
        manager = _make_manager(bridge, synth)
        text_sends: "list[str]" = []

        async def send_text(t: str) -> bool:
            text_sends.append(t)
            return True

        ok = asyncio.run(manager.deliver_proactive(USER_A, "想你了",
                                                   mood_snapshot=("happy", 0.6),
                                                   send_text=send_text))
        assert ok is True
        assert len(bridge.voices) == 1
        assert text_sends == []  # 成功只语音

    def test_proactive_failure_falls_back_to_text(self):
        """proactive 轨合成失败：回退纯文字（对齐 proactive.py ⑧ 现有实现）。"""
        bridge = FakeBridge()
        synth = SynthRecorder(default=None)
        manager = _make_manager(bridge, synth)
        text_sends: "list[str]" = []

        async def send_text(t: str) -> bool:
            text_sends.append(t)
            return True

        ok = asyncio.run(manager.deliver_proactive(USER_A, "在忙吗", send_text=send_text))
        assert ok is True
        assert text_sends == ["在忙吗"]
        assert bridge.voices == []

    def test_proactive_voice_send_failure_falls_back_to_text(self):
        """proactive 轨语音发送失败：同样回退纯文字。"""
        bridge = FakeBridge()
        bridge.fail_record = True
        synth = SynthRecorder()
        manager = _make_manager(bridge, synth)
        text_sends: "list[str]" = []

        async def send_text(t: str) -> bool:
            text_sends.append(t)
            return True

        ok = asyncio.run(manager.deliver_proactive(USER_A, "抱抱", send_text=send_text))
        assert ok is True
        assert text_sends == ["抱抱"]

    def test_proactive_always_send_text_sends_both(self):
        """proactive 轨 always_send_text=true：语音+文字双发（dual_output=true 口径）。"""
        bridge = FakeBridge()
        synth = SynthRecorder()
        manager = _make_manager(bridge, synth)
        text_sends: "list[str]" = []

        async def send_text(t: str) -> bool:
            text_sends.append(t)
            return True

        ok = asyncio.run(manager.deliver_proactive(
            USER_A, "晚安", always_send_text=True, send_text=send_text))
        assert ok is True
        assert len(bridge.voices) == 1
        assert text_sends == ["晚安"]

    def test_voice_send_passes_through_shared_limiter(self):
        """语音发送前过共享限流：限流器 stall 期间不发送（与文字共用实例）。"""
        limiter_calls: "list[float]" = []

        class CountingLimiter:
            async def acquire(self):
                limiter_calls.append(time.monotonic())

        bridge = FakeBridge()
        synth = SynthRecorder()
        manager = _make_manager(bridge, synth, limiter=CountingLimiter())

        async def driver():
            manager.enqueue_reply(USER_A, ["一", "二"])
            await _drain(manager, USER_A)

        asyncio.run(driver())
        assert len(limiter_calls) == 2  # 每段语音发送前各取一个名额


# ---------------------------------------------------------------------------
# trigger_probability 按回复整体抽样一次
# ---------------------------------------------------------------------------


class TestTriggerSampling:
    def test_probability_zero_never_fires(self):
        bridge = FakeBridge()
        synth = SynthRecorder()
        manager = _make_manager(bridge, synth, trigger_probability=0.0, rng=random.Random(1))

        async def driver():
            assert manager.enqueue_reply(USER_A, ["一", "二"]) is False
            assert manager.enqueue_reply(USER_A, ["三"]) is False
            await asyncio.sleep(0.01)  # 给 worker（若有）逃逸机会

        asyncio.run(driver())
        assert synth.calls == [] and bridge.voices == []
        assert USER_A not in manager._queues  # 从未入队（无 worker 启动）

    def test_probability_one_always_fires(self):
        bridge = FakeBridge()
        synth = SynthRecorder()
        manager = _make_manager(bridge, synth, trigger_probability=1.0, rng=random.Random(1))

        async def driver():
            assert manager.enqueue_reply(USER_A, ["一"]) is True
            await _drain(manager, USER_A)

        asyncio.run(driver())
        assert len(bridge.voices) == 1

    def test_sampled_once_per_whole_reply(self):
        """多段回复只掷一次点：rng.random() 恰好调用一次（全或无）。"""
        bridge = FakeBridge()
        synth = SynthRecorder()

        class CountingRng(random.Random):
            def __init__(self):
                super().__init__(42)
                self.rolls = 0

            def random(self):
                self.rolls += 1
                return super().random()

        rng = CountingRng()
        # random.Random(42) 首掷 ≈0.639：p=0.7 确保命中（断言的是「只掷一次」）
        manager = _make_manager(bridge, synth, trigger_probability=0.7, rng=rng)

        async def driver():
            assert manager.enqueue_reply(USER_A, ["一", "二", "三"]) is True
            await _drain(manager, USER_A)

        asyncio.run(driver())
        assert rng.rolls == 1          # 整条回复只抽样一次
        assert len(synth.calls) == 3   # 命中后三段全带语音（all）

    def test_miss_skips_all_segments(self):
        """掷点未命中：三段全部无语音（且不启动 worker）。"""
        bridge = FakeBridge()
        synth = SynthRecorder()

        class HighRng(random.Random):
            def random(self):
                return 0.99

        manager = _make_manager(bridge, synth, trigger_probability=0.5, rng=HighRng())

        async def driver():
            assert manager.enqueue_reply(USER_A, ["一", "二", "三"]) is False
            await asyncio.sleep(0.01)

        asyncio.run(driver())
        assert synth.calls == [] and bridge.voices == []
        assert USER_A not in manager._queues  # 未命中根本不入队

    def test_roll_logged_at_debug(self, caplog):
        """DEBUG 记录掷点（roll/p/结论），便于线下核对触发率。"""
        import logging

        bridge = FakeBridge()
        synth = SynthRecorder()
        manager = _make_manager(bridge, synth, trigger_probability=1.0, rng=random.Random(1))

        async def driver():
            manager.enqueue_reply(USER_A, ["一"])
            await _drain(manager, USER_A)

        with caplog.at_level(logging.DEBUG, logger="virtuallover.voice.manager"):
            asyncio.run(driver())
        assert "trigger 抽样" in caplog.text
        assert "p=1.00" in caplog.text


# ---------------------------------------------------------------------------
# 引擎状态门：waiting/degraded 降级纯文字，后台继续等
# ---------------------------------------------------------------------------


class TestEngineGateDegradation:
    def test_gate_blocked_proactive_falls_back_to_text_without_synth(self):
        """degraded 冷却期：跳过合成直接 None → proactive 回退纯文字。"""
        bridge = FakeBridge()
        synth = SynthRecorder()
        manager = _make_manager(bridge, synth, engine_gate=lambda: False)
        text_sends: "list[str]" = []

        async def send_text(t: str) -> bool:
            text_sends.append(t)
            return True

        ok = asyncio.run(manager.deliver_proactive(USER_A, "在吗", send_text=send_text))
        assert ok is True and text_sends == ["在吗"]
        assert synth.calls == []                      # 一次合成都没尝试（跳过重试）
        assert manager.stats["degraded_skips"] == 1

    def test_gate_blocked_reply_job_waits_then_recovers(self):
        """waiting 期间该条降级纯文字、后台继续等：门恢复后下一条自动回语音。"""
        bridge = FakeBridge()
        synth = SynthRecorder()
        gate = {"ready": False}
        manager = _make_manager(bridge, synth, engine_gate=lambda: gate["ready"])

        async def driver():
            manager.enqueue_reply(USER_A, ["冷启期间"])   # 门关：该条降级
            await _drain(manager, USER_A)
            assert synth.calls == [] and bridge.voices == []
            gate["ready"] = True                          # 引擎就绪
            manager.enqueue_reply(USER_A, ["恢复后"])     # 后台 worker 仍在
            await _drain(manager, USER_A)

        asyncio.run(driver())
        assert [c[0] for c in synth.calls] == ["恢复后"]  # 恢复自动回语音
        assert len(bridge.voices) == 1
        assert manager.stats["degraded_skips"] == 1

    def test_gate_error_fails_open(self):
        """状态门读不出（抛异常）：放行尝试合成（fail-open，合成自身有超时兜底）。"""

        def bad_gate():
            raise RuntimeError("状态不可得")

        bridge = FakeBridge()
        synth = SynthRecorder()
        manager = _make_manager(bridge, synth, engine_gate=bad_gate)

        async def driver():
            manager.enqueue_reply(USER_A, ["一"])
            await _drain(manager, USER_A)

        asyncio.run(driver())
        assert len(synth.calls) == 1 and len(bridge.voices) == 1


# ---------------------------------------------------------------------------
# 裸形态串行锁（§1 契约的门面兜底实现）
# ---------------------------------------------------------------------------


class TestBareSynthLock:
    def test_concurrent_synthesize_windows_never_overlap(self):
        """并发 synthesize：请求时间窗零重叠（单飞锁）。"""
        windows: "list[tuple[float, float]]" = []

        async def synth_fn(text, mood=None):
            start = time.monotonic()
            await asyncio.sleep(0.05)
            windows.append((start, time.monotonic()))
            return b"RIFF-wave"

        async def _send_voice(user_id, audio):
            return True

        manager = VoiceManager(synth=synth_fn, send_voice=_send_voice,
                               rng=random.Random(1))

        async def driver():
            results = await asyncio.gather(*(manager.synthesize(f"段{i}") for i in range(3)))
            assert all(r == b"RIFF-wave" for r in results)

        asyncio.run(driver())
        windows.sort()
        for (_, prev_end), (next_start, _) in zip(windows, windows[1:]):
            assert next_start >= prev_end  # 时间窗零重叠

    def test_synth_exception_degrades_to_none(self):
        """合成抛异常：按失败降级返回 None（绝不把异常漏进文字管线）。"""

        async def bad_synth(text, mood=None):
            raise RuntimeError("引擎挂了")

        async def _send_voice(user_id, audio):
            return True

        manager = VoiceManager(synth=bad_synth, send_voice=_send_voice, rng=random.Random(1))
        assert asyncio.run(manager.synthesize("一")) is None

    def test_empty_text_raises_value_error(self):
        """空文本是编程错误：抛 ValueError（发送侧应先过滤）。"""
        async def _send_voice(user_id, audio):
            return True

        manager = VoiceManager(synth=SynthRecorder(), send_voice=_send_voice)
        with pytest.raises(ValueError):
            asyncio.run(manager.synthesize("   "))


# ---------------------------------------------------------------------------
# mood 快照同源（persona 装配时刻一次快照贯穿文字注入与语音映射）
# ---------------------------------------------------------------------------


class TestMoodSnapshotSameSource:
    def test_effective_mood_decay_matches_injection_math(self):
        """effective_mood：τ=4h 衰减 + <0.15 回落 calm（与注入片段同一公式）。"""
        from virtuallover.soul.mood import MoodState, effective_mood

        now = 1_000_000.0
        fresh = MoodState(mood="happy", mood_intensity=0.6, updated_at=now)
        assert effective_mood(fresh, now=now) == ("happy", 0.6)      # 刚更新不衰减
        old = MoodState(mood="excited", mood_intensity=0.5, updated_at=now - 8 * 3600)
        mood, intensity = effective_mood(old, now=now)               # 0.5*exp(-2)≈0.068<0.15
        assert mood == "calm" and intensity < 0.15

    def test_pipeline_snapshot_feeds_both_text_and_voice(self, tmp_path):
        """管线端到端：一次快照——user 侧注入「开心」（O6）与语音收到的 happy 同源。"""
        bridge = FakeBridge()
        chat = FakeChat(reply="你夸我我就开心啦")
        synth = SynthRecorder()
        manager = _make_manager(bridge, synth)
        ctx = _make_ctx(tmp_path, bridge=bridge, chat=chat, mood_engine=_mood_engine(tmp_path))
        ctx.voice_manager = manager

        asyncio.run(_handle_user_message(ctx, {"type": "message", "user_id": USER_A,
                                               "text": "宝贝你真好"}))
        # ① 文字注入面：O6 起 mood 片段在末条 user 消息侧（资料块内），
        #    system 前缀保持稳定（prompt_cache_key 命中开关）
        system_text = chat.calls[0][0]["content"]
        assert "开心" not in system_text          # system 不再拼 mood
        user_text = chat.calls[0][-1]["content"]  # 末条 user 消息
        assert "开心" in user_text
        # ② 语音映射面：合成收到的快照是同一有效状态（happy, intensity>0）
        assert synth.calls, "语音 worker 应已消费该回复"
        mood_key, intensity = synth.calls[0][1]
        assert mood_key == "happy" and intensity > 0
        # ③ 同源性：effective_mood(同一状态) 与注入渲染共用一套衰减数学（§1 口径）
        from virtuallover.soul.mood import effective_mood as eff
        state = ctx.mood_engine.snapshot_state(USER_A)
        assert eff(state)[0] == "happy"


# ---------------------------------------------------------------------------
# 收尾与装配
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_aclose_cancels_workers(self):
        """aclose：挂起中的 worker 被取消并等待退出（无悬空任务告警）。"""
        bridge = FakeBridge()
        release = asyncio.Event()

        async def synth_fn(text, mood=None):
            await release.wait()
            return b"RIFF"

        async def _send_voice(user_id, audio):
            return True

        manager = VoiceManager(synth=synth_fn, send_voice=_send_voice, rng=random.Random(1))

        async def driver():
            manager.enqueue_reply(USER_A, ["一"])
            await asyncio.sleep(0.01)   # worker 进入挂起合成
            tasks = list(manager._workers.values())
            assert len(tasks) == 1
            await manager.aclose()
            assert manager._workers == {}
            release.set()
            await asyncio.sleep(0)
            assert all(t.done() or t.cancelled() for t in tasks)

        asyncio.run(driver())

    def test_cleanup_calls_engine_cleanup_once(self):
        """cleanup：转调引擎自有清理（终止自有 GSVI 的核验链在引擎/lifecycle）。"""
        calls: "list[int]" = []
        manager = VoiceManager(synth=SynthRecorder(),
                               send_voice=lambda u, a: _noop_true(),
                               engine_cleanup=lambda: calls.append(1),
                               rng=random.Random(1))
        manager.cleanup()
        manager.cleanup()  # 幂等：二次调用不重复
        assert calls == [1]

    def test_setup_wires_manager_and_trigger_probability(self, tmp_path):
        """setup(ctx)：挂 ctx.voice_manager、trigger_probability 从 [tts] 读入。"""
        cfg = VlConfig(session_id=FAKE_SESSION, admins=[USER_A], whitelist=[USER_A])
        cfg.tts.trigger_probability = 0.25
        ctx = AppContext(cfg=cfg, bus=EventBus(), repo_root=tmp_path)
        ctx.rate_limiter = RateLimiter(max_count=10, window_seconds=30)

        from virtuallover.voice import manager as vm_mod

        cleanup = vm_mod.setup(ctx)
        assert callable(cleanup)
        manager = ctx.voice_manager
        assert isinstance(manager, VoiceManager)
        assert manager.trigger_probability == pytest.approx(0.25)
        assert manager._limiter is ctx.rate_limiter          # 共享限流实例
        assert manager._engine_provider() is None            # 引擎未装配（裸形态）
        manager.cleanup()

    def test_setup_skips_when_tts_disabled(self, tmp_path):
        """tts.enabled=false：不装配（管线纯文字），无 voice_manager 槽位。"""
        cfg = VlConfig(session_id=FAKE_SESSION, admins=[USER_A], whitelist=[USER_A])
        cfg.tts.enabled = False
        ctx = AppContext(cfg=cfg, bus=EventBus(), repo_root=tmp_path)

        from virtuallover.voice import manager as vm_mod

        assert vm_mod.setup(ctx) is None
        assert ctx.voice_manager is None


async def _noop_true() -> bool:
    return True
