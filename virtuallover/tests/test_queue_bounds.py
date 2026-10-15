# -*- coding: utf-8 -*-
"""队列有界 + 幽灵回复契约测试（五轮修缮 R2 任务 4/6）。

覆盖面：
1. 发送 FIFO 有界（任务 6）：_UserRuntime.send_queue maxsize=50；满丢最旧 +
   告警计数；被丢的轮内作业按 on_fail([])「零段送达」收尾（状态机不卡死、
   不写 assistant 行）。
2. 命令 3s 窗去重（任务 6）：同一用户同一命令窗口内第二次到达只处理一次；
   窗口外放行；普通消息（非「/」前缀）绝不去重。
3. 幽灵回复修复（任务 4）：发送短路后 assistant 行只写**实际送达段**——
   部分送达只落已说出的话；零段送达不写 assistant 行（她没说过就不许记忆）。
4. LLM 失败计数（任务 3 的管线侧数据源）：chat 最终失败 → llm_fail_count+1。
5. 语音队列有界（任务 6）：maxsize=20，满丢最旧 + queue_dropped 计数；
   VoiceManager.status() 基线（无引擎栈时状态卡兜底）。

驱动：pipeline 走 tests/_o34_contract.py 契约垫片（FakeClock/FakeBridge/
ScriptedChatClient，全程假件零触网）；语音用注入桩。
运行：python -m pytest virtuallover/tests/test_queue_bounds.py -q
"""

from __future__ import annotations

import asyncio

import pytest

from virtuallover.brain.history import HistoryStore
from virtuallover.brain.llm import LLMError
from virtuallover.tests._o34_contract import (
    FAKE_SESSION,
    FakeClock,
    attach_pipeline,
    make_ctx,
    make_event,
    settle,
    wait_until,
)
from virtuallover.voice.manager import VOICE_QUEUE_MAX_ITEMS, VoiceManager

USER_A = "10001"   # 占位白名单用户（禁真实 QQ 号）


# ---------------------------------------------------------------------------
# 1) 发送 FIFO 有界：maxsize 50 + 满丢最旧
# ---------------------------------------------------------------------------

def test_send_queue_is_bounded_at_fifty(tmp_path):
    """契约常量：每用户发送队列 maxsize=50。"""
    from virtuallover.brain.pipeline import SEND_QUEUE_MAX_ITEMS, MessagePipeline

    assert SEND_QUEUE_MAX_ITEMS == 50
    ctx = make_ctx(tmp_path, now_fn=FakeClock())

    async def scenario():
        handle, why = await attach_pipeline(ctx)
        if handle is None:
            pytest.skip(why)
        try:
            st = handle.pipeline._user(USER_A)
            assert st.send_queue.maxsize == 50
        finally:
            await handle.aclose()
            ctx.history.close()

    asyncio.run(scenario())


def test_full_queue_drops_oldest_and_counts(tmp_path):
    """队列满 50：第 52 个入队 → 最旧被丢、丢弃计数 +1、队列长恒 ≤50。"""
    from virtuallover.brain.pipeline import _SendJob

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=FakeClock())
        handle, why = await attach_pipeline(ctx)
        if handle is None:
            pytest.skip(why)
        try:
            st = handle.pipeline._user(USER_A)
            # worker 由首作业挂起占住（hold_after=0：首段在途永不完成）
            ctx.bridge.hold_after = 0
            st.send_queue.put_nowait(_SendJob(user_id=USER_A, params=None,
                                              segments=["占位首段"]))
            # 再灌 51 个纯文字作业（gen=-1，无回调）：共 52 项入 50 容量
            # → 丢 2 个最旧（占位首段、作业0），队列长恒 ≤50
            for i in range(51):
                handle.pipeline._enqueue_text_job(st, [f"作业{i}。一整段话。"],
                                                  ctx.cfg.reply.segmented)
            assert st.send_queue.qsize() == 50, "队列长度必须恒 ≤ maxsize"
            assert handle.pipeline.send_dropped_count == 2, (
                f"满丢最旧未计数：{handle.pipeline.send_dropped_count}")
            # 队首应是幸存的最旧者「作业1」（占位首段与作业0 已被挤掉）
            queued_first = st.send_queue._queue[0]
            assert queued_first.segments and "作业1" in queued_first.segments[0], (
                f"应丢最旧（占位首段、作业0），队首为作业1：{queued_first.segments!r}")
        finally:
            ctx.bridge.hold_event.set()   # 放行挂起的 worker，收尾不悬挂
            await handle.aclose()
            ctx.history.close()

    asyncio.run(scenario())


def test_dropped_round_job_fails_with_empty_segments(tmp_path):
    """被丢的轮内作业（gen>=0 带回调）按 on_fail([]) 收尾：状态复位、零段送达。"""
    from virtuallover.brain.pipeline import _SendJob

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=FakeClock())
        handle, why = await attach_pipeline(ctx)
        if handle is None:
            pytest.skip(why)
        try:
            st = handle.pipeline._user(USER_A)
            st.state = "RUNNING"          # 模拟一代轮在发送中
            st.gen = 7
            fail_calls: "list[list[str]]" = []

            async def on_fail(sent):
                fail_calls.append(list(sent))
                await handle.pipeline._finish_round(st, 7)

            # 轮内作业先入队（队首）→ 再灌 50 个无回调作业：第 50 个入队时
            # 队列满 50，最旧者恰是轮内作业 → 被挤掉 → 触发 on_fail([])
            from virtuallover.brain.pipeline import _SendJob as Job
            job = Job(user_id=USER_A, params=None, segments=["她要说的话。"], gen=7,
                      on_fail=on_fail)
            handle.pipeline._enqueue_send_job(st, job)
            assert st.send_queue.qsize() == 1
            for i in range(50):
                handle.pipeline._enqueue_text_job(st, [f"占坑{i}。一整段话。"],
                                                  ctx.cfg.reply.segmented)
            await wait_until(lambda: handle.pipeline.send_dropped_count >= 1, timeout=3.0)
            await asyncio.sleep(0.1)      # 让被丢作业的失败收尾跑完
            assert handle.pipeline.send_dropped_count == 1
            assert fail_calls == [[]], f"被丢作业应按零段送达收尾：{fail_calls!r}"
            assert st.state == "IDLE", "on_fail([]) 必须 _finish_round 复位状态机"
        finally:
            await handle.aclose()
            ctx.history.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 2) 命令 3s 窗去重
# ---------------------------------------------------------------------------

def test_duplicate_command_within_window_processed_once(tmp_path):
    """同一命令 3s 内两次到达：router 只处理一次；窗口外放行。"""
    clock = FakeClock()

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        handle, why = await attach_pipeline(ctx)
        if handle is None:
            pytest.skip(why)
        try:
            router = ctx.router
            calls: "list[str]" = []

            async def _spy(text, user_id, admins=None, whitelist=None):
                calls.append(text)
                return None   # 非已知命令：静默，但调用本身可观测

            router.handle = _spy   # 探针：只记录路由调用

            await handle.emit(make_event(USER_A, "/状态", 1))
            await wait_until(lambda: len(calls) == 1, timeout=5.0)
            clock.advance(1.0)     # 窗口内（<3s）重发同一条命令
            await handle.emit(make_event(USER_A, "/状态", 2))
            await asyncio.sleep(0.15)   # 通道串行消化这条被去重的消息
            assert calls == ["/状态"], f"3s 窗内重复命令应被去重：{calls!r}"

            clock.advance(5.0)     # 窗口外：放行
            await handle.emit(make_event(USER_A, "/状态", 3))
            await wait_until(lambda: len(calls) == 2, timeout=5.0)
            assert calls == ["/状态", "/状态"], "窗口外的同命令必须照常处理"
        finally:
            await handle.aclose()
            ctx.history.close()

    asyncio.run(scenario())


def test_plain_text_never_deduplicated(tmp_path):
    """连发同一句普通消息是正常语义——绝不去重（只有「/」前缀才判重）。"""
    clock = FakeClock()

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        handle, why = await attach_pipeline(ctx)
        if handle is None:
            pytest.skip(why)
        try:
            chat = handle.chat
            chat.auto_release = True
            clock.advance(0.01)
            await handle.emit(make_event(USER_A, "在吗", 1))
            clock.advance(0.01)     # 0.01s 后重发同句（打断窗口内会合并重跑，
            await handle.emit(make_event(USER_A, "在吗", 2))  # 但绝不会被「去重」吞掉）
            # 收敛：两条到达都必须落 user 行（到达语义不丢消息）
            await wait_until(
                lambda: len([r for r in ctx.history.get_records(FAKE_SESSION)
                             if r["role"] == "user"]) == 2, timeout=8.0)
            await settle(handle, timeout=8.0)
            sent = ctx.bridge.sent_texts()
            assert sent.count("嗯嗯 好呀。") >= 1, f"普通消息必须至少回应一次：{sent!r}"
            user_rows = [r for r in ctx.history.get_records(FAKE_SESSION)
                         if r["role"] == "user"]
            assert len(user_rows) == 2, f"两条到达都应落 user 行：{len(user_rows)}"
        finally:
            await handle.aclose()
            ctx.history.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 3) 幽灵回复修复：on_fail 只写实际送达段
# ---------------------------------------------------------------------------

def _assistant_rows(ctx) -> "list[str]":
    return [r["content"] for r in ctx.history.get_records(FAKE_SESSION)
            if r["role"] == "assistant"]


def test_partial_delivery_archives_only_sent_segments(tmp_path):
    """3 段回复第 2 段发送失败 → assistant 行只含已送达的第 1 段。"""
    clock = FakeClock()

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        handle, why = await attach_pipeline(ctx)
        if handle is None:
            pytest.skip(why)
        try:
            chat = handle.chat
            chat.auto_release = True
            chat.set_replies(["一段话。二段话。三段话。"])
            ctx.bridge.fail_on = {2}    # 第 2 次发送尝试失败 → 短路
            await handle.emit(make_event(USER_A, "讲个三段的", 1))
            await chat.wait_calls(1)    # 先等轮起跑（防 settle 对 IDLE 空窗误判）
            await settle(handle, timeout=8.0)

            rows = _assistant_rows(ctx)
            assert rows == ["一段话。"], (
                f"幽灵回复修复违约（应只写实际送达段）：{rows!r}")
        finally:
            await handle.aclose()
            ctx.history.close()

    asyncio.run(scenario())


def test_zero_delivery_writes_no_assistant_row(tmp_path):
    """第 1 段就失败（零送达）→ 不写 assistant 行——她没说过就不许进历史。"""
    clock = FakeClock()

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        handle, why = await attach_pipeline(ctx)
        if handle is None:
            pytest.skip(why)
        try:
            chat = handle.chat
            chat.auto_release = True
            chat.set_replies(["一段话。二段话。"])
            ctx.bridge.fail_on = {1}    # 首段即失败
            await handle.emit(make_event(USER_A, "说两句", 1))
            await chat.wait_calls(1)    # 先等轮起跑（防 settle 对 IDLE 空窗误判）
            await settle(handle, timeout=8.0)

            assert _assistant_rows(ctx) == [], (
                f"零段送达不得写 assistant 行（幽灵回复）：{_assistant_rows(ctx)!r}")
            # user 行照常在到达时刻落库（§7.1-2 语义不变）
            user_rows = [r for r in ctx.history.get_records(FAKE_SESSION)
                         if r["role"] == "user"]
            assert len(user_rows) == 1
        finally:
            await handle.aclose()
            ctx.history.close()

    asyncio.run(scenario())


def test_full_delivery_archives_full_reply(tmp_path):
    """全段送达（on_done 路径）：行为与旧版一致——整条落库。"""
    clock = FakeClock()

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        handle, why = await attach_pipeline(ctx)
        if handle is None:
            pytest.skip(why)
        try:
            chat = handle.chat
            chat.auto_release = True
            chat.set_replies(["一段话。二段话。"])
            await handle.emit(make_event(USER_A, "说两句", 1))
            await chat.wait_calls(1)    # 先等轮起跑（防 settle 对 IDLE 空窗误判）
            await settle(handle, timeout=8.0)
            rows = _assistant_rows(ctx)
            assert len(rows) == 1 and "一段话。" in rows[0] and "二段话。" in rows[0], (
                f"完整送达应整条落库：{rows!r}")
        finally:
            await handle.aclose()
            ctx.history.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 4) LLM 失败计数（任务 3 管线侧数据源）
# ---------------------------------------------------------------------------

class _AlwaysFailsChat:
    """恒失败的假 chat 客户端（重试/fallback 耗尽后的最终失败形态）。"""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def in_flight(self) -> int:
        return 0

    async def chat_async(self, messages, **kwargs):
        self.calls += 1
        raise LLMError("chat HTTP 403：error.Forbidden/AccountOverdueError", status=403)


def test_llm_fail_count_increments_and_round_fails_silently(tmp_path):
    """chat 最终失败 → llm_fail_count +1、本轮静默（无发送无 assistant 行）。"""
    clock = FakeClock()

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        ctx.chat_client = _AlwaysFailsChat()
        handle, why = await attach_pipeline(ctx)
        if handle is None:
            pytest.skip(why)
        try:
            clock.advance(0.01)
            await handle.emit(make_event(USER_A, "在吗", 1))
            await wait_until(lambda: ctx.chat_client.calls >= 1, timeout=5.0)
            await settle(handle, timeout=8.0)
            assert handle.pipeline.llm_fail_count == 1, (
                f"LLM 最终失败必须计数：{handle.pipeline.llm_fail_count}")
            assert ctx.bridge.sent == [], "失败轮必须静默（零发送）"
            assert _assistant_rows(ctx) == [], "失败轮不写 assistant 行"
        finally:
            await handle.aclose()
            ctx.history.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 5) 语音队列有界 + VoiceManager.status() 基线
# ---------------------------------------------------------------------------

def _make_manager() -> VoiceManager:
    async def _send(user_id: str, audio: bytes) -> bool:
        return True

    return VoiceManager(send_voice=_send, trigger_probability=1.0)


def test_voice_queue_bounded_at_twenty_drops_oldest():
    """语音队列满 20 丢最旧：队列长恒 ≤20、queue_dropped 计数。"""
    from virtuallover.voice.manager import VoiceJob

    async def scenario():
        manager = _make_manager()
        # 不启动 worker：手工占坑队列，纯测入队边界逻辑
        manager._queues["10001"] = asyncio.Queue(maxsize=VOICE_QUEUE_MAX_ITEMS)
        for i in range(22):
            fired = manager.enqueue_reply("10001", [f"第{i}段语音台词。"])
            assert fired is True
        assert manager._queues["10001"].qsize() == 20
        assert manager.stats["queue_dropped"] == 2
        head = manager._queues["10001"]._queue[0]
        assert head.segments == ["第2段语音台词。"], "应丢最旧（第 0/1 个）"

    asyncio.run(scenario())


def test_voice_manager_status_baseline_without_engine():
    """无引擎栈：status() 基线如实报 not_started + 门面观测数据，绝不假装在线。"""
    manager = _make_manager()
    status = manager.status()
    assert status["lifecycle"] == "not_started"
    assert status["ownership"] is None
    assert status["queue_max_items"] == VOICE_QUEUE_MAX_ITEMS == 20
    assert set(status["stats"]) >= {"jobs", "segments_sent", "segments_failed",
                                    "degraded_skips", "queue_dropped"}


def test_voice_manager_status_merges_engine_status():
    """引擎栈在位（转发前窗口）：status() 返回引擎全量状态。"""
    manager = _make_manager()

    class _FakeEngine:
        def status(self) -> dict:
            return {"lifecycle": "ready", "ownership": "owned", "character": "占位"}

    manager._engine_provider = lambda: _FakeEngine()
    status = manager.status()
    assert status["lifecycle"] == "ready" and status["ownership"] == "owned"


def test_voice_manager_status_survives_engine_error():
    """引擎 status 抛异常：退回门面基线（观测面故障不炸状态卡）。"""
    manager = _make_manager()

    class _BoomEngine:
        def status(self) -> dict:
            raise RuntimeError("引擎状态炸了")

    manager._engine_provider = lambda: _BoomEngine()
    status = manager.status()
    assert status["lifecycle"] == "not_started"  # 基线兜底
