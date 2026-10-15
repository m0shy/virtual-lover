# -*- coding: utf-8 -*-
"""大脑真探活契约测试（五轮修缮 R2 任务 2/8：last_error + embedding 熔断）。

背景实证（vl.log:4420）：ark 欠费 403 → ``LLMError: chat HTTP 403：
error.Forbidden/AccountOverdueError``，她哑 8 小时而控制台可达性探针全绿。
本文件钉死两条链路：

1. ChatClient 真探活观测面（任务 2）：
   - 调用最终失败（重试耗尽）→ last_error=「HTTP code+摘要串」+ 时刻落笔；
   - fallback 链的成败也归主客户端观测面（控制台只读主客户端）；
   - 成功 → last_error 清零（探活语义「成功清零」）；
   - recent_failure(600s)：窗口内有失败才吐原因串（brain 行黄灯判定）。
2. EmbeddingClient 熔断（任务 8）：连续 3 次调用级失败 → 10 分钟跳过请求
   （假钟判定，不真睡）；期间请求在咽喉处直接抛（不再触网）；成功清零复位；
   控制台 vl_status 消费 breaker_open 渲染「记忆检索降级中」黄灯（端点侧
   契约在 test_recent_errors.py）。

测试全程注入假 urlopen/假钟/假 sleep——绝不发真实网络请求、绝不含真实 key。
运行：python -m pytest virtuallover/tests/test_brain_health.py -q
"""

from __future__ import annotations

import asyncio
import json
import types
import urllib.error
from types import SimpleNamespace

import numpy as np

from virtuallover.brain.llm import (
    ChatClient, EmbeddingClient, EmbeddingDimError, LLMError,
    EMBED_BREAKER_OPEN_SECONDS, EMBED_BREAKER_THRESHOLD,
    RECENT_FAILURE_WINDOW_SECONDS,
)


# ---------------------------------------------------------------------------
# 假件：可控 urlopen（返回 HTTP 响应或抛 HTTPError）
# ---------------------------------------------------------------------------

class FakeUrlopen:
    """假 urlopen：按脚本逐次响应；记 URL 供「熔断期零触网」断言。"""

    def __init__(self, script: "list") -> None:
        # script 元素：("ok", body_dict) | ("status", code, body_dict)
        # | ("http_error", code, body_dict) | ("network",)
        self.script = list(script)
        self.urls: "list[str]" = []

    def __call__(self, request, timeout=None):
        self.urls.append(request.full_url)
        action = self.script.pop(0) if self.script else ("ok", {"choices": []})
        if action[0] == "ok":
            return _FakeResponse(200, action[1])
        if action[0] == "status":
            return _FakeResponse(action[1], action[2])
        if action[0] == "http_error":
            raise urllib.error.HTTPError(
                request.full_url, action[1], "mock", hdrs=None,
                fp=_BytesIO(json.dumps(action[2]).encode("utf-8")))
        raise OSError("连接拒绝（mock）")


class _BytesIO:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:  # HTTPError.fp 会被 with 关闭，需要 no-op close
        pass


class _FakeHeaders:
    def __init__(self, retry_after=None) -> None:
        self._ra = retry_after

    def get(self, name):
        return self._ra if name == "Retry-After" else None


class _FakeResponse:
    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self._body = json.dumps(body).encode("utf-8")
        self.headers = _FakeHeaders()

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeClock:
    """手动推进的假钟（秒）。"""

    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


def _cfg(**kwargs) -> SimpleNamespace:
    base = dict(api_base="https://ark.example/api/v3", api_key="sk-占位-绝不真实",
                model="test-model", timeout=1, max_retries=0, retry_on_429=False)
    base.update(kwargs)
    return SimpleNamespace(**base)


_OK_BODY = {"choices": [{"message": {"content": "好"}, "finish_reason": "stop"}]}
_ERR_403 = {"error": {"code": "AccountOverdueError", "type": "Forbidden"}}


# ---------------------------------------------------------------------------
# 1) ChatClient 真探活观测面
# ---------------------------------------------------------------------------

def test_chat_failure_records_last_error_with_http_code():
    """最终失败：last_error 记 HTTP code + 摘要串（无密钥无正文）。"""
    opener = FakeUrlopen([("http_error", 403, _ERR_403)])
    client = ChatClient(_cfg(), urlopen=opener, sleep=lambda s: None)
    message = ""
    try:
        client.chat([{"role": "user", "content": "在吗"}])
        raise AssertionError("403 应抛 LLMError")
    except LLMError as exc:
        message = str(exc)
        assert "403" in message and "AccountOverdueError" in message
    assert client.last_error == message
    assert client.last_error_ts > 0
    assert "403" in client.recent_failure()
    # 观测串绝不带 key（_error_summary 摘要契约的端到端复核）
    assert "sk-占位" not in client.last_error


def test_chat_success_clears_last_error():
    """成功清零：先失败后成功 → last_error 清空、last_ok_ts 落笔。"""
    opener = FakeUrlopen([("http_error", 403, _ERR_403), ("ok", _OK_BODY)])
    clock = FakeClock()
    client = ChatClient(_cfg(), urlopen=opener, sleep=lambda s: None, clock=clock)
    try:
        client.chat([{"role": "user", "content": "在吗"}])
    except LLMError:
        pass
    assert client.recent_failure()
    clock.advance(1)
    reply = client.chat([{"role": "user", "content": "在吗"}])
    assert reply.content == "好"
    assert client.last_error == ""
    assert client.last_error_ts == 0.0
    assert client.last_ok_ts == clock.t
    assert client.recent_failure() == ""


def test_recent_failure_expires_after_window():
    """10 分钟窗口外失败不再算「最近」（黄灯自熄，等下一次调用刷新）。"""
    opener = FakeUrlopen([("http_error", 403, _ERR_403)])
    clock = FakeClock()
    client = ChatClient(_cfg(), urlopen=opener, sleep=lambda s: None, clock=clock)
    try:
        client.chat([{"role": "user", "content": "在吗"}])
    except LLMError:
        pass
    assert client.recent_failure()
    clock.advance(RECENT_FAILURE_WINDOW_SECONDS + 1)
    assert client.recent_failure() == ""
    assert client.last_error != ""  # 痕迹仍在，只是不再「最近」


def test_fallback_success_records_ok_on_primary():
    """fallback 救回：主客户端观测面记成功（控制台只读主客户端，必须归一）。"""
    primary = ChatClient(_cfg(), urlopen=FakeUrlopen([("http_error", 403, _ERR_403)]),
                         sleep=lambda s: None)
    backup = ChatClient(_cfg(), urlopen=FakeUrlopen([("ok", _OK_BODY)]),
                        sleep=lambda s: None, provider_id="chat-fallback")
    primary.fallbacks = [backup]
    reply = primary.chat([{"role": "user", "content": "在吗"}])
    assert reply.content == "好" and reply.provider_id == "chat-fallback"
    assert primary.last_error == "" and primary.last_ok_ts > 0


def test_fallback_failure_records_fallback_error_on_primary():
    """fallback 也失败：主客户端记录时间上最后一次失败（「最近」的语义）。"""
    primary = ChatClient(_cfg(), urlopen=FakeUrlopen([("http_error", 403, _ERR_403)]),
                         sleep=lambda s: None, clock=FakeClock())
    backup = ChatClient(_cfg(model="backup-model"),
                        urlopen=FakeUrlopen([("network",)]),
                        sleep=lambda s: None, clock=FakeClock())
    primary.fallbacks = [backup]
    try:
        primary.chat([{"role": "user", "content": "在吗"}])
        raise AssertionError("全链失败应抛")
    except LLMError:
        pass
    # 观测面持有「最近一次」失败 = fallback 的网络错误（时间序最后）；
    # 403 是先发生的旧失败，已被更近的失败覆盖——这正是「最近调用失败」口径
    assert "网络错误" in primary.recent_failure()
    assert primary.last_error_ts > 0


# ---------------------------------------------------------------------------
# 2) EmbeddingClient 熔断（3 败 → 10min 跳过 → 半开）
# ---------------------------------------------------------------------------

def _emb_cfg(**kwargs) -> SimpleNamespace:
    base = dict(api_base="https://ark.example/api/v3", api_key="sk-占位-绝不真实",
                model="test-emb", dim=4, timeout=1, max_retries=0)
    base.update(kwargs)
    return SimpleNamespace(**base)


_OK_EMB = {"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]}]}
_ERR_EMB_403 = {"error": {"code": "AccountOverdueError", "type": "Forbidden"}}


def test_embedding_breaker_opens_after_three_consecutive_failures():
    """连续 3 次调用级失败 → 熔断开路；第 4 次调用在咽喉处直接抛、零触网。"""
    opener = FakeUrlopen([("http_error", 403, _ERR_EMB_403)] * EMBED_BREAKER_THRESHOLD)
    clock = FakeClock()
    client = EmbeddingClient(_emb_cfg(), urlopen=opener, sleep=lambda s: None,
                             cache_max_bytes=0, clock=clock)
    for _ in range(EMBED_BREAKER_THRESHOLD):
        try:
            client.embed("一句要记的话")
        except LLMError:
            pass
    assert client.fail_streak == EMBED_BREAKER_THRESHOLD
    assert client.breaker_open() is True
    urls_after_open = len(opener.urls)
    try:
        client.embed("又一句")
        raise AssertionError("熔断期应直接抛、不触网")
    except LLMError as exc:
        assert "熔断" in str(exc)
    assert len(opener.urls) == urls_after_open, "熔断期不得发出任何 HTTP 请求"


def test_embedding_breaker_half_open_after_window_via_fake_clock():
    """假钟推过 10 分钟 → 半开：下一次请求真实放行，成功即复位。"""
    opener = FakeUrlopen([("http_error", 403, _ERR_EMB_403)] * EMBED_BREAKER_THRESHOLD
                         + [("ok", _OK_EMB)])
    clock = FakeClock()
    client = EmbeddingClient(_emb_cfg(), urlopen=opener, sleep=lambda s: None,
                             cache_max_bytes=0, clock=clock)
    for _ in range(EMBED_BREAKER_THRESHOLD):
        try:
            client.embed("一句话")
        except LLMError:
            pass
    assert client.breaker_open()
    clock.advance(EMBED_BREAKER_OPEN_SECONDS + 1)
    assert client.breaker_open() is False, "窗口过后应自然半开（时钟判定）"
    vec = client.embed("半开后第一句")
    assert np.allclose(vec, [0.1, 0.2, 0.3, 0.4], atol=1e-6)
    assert client.fail_streak == 0, "成功清零（连败复位）"


def test_embedding_success_resets_streak_before_threshold():
    """偶发失败（未达 3 次）后成功 → 连败清零，不进熔断。"""
    opener = FakeUrlopen([("http_error", 500, _ERR_EMB_403), ("ok", _OK_EMB)])
    client = EmbeddingClient(_emb_cfg(), urlopen=opener, sleep=lambda s: None,
                             cache_max_bytes=0, clock=FakeClock())
    try:
        client.embed("第一句")
    except LLMError:
        pass
    assert client.fail_streak == 1 and not client.breaker_open()
    client.embed("第二句")
    assert client.fail_streak == 0


def test_embedding_breaker_covers_batch_path():
    """embed_batch 走同一咽喉：熔断期批量请求同样零触网直接抛。"""
    opener = FakeUrlopen([("http_error", 403, _ERR_EMB_403)] * EMBED_BREAKER_THRESHOLD)
    client = EmbeddingClient(_emb_cfg(), urlopen=opener, sleep=lambda s: None,
                             cache_max_bytes=0, clock=FakeClock())
    for _ in range(EMBED_BREAKER_THRESHOLD):
        try:
            client.embed("一句话")
        except LLMError:
            pass
    urls_before = len(opener.urls)
    try:
        client.embed_batch(["甲", "乙"])
        raise AssertionError("批量路径也应被熔断拦下")
    except LLMError as exc:
        assert "熔断" in str(exc)
    assert len(opener.urls) == urls_before


def test_embedding_dim_error_counts_as_failure():
    """维度不符（EmbeddingDimError⊂LLMError）同样喂熔断计数——防坏上游拖垮检索。"""
    bad = {"data": [{"embedding": [0.1, 0.2]}]}  # 2 维 ≠ dim=4
    opener = FakeUrlopen([("status", 200, bad)] * (EMBED_BREAKER_THRESHOLD + 1))
    client = EmbeddingClient(_emb_cfg(), urlopen=opener, sleep=lambda s: None,
                             cache_max_bytes=0, clock=FakeClock())
    for _ in range(EMBED_BREAKER_THRESHOLD):
        try:
            client.embed("一句话")
        except EmbeddingDimError:
            pass
    assert client.breaker_open() is True


def test_console_ping_reports_failure_and_success():
    """测她一句话（任务 2 的控制台侧）：真 1-token 对话的成败契约。"""
    from virtuallover.console.core import brain_ping_payload

    class _OkClient:
        async def chat_async(self, messages, **kwargs):
            assert kwargs.get("max_tokens") == 1, "探针契约：1-token 对话"
            return SimpleNamespace(content="好", provider_id="chat")

    class _DeadClient:
        async def chat_async(self, messages, **kwargs):
            raise LLMError("chat HTTP 403：error.Forbidden/AccountOverdueError",
                           status=403)

    ok = brain_ping_payload(types.SimpleNamespace(chat_client=_OkClient()))
    assert ok["ok"] is True and ok["reply"] == "好"

    dead = brain_ping_payload(types.SimpleNamespace(chat_client=_DeadClient()))
    assert dead["ok"] is False and "403" in dead["error"]

    none = brain_ping_payload(types.SimpleNamespace(chat_client=None))
    assert none["ok"] is False and "未装配" in none["error"]


# 事件循环冒烟：确认包内 asyncio 依赖无误（brain_ping 的 async 本体）
def test_brain_ping_async_smoke():
    from virtuallover.console.core import brain_ping_async

    class _Echo:
        async def chat_async(self, messages, **kwargs):
            return SimpleNamespace(content="好", provider_id="chat")

    result = asyncio.run(brain_ping_async(types.SimpleNamespace(chat_client=_Echo())))
    assert result["ok"] is True
