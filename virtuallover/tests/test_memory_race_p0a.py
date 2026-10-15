# -*- coding: utf-8 -*-
"""P0-a MemoryService 丢更新竞态回归（PERF_SPEC §1 / §7.2 锁边界 / §7.3）。

历史定位：本文件最初以 xfail(strict) 先红——复现 §1 实锤竞态（_summarize 在
await chat 前快照 state，atomic 窗口用陈旧对象整体清零 buffer/count，慢 chat
期间并发 on_message 被静默抹掉）。P0-a 修复（per-session 锁 + atomic 前重读
+ 按快照长度前缀清除）落地后本测试转绿，标记随之摘除，转为常驻回归。

本文件聚焦 §1 的字面场景（总结在途期间 on_message ×2 → pending==2），
作为「实现组视角」的 test_memory_race.py（锁边界/隔离性五测）的补充：
从消息管线调用语义（background=False 内联总结）驱动同一竞态窗口。

运行：python -m pytest virtuallover/tests/test_memory_race_p0a.py -q
"""

from __future__ import annotations

import asyncio

from virtuallover.memory.service import MemoryService
from virtuallover.tests._o34_contract import wait_until

SESSION = "竞态回归:FriendMessage:10001"  # 占位会话标识


class GatedChat:
    """慢 chat 回调：进入即置 entered，测试放行 release 后才返回总结文本。"""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        self.entered.set()
        await self.release.wait()
        return "第一二句的总结。"


def test_slow_chat_with_concurrent_on_message_keeps_pending(fake_store):
    """慢 chat_fn 期间并发 on_message ×2 → 总结完成后 pending == 2（零丢失）。"""
    store = fake_store()
    gated = GatedChat()
    service = MemoryService(store, chat_fn=gated, num_pairs=1)  # 阈值 = 2

    async def scenario():
        # 1) 第一条：count=1（未达阈值，不触发）
        await service.on_message(SESSION, "user", "第一句")

        # 2) 第二条：count=2 达阈值 → background=False 内联总结 → chat 被闸门挂住
        summarizing = asyncio.create_task(
            service.on_message(SESSION, "user", "第二句", background=False))
        await wait_until(lambda: gated.entered.is_set(), timeout=3.0)

        # 3) 总结在途期间并发 on_message ×2（§1 复现脚本）
        await asyncio.gather(
            service.on_message(SESSION, "user", "第三句"),
            service.on_message(SESSION, "user", "第四句"),
        )
        assert service.pending_count(SESSION) == 4, (
            "并发两条未计入（竞态不止丢总结侧）")

        # 4) 放行 chat：总结入库 + 计数清零（当前实现把第 3、4 条一并抹掉）
        gated.release.set()
        await summarizing

        # 5) 回归断言（§1）：pending == 2 —— 本次总结只消费前缀 [m1, m2]，
        #    在途到达的第 3、4 条必须保留待下一轮总结
        assert service.pending_count(SESSION) == 2, (
            f"pending={service.pending_count(SESSION)}：慢 chat 期间的并发消息被丢弃"
            "（P0-a 丢更新竞态）")

    asyncio.run(scenario())


def test_concurrent_on_message_without_summary_no_loss(fake_store):
    """无总结在途时并发 on_message ×2：计数守恒（基线，当前即应绿）。"""
    store = fake_store()
    service = MemoryService(store, chat_fn=GatedChat(), num_pairs=4)  # 阈值 8，不触发

    async def scenario():
        await asyncio.gather(
            service.on_message(SESSION, "user", "甲"),
            service.on_message(SESSION, "user", "乙"),
        )
        assert service.pending_count(SESSION) == 2

    asyncio.run(scenario())
