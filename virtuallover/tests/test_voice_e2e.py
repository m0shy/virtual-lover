# -*- coding: utf-8 -*-
"""语音链路端到端冒烟（VOICE_SPEC §2/§4，test_voice_e2e.py —— 集成组分配文件）。

与 test_voice_pipeline（假 synth 的管线行为）、test_voice_manager（engine 全栈
单元）互补：本文件把**真实装配路径**接起来——manager.setup + engine.setup 按
__main__ 装配顺序挂到同一 AppContext，VoiceManager 的 send_voice 走
_make_send_voice(ctx) 的 bridge.send_record_b64 真回调，合成走 fake GSVI
（tests/fake_gsvi.py 契约保真），消息从 _handle_user_message 主管线入口进入：

    模拟消息 → mood 快照 → LLM 回复（假 chat）→ 文字分段发送
            → voice_manager.enqueue_reply（每用户有序队列）
            → voice_engine.synthesize（映射→缓存→单飞 /tts）
            → fake GSVI 出 wav → bridge.send_record_b64（base64 record 段）

点名断言（任务书「模拟消息→文字+语音双发，断言 record 段 base64」）：
- 文字先达且内容分段不变（文字路径零改动）；
- 每段一条 record 语音，参数是**真回调产出的 base64**，解码 == fake GSVI 的
  wav 体（RIFF 帧头）；
- GSVI 收到的生效参数：角色经 .lower() 怪癖、情绪来自 7 态映射（夸奖→傻白甜）；
- 串行化：多段时间窗零重叠；注册表只拉一次（character_list 禁轮询）。

另含本次集成修复的三个接线断点的回归断言（详见 TestWiringRegression）。

安全规约：号码一律占位段；auto_start=False——测试绝不拉起真实 GSVI 子进程；
全部数据收敛在 tmp_path。
"""

from __future__ import annotations

import asyncio
import base64
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fake_gsvi import FakeGsvi, MIN_WAV
from virtuallover.__main__ import AppContext, _handle_user_message
from virtuallover.brain.sender import RateLimiter
from virtuallover.config import VlConfig
from virtuallover.events import EventBus
from virtuallover.soul.mood import MoodEngine
from virtuallover.voice import engine as engine_mod
from virtuallover.voice import manager as manager_mod
from virtuallover.voice.manager import VoiceManager
from virtuallover.voice.lifecycle import (
    LifecycleParams, GsviLifecycle, STATE_READY,
)

# 占位用户（敏感值规约：号码一律明显占位段）
USER_A = "20000"
FAKE_SESSION = "测试人格:FriendMessage:20000"


# ---------------------------------------------------------------------------
# 假件：桥（record base64 记录面）与 chat
# ---------------------------------------------------------------------------


class FakeBridge:
    """假桥：文字与 record 语音都记录进同一 order 序列（先达断言数据面）。"""

    def __init__(self) -> None:
        self.order: "list[tuple[str, object]]" = []
        self.voices: "list[str]" = []  # send_record_b64 收到的 base64 原文

    async def send_private_text(self, user_id, text):
        self.order.append(("text", str(text)))
        return types_ns(ok=True)

    async def send_record_b64(self, user_id, b64):
        self.voices.append(str(b64))
        self.order.append(("voice", user_id))
        return types_ns(ok=True)


class FakeChat:
    """假 chat 客户端：返回固定回复（内容可通过 reply 改）。"""

    def __init__(self, reply: str = "你夸得我心花怒放啦！") -> None:
        self.reply = reply
        self.calls: "list[list[dict]]" = []

    async def chat_async(self, messages, **kwargs):  # extra_body 等运行期参数透传
        # （O6 主管线传 prompt_cache_key；假件不校验参数面）
        self.calls.append([dict(m) for m in messages])
        return types_ns(content=self.reply)


def types_ns(**kwargs):
    """轻量命名空间（SimpleNamespace 的本地别名，免顶部多余 import）。"""
    import types
    return types.SimpleNamespace(**kwargs)


# ---------------------------------------------------------------------------
# 真实装配路径（对齐 __main__ 的装配顺序与回调接线）
# ---------------------------------------------------------------------------


def _assemble(gsvi: FakeGsvi, tmp_path: Path) -> AppContext:
    """按 __main__ 顺序做最小真实装配：voice.manager → voice.engine。"""
    cfg = VlConfig(session_id=FAKE_SESSION, admins=[USER_A], whitelist=[USER_A])
    cfg.tts.base_url = gsvi.base_url       # 指向 fake GSVI（随机端口）
    cfg.tts.character = "Huohua"
    cfg.tts.auto_start = False             # 测试绝不拉起真实 GSVI 子进程
    cfg.tts.trigger_probability = 1.0      # 每条回复都带语音（冒烟要见双发）
    cfg.persona.system_prompt = "你是「她」（测试占位人格）。"
    cfg.memes.enable = False
    ctx = AppContext(cfg=cfg, bus=EventBus(), repo_root=tmp_path)
    ctx.rate_limiter = RateLimiter(max_count=10, window_seconds=30)
    ctx.bridge = FakeBridge()
    ctx.chat_client = FakeChat()
    ctx.mood_engine = MoodEngine(
        tmp_path / "mood_data",
        config={"keyword_groups": {}, "daily_bonus_enabled": True,
                "inject_enabled": True},
    )
    # 与 __main__._SETUP_TARGETS 相同的装配顺序（engine 在 manager 之后：
    # 控制台契约转发到门面 + engine_provider 接缝就绪）
    cleanups: "list" = []
    cleanup = manager_mod.setup(ctx)
    assert callable(cleanup) and isinstance(ctx.voice_manager, VoiceManager)
    cleanups.append(cleanup)
    cleanup = engine_mod.setup(ctx)
    assert callable(cleanup) and ctx.voice_engine is not None
    cleanups.append(cleanup)
    ctx._e2e_cleanups = cleanups  # 测试自用槽位（AppContext 无此字段，动态挂）
    return ctx


async def _adopt_ready(ctx: AppContext) -> None:
    """fake GSVI 已在跑 → adopt-first 收编为 external ready（绝不 Popen）。"""
    engine = ctx.voice_engine
    state = await engine.lifecycle.adopt_or_start()
    assert state == STATE_READY, f"fake GSVI 在跑，应收编为 ready（实际 {state}）"


def _drain(manager: VoiceManager, user_id: str) -> "asyncio.Task":
    """等该用户语音队列清空（worker 处理完毕的确定性同步点）。"""

    async def _join():
        queue = manager._queues.get(user_id)
        if queue is not None:
            await queue.join()

    return asyncio.ensure_future(_join())


def _teardown(ctx: AppContext) -> None:
    for cleanup in reversed(getattr(ctx, "_e2e_cleanups", [])):
        try:
            cleanup()
        except Exception:  # noqa: BLE001 —— 收尾从宽
            pass


# ---------------------------------------------------------------------------
# 冒烟主体：模拟消息 → 文字+语音双发（record 段 base64）
# ---------------------------------------------------------------------------


class TestEndToEndDualOutput:
    def test_message_yields_text_then_record_base64(self, fake_gsvi, tmp_path):
        """一条消息 → 文字先达 + record 语音附加；record 参数可解码回 wav。"""
        ctx = _assemble(fake_gsvi, tmp_path)
        try:
            asyncio.run(self._drive(ctx))
        finally:
            _teardown(ctx)

        bridge: FakeBridge = ctx.bridge
        kinds = [k for k, _ in bridge.order]
        texts = [v for k, v in bridge.order if k == "text"]
        # ① 文字轨先达且内容零改动（分段/顺序与 LLM 回复一致）
        assert texts == ["你夸得我心花怒放啦！"]
        # ② 语音双发：恰好一条 record，且**晚于**文字（绝不 await 合成）
        assert kinds == ["text", "voice"]
        assert len(bridge.voices) == 1
        audio = base64.b64decode(bridge.voices[0], validate=True)
        assert audio == fake_gsvi.wav_body          # fake GSVI 的完整 wav 体
        assert audio.startswith(b"RIFF")            # record 段是真 wav

    async def _drive(self, ctx: AppContext) -> None:
        await _adopt_ready(ctx)
        await _handle_user_message(ctx, {"type": "message", "user_id": USER_A,
                                         "text": "宝贝你真好"})
        await _drain(ctx.voice_manager, USER_A)

    def test_gsvi_effective_params_character_and_emotion(self, fake_gsvi, tmp_path):
        """GSVI 侧生效参数：角色 .lower() 怪癖；夸奖→happy→傻白甜（7 态映射）。"""
        ctx = _assemble(fake_gsvi, tmp_path)
        try:
            asyncio.run(self._drive(ctx))
        finally:
            _teardown(ctx)
        tts = fake_gsvi.tts_requests
        assert len(tts) == 1
        effective = tts[0]["effective"]
        assert effective["character"] == "huohua"    # string 参数 .lower() 怪癖
        assert effective["emotion"] == "傻白甜"       # 中文情绪名不受 lower 影响
        assert effective["text_language"] == "zh"
        assert effective["format"] == "wav"
        # 请求参数成对携带（SPEC §9 别名契约）：cha_name/character 等别名同值
        assert tts[0]["params"]["character"] == "Huohua"
        assert tts[0]["params"]["character_emotion"] == "傻白甜"

    def test_two_segments_serialized_and_registry_fetched_once(self, fake_gsvi, tmp_path):
        """两段回复：两段语音串行（时间窗零重叠）+ character_list 只拉一次。"""
        ctx = _assemble(fake_gsvi, tmp_path)
        ctx.chat_client.reply = "第一句呀！第二句呀！"
        ctx.cfg.reply.segmented.split_mode = "words"
        ctx.cfg.reply.segmented.split_words = ["！"]
        ctx.cfg.reply.segmented.words_count_threshold = 1  # 强制按词分段
        try:
            asyncio.run(self._drive(ctx))
        finally:
            _teardown(ctx)

        bridge: FakeBridge = ctx.bridge
        texts = [v for k, v in bridge.order if k == "text"]
        assert texts == ["第一句呀！", "第二句呀！"]
        assert len(bridge.voices) == 2
        for b64 in bridge.voices:
            assert base64.b64decode(b64, validate=True) == fake_gsvi.wav_body
        # 串行化：/tts 请求时间窗零重叠（fake 记录起止 monotonic）
        windows = sorted((r["start_ts"], r["end_ts"])
                         for r in fake_gsvi.tts_requests)
        assert len(windows) == 2
        assert windows[1][0] >= windows[0][1]
        # 注册表禁轮询：无论几段合成，/character_list 全程只出网一次
        assert sum(1 for r in fake_gsvi.requests
                   if r["path"] == "/character_list") == 1

    def test_second_identical_reply_hits_cache(self, fake_gsvi, tmp_path):
        """同文本同情绪第二条回复：缓存命中，不出网（/tts 请求数不变）。"""
        ctx = _assemble(fake_gsvi, tmp_path)
        try:
            # 同一事件循环内连发两条相同消息（manager 的单飞锁绑定首个 await
            # 它的循环——生产为常驻主循环，测试也须单循环驱动，语义一致）
            async def driver():
                await _adopt_ready(ctx)
                await _handle_user_message(ctx, {"type": "message", "user_id": USER_A,
                                                 "text": "宝贝你真好"})
                await _drain(ctx.voice_manager, USER_A)
                assert len(fake_gsvi.tts_requests) == 1
                await _handle_user_message(ctx, {"type": "message", "user_id": USER_A,
                                                 "text": "宝贝你真好"})
                await _drain(ctx.voice_manager, USER_A)

            asyncio.run(driver())
        finally:
            _teardown(ctx)
        assert len(fake_gsvi.tts_requests) == 1     # 第二次零新请求（缓存命中）
        # 但用户侧照常收到文字+语音（缓存命中对用户透明）
        assert len(ctx.bridge.voices) == 2

    def test_mood_getter_feeds_mapping_when_snapshot_missing(self, fake_gsvi, tmp_path):
        """无显式快照（manager.synthesize 直调）：engine 的 mood_getter 供数——
        MoodEngine 空状态 → calm → 聊天（R3-2a 映射重排；结构化 getter，禁解析注入串）。"""
        ctx = _assemble(fake_gsvi, tmp_path)
        try:
            asyncio.run(self._drive_getter(ctx, fake_gsvi))
        finally:
            _teardown(ctx)
        effective = fake_gsvi.tts_requests[-1]["effective"]
        assert effective["emotion"] == "聊天"

    async def _drive_getter(self, ctx: AppContext, gsvi: FakeGsvi) -> None:
        await _adopt_ready(ctx)
        audio = await ctx.voice_manager.synthesize("喂喂，在吗", None)
        assert audio == gsvi.wav_body


# ---------------------------------------------------------------------------
# 集成修复回归（本次接线断点：adopt 防重入 / start 同步门面 / uptime 时钟）
# ---------------------------------------------------------------------------


class _Spawner:
    """假 spawner：返回固定 pid，记录 argv/cwd（lifecycle 测试同款手法）。"""

    def __init__(self) -> None:
        self.calls: "list[tuple[list, str]]" = []

    def __call__(self, argv: list, cwd: str) -> int:
        self.calls.append((argv, cwd))
        return 424242


class TestWiringRegression:
    def test_repeat_adopt_never_flips_owned_to_external(self, tmp_path):
        """修复回归：owned/ready 后重复 adopt_or_start —— ownership 保持 owned、
        pid 文件保留（旧行为会收编成 external 并删 pid，自己的进程停不掉）。"""
        spawner = _Spawner()
        probes = iter([False, True, True, True])  # 无主→拉起；此后一直有主

        def probe() -> bool:
            return next(probes, True)

        lc = GsviLifecycle(
            LifecycleParams(auto_start=True, pid_path=tmp_path / "voice" / "voice-engine.pid",
                            log_path=tmp_path / "logs" / "voice-engine.log"),
            repo_root=tmp_path, probe=probe, spawner=spawner,
            cmdline_reader=lambda pid: f"python scripts/voice_engine_entry.py (pid={pid})",
            killer=lambda pid: True,
        )
        assert asyncio.run(lc.adopt_or_start()) == STATE_READY
        assert lc.ownership == "owned" and lc.pid == 424242
        assert (tmp_path / "voice" / "voice-engine.pid").read_text(encoding="ascii") == "424242"

        # 控制台重复点「启动」→ 端口有主：守卫直接返回，绝不误收编
        assert asyncio.run(lc.adopt_or_start()) == STATE_READY
        assert lc.ownership == "owned" and lc.pid == 424242
        assert (tmp_path / "voice" / "voice-engine.pid").exists()
        assert len(spawner.calls) == 1  # 也绝不重复拉起

    def test_engine_start_without_main_loop_reports_state(self, fake_gsvi, tmp_path):
        """修复回归：engine.start() 是同步方法；无主循环时如实返回当前状态，
        绝不把 _loop/bg_task 污染到 asyncio.run 的临时循环上。"""
        ctx = _assemble(fake_gsvi, tmp_path)
        try:
            engine = ctx.voice_engine
            result = engine.start()   # 同步调用（控制台线程语义），无事件循环
            assert result == {"state": "not_started", "ownership": None}
            assert engine._loop is None            # 循环引用未被污染
            assert engine._bg_task is None         # 后台任务未被绑到临时循环
            stopped = engine.stop()                # 同步 stop 同样可用
            assert stopped["state"] in ("not_started", "ready")
        finally:
            _teardown(ctx)

    def test_engine_start_via_main_loop_adopts_and_reports(self, fake_gsvi, tmp_path):
        """修复回归：主循环在跑时 engine.start()（控制台线程同步调用）经
        run_coroutine_threadsafe 回主循环 adopt 收编，阻塞至状态机返回。"""
        ctx = _assemble(fake_gsvi, tmp_path)
        loop = asyncio.new_event_loop()
        import threading

        worker = threading.Thread(target=loop.run_forever, daemon=True)
        worker.start()
        try:
            engine = ctx.voice_engine

            def _inject_loop():
                engine._loop = loop

            loop.call_soon_threadsafe(_inject_loop)
            deadline = time.monotonic() + 5
            while engine._loop is not loop and time.monotonic() < deadline:
                time.sleep(0.01)
            result = engine.start()   # fake GSVI 在跑 → 收编 external ready
            assert result["state"] == STATE_READY
            assert result["ownership"] == "external"
            assert engine.lifecycle.state_name() == STATE_READY
        finally:
            # 先在循环内落地取消 start_background 起的 run_forever 任务，
            # 再停循环——否则 close 时任务悬空，GC 时报 destroyed-but-pending
            engine = ctx.voice_engine
            bg = engine._bg_task
            if bg is not None and not bg.done():
                bg.cancel()
                try:
                    loop.run_until_complete(bg)
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            loop.call_soon_threadsafe(loop.stop)
            worker.join(timeout=5)
            loop.close()
            _teardown(ctx)

    def test_status_uptime_uses_lifecycle_clock_not_wall(self, fake_gsvi, tmp_path):
        """修复回归：status().uptime_s 由 lifecycle 同钟求差（秒级），
        绝不是 wall−monotonic 的"系统开机时长"（时钟基准混用旧行为）。"""
        ctx = _assemble(fake_gsvi, tmp_path)
        try:
            asyncio.run(_adopt_ready(ctx))
            status = ctx.voice_engine.status()
            assert status["lifecycle"] == STATE_READY
            uptime = status["uptime_s"]
            assert uptime is not None and 0.0 <= uptime < 60.0
            # 状态卡面板（console.voice）拿到真 engine 状态时同样正确：
            from virtuallover.console import voice as console_voice

            payload = console_voice.VoicePageService(runtime=ctx).status()
            assert isinstance(payload["uptime_s"], (int, float))
            assert 0.0 <= payload["uptime_s"] < 60.0
        finally:
            _teardown(ctx)
