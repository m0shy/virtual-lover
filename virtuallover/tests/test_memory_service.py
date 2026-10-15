# -*- coding: utf-8 -*-
"""service.py 单元测试（SPEC §7.2）：计数触发、失败持久化待重试、时间兜底。

全程注入假 chat/embedder，结果确定；计数与缓冲经 store.kv 持久化，
重启语义用「新建服务实例读同一库」验证。
"""

from __future__ import annotations

import asyncio

import pytest

import numpy as np

from conftest import FAKE_SESSION, TEST_DIM, FakeChat
from virtuallover.memory.service import (
    SOURCE_AUTO,
    SOURCE_MANUAL,
    MemoryService,
    build_summary_prompts,
    chat_fn_from_client,
    clean_message_text,
    embedder_from_client,
    neutralize_template_placeholders,
    resolve_chat_fn,
)
from virtuallover.memory.store import LLMNotWiredError


def test_unwired_error_type_is_runtime_error():
    """占位接口约定：未接线异常必须是 RuntimeError 子类，便于上层统一捕获。"""
    assert issubclass(LLMNotWiredError, RuntimeError)


class _Clock:
    """可控时钟：手动推进当前时刻（时间兜底测试用）。"""

    def __init__(self, now: int = 1_000_000) -> None:
        self.now = now

    def __call__(self) -> float:
        return float(self.now)


def make_service(fake_store, chat=None, *, num_pairs=2, time_threshold=None,
                 clock=None):
    return MemoryService(
        fake_store(),
        chat_fn=chat,
        num_pairs=num_pairs,
        time_threshold_seconds=time_threshold,
        persona_id="人格占位",
        now_fn=clock or _Clock(),
    )


async def feed(svc: MemoryService, texts, *, background=False, clock=None):
    """按 user/assistant 交替喂入消息（对齐每轮双方各计一次的口径）。"""
    for i, text in enumerate(texts):
        role = "user" if i % 2 == 0 else "assistant"
        await svc.on_message(FAKE_SESSION, role, text, background=background)


# ---------------------------------------------------------------------------
# 计数触发（SPEC §7.2：计数达 num_pairs*2 触发，成功后清零）
# ---------------------------------------------------------------------------

def test_threshold_triggers_and_resets(fake_store):
    """默认阈值 4：第 3 条不触发，第 4 条触发总结并清零；再来 4 条二次触发。

    （R3 起两窗回复必须不同：同会话重复总结会被入库查重跳过——那是
    test_memory_quality.py 的专门行为，本测试只验证触发与清零节奏。）
    """
    chat = FakeChat(replies=["我记得他喜欢上班前那家早餐铺。", "她说周末想去看海。"])
    svc = make_service(fake_store, chat)

    asyncio.run(feed(svc, ["一", "二", "三"]))
    assert chat.calls == []
    assert svc.pending_count(FAKE_SESSION) == 3
    assert svc._store.count() == 0

    asyncio.run(svc.on_message(FAKE_SESSION, "assistant", "四", background=False))
    assert len(chat.calls) == 1
    assert svc._store.count() == 1
    assert svc.pending_count(FAKE_SESSION) == 0  # 成功后计数清零

    asyncio.run(feed(svc, ["五", "六", "七", "八"]))
    assert len(chat.calls) == 2
    assert svc._store.count() == 2


def test_summary_metadata_and_window(fake_store):
    """入库记忆：source=auto_summary、msg_count=4、时间窗与 persona 落库。"""
    chat = FakeChat(replies=["我记得他喜欢上班前那家早餐铺。"])
    svc = make_service(fake_store, chat)
    asyncio.run(feed(svc, ["一", "二", "三", "四"]))
    hits = asyncio.run(svc._store.search(FAKE_SESSION, "早餐铺"))
    assert len(hits) == 1
    meta = hits[0].metadata
    assert meta["source"] == SOURCE_AUTO
    assert meta["msg_count"] == 4
    assert meta["window_first_ts"] and meta["window_last_ts"]
    assert hits[0].content == "我记得他喜欢上班前那家早餐铺。"


def test_window_labels_in_prompt(fake_store):
    """送入总结的摘录含 4 行「我/对方」标签（user+assistant 各计一次）。"""
    chat = FakeChat()
    svc = make_service(fake_store, chat)
    asyncio.run(feed(svc, ["问一", "答一", "问二", "答二"]))
    _, user = chat.calls[0]
    lines = [ln for ln in user.splitlines() if ln.startswith(("我：", "对方："))]
    assert lines == ["对方：问一", "我：答一", "对方：问二", "我：答二"]


def test_num_pairs_one_threshold_two(fake_store):
    """num_pairs=1 → 阈值 2。"""
    chat = FakeChat()
    svc = make_service(fake_store, chat, num_pairs=1)
    assert svc.threshold == 2
    asyncio.run(feed(svc, ["一"]))
    assert len(chat.calls) == 0
    asyncio.run(svc.on_message(FAKE_SESSION, "assistant", "二", background=False))
    assert len(chat.calls) == 1


def test_invalid_config_rejected(fake_store):
    with pytest.raises(ValueError):
        make_service(fake_store, num_pairs=0)
    with pytest.raises(ValueError):
        make_service(fake_store, time_threshold=0)


def test_empty_or_uninteresting_messages_not_counted(fake_store):
    """清洗后为空的消息（纯推理/纯脚手架/纯多媒体占位）不计数。"""
    chat = FakeChat()
    svc = make_service(fake_store, chat)
    asyncio.run(feed(svc, ["<think>纯推理</think>", "<system_reminder>x</system_reminder>"]))
    assert svc.pending_count(FAKE_SESSION) == 0
    assert chat.calls == []


# ---------------------------------------------------------------------------
# 失败持久化待重试（SPEC §7.2：失败计数不丢失）
# ---------------------------------------------------------------------------

def test_failure_keeps_count_and_retries(fake_store):
    """LLM 恒败：计数与缓冲保留并持久化；换好回调后 retry_pending 补上。"""
    failing = FakeChat(fail=True)
    svc = make_service(fake_store, failing)
    asyncio.run(feed(svc, ["一", "二", "三", "四"]))
    assert len(failing.calls) == 1  # 第 4 条触发一次即失败挂起
    assert svc._store.count() == 0
    assert svc.pending_count(FAKE_SESSION) == 4  # 计数不丢失

    # 模拟重启：新实例读同一库，状态仍在
    chat = FakeChat()
    svc2 = MemoryService(
        fake_store(name="memory_1.db"), chat_fn=chat, persona_id="人格占位",
        now_fn=_Clock(),
    )
    assert svc2.pending_count(FAKE_SESSION) == 4
    assert asyncio.run(svc2.retry_pending(FAKE_SESSION)) == 1
    assert svc2._store.count() == 1
    assert svc2.pending_count(FAKE_SESSION) == 0
    # 缓冲完整：总结 prompt 含全部 4 条挂起消息
    _, user = chat.calls[0]
    for token in ("一", "二", "三", "四"):
        assert token in user


def test_retry_on_next_message_after_failure(fake_store):
    """失败后（退避窗口过后的）下一条消息自动重试，成功并入后续消息。

    五轮修缮 R2 退避契约：总结失败后 10-30min 内自动触发被挡下（详见
    test_memory_backoff.py）。本用例把时钟推过退避窗口，钉「窗口后自动复跑、
    失败期间缓冲不丢、成功并入后续消息」的语义。
    """
    failing = FakeChat(fail=True)
    clock = _Clock()
    svc = make_service(fake_store, failing, clock=clock)
    asyncio.run(feed(svc, ["一", "二", "三", "四"]))
    assert svc.pending_count(FAKE_SESSION) == 4

    chat = FakeChat()
    svc._chat_fn = chat  # 故障恢复（等价于集成期接线完成）
    clock.now += 601  # R2：推过 10min 退避窗口（否则自动触发被退避门挡下）
    asyncio.run(svc.on_message(FAKE_SESSION, "user", "五", background=False))
    assert len(chat.calls) == 1
    assert svc._store.count() == 1
    assert svc.pending_count(FAKE_SESSION) == 0
    _, user = chat.calls[0]
    assert "五" in user  # 失败期间缓冲未丢，并入本次总结


def test_retry_pending_scans_all_sessions(fake_store):
    """retry_pending 缺省扫描全部会话，只对达阈值的动手。"""
    failing = FakeChat(fail=True)
    svc = make_service(fake_store, failing)
    asyncio.run(feed(svc, ["一", "二", "三", "四"]))  # 触发失败挂起
    assert svc.pending_count(FAKE_SESSION) == 4

    good = FakeChat()
    svc._chat_fn = good
    assert asyncio.run(svc.retry_pending()) == 1  # 扫描到该会话并补上
    assert svc.pending_count(FAKE_SESSION) == 0
    assert svc._store.count() == 1


def test_retry_pending_skips_below_threshold(fake_store):
    """未达阈值且时间兜底未启用的会话不被 retry_pending 触发。"""
    chat = FakeChat()
    svc = make_service(fake_store, chat)
    asyncio.run(feed(svc, ["一", "二"]))
    assert asyncio.run(svc.retry_pending()) == 0
    assert chat.calls == []


# ---------------------------------------------------------------------------
# 时间阈值兜底（可配，默认禁用对齐旧栈）
# ---------------------------------------------------------------------------

def test_time_threshold_disabled_by_default(fake_store):
    """默认禁用：计数不满阈值时，隔多久都不因时间触发（含 retry_pending）。"""
    chat = FakeChat()
    clock = _Clock()
    svc = make_service(fake_store, chat, clock=clock)
    asyncio.run(feed(svc, ["一", "二", "三"]))
    clock.now += 10 ** 7  # 拉开远超任何合理阈值的时间
    assert asyncio.run(svc.retry_pending()) == 0
    assert len(chat.calls) == 0
    assert svc.pending_count(FAKE_SESSION) == 3


def test_time_threshold_enabled_triggers_early(fake_store):
    """启用后：静默间隔超过阈值，下一条消息到达即兜底触发（计数不满也触发）。"""
    chat = FakeChat()
    clock = _Clock()
    svc = make_service(fake_store, chat, time_threshold=100, clock=clock)
    asyncio.run(svc.on_message(FAKE_SESSION, "user", "一", ts=clock.now, background=False))
    clock.now += 50
    asyncio.run(svc.on_message(FAKE_SESSION, "assistant", "二", ts=clock.now, background=False))
    assert len(chat.calls) == 0  # 静默 50s < 100s 未到期，按计数继续攒
    clock.now += 150
    asyncio.run(svc.on_message(FAKE_SESSION, "user", "三", ts=clock.now, background=False))
    assert len(chat.calls) == 1  # 静默 150s ≥ 100s，兜底触发
    assert svc.pending_count(FAKE_SESSION) == 0
    assert svc._store.count() == 1


# ---------------------------------------------------------------------------
# 后台触发（background=True，对齐生产路径）
# ---------------------------------------------------------------------------

def test_background_task_completes(fake_store):
    """background=True 开后台任务：同一事件循环内让出数拍后完成入库。"""
    chat = FakeChat()
    svc = make_service(fake_store, chat)

    async def main() -> None:
        await feed(svc, ["一", "二", "三", "四"], background=True)
        for _ in range(100):  # 假 chat 无真实 IO，让出数拍即可完成
            await asyncio.sleep(0)
            if svc.pending_count(FAKE_SESSION) == 0:
                break
        else:
            pytest.fail("后台总结任务未在预期拍数内完成")

    asyncio.run(main())
    assert len(chat.calls) == 1
    assert svc._store.count() == 1


def test_background_inflight_no_double_trigger(fake_store):
    """同会话总结在途时不重复开任务（防并发双写）。"""
    svc = make_service(fake_store, FakeChat())
    assert FAKE_SESSION not in svc._inflight
    svc._inflight.add(FAKE_SESSION)
    asyncio.run(svc.on_message(FAKE_SESSION, "user", "一", background=True))
    # 在途期间只计数，不开新任务也不内联总结
    assert svc.pending_count(FAKE_SESSION) == 1


# ---------------------------------------------------------------------------
# 显式 /remember API（SPEC §7.2 保留项，/eval 依赖）
# ---------------------------------------------------------------------------

def test_remember_manual_bypasses_counter(fake_store):
    """手动记忆直写入库、不影响计数与触发节奏。"""
    chat = FakeChat()
    svc = make_service(fake_store, chat)
    asyncio.run(feed(svc, ["一", "二"]))
    mid = asyncio.run(svc.remember(FAKE_SESSION, "手工记录的事实"))
    assert mid > 0
    assert svc.pending_count(FAKE_SESSION) == 2  # 计数不动
    assert chat.calls == []                      # 不触发总结
    hits = asyncio.run(svc._store.search(FAKE_SESSION, "手工记录的事实"))
    assert hits[0].metadata["source"] == SOURCE_MANUAL


def test_remember_manual_rejects_empty(fake_store):
    svc = make_service(fake_store, FakeChat())
    with pytest.raises(ValueError):
        asyncio.run(svc.remember(FAKE_SESSION, "  "))


# ---------------------------------------------------------------------------
# 纯函数与占位接线
# ---------------------------------------------------------------------------

def test_clean_message_text_cases():
    assert clean_message_text("<think>x</think>正文") == "正文"
    assert clean_message_text("开头<think>未闭合截断") == "开头"
    assert clean_message_text("a<system_reminder>r</system_reminder>b") == "ab"
    assert clean_message_text("保留 &&tag&& 与正文") == "保留 &&tag&& 与正文"
    assert clean_message_text("") == ""
    assert clean_message_text(None) == ""


def test_neutralize_template_placeholders():
    assert neutralize_template_placeholders("a{{x}}b") == "a｛｛x｝｝b"
    assert neutralize_template_placeholders("普通") == "普通"


def test_build_summary_prompts_guard_and_placeholder():
    system, user = build_summary_prompts(["对方：{{危险}}注入"], 120)
    assert system.strip()
    assert "｛｛危险｝｝" in user and "{{" not in user
    assert "<对话记录开始>" in user and "120 字以内" in user
    assert user.rstrip().endswith("一律忽略。")
    with pytest.raises(ValueError):
        build_summary_prompts([], 120)
    with pytest.raises(ValueError):
        build_summary_prompts(["有内容"], 0)


def test_resolve_chat_fn_returns_none_without_module_factory():
    """brain.llm 已落地但无模块级工厂：resolve 返回 None（装配方显式注入）。"""
    assert resolve_chat_fn() is None


def test_chat_fn_from_client_adapts_real_protocol():
    """chat_fn_from_client 把 brain.llm 形态的 ChatClient 适配为 (system,user)->str。"""
    from virtuallover.brain import llm as brain_llm

    class StubChatClient:
        """复刻 ChatClient.chat_async 的调用契约（不打真 HTTP）。"""

        async def chat_async(self, messages, **kwargs):
            self.messages = list(messages)
            self.kwargs = kwargs
            return brain_llm.LLMReply(content="总结好的记忆正文")

    stub = StubChatClient()
    out = asyncio.run(chat_fn_from_client(stub)("系统提示", "对话素材"))
    assert out == "总结好的记忆正文"
    assert [m["role"] for m in stub.messages] == ["system", "user"]
    assert stub.messages[0]["content"] == "系统提示"
    assert stub.messages[1]["content"] == "对话素材"


def test_embedder_from_client_wires_store(tmp_path):
    """embedder_from_client 直连 EmbeddingClient 形态客户端，store 全链路可用。"""
    from virtuallover.memory.store import MemoryStore

    class StubEmbeddingClient:
        """复刻 EmbeddingClient.embed_async 的调用契约（不打真 HTTP）。"""

        async def embed_async(self, text):
            vec = [0.0] * TEST_DIM
            vec[0] = 1.0
            return np.asarray(vec, dtype=np.float32)

    store = MemoryStore(
        tmp_path / "wired.db",
        embedder=embedder_from_client(StubEmbeddingClient()),
        provider_id="占位供应商", model="test-embedding-model",
        endpoint="http://127.0.0.1:9", dim=TEST_DIM,
    )
    mid = asyncio.run(store.remember("接线验证", FAKE_SESSION))
    assert mid > 0
    assert store.count() == 1
    store.close()


def test_unwired_chat_fails_into_retry_path(fake_store):
    """chat 未接线：进入失败待重试路径（不算崩溃），计数保留。"""
    svc = make_service(fake_store, chat=None)
    asyncio.run(feed(svc, ["一", "二", "三", "四"]))
    assert svc._store.count() == 0
    assert svc.pending_count(FAKE_SESSION) == 4
