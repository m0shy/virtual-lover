# -*- coding: utf-8 -*-
"""brain/llm.py 契约测试：本地线程 HTTP server 模拟 OpenAI 兼容接口（SPEC §4）。

覆盖点：
- chat 正常路径：Authorization Bearer、请求体扁平化、content 双形态（字符串与
  分段列表）扁平化、reasoning_content 不进 content；
- think 处理：assistant 历史 think 段与 <think> 标签剥离不回传；
- tool_calls 容错忽略（返回文本 + 记警告）；
- 重试：500 指数退避（base 1s/2s/4s）、429 尊重 Retry-After、429 可关；
- fallback 列表：主客户端重试耗尽后依次兜底；
- 日志打码：caplog 全量文本不出现 api_key 原文与用户消息正文；
- embedding：请求体、2048 维 float32 向量、维度不符抛 EmbeddingDimError、503 重试；
- O6：prompt_cache_key 进请求体、usage cached_tokens 日志带 key 关联；
- O7：embedding LRU（实例级/键四元组/失败不缓存/容量淘汰/同文本单飞/批量命中拆分）。

测试服务端用 http.server（标准库）+ 脚本队列：每个用例注入一系列
(status, headers, body) 响应脚本，服务端按序弹出并记录收到的请求。
不依赖任何外部网络与真实密钥（key 均为测试占位值）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

# sys.path 引导：virtuallover/ 是 namespace 包，仓库根必须可导入。
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from virtuallover.brain.llm import (  # noqa: E402
    ChatClient,
    EmbeddingClient,
    EmbeddingDimError,
    LLMError,
    content_to_text,
    mask_secrets,
    strip_assistant_think,
    strip_think_tags,
)

# 测试占位密钥与占位正文标记：断言它们绝不出现在日志里
TEST_KEY = "sk-test-placeholder-key"
USER_BODY_MARKER = "USER-PRIVATE-BODY-MARKER"

CHAT_PATH = "/v1/chat/completions"
EMBED_PATH = "/v1/embeddings"


# ---------------------------------------------------------------------------
# 脚本化 mock server
# ---------------------------------------------------------------------------


class _ScriptedHandler(BaseHTTPRequestHandler):
    """按 server.scripts 队列逐个弹响应；请求（头+体）记录进 server.requests。"""

    def _read_request(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {"_raw": raw.decode("utf-8", errors="replace")}
        self.server.requests.append(
            {
                "path": self.path,
                "headers": {k: v for k, v in self.headers.items()},
                "body": body,
            }
        )

    def _send_scripted(self) -> None:
        if self.server.scripts:
            script = self.server.scripts.pop(0)
        else:  # 脚本耗尽：按 500 处理，让用例显式感知
            script = {"status": 500, "body": {"error": "script exhausted"}}
        payload = json.dumps(script.get("body", {}), ensure_ascii=False).encode("utf-8")
        self.send_response(int(script.get("status", 200)))
        for key, value in (script.get("headers") or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 —— http.server 命名约定
        self._read_request()
        self._send_scripted()

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # 静默默认访问日志


def _make_server(scripts: list[dict]) -> tuple[str, ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ScriptedHandler)
    server.daemon_threads = True
    server.scripts = list(scripts)  # type: ignore[attr-defined]
    server.requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{server.server_address[1]}/v1", server


@pytest.fixture
def llm_server():
    """启动一个空脚本的 mock server，用例内按需填脚本；结束自动关停。"""
    base, server = _make_server([])
    yield base, server
    server.shutdown()
    server.server_close()


def _chat_cfg(base: str, **overrides) -> SimpleNamespace:
    values = {
        "api_base": base,
        "api_key": TEST_KEY,
        "model": "test-model",
        "timeout": 5,
        "max_retries": 3,
        "retry_on_429": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _embed_cfg(base: str, **overrides) -> SimpleNamespace:
    values = {
        "api_base": base,
        "api_key": TEST_KEY,
        "model": "test-embed",
        "dim": 2048,
        "timeout": 5,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _chat_ok(content, reasoning=None, tool_calls=None, finish="stop") -> dict:
    message: dict = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "status": 200,
        "body": {
            "choices": [{"message": message, "finish_reason": finish}],
        },
    }


def _embed_ok(dim: int = 2048) -> dict:
    return {
        "status": 200,
        "body": {"data": [{"embedding": [0.1] * dim}], "model": "test-embed"},
    }


class _RecordingSleep:
    """记录退避调用序列的 sleep 替身。"""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


# ---------------------------------------------------------------------------
# chat：正常路径与内容规整
# ---------------------------------------------------------------------------


def test_chat_success_sends_bearer_and_flat_messages(llm_server):
    base, server = llm_server
    server.scripts.append(_chat_ok("宝宝好呀~"))
    client = ChatClient(_chat_cfg(base))
    reply = client.chat([{"role": "user", "content": "在吗"}])

    assert reply.content == "宝宝好呀~"
    assert reply.reasoning is None
    assert reply.finish_reason == "stop"
    assert reply.provider_id == "chat"
    assert reply.attempts == 1

    request = server.requests[0]
    assert request["path"] == CHAT_PATH
    assert request["headers"]["Authorization"] == f"Bearer {TEST_KEY}"
    assert request["body"]["model"] == "test-model"
    assert request["body"]["stream"] is False
    assert request["body"]["messages"] == [{"role": "user", "content": "在吗"}]


def test_chat_accepts_dual_form_content_on_both_sides(llm_server):
    """请求与响应两侧都接受分段列表 content（SPEC §4 双形态）。"""
    base, server = llm_server
    # 响应：分段列表（text + think 段）→ 只扁平化 text
    server.scripts.append(
        _chat_ok(
            [
                {"type": "think", "think": "内心戏不该出口"},
                {"type": "text", "text": "分段"},
                {"type": "text", "text": "回复"},
            ],
            reasoning="reasoning 不入库不回发",
        )
    )
    client = ChatClient(_chat_cfg(base))
    # 请求：assistant 历史 content 为分段列表（含 think 段与 reasoning_content）
    history = [
        {"role": "user", "content": USER_BODY_MARKER},
        {
            "role": "assistant",
            "content": [
                {"type": "think", "think": "旧内心戏"},
                {"type": "text", "text": "旧回复"},
            ],
            "reasoning_content": "旧 reasoning",
        },
    ]
    reply = client.chat(history)

    assert reply.content == "分段回复"
    assert reply.reasoning == "reasoning 不入库不回发"

    sent_messages = server.requests[0]["body"]["messages"]
    # assistant 历史被扁平化为纯文本且 think/reasoning 均已剥离
    assert sent_messages[1]["content"] == "旧回复"
    assert "旧内心戏" not in json.dumps(sent_messages, ensure_ascii=False)
    assert "旧 reasoning" not in json.dumps(sent_messages, ensure_ascii=False)
    # content 已统一为字符串形态
    assert all(isinstance(m["content"], str) for m in sent_messages)


def test_chat_strips_think_tags_from_assistant_history(llm_server):
    base, server = llm_server
    server.scripts.append(_chat_ok("好的"))
    client = ChatClient(_chat_cfg(base))
    client.chat(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "<think>盘算一下</think>回答正文"},
        ]
    )
    sent = server.requests[0]["body"]["messages"]
    assert sent[1]["content"] == "回答正文"
    assert "<think>" not in sent[1]["content"]


def test_chat_tool_calls_tolerated_and_logged(llm_server, caplog):
    base, server = llm_server
    server.scripts.append(
        _chat_ok(
            "文本照常返回",
            tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "x"}}],
        )
    )
    client = ChatClient(_chat_cfg(base))
    with caplog.at_level(logging.WARNING, logger="virtuallover.brain.llm"):
        reply = client.chat([{"role": "user", "content": "hi"}])
    assert reply.content == "文本照常返回"
    assert any("tool_calls" in record.message for record in caplog.records)


def test_content_to_text_edge_cases():
    """双形态扁平化的边界：None / 单段 dict / 杂项段 / 段对象。"""
    assert content_to_text(None) == ""
    assert content_to_text({"type": "text", "text": "单段"}) == "单段"
    assert content_to_text({"type": "image_url", "image_url": "x"}) == ""
    assert content_to_text([{"type": "text", "text": "a"}, "裸串"]) == "a裸串"
    assert strip_think_tags("无标签") == "无标签"
    assert strip_think_tags("残<think>未闭合") == "残"
    assert strip_assistant_think([{"role": "user", "content": "<think>保留</think>用户的话"}])[
        0
    ]["content"] == "<think>保留</think>用户的话"


def test_mask_secrets_masks_sensitive_keys():
    masked = mask_secrets(
        {"api_key": "K1", "Authorization": "Bearer K2", "nested": {"access_token": "K3", "plain": "ok"}}
    )
    assert masked == {"api_key": "<masked>", "Authorization": "<masked>",
                      "nested": {"access_token": "<masked>", "plain": "ok"}}


# ---------------------------------------------------------------------------
# chat：重试 / 429 / fallback / 日志打码
# ---------------------------------------------------------------------------


def test_chat_retries_500_with_exponential_backoff(llm_server):
    base, server = llm_server
    server.scripts.extend(
        [
            {"status": 500, "body": {"error": "boom-1"}},
            {"status": 500, "body": {"error": "boom-2"}},
            _chat_ok("第三次成功"),
        ]
    )
    sleep_recorder = _RecordingSleep()
    client = ChatClient(_chat_cfg(base), sleep=sleep_recorder)
    reply = client.chat([{"role": "user", "content": "hi"}])

    assert reply.content == "第三次成功"
    assert reply.attempts == 3
    assert sleep_recorder.calls == [1.0, 2.0]  # base * 2^(retry_index)
    assert len(server.requests) == 3


def test_chat_429_respects_retry_after(llm_server):
    base, server = llm_server
    server.scripts.extend(
        [
            {"status": 429, "headers": {"Retry-After": "2.5"}, "body": {"error": "rate"}},
            _chat_ok("恢复了"),
        ]
    )
    sleep_recorder = _RecordingSleep()
    client = ChatClient(_chat_cfg(base), sleep=sleep_recorder)
    reply = client.chat([{"role": "user", "content": "hi"}])

    assert reply.content == "恢复了"
    assert reply.attempts == 2
    assert sleep_recorder.calls == [2.5]


def test_chat_429_can_be_disabled(llm_server):
    base, server = llm_server
    server.scripts.append({"status": 429, "body": {"error": "rate"}})
    client = ChatClient(_chat_cfg(base, retry_on_429=False), sleep=_RecordingSleep())
    with pytest.raises(LLMError) as excinfo:
        client.chat([{"role": "user", "content": "hi"}])
    assert excinfo.value.status == 429
    assert excinfo.value.retriable is False
    assert len(server.requests) == 1  # 未重试


def test_chat_retries_exhausted_raises(llm_server):
    base, server = llm_server
    for i in range(4):  # max_retries=3 → 首次 + 3 重试 = 4 次
        server.scripts.append({"status": 503, "body": {"error": f"down-{i}"}})
    client = ChatClient(_chat_cfg(base), sleep=_RecordingSleep())
    with pytest.raises(LLMError) as excinfo:
        client.chat([{"role": "user", "content": "hi"}])
    assert excinfo.value.status == 503
    assert len(server.requests) == 4


def test_chat_fallback_chain_after_primary_exhausted(llm_server):
    """主客户端重试耗尽 → fallback 列表依次兜底（SPEC §4）。"""
    base, server = llm_server
    # 主客户端：4 次全 503
    for i in range(4):
        server.scripts.append({"status": 503, "body": {"error": f"primary-{i}"}})
    # fallback 客户端（同 server，不同 model 路径不影响）：第二次尝试成功
    server.scripts.append({"status": 502, "body": {"error": "fallback-first"}})
    server.scripts.append(_chat_ok("fallback 接住了"))

    primary = ChatClient(_chat_cfg(base), provider_id="primary", sleep=_RecordingSleep())
    fallback = ChatClient(_chat_cfg(base), provider_id="backup", sleep=_RecordingSleep())
    primary.fallbacks.append(fallback)

    reply = primary.chat([{"role": "user", "content": "hi"}])
    assert reply.content == "fallback 接住了"
    assert reply.provider_id == "backup"
    # 主 4 次 + fallback 2 次
    assert len(server.requests) == 6


def test_chat_should_retry_false_stops_retry_early(llm_server):
    """R5 被弃轮止损：should_retry=False → 失败一次即抛，零退避零重试。

    修前被弃代会带着 1s/2s/4s 退避打满重试额度（本地大脑多 slot 被僵尸
    请求占满，新代挤不进 → burst 全静默，vl.log 00:57:55 实测）。
    lambda:False 模拟真实态：请求在途期间轮已被更新代取代。"""
    base, server = llm_server
    for i in range(4):  # 全 500：若无止损将重试 3 次
        server.scripts.append({"status": 500, "body": {"error": f"boom-{i}"}})
    sleep_recorder = _RecordingSleep()
    client = ChatClient(_chat_cfg(base), sleep=sleep_recorder)

    with pytest.raises(LLMError) as excinfo:
        client.chat([{"role": "user", "content": "hi"}],
                    should_retry=lambda: False)

    assert excinfo.value.status == 500
    assert len(server.requests) == 1      # 只发了 1 次，没有重试
    assert sleep_recorder.calls == []     # 没进退避等待


def test_chat_should_retry_false_gates_fallback_switch(llm_server):
    """R5 被弃轮止损：主客户端失败且调用方已放弃 → 不再切 fallback。"""
    base, server = llm_server
    server.scripts.append({"status": 503, "body": {"error": "primary-down"}})

    primary = ChatClient(_chat_cfg(base), provider_id="primary", sleep=_RecordingSleep())
    fallback = ChatClient(_chat_cfg(base), provider_id="backup", sleep=_RecordingSleep())
    primary.fallbacks.append(fallback)

    with pytest.raises(LLMError):
        primary.chat([{"role": "user", "content": "hi"}],
                     should_retry=lambda: False)
    # 主 1 次（不可重试的 503 直接抛）+ fallback 0 次（调用方已放弃）
    assert len(server.requests) == 1


def test_chat_should_retry_none_keeps_legacy_behavior(llm_server):
    """不传 should_retry（默认 None）时重试/fallback 行为与修前完全一致。"""
    base, server = llm_server
    server.scripts.extend(
        [
            {"status": 500, "body": {"error": "boom-1"}},
            _chat_ok("第二次成功"),
        ]
    )
    client = ChatClient(_chat_cfg(base), sleep=_RecordingSleep())
    reply = client.chat([{"role": "user", "content": "hi"}])
    assert reply.content == "第二次成功"
    assert reply.attempts == 2
    assert len(server.requests) == 2


def test_chat_error_message_masks_sensitive_body(llm_server):
    """错误日志只取结构字段：密钥原文与错误 message 自由文本都不出现。"""
    base, server = llm_server
    server.scripts.append(
        {"status": 401, "body": {"error": {"api_key": TEST_KEY, "message": "bad key"}}}
    )
    client = ChatClient(_chat_cfg(base))
    with pytest.raises(LLMError) as excinfo:
        client.chat([{"role": "user", "content": "hi"}])
    message = str(excinfo.value)
    assert TEST_KEY not in message
    assert "bad key" not in message  # message 自由文本不进日志（可能内嵌密钥/正文）
    assert "401" in message
    assert "error" in message


def test_chat_logs_never_contain_key_or_user_body(llm_server, caplog):
    """日志打码：任何级别日志不出现 api_key 原文与用户消息正文（SPEC §4）。"""
    base, server = llm_server
    server.scripts.extend(
        [
            {"status": 500, "body": {"error": {"echo": USER_BODY_MARKER}}},
            _chat_ok("回复正文"),
        ]
    )
    client = ChatClient(_chat_cfg(base), sleep=_RecordingSleep())
    with caplog.at_level(logging.DEBUG, logger="virtuallover.brain.llm"):
        client.chat([{"role": "user", "content": USER_BODY_MARKER}])
    log_text = caplog.text
    assert TEST_KEY not in log_text
    assert USER_BODY_MARKER not in log_text
    # Authorization 头明文也不进日志
    assert f"Bearer {TEST_KEY}" not in log_text


def test_chat_async_wrapper(llm_server):
    base, server = llm_server
    server.scripts.append(_chat_ok("异步也行"))
    client = ChatClient(_chat_cfg(base))
    reply = asyncio.run(client.chat_async([{"role": "user", "content": "hi"}]))
    assert reply.content == "异步也行"
    assert reply.attempts == 1


# ---------------------------------------------------------------------------
# embedding 客户端
# ---------------------------------------------------------------------------


def test_embedding_success_returns_float32_vector(llm_server):
    base, server = llm_server
    server.scripts.append(_embed_ok(2048))
    client = EmbeddingClient(_embed_cfg(base))
    vector = client.embed("一段查询文本")

    assert vector.shape == (2048,)
    assert vector.dtype.name == "float32"
    request = server.requests[0]
    assert request["path"] == EMBED_PATH
    assert request["headers"]["Authorization"] == f"Bearer {TEST_KEY}"
    assert request["body"]["model"] == "test-embed"
    assert request["body"]["input"] == ["一段查询文本"]


def test_embedding_dim_mismatch_raises(llm_server):
    base, server = llm_server
    server.scripts.append(_embed_ok(1024))
    client = EmbeddingClient(_embed_cfg(base))
    with pytest.raises(EmbeddingDimError):
        client.embed("维度不符")
    assert len(server.requests) == 1  # 维度错误不可重试


def test_embedding_retries_on_503(llm_server):
    base, server = llm_server
    server.scripts.extend(
        [{"status": 503, "body": {"error": "busy"}}, _embed_ok(2048)]
    )
    client = EmbeddingClient(_embed_cfg(base), sleep=_RecordingSleep())
    vector = client.embed("重试后成功")
    assert vector.shape == (2048,)
    assert len(server.requests) == 2


def test_embedding_batch_and_async(llm_server):
    base, server = llm_server
    # 批量：两条输入对应两条 data
    server.scripts.append(
        {"status": 200, "body": {"data": [{"embedding": [0.5] * 2048}, {"embedding": [0.25] * 2048}]}}
    )
    client = EmbeddingClient(_embed_cfg(base))
    matrix = client.embed_batch(["a", "b"])
    assert matrix.shape == (2, 2048)
    assert matrix[1][0] == 0.25

    # 异步单条：独立一次请求
    server.scripts.append(_embed_ok(2048))
    single = asyncio.run(client.embed_async("c"))
    assert single.shape == (2048,)
    assert server.requests[-1]["body"]["input"] == ["c"]


# ---------------------------------------------------------------------------
# PERF_SPEC §3-O6：prompt_cache_key + usage cached_tokens 日志
# ---------------------------------------------------------------------------

def test_prompt_cache_key_reaches_request_body(llm_server):
    """O6：调用方 extra_body 的 prompt_cache_key 原样进入请求体顶层。"""
    base, server = llm_server
    server.scripts.append(_chat_ok("好的"))
    client = ChatClient(_chat_cfg(base))
    client.chat(
        [{"role": "user", "content": "在吗"}],
        extra_body={"prompt_cache_key": "测试人格:FriendMessage:10001"},
    )
    body = server.requests[0]["body"]
    assert body["prompt_cache_key"] == "测试人格:FriendMessage:10001"


def test_usage_cached_tokens_logged_with_cache_key(llm_server, caplog):
    """O6：usage cached_tokens 与 prompt_cache_key 同行落日志（验收对账靠它）。"""
    base, server = llm_server
    ok = _chat_ok("嗯")
    ok["body"]["usage"] = {
        "prompt_tokens": 900,
        "completion_tokens": 12,
        "prompt_tokens_details": {"cached_tokens": 777},
    }
    server.scripts.append(ok)
    client = ChatClient(_chat_cfg(base))
    with caplog.at_level(logging.INFO, logger="virtuallover.brain.llm"):
        client.chat(
            [{"role": "user", "content": "hi"}],
            extra_body={"prompt_cache_key": "sess-x"},
        )
    line = [r for r in caplog.records if "cached_tokens" in r.getMessage()]
    assert line, "usage 日志缺失"
    msg = line[-1].getMessage()
    assert "cached_tokens=777" in msg
    assert "prompt_cache_key=sess-x" in msg


def test_usage_log_dash_without_cache_key_and_no_usage(llm_server, caplog):
    """无 cache_key 打 "-"；响应缺 usage 块时不打 usage 行（绝不报错）。"""
    base, server = llm_server
    server.scripts.append(_chat_ok("嗯"))  # 无 usage 字段
    client = ChatClient(_chat_cfg(base))
    with caplog.at_level(logging.INFO, logger="virtuallover.brain.llm"):
        client.chat([{"role": "user", "content": "hi"}])
    assert not [r for r in caplog.records if "cached_tokens" in r.getMessage()]


# ---------------------------------------------------------------------------
# PERF_SPEC §3-O7：embedding 向量 LRU（实例级 / 键四元组 / 失败不缓存 /
# 容量淘汰 / in-flight 同文本单飞 / 批量命中拆分）
# ---------------------------------------------------------------------------

def test_embedding_cache_hit_skips_network(llm_server):
    """同实例同文本第二次 embed 不触网，返回值一致；新实例各自有缓存（实例级）。"""
    base, server = llm_server
    server.scripts.append(_embed_ok(2048))
    client = EmbeddingClient(_embed_cfg(base))
    first = client.embed("同样的查询")
    second = client.embed("同样的查询")
    assert len(server.requests) == 1  # 命中缓存：第二次无网络请求
    assert (first == second).all()
    assert client.cache_hits == 1 and client.cache_entries == 1
    # 实例级隔离：新实例（同配置）没有缓存，必须自己触网
    server.scripts.append(_embed_ok(2048))
    fresh = EmbeddingClient(_embed_cfg(base))
    fresh.embed("同样的查询")
    assert len(server.requests) == 2


def test_embedding_cache_key_is_four_tuple(llm_server):
    """O7 键 = (model, style, dim, text)：任一维度变化即不同键。"""
    base, _server = llm_server
    client = EmbeddingClient(_embed_cfg(base))
    assert client._cache_key("文本") == ("test-embed", "openai", 2048, "文本")
    other = EmbeddingClient(_embed_cfg(base, dim=1024))
    assert other._cache_key("文本")[2] == 1024


def test_embedding_failure_not_cached(llm_server):
    """失败不缓存：请求失败后重试同文本必须真实触网。"""
    base, server = llm_server
    server.scripts.append({"status": 500, "body": {"error": "boom"}})
    client = EmbeddingClient(_embed_cfg(base, max_retries=0), sleep=lambda _s: None)
    with pytest.raises(LLMError):
        client.embed("会失败的文本")
    assert client.cache_entries == 0
    server.scripts.append(_embed_ok(2048))
    vec = client.embed("会失败的文本")  # 不吃缓存，真实重发
    assert vec.shape == (2048,)
    assert len(server.requests) == 2


def test_embedding_lru_evicts_oldest(llm_server):
    """容量上限按字节淘汰最旧（64MB 默认；此处注入小上限验证 LRU 行为）。"""
    base, server = llm_server
    small = 2 * 2048 * 4  # 2 条向量大小（8KB/条）
    client = EmbeddingClient(_embed_cfg(base), cache_max_bytes=small)
    for text in ("甲", "乙", "丙"):
        server.scripts.append(_embed_ok(2048))
        client.embed(text)
    assert client.cache_entries == 2  # 超容后「甲」被淘汰
    assert client.cache_bytes <= small
    server.scripts.append(_embed_ok(2048))
    client.embed("甲")  # 被淘汰的最旧条目：重新触网
    assert len(server.requests) == 4


def test_embedding_inflight_singleflight(llm_server):
    """并发同文本只发一次请求（in-flight 单飞，threading.Event 协调）。"""
    base, server = llm_server
    server.scripts.append(_embed_ok(2048))
    client = EmbeddingClient(_embed_cfg(base))
    barrier = threading.Barrier(2)
    results: list = []

    def worker() -> None:
        barrier.wait()
        results.append(client.embed("并发同文本"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 2
    assert (results[0] == results[1]).all()
    assert len(server.requests) == 1  # 单飞：只触网一次


def test_embedding_batch_splits_cached_and_miss(llm_server):
    """批量先拆命中：命中部分不触网，未命中的合并为一次批量请求。"""
    base, server = llm_server
    server.scripts.append(_embed_ok(2048))
    client = EmbeddingClient(_embed_cfg(base))
    client.embed("已缓存条目")
    server.scripts.append(
        {"status": 200, "body": {"data": [
            {"embedding": [0.5] * 2048}, {"embedding": [0.25] * 2048}]}}
    )
    matrix = client.embed_batch(["已缓存条目", "新条目甲", "新条目乙"])
    assert matrix.shape == (3, 2048)
    assert len(server.requests) == 2  # 首次单条 + 批量一次（只含 2 条 miss）
    miss_input = server.requests[-1]["body"]["input"]
    assert miss_input == ["新条目甲", "新条目乙"]
    assert client.cache_hits == 1
