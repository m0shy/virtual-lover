# -*- coding: utf-8 -*-
"""O4「worker 解耦（per-user 双队列）」characterization 测试（先红，PERF_SPEC §2-O4/§7.2/§7.3）。

先红机制（给实现组的阅读说明）：
- 契约依赖的测试（①④ + 回查文本并入）未落地时 skip（理由点名契约符号，
  见 tests/_o34_contract.py）；断言写实，红 = 实现违约，禁止改断言迁就实现；
- 两个「现状已违约」的测试（②回查阻塞 / ③发送不短路）曾以 xfail(strict=True)
  标定真红，O4 落地后已摘标记转正（先红→先绿的翻转点）。

五条 → 测试映射（§7.3 顺序）：

==========================  =========================================================
§7.3 条目                    测试
==========================  =========================================================
①per-user FIFO 保序+守恒    test_per_user_fifo_order_and_count_conservation（skip 先红）
②回查 ≤10s 不阻塞他 user    test_reply_lookup_does_not_block_other_user（已转正）
③发送失败短路剩余段         test_send_failure_short_circuits_remaining_segments（已转正）
④interrupt 清空被弃轮未发段 test_interrupt_clears_unsent_segments_of_discarded_round（skip 先红）
⑤EventBus 特性三件          test_event_bus_snapshot_iteration / _exception_barrier /
                            _error_reroute（锚定「语义不动」，应常绿）
（补充）回查文本并入轮      test_pipeline_lookup_attaches_reply_text（skip 先红）
==========================  =========================================================

运行：python -m pytest virtuallover/tests/test_o4_pipeline.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace

import pytest
from websockets.asyncio.client import connect

from virtuallover.brain.sender import RateLimiter
from virtuallover.events import EVENT_ERROR
from virtuallover.tests._o34_contract import (
    FAKE_SESSION,
    FakeBridge,
    FakeClock,
    attach_pipeline,
    make_cfg,
    make_ctx,
    make_event,
    settle,
    wait_until,
)

USER_A = "10001"   # 占位白名单用户（禁真实 QQ 号）
USER_B = "10002"   # 占位白名单用户
TOKEN = "vl-test-token-placeholder"
SELF_ID = "10000"  # 占位机器人账号


# ---------------------------------------------------------------------------
# ① per-user FIFO 保序 + 计数守恒
# ---------------------------------------------------------------------------

def test_per_user_fifo_order_and_count_conservation(tmp_path):
    """两个 user 交错到达的消息：各自回复按到达序 FIFO 出帧，计数守恒。

    §2-O4：per-user 管线队列 + 发送 FIFO；§2-O4 保序条款「并发压测后
    user 序单调、计数守恒」延伸到发送面与历史行。
    """
    clock = FakeClock()

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        try:
            handle, why = await attach_pipeline(ctx)
            if handle is None:
                pytest.skip(why)
            chat = handle.chat
            chat.auto_release = True

            def _echo(call):
                """回复回声：把本轮最后一条 user 文本原样带回（可钉定归属）。"""
                for msg in reversed(call.messages):
                    if str(msg.get("role")) == "user":
                        return str(msg.get("content") or "")
                return "空回复。"

            chat.reply_fn = _echo

            # 两个 user 严格交错到达（A1,B1,A2,B2,A3,B3）
            inter: list[tuple[str, str, int]] = []
            for i in range(1, 4):
                inter.append((USER_A, f"甲第{i}句", 100 + i))
                inter.append((USER_B, f"乙第{i}句", 200 + i))
            for user, text, mid in inter:
                await handle.emit(make_event(user, text, mid))
            await settle(handle, timeout=8.0)  # 6 条串行管线+落库，死线从宽

            bridge = handle.bridge

            def _markers(user: str) -> list[str]:
                got = []
                for record in bridge.sent:
                    if record.user_id == user and record.ok:
                        for i in range(1, 4):
                            if f"{'甲' if user == USER_A else '乙'}第{i}句" in record.text:
                                got.append(i)
                                break
                return got

            # 保序：每个 user 的回复严格按其消息顺序出帧
            assert _markers(USER_A) == [1, 2, 3], f"A 回复乱序：{_markers(USER_A)}"
            assert _markers(USER_B) == [1, 2, 3], f"B 回复乱序：{_markers(USER_B)}"
            # 计数守恒：6 条消息恰 6 条成功回复，无丢失无重复
            assert len([r for r in bridge.sent if r.ok]) == 6, (
                f"发送计数不守恒：{bridge.sent_texts()!r}")

            # 历史行：同会话 user 行单调（到达序）、计数守恒 6+6
            records = ctx.history.get_records(FAKE_SESSION)
            user_rows = [r["content"] for r in records if r["role"] == "user"]
            asst_rows = [r for r in records if r["role"] == "assistant"]
            assert user_rows == [t for _, t, _ in inter], f"user 序不单调：{user_rows!r}"
            assert len(asst_rows) == 6
        finally:
            ctx.history.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# ② 回查 ≤10s 不阻塞他 user（O4 落地：回查已移出桥帧分发路径，摘 xfail）
# ---------------------------------------------------------------------------

def test_reply_lookup_does_not_block_other_user():
    """A 的 reply 回查未决期间，B 的事件必须已上抛（O4②）。

    现状：桥 `_handle_message_event` 在返回前 `await self.get_msg_text(...)`
    （桥事件 worker 串行）→ A 的 10s 回查卡死整个 worker → B 被阻塞。
    目标（§2-O4）：帧路由只做分发，回查随事件进 per-user 管线。
    """
    from virtuallover.bridge.onebot_server import OneBotBridge

    async def _private_event(user: str, text: str, mid: int, reply_id=None) -> dict:
        segments = [{"type": "text", "data": {"text": text}}]
        if reply_id is not None:
            segments.insert(0, {"type": "reply", "data": {"id": str(reply_id)}})
        return {
            "time": 1710000000, "self_id": int(SELF_ID), "post_type": "message",
            "message_type": "private", "sub_type": "friend", "message_id": mid,
            "user_id": int(user), "message": segments, "raw_message": text,
            "font": 0, "sender": {"user_id": int(user), "nickname": "占位"},
        }

    async def scenario():
        events: asyncio.Queue = asyncio.Queue()
        # port=0：OS 分配空闲端口——本机 vl 常驻进程占着 6199，默认端口必撞 10048，
        # 那是「假红」；本测试的红必须是回查阻塞断言本身（--runxfail 可复核）
        bridge = OneBotBridge(token=TOKEN, self_id=SELF_ID, on_event=events.put,
                              whitelist=None, media_dir=None, port=0)
        await bridge.start()
        try:
            headers = {"Authorization": f"Bearer {TOKEN}", "X-Self-ID": SELF_ID}
            async with connect(f"ws://127.0.0.1:{bridge.bound_port}/ws",
                               additional_headers=headers) as ws:
                # A 带 reply 段（桥需 get_msg 回查）；B 普通消息紧随其后
                await ws.send(json.dumps(await _private_event(USER_A, "引用我看看", 1,
                                                              reply_id=5001),
                                          ensure_ascii=False))
                await ws.send(json.dumps(await _private_event(USER_B, "普通消息", 2),
                                          ensure_ascii=False))

                # 客户端**不回应** get_msg：回查处于未决（≤10s 窗口内）。
                # 目标语义（§2-O4）：回查随事件进 per-user 管线——A 与 B 的事件
                # 都应在回查未决期间上抛。现状（回查内联在 _handle_message_event
                # 里、先回查后上抛）：A 自身也被 10s 回查卡住，两个事件都不来。
                got_a = got_b = None
                with contextlib.suppress(TimeoutError):
                    got_a = await asyncio.wait_for(events.get(), timeout=2.0)
                with contextlib.suppress(TimeoutError):
                    got_b = await asyncio.wait_for(events.get(), timeout=2.0)
                assert got_a is not None and got_a["user_id"] == USER_A, (
                    "A 的事件被自身的 reply 回查阻塞上抛 —— "
                    "回查必须随事件进 per-user 管线，不得内联在帧分发路径（§2-O4）")
                assert got_b is not None and got_b["user_id"] == USER_B, (
                    "B 的事件被 A 的 reply 回查阻塞（§2-O4）")
        finally:
            await bridge.stop()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# ③ 发送失败短路剩余段（O4 落地：_send_texts 已短路，摘 xfail）
# ---------------------------------------------------------------------------

def test_send_failure_short_circuits_remaining_segments():
    """第 2 段发送失败 → 第 3 段不得再尝试（O4③）。

    §2-O4「发送失败短路本回复剩余段（修掉只告警不抛的现状）」。
    断在 `_send_texts`（命令/日记/主回复共用的发送编排缝）：
    O4 后该缝统一收编进 per-user 发送 FIFO，短路语义一并继承。
    """
    from virtuallover.__main__ import _send_texts

    async def scenario():
        cfg = make_cfg(FakeClock())
        bridge = FakeBridge()
        bridge.fail_on = {2}  # 第 2 次发送尝试直接失败
        ctx = SimpleNamespace(bridge=bridge,
                              rate_limiter=RateLimiter(max_count=10_000, window_seconds=1.0))
        # 阈值 10 + 句号正则：一条三段回复
        await _send_texts(ctx, USER_A, ["第一段。第二段。第三段。"], cfg.reply.segmented)
        assert bridge.sent_texts() == ["第一段。", "第二段。"], (
            f"发送失败未短路剩余段，仍尝试了：{bridge.sent_texts()!r}")

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# ④ interrupt 丢弃时清空被弃轮的未发段
# ---------------------------------------------------------------------------

def test_interrupt_clears_unsent_segments_of_discarded_round(tmp_path):
    """首段在途时新消息到达 → 被弃轮的未发段必须清空，不得再出帧（O4④）。

    §2-O4「interrupt 丢弃时清空该回复未发段」+ §7.2「'清空未发段'限定被弃轮
    （首段未发出的轮）」。构造：三段回复的首段发送挂在桥上（在途未完成），
    打断期内新消息到达 → 该轮被弃 → 第 2/3 段必须永远不再出帧；
    在途首段按已在传输层发出处理（实现二选一：完成或放弃，均不算违约）。
    """
    clock = FakeClock()

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        try:
            handle, why = await attach_pipeline(ctx)
            if handle is None:
                pytest.skip(why)
            chat = handle.chat
            chat.auto_release = False
            chat.set_replies(["被弃一段。被弃二段。被弃三段。", "重跑回复。"])
            bridge = handle.bridge
            bridge.hold_after = 0   # 第 1 次发送起挂起：首段在途、2/3 段未发

            await handle.emit(make_event(USER_A, "讲个长的", 1))
            await chat.wait_calls(1)
            chat.calls[0].release.set()          # 旧轮 LLM 完成，进入发送
            await wait_until(lambda: bridge.in_flight >= 1, timeout=3.0)

            # 首段在途（未完成出帧）+ 打断期内新消息 → 被弃轮未发段必须清空
            await handle.emit(make_event(USER_A, "别说了", 2))
            # 段间隔有 ≥1.2s 拟人硬下限（O2a），死线放宽
            await chat.wait_calls(2, timeout=8.0)  # 立即重跑
            bridge.hold_event.set()                # 放行在途首段（结果见下方容断言）
            chat.calls[1].release.set()
            await settle(handle, timeout=8.0)

            assert handle.superseded_count == 1
            sent_texts = bridge.sent_texts()
            assert "被弃二段。" not in sent_texts, f"被弃轮未发段仍出帧：{sent_texts!r}"
            assert "被弃三段。" not in sent_texts, f"被弃轮未发段仍出帧：{sent_texts!r}"
            assert "重跑回复。" in bridge.completed, "重跑轮回复未发出"
            merged = ""
            for msg in reversed(chat.calls[1].messages):
                if str(msg.get("role")) == "user":
                    merged = str(msg.get("content") or "")
                    break
            assert "讲个长的" in merged and "别说了" in merged
        finally:
            ctx.history.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# （补充契约）回查文本随事件进管线并并入本轮用户消息
# ---------------------------------------------------------------------------

def test_pipeline_lookup_attaches_reply_text(tmp_path):
    """带 reply_id 的事件进管线：回查文本并入本轮用户消息（对齐 __main__ 现行为）。

    §2-O4「reply 回查（≤10s）随事件进管线」；__main__._handle_user_message
    现行为 = f"[引用消息] {reply_text}\\n{text}"，characterization 钉住该语义。
    """
    clock = FakeClock()

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        try:
            handle, why = await attach_pipeline(ctx)
            if handle is None:
                pytest.skip(why)
            handle.bridge.lookup_reply = "被引用的原话"
            await handle.emit(make_event(USER_A, "你看这条", 1, reply_id=5001))
            await settle(handle)

            assert len(handle.chat.calls) == 1
            user_text = ""
            for msg in reversed(handle.chat.calls[0].messages):
                if str(msg.get("role")) == "user":
                    user_text = str(msg.get("content") or "")
                    break
            assert "被引用的原话" in user_text, f"回查文本未并入本轮：{user_text!r}"
            assert "你看这条" in user_text
        finally:
            ctx.history.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# ⑤ EventBus 特性三件（锚定「EventBus.emit 语义不动」，应常绿）
# ---------------------------------------------------------------------------

def test_event_bus_snapshot_iteration():
    """emit 期间退订自己/新增处理器：快照迭代，本轮新增不触发、无 RuntimeError。"""
    from virtuallover.events import EventBus

    async def scenario():
        bus = EventBus()
        first_round: list = []
        second_round: list = []
        holder: dict = {}

        def handler_a(event):
            holder["unsub"]()            # 迭代中退订自己
            bus.on("message", handler_b)  # 迭代中新增（不得进本轮快照）
            first_round.append(event.payload)

        def handler_b(event):
            second_round.append(event.payload)

        holder["unsub"] = bus.on("message", handler_a)
        await bus.emit("message", 1)
        assert first_round == [1]
        assert second_round == [], "本轮新增处理器被触发（快照迭代被破坏）"
        await bus.emit("message", 2)
        assert second_round == [2], "下一轮应触发新增处理器"
        assert bus.handler_count("message") == 1  # a 已退订，仅剩 b

    asyncio.run(scenario())


def test_event_bus_exception_barrier():
    """单个处理器抛异常：后续处理器照常执行，异常记入 event.errors（异常屏障）。"""
    from virtuallover.events import EventBus

    async def scenario():
        bus = EventBus()
        order: list[str] = []

        def bad(event):
            order.append("bad")
            raise RuntimeError("处理器炸了")

        def good(event):
            order.append("good")

        bus.on("message", bad)
        bus.on("message", good)
        event = await bus.emit("message", "x")
        assert order == ["bad", "good"], f"屏障失效，后续处理器未执行：{order!r}"
        assert len(event.errors) == 1 and isinstance(event.errors[0], RuntimeError)
        assert bus.handler_count("message") == 2  # 处理器不被摘除

    asyncio.run(scenario())


def test_event_bus_error_reroute():
    """处理器异常转投 EVENT_ERROR（payload 含 source_event/exception/handler），
    且 EVENT_ERROR 自身的处理器异常不再递归（恰一次转投）。"""
    from virtuallover.events import EventBus

    async def scenario():
        bus = EventBus()
        got: list = []

        def bad(event):
            raise ValueError("源事件处理器炸了")

        def err_handler(event):
            got.append(dict(event.payload) if isinstance(event.payload, dict) else event.payload)
            raise RuntimeError("错误处理器自己也炸")  # 不得递归

        bus.on("message", bad)
        bus.on(EVENT_ERROR, err_handler)
        await bus.emit("message", "payload-x")

        assert len(got) == 1, f"EVENT_ERROR 转投应恰一次（无递归）：{got!r}"
        payload = got[0]
        assert payload.get("source_event") == "message"
        assert isinstance(payload.get("exception"), ValueError)
        assert "bad" in str(payload.get("handler") or "")

    asyncio.run(scenario())
