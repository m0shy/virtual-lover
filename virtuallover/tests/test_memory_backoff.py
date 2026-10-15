# -*- coding: utf-8 -*-
"""记忆总结失败退避 + 缓冲硬上限契约测试（五轮修缮 R2 任务 7）。

背景实证：ark 欠费 403 期间旧实现每 4 条消息就重开一次总结任务（LLM+embedding
全链路白撞 8 小时）。本文件钉死修复后的契约：

1. 退避门（10-30min 不重试）：总结失败后连败 +1、自动触发被挡下；计数与
   缓冲照常累积（「计数照累」语义）；退避窗口过后（假钟推进）下一条消息
   自动复跑；成功复位连败与退避时刻。
2. 缓冲硬上限 200：退避期素材有界——第 201 条起丢最旧，触发计数照累。
3. 显式 retry_pending 不受退避门限制（人工/运维指令语义就是「现在就试」）。

全部假件（fake_store 假嵌入 + FakeChat 假 LLM + 可注入时钟），零触网。
运行：python -m pytest virtuallover/tests/test_memory_backoff.py -q
"""

from __future__ import annotations

import asyncio
import time

import pytest

from virtuallover.memory.service import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_MAX_SECONDS,
    BUFFER_HARD_CAP,
    MemoryService,
)

SESSION = "测试人格:FriendMessage:12345678"


class _FakeClock:
    """可推进假钟（epoch 秒语义，MemoryService.now_fn 注入用）。"""

    def __init__(self) -> None:
        self.t = 1_700_000_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


class _ScriptedChat:
    """假 chat：按 fail_times 次数先失败后成功；记录调用。"""

    def __init__(self, fail_times: int = 0) -> None:
        self.calls = 0
        self.fail_times = fail_times

    async def __call__(self, system: str, user: str) -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("模拟 ark 403 欠费")
        return "她喜欢在深夜散步，怕黑但爱看星星。"


def _make_service(fake_store, clock: _FakeClock, chat: _ScriptedChat) -> MemoryService:
    return MemoryService(
        fake_store(),
        chat_fn=chat,
        num_pairs=2,             # 阈值 = 4 条消息
        now_fn=clock,
    )


async def _pump(service: MemoryService, n: int, *, ts: int | None = None) -> None:
    """灌 n 条消息（内联触发，background=False——结果确定，不与后台任务赛跑）。"""
    for i in range(n):
        await service.on_message(SESSION, "user", f"第 {i} 句话，说说今天的心情。",
                                 ts=ts, background=False)


def test_backoff_constants_within_task_band():
    """任务口径：退避落在 10-30 分钟带内。"""
    assert BACKOFF_BASE_SECONDS == 600.0
    assert BACKOFF_MAX_SECONDS == 1800.0
    assert MemoryService._backoff_seconds(1) == 600.0   # 首败 10min
    assert MemoryService._backoff_seconds(2) == 1200.0  # 再败 20min
    assert MemoryService._backoff_seconds(3) == 1800.0  # 30min 封顶
    assert MemoryService._backoff_seconds(9) == 1800.0  # 更大连败不再增长


def test_failure_backs_off_and_blocks_auto_retry(fake_store):
    """失败一次 → 退避 10min；窗口内消息照常计数但不触发总结任务。"""
    clock = _FakeClock()
    chat = _ScriptedChat(fail_times=1)
    service = _make_service(fake_store, clock, chat)

    async def scenario():
        await _pump(service, 4)                    # 达阈值 → 触发（内联）→ 失败
        assert chat.calls == 1
        state = service._load_state(SESSION)
        assert state.fail_count == 1
        assert state.next_retry_ts == int(clock.t + 600)
        assert state.count == 4, "失败后计数保留待重试（不丢）"

        # 退避窗口内再灌 4 条：计数照累（8），但绝不重开总结任务
        clock.advance(60)                          # +1min（<10min 窗口）
        await _pump(service, 4, ts=int(clock.t))
        assert chat.calls == 1, "退避窗口内不得重试总结（任务口径：10-30min 不重试）"
        state = service._load_state(SESSION)
        assert state.count == 8, "计数照累：退避期计数继续累积"
        assert len(state.buffer) == 8, "缓冲照常累积素材"

    asyncio.run(scenario())


def test_backoff_expires_and_next_message_retries(fake_store):
    """假钟推过 10min 窗口 → 下一条消息到达即自动复跑并成功、退避复位。"""
    clock = _FakeClock()
    chat = _ScriptedChat(fail_times=1)
    service = _make_service(fake_store, clock, chat)

    async def scenario():
        await _pump(service, 4)
        assert chat.calls == 1 and service._load_state(SESSION).fail_count == 1

        clock.advance(BACKOFF_BASE_SECONDS + 1)    # 推过退避窗口
        await _pump(service, 1, ts=int(clock.t))   # 计数 5 >= 阈值（照累）→ 复跑成功
        assert chat.calls == 2, "窗口过后的下一条消息应自动复跑总结"
        state = service._load_state(SESSION)
        assert state.fail_count == 0 and state.next_retry_ts is None, (
            "成功必须复位连败与退避时刻")
        assert state.count == 0 and state.buffer == [], (
            "成功后 5 条素材全部总结入库，count 与缓冲同源归零")
        # 记忆真的入库了
        hits = await service._store.search(SESSION, "深夜散步", top_k=3)
        assert hits, "总结应已入库可检索"

    asyncio.run(scenario())


def test_buffer_hard_cap_trims_oldest_keeps_counting(fake_store):
    """第 201 条起丢最旧素材；触发计数照累不受影响。"""
    clock = _FakeClock()
    chat = _ScriptedChat(fail_times=10**9)   # 恒失败：素材只进不出
    service = _make_service(fake_store, clock, chat)

    async def scenario():
        # 首次触发失败 → 进入退避；之后计数照累不触发
        await _pump(service, 4)
        assert chat.calls == 1
        for i in range(BUFFER_HARD_CAP + 10):
            clock.advance(0.01)
            await service.on_message(SESSION, "user", f"洪水消息 {i}。", ts=int(clock.t))
        state = service._load_state(SESSION)
        assert len(state.buffer) == BUFFER_HARD_CAP, (
            f"缓冲必须钉在硬上限：{len(state.buffer)}")
        # 4 + 210 = 214 条 → 裁掉最旧 14 条（第 0-3 句 + 洪水消息 0-9）
        assert state.buffer[0]["content"] == "洪水消息 10。", (
            "最旧的 14 条素材应被丢弃（保留最新 200 条）")
        assert state.buffer[-1]["content"] == "洪水消息 209。"
        assert state.count == 4 + BUFFER_HARD_CAP + 10, "计数照累（不因裁剪回退）"

    asyncio.run(scenario())


def test_retry_pending_bypasses_backoff(fake_store):
    """显式 retry_pending 不受退避门限制（人工指令语义=现在就试）。"""
    clock = _FakeClock()
    chat = _ScriptedChat(fail_times=1)
    service = _make_service(fake_store, clock, chat)

    async def scenario():
        await _pump(service, 4)
        assert chat.calls == 1
        state = service._load_state(SESSION)
        assert state.next_retry_ts is not None    # 已在退避期
        succeeded = await service.retry_pending(SESSION)
        assert chat.calls == 2, "显式重试必须无视退避门"
        assert succeeded == 1
        state = service._load_state(SESSION)
        assert state.fail_count == 0 and state.next_retry_ts is None

    asyncio.run(scenario())


def test_backoff_state_survives_persistence_roundtrip(fake_store):
    """退避字段随 kv 持久化：新服务实例（同一库，模拟重启）读后退避仍生效。"""
    clock = _FakeClock()
    chat = _ScriptedChat(fail_times=1)
    store = fake_store(name="shared_backoff.db")   # 两个服务实例共用同一库
    service = MemoryService(store, chat_fn=chat, num_pairs=2, now_fn=clock)

    async def scenario():
        await _pump(service, 4)
        assert chat.calls == 1
        assert service._load_state(SESSION).fail_count == 1
        # 模拟重启：新服务实例（同库）读同一状态
        service2 = MemoryService(store, chat_fn=_ScriptedChat(fail_times=0),
                                 num_pairs=2, now_fn=clock)
        clock.advance(1)
        await _pump(service2, 1, ts=int(clock.t))
        assert service2._load_state(SESSION).count == 5, "重启后计数与缓冲不丢"
        assert service2.pending_count(SESSION) == 5

    asyncio.run(scenario())
