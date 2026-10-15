# -*- coding: utf-8 -*-
"""P0-a 记忆丢更新竞态回归（PERF_SPEC §1 / §7.2 锁边界 / §7.3 characterization）。

旧实现丢更新的机理（十参谋复现实锤）：_summarize 在入口一次性读走状态，
await chat（慢）返回后用陈旧对象把缓冲整仓清空——总结在途期间到达的消息
被静默丢弃。修复后锁边界：per-session asyncio.Lock 只包无 LLM await 的
状态读改写（on_message 的 load/append/save、_summarize 入口快照、atomic
前重读+按快照长度前缀清除），禁止跨 await chat 持锁；_inflight 抑制保留。

全部注入确定性假 chat/embedder；慢 chat 用事件对（started/release）精确
控制挂起窗口，关键 await 均加超时——若修复回退（锁跨 chat 持有），本文件
表现为 TimeoutError 失败而非测试挂死。
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import FAKE_SESSION, FakeChat, make_fake_embedder
from virtuallover.memory.service import MemoryService
from virtuallover.memory.store import MemoryStore

TEST_DIM = 8
# 占位会话之外的第二个会话（per-session 隔离用），号码段为明显占位值
SESSION_B = "测试人格:FriendMessage:87654321"

_WAIT = 5  # 秒：任一关键 await 的死线（修复回退时快速失败，不挂死）


class _Clock:
    """可控时钟（与 test_memory_service 同款）：手动推进当前时刻。"""

    def __init__(self, now: int = 1_000_000) -> None:
        self.now = now

    def __call__(self) -> float:
        return float(self.now)


def make_race_service(store: MemoryStore, chat) -> MemoryService:
    return MemoryService(
        store,
        chat_fn=chat,
        num_pairs=2,
        persona_id="人格占位",
        now_fn=_Clock(),
    )


@pytest.fixture()
def race_store(tmp_path):
    """独立临时库（不与 test_memory_service 的 fake_store 夹具串味）。"""
    return MemoryStore(
        tmp_path / "race.db",
        embedder=make_fake_embedder(dim=TEST_DIM),
        provider_id="占位供应商",
        model="test-embedding-model",
        endpoint="http://127.0.0.1:9",
        dim=TEST_DIM,
    )


# ---------------------------------------------------------------------------
# §7.3 characterization P0-a：慢 chat_fn 期间 on_message×2 → pending==2
# ---------------------------------------------------------------------------

def test_slow_chat_does_not_drop_concurrent_messages(race_store):
    """慢 chat 在途期间并发到达两条消息：总结完成后 pending==2、缓冲完整保留。

    修复前：两条被 _summarize 的陈旧整仓清空静默丢弃（pending==0）。
    """
    started = asyncio.Event()
    release = asyncio.Event()
    chat_calls: list[tuple[str, str]] = []

    async def slow_chat(system: str, user: str) -> str:
        chat_calls.append((system, user))
        started.set()
        await release.wait()  # 挂起：模拟慢 LLM
        return "慢总结的记忆正文"

    svc = make_race_service(race_store, slow_chat)
    S = FAKE_SESSION

    async def main() -> None:
        # 预热 3 条（不触发）
        for role, text in (("user", "一"), ("assistant", "二"), ("user", "三")):
            await svc.on_message(S, role, text, background=False)
        assert svc.pending_count(S) == 3
        # 第 4 条触发后台总结；等 chat 真正挂起（入口快照已定格 4 条）
        await asyncio.wait_for(
            svc.on_message(S, "assistant", "四", background=True), _WAIT)
        await asyncio.wait_for(started.wait(), _WAIT)
        # 慢 chat 在途期间并发到达两条（若锁跨 chat 持有，此处死锁→超时失败）
        await asyncio.wait_for(
            svc.on_message(S, "user", "五", background=True), _WAIT)
        await asyncio.wait_for(
            svc.on_message(S, "assistant", "六", background=True), _WAIT)
        assert svc.pending_count(S) == 6
        # 放行 chat，等总结落库
        release.set()
        for _ in range(1000):
            await asyncio.sleep(0)
            if race_store.count() == 1:
                break
        else:
            pytest.fail("总结未在预期拍数内落库")

    asyncio.run(main())
    # ★ 回归断言：丢更新修复（PERF_SPEC §1：pending==2）
    assert race_store.count() == 1
    assert svc.pending_count(S) == 2
    state = svc._load_state(S)
    assert [m["content"] for m in state.buffer] == ["五", "六"]
    assert [m["role"] for m in state.buffer] == ["user", "assistant"]
    # 快照语义：送入总结的是进入时的 4 条，不含慢 chat 期间新到的两条
    assert len(chat_calls) == 1
    _, user_prompt = chat_calls[0]
    for token in ("一", "二", "三", "四"):
        assert token in user_prompt
    for token in ("五", "六"):
        assert token not in user_prompt
    # 入库元数据描述的是快照窗口（4 条），不是混杂后的 6 条
    hits = asyncio.run(race_store.search(S, "慢总结的记忆正文"))
    assert hits and hits[0].metadata["msg_count"] == 4


def test_leftover_messages_summarized_next_round(race_store):
    """慢总结期间保留的尾部消息不丢：下一轮触发时完整进入新总结。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_chat(system: str, user: str) -> str:
        started.set()
        await release.wait()
        return "第一轮总结"

    svc = make_race_service(race_store, slow_chat)
    S = FAKE_SESSION

    async def main() -> None:
        for role, text in (("user", "一"), ("assistant", "二"), ("user", "三")):
            await svc.on_message(S, role, text, background=False)
        await asyncio.wait_for(
            svc.on_message(S, "assistant", "四", background=True), _WAIT)
        await asyncio.wait_for(started.wait(), _WAIT)
        await asyncio.wait_for(svc.on_message(S, "user", "五", background=True), _WAIT)
        await asyncio.wait_for(
            svc.on_message(S, "assistant", "六", background=True), _WAIT)
        release.set()
        for _ in range(1000):
            await asyncio.sleep(0)
            if race_store.count() == 1:
                break
        else:
            pytest.fail("第一轮总结未落库")
        # 换好 chat，再喂两条凑满阈值：第二轮总结必须含慢总结期间保留的四条
        good = FakeChat(replies=["第二轮总结"])
        svc._chat_fn = good
        await svc.on_message(S, "user", "七", background=False)
        await asyncio.wait_for(
            svc.on_message(S, "assistant", "八", background=False), _WAIT)
        for _ in range(1000):
            await asyncio.sleep(0)
            if race_store.count() == 2:
                break
        else:
            pytest.fail("第二轮总结未落库")
        _, user = good.calls[0]
        for token in ("五", "六", "七", "八"):
            assert token in user  # 尾部消息一条不少地进入第二轮窗口
        assert svc.pending_count(S) == 0

    asyncio.run(main())
    assert race_store.count() == 2
    assert svc.pending_count(S) == 0


# ---------------------------------------------------------------------------
# 锁边界两翼：per-session 隔离 + 同会话锁不跨 chat await
# ---------------------------------------------------------------------------

def test_lock_is_per_session_not_global(race_store):
    """A 会话总结在途（chat 挂起）时，B 会话的 on_message 必须畅通。

    若锁是全局单把，B 会在 A 的 chat 窗口内被阻塞 → wait_for 超时失败。
    """
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_chat(system: str, user: str) -> str:
        started.set()
        await release.wait()
        return "A 会话的总结"

    svc = make_race_service(race_store, slow_chat)
    A, B = FAKE_SESSION, SESSION_B

    async def main() -> None:
        for role, text in (("user", "一"), ("assistant", "二"), ("user", "三")):
            await svc.on_message(A, role, text, background=False)
        await asyncio.wait_for(svc.on_message(A, "assistant", "四", background=True), _WAIT)
        await asyncio.wait_for(started.wait(), _WAIT)
        # A 的 chat 挂起期间：B 会话写入零阻塞（per-session 锁隔离）
        await asyncio.wait_for(svc.on_message(B, "user", "甲", background=False), _WAIT)
        await asyncio.wait_for(svc.on_message(B, "assistant", "乙", background=False), _WAIT)
        assert svc.pending_count(B) == 2
        assert svc.pending_count(A) == 4  # A 的快照状态未被 B 干扰
        release.set()
        for _ in range(1000):
            await asyncio.sleep(0)
            if race_store.count() == 1:
                break
        else:
            pytest.fail("A 会话总结未落库")

    asyncio.run(main())
    assert svc.pending_count(B) == 2   # B 的计数不受 A 的总结清零影响
    assert svc.pending_count(A) == 0
    assert race_store.count(SESSION_B) == 0  # B 未触发总结，无入库


def test_same_session_lock_not_held_across_chat(race_store):
    """同会话：chat 挂起期间 on_message 必须能推进（锁不跨 await chat 持有）。

    修复回退成「锁包住整个 _summarize」时，本用例表现为死锁 → 超时失败。
    """
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_chat(system: str, user: str) -> str:
        started.set()
        await release.wait()
        return "总结正文"

    svc = make_race_service(race_store, slow_chat)
    S = FAKE_SESSION

    async def main() -> None:
        for role, text in (("user", "一"), ("assistant", "二"), ("user", "三")):
            await svc.on_message(S, role, text, background=False)
        await asyncio.wait_for(svc.on_message(S, "assistant", "四", background=True), _WAIT)
        await asyncio.wait_for(started.wait(), _WAIT)
        # chat 仍挂起：同会话新消息必须在 _WAIT 内完成登记（§7.2 锁边界）
        await asyncio.wait_for(svc.on_message(S, "user", "五", background=True), _WAIT)
        assert svc.pending_count(S) == 5
        release.set()

    asyncio.run(main())


def test_inflight_suppression_still_holds(race_store):
    """_inflight 抑制保留：慢总结在途时阈值再触达不开第二个总结任务。"""
    started = asyncio.Event()
    release = asyncio.Event()
    chat_calls: list[str] = []

    async def slow_chat(system: str, user: str) -> str:
        chat_calls.append(user)
        started.set()
        await release.wait()
        return "总结正文"

    svc = make_race_service(race_store, slow_chat)
    S = FAKE_SESSION

    async def main() -> None:
        for role, text in (("user", "一"), ("assistant", "二"), ("user", "三")):
            await svc.on_message(S, role, text, background=False)
        await asyncio.wait_for(svc.on_message(S, "assistant", "四", background=True), _WAIT)
        await asyncio.wait_for(started.wait(), _WAIT)
        # 在途期间连续两条（count 达 6，再次越过阈值）：只允许在途的那一次总结
        await asyncio.wait_for(svc.on_message(S, "user", "五", background=True), _WAIT)
        await asyncio.wait_for(
            svc.on_message(S, "assistant", "六", background=False), _WAIT)
        release.set()
        for _ in range(1000):
            await asyncio.sleep(0)
            if race_store.count() == 1:
                break
        else:
            pytest.fail("总结未落库")
        await asyncio.sleep(0)  # 让 finally 的 inflight 清理跑完
        assert S not in svc._inflight

    asyncio.run(main())
    assert len(chat_calls) == 1   # 全程只发起一次 LLM 总结
    assert race_store.count() == 1
    assert svc.pending_count(S) == 2
