# -*- coding: utf-8 -*-
"""多模态②语音转文字（STT）测试 —— mock 转写 API / 无 provider 降级 / 音频段处理。

覆盖面（对齐任务书三条测试要求）：

==========================  =========================================================
任务书条目                  测试
==========================  =========================================================
mock 转写 API               test_transcribe_success_mock_api / test_transcribe_uses_
                            stt_key_over_chat_key / test_transcribe_passes_timeout
                            test_transcribe_http_error_raises_stterror（含密钥不泄露）
                            test_transcribe_network_error_raises_stterror
无 provider 降级            test_disabled_client_raises / test_provider_without_model_
                            disabled / test_replace_placeholder_*（[语音消息] 占位）
音频段处理                  test_iter_record_files_* / test_decode_audio_*（坏 b64/
                            空音频/超限）
管线集成（record 段端到端）  test_pipeline_transcribes_voice_message（LLM 看到文字）
                            test_pipeline_falls_back_without_provider（默认零行为）
                            test_pipeline_falls_back_on_transcribe_failure
                            test_pipeline_falls_back_on_missing_audio
                            test_pipeline_mixed_text_and_voice
配置默认值                  test_config_defaults_empty / test_example_has_stt_keys
==========================  =========================================================

安全规约（同仓库既有测试）：QQ 号一律 10000/10001 占位号段；密钥用
test-stt-key 占位值；不发任何真实网络请求（urlopen 全部注入替身）。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import urllib.error
from email.message import Message
from types import SimpleNamespace

import pytest

from virtuallover.brain import stt as stt_mod
from virtuallover.brain.pipeline import MessagePipeline
from virtuallover.brain.stt import (
    PLACEHOLDER_RECORD,
    PLACEHOLDER_VOICE,
    STTError,
    SttClient,
    build_stt_client,
    iter_record_files,
    replace_voice_placeholder,
)
from virtuallover.config import VlConfig
from virtuallover.tests._o34_contract import (
    FakeClock,
    PipelineHandle,
    make_ctx,
    settle,
)

USER_A = "10001"   # 占位白名单用户（禁真实 QQ 号）
AUDIO_BYTES = b"fake-mp3-audio-bytes-for-stt-test"
AUDIO_B64 = base64.b64encode(AUDIO_BYTES).decode("ascii")


# ---------------------------------------------------------------------------
# 假 urlopen（mock 转写 API 的替身；绝不触网）
# ---------------------------------------------------------------------------


def _fake_urlopen_with_text(text: str, recorded: dict):
    """返回记录请求并应答 {"text": ...} 的假 urlopen。"""

    def _open(request, timeout=None):
        recorded["request"] = request
        recorded["timeout"] = timeout
        body = json.dumps({"text": text}).encode("utf-8")
        resp = SimpleNamespace(status=200, read=lambda: body)
        return contextlib.nullcontext(resp)

    return _open


def _failing_urlopen(recorded: dict):
    """返回抛 HTTP 401（含错误体）的假 urlopen，验证 STTError 包装。"""

    def _open(request, timeout=None):
        recorded["request"] = request
        headers = Message()
        headers["Content-Type"] = "application/json"
        raise urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", headers,
            io.BytesIO(b'{"error":{"code":"invalid_api_key"}}'))

    return _open


def _network_error_urlopen():
    """返回抛 URLError 形态网络错误的假 urlopen（连接拒绝场景）。"""

    def _open(request, timeout=None):
        raise OSError("connection refused")

    return _open


def _client(cfg_chat, urlopen, **kwargs) -> SttClient:
    return SttClient(cfg_chat, urlopen=urlopen, **kwargs)


def _chat_cfg(**overrides):
    """构造 STT 客户端测试用的 [chat] 配置（占位密钥，绝不真实）。"""
    chat = VlConfig().chat
    chat.api_base = "https://stt.example.com/api/v3"
    chat.api_key = "test-chat-key"
    chat.stt_provider = "openai"
    chat.stt_api_key = "test-stt-key"
    chat.stt_model = "whisper-1"
    for key, value in overrides.items():
        setattr(chat, key, value)
    return chat


# ---------------------------------------------------------------------------
# 1) mock 转写 API：请求形态与响应解析
# ---------------------------------------------------------------------------


def test_transcribe_success_mock_api():
    """云端 STT：POST {api_base}/audio/transcriptions，multipart 带 model+音频，回 text。"""
    recorded: dict = {}
    client = _client(_chat_cfg(), _fake_urlopen_with_text("晚安，做个好梦。", recorded))

    heard = client.transcribe(AUDIO_B64)

    assert heard == "晚安，做个好梦。"
    request = recorded["request"]
    assert request.full_url == "https://stt.example.com/api/v3/audio/transcriptions"
    assert request.get_header("Authorization") == "Bearer test-stt-key"
    content_type = request.get_header("Content-type") or ""
    assert content_type.startswith("multipart/form-data; boundary=")
    # multipart 体：model 字段 + 解码后的音频字节都在
    body = request.data
    assert b'name="model"' in body and b"whisper-1" in body
    assert b'name="file"' in body and AUDIO_BYTES in body


def test_transcribe_uses_stt_key_over_chat_key():
    """stt_api_key 优先；留空回退 [chat].api_key（同账号 ASR 共 key 场景）。"""
    recorded: dict = {}
    client = _client(_chat_cfg(), _fake_urlopen_with_text("好", recorded))
    client.transcribe(AUDIO_B64)
    assert recorded["request"].get_header("Authorization") == "Bearer test-stt-key"

    recorded.clear()
    fallback = _client(_chat_cfg(stt_api_key=""),
                       _fake_urlopen_with_text("好", recorded))
    assert fallback.enabled
    fallback.transcribe(AUDIO_B64)
    assert recorded["request"].get_header("Authorization") == "Bearer test-chat-key"


def test_transcribe_passes_timeout():
    """读超时透传给 urlopen（默认 30s，构造可注入覆盖）。"""
    recorded: dict = {}
    client = _client(_chat_cfg(), _fake_urlopen_with_text("好", recorded), timeout=5.0)
    client.transcribe(AUDIO_B64)
    assert recorded["timeout"] == 5.0
    assert stt_mod.STT_TIMEOUT_SECONDS == 30.0


def test_transcribe_empty_text_returns_empty_string():
    """上游没听出内容（text 为空）：返回空串（调用方降级占位），不抛异常。"""
    recorded: dict = {}
    client = _client(_chat_cfg(), _fake_urlopen_with_text("  ", recorded))
    assert client.transcribe(AUDIO_B64) == ""


def test_transcribe_http_error_raises_stterror():
    """HTTP 401 → STTError；错误摘要只含枚举字段，密钥/正文不进异常消息。"""
    recorded: dict = {}
    client = _client(_chat_cfg(), _failing_urlopen(recorded))
    with pytest.raises(STTError) as excinfo:
        client.transcribe(AUDIO_B64)
    message = str(excinfo.value)
    assert "401" in message
    assert "error.invalid_api_key" in message
    assert "test-stt-key" not in message


def test_transcribe_network_error_raises_stterror():
    """连接拒绝等网络层错误 → STTError（单次尝试不重试，对话场景快降级）。"""
    client = _client(_chat_cfg(), _network_error_urlopen())
    with pytest.raises(STTError):
        client.transcribe(AUDIO_B64)


# ---------------------------------------------------------------------------
# 2) 无 provider 降级：enabled 判定 + [语音消息] 占位
# ---------------------------------------------------------------------------


def test_disabled_client_raises_on_transcribe():
    """未配 stt_provider：enabled=False，transcribe 防御性抛 STTError。"""
    chat = VlConfig().chat          # 全默认：stt_provider=""
    client = build_stt_client(chat)
    assert client.enabled is False
    with pytest.raises(STTError):
        client.transcribe(AUDIO_B64)


def test_provider_without_model_disabled():
    """配了 provider 但没配 model：不可用（降级占位），配置缺口打告警日志。"""
    chat = _chat_cfg(stt_model="")
    client = build_stt_client(chat)
    assert client.enabled is False


def test_replace_placeholder_success():
    """转写成功：[语音] 占位替换为转写文本，只换第一个。"""
    assert replace_voice_placeholder("[语音]", "晚安呀") == "晚安呀"
    assert replace_voice_placeholder("听听这个[语音]", "录好了") == "听听这个录好了"
    # 多段语音：每次调用换一个（管线按 record 段逐个喂）
    two = "前[语音]中[语音]后"
    once = replace_voice_placeholder(two, "一")
    assert once == "前一中[语音]后"
    assert replace_voice_placeholder(once, "二") == "前一中二后"


def test_replace_placeholder_fallback_when_heard_empty():
    """转写不可用/为空：[语音] → [语音消息]（本地降级占位，与 [图片] 同款）。"""
    assert replace_voice_placeholder("[语音]", "") == PLACEHOLDER_VOICE
    assert replace_voice_placeholder("[语音]", "   ") == PLACEHOLDER_VOICE
    assert replace_voice_placeholder("发来[语音]听听", "") == f"发来{PLACEHOLDER_VOICE}听听"


def test_replace_placeholder_noop_without_marker():
    """文本里没有 [语音] 占位：原样返回（误调无害）。"""
    assert replace_voice_placeholder("普通文本", "话") == "普通文本"
    assert replace_voice_placeholder("", "话") == ""


# ---------------------------------------------------------------------------
# 3) 音频段处理：record 段收集 + base64 解码护栏
# ---------------------------------------------------------------------------


def test_iter_record_files_extracts_in_order():
    """混合段列表：按出现序提取 record 段 file 标识，其余段忽略。"""
    segments = [
        {"type": "text", "data": {"text": "听听"}},
        {"type": "record", "data": {"file": "sil_a.mp3"}},
        {"type": "image", "data": {"url": "https://x/img.jpg"}},
        {"type": "record", "data": {"file": "sil_b.amr"}},
        {"type": "record", "data": {}},          # 缺 file：容错跳过
        {"type": "record"},                      # 缺 data：容错跳过
        "not-a-dict",                            # 非 dict：容错跳过
    ]
    assert iter_record_files(segments) == ["sil_a.mp3", "sil_b.amr"]
    assert iter_record_files(None) == []
    assert iter_record_files([]) == []


def test_decode_audio_rejects_bad_base64():
    """坏 base64 / 空串 / 解码为 0 字节：一律 STTError（管线降级占位）。"""
    client = _client(_chat_cfg(), _fake_urlopen_with_text("好", {}))
    with pytest.raises(STTError):
        client.transcribe("!!!not-base64!!!")
    with pytest.raises(STTError):
        client.transcribe("")
    empty = base64.b64encode(b"").decode("ascii")
    with pytest.raises(STTError):
        client.transcribe(empty)


def test_decode_audio_rejects_oversize(monkeypatch):
    """音频超限（STT_MAX_AUDIO_BYTES）：STTError，不把超大请求发给云端。"""
    monkeypatch.setattr(stt_mod, "STT_MAX_AUDIO_BYTES", 8)
    client = _client(_chat_cfg(), _fake_urlopen_with_text("好", {}))
    with pytest.raises(STTError):
        client.transcribe(base64.b64encode(b"1234567890").decode("ascii"))


def test_decode_audio_tolerates_whitespace():
    """base64 夹带空白字符（部分实现换行折行）：容忍并正常解码。"""
    recorded: dict = {}
    client = _client(_chat_cfg(), _fake_urlopen_with_text("好", recorded))
    padded = "\n".join(AUDIO_B64[i:i + 16] for i in range(0, len(AUDIO_B64), 16))
    assert client.transcribe(padded) == "好"
    assert AUDIO_BYTES in recorded["request"].data


# ---------------------------------------------------------------------------
# 4) 管线集成：record 段端到端（get_record → transcribe → 替换占位 → 文本管线）
# ---------------------------------------------------------------------------


def _voice_event(message_id: int, file_id: str, *,
                 text: str = PLACEHOLDER_RECORD,
                 extra_segments: "list[dict] | None" = None) -> dict:
    """构造与桥 _handle_message_event 同形的语音事件 dict。"""
    segments = list(extra_segments or [])
    segments.append({"type": "record", "data": {"file": file_id}})
    return {
        "type": "message",
        "time": 1710000000,
        "self_id": "10000",
        "user_id": USER_A,
        "message_id": str(message_id),
        "text": text,
        "segments": segments,
        "reply_id": None,
        "reply_text": None,
        "raw": {},
    }


def _echo_last_user_text(call):
    """回复回声：把本轮最后一条 user 文本原样带回（钉定 LLM 实际看到的内容）。"""
    for msg in reversed(call.messages):
        if str(msg.get("role")) == "user":
            return str(msg.get("content") or "")
    return "空回复。"


def _make_pipeline_ctx(tmp_path, *, chat_overrides: "dict | None" = None):
    """管线集成夹具：make_ctx + [chat] STT 配置覆盖（占位密钥）。"""
    clock = FakeClock()
    ctx = make_ctx(tmp_path, now_fn=clock)
    for key, value in (chat_overrides or {}).items():
        setattr(ctx.cfg.chat, key, value)
    return ctx, clock


def _attach_get_record(ctx, results: "dict[str, object]") -> "list[str]":
    """给假桥补 get_record_base64 契约面（按 file 查表应答），返回调用记录。"""
    calls: list[str] = []

    async def _get_record(file: str = "", out_format: str = "mp3"):
        calls.append(file)
        return results.get(file)

    ctx.bridge.get_record_base64 = _get_record
    return calls


def test_pipeline_transcribes_voice_message(tmp_path):
    """端到端：语音事件 → get_record → 转写 → LLM 看到文字 → 回复与历史同源。"""
    recorded: dict = {}
    ctx, _clock = _make_pipeline_ctx(tmp_path, chat_overrides={
        "stt_provider": "test",
        "stt_model": "test-asr",
        "stt_api_key": "test-stt-key",
        "api_base": "https://stt.example.com/api/v3",
    })
    get_record_calls = _attach_get_record(ctx, {"sil_100.mp3": AUDIO_B64})
    client = _client(ctx.cfg.chat, _fake_urlopen_with_text("晚安，做个好梦。", recorded))

    async def scenario():
        pipeline = MessagePipeline(ctx, now_fn=ctx.now_fn, stt_client=client)
        handle = PipelineHandle(pipeline, ctx, driver="direct",
                                module_name="virtuallover.brain.pipeline",
                                class_name="MessagePipeline", clock_ok=True)
        try:
            handle.chat.reply_fn = _echo_last_user_text
            await handle.emit(_voice_event(500, "sil_100.mp3"))
            await settle(handle, timeout=8.0)
            # 桥取音频被调用（file 透传）
            assert get_record_calls == ["sil_100.mp3"]
            # 转写请求形态正确（URL / 密钥 / model 随 multipart 体上行）
            assert recorded["request"].full_url.endswith("/audio/transcriptions")
            assert recorded["request"].get_header("Authorization") == \
                "Bearer test-stt-key"
            assert b"test-asr" in recorded["request"].data
            # LLM 收到的是文字（资料块注入不改变子串存在性，断言子串）
            assert "晚安，做个好梦。" in handle.chat.calls[0].messages[-1]["content"]
            # 回复按正常文本管线发出
            assert any("晚安" in t for t in handle.bridge.completed)
            # 历史与记忆计数消费的是转写文本（user 行 = 转写原文）
            records = ctx.history.get_records(
                "测试人格:FriendMessage:10001")
            user_rows = [r["content"] for r in records if r["role"] == "user"]
            assert user_rows == ["晚安，做个好梦。"]
        finally:
            await handle.aclose()
            ctx.history.close()

    asyncio.run(scenario())


def test_pipeline_falls_back_without_provider(tmp_path):
    """未配 STT（默认配置）：语音消息降级 [语音消息] 占位，LLM 照常回应、零触网。"""
    ctx, _clock = _make_pipeline_ctx(tmp_path)   # 全默认：stt_provider=""
    get_record_calls = _attach_get_record(ctx, {"sil_100.mp3": AUDIO_B64})

    async def scenario():
        # 走缺省装配路径（不注入 client）：内部按 cfg 构造 enabled=False 客户端
        pipeline = MessagePipeline(ctx, now_fn=ctx.now_fn)
        handle = PipelineHandle(pipeline, ctx, driver="direct",
                                module_name="virtuallover.brain.pipeline",
                                class_name="MessagePipeline", clock_ok=True)
        try:
            handle.chat.reply_fn = _echo_last_user_text
            await handle.emit(_voice_event(501, "sil_100.mp3"))
            await settle(handle, timeout=8.0)
            # 未配 provider：连 get_record 都不调（零网络开销）
            assert get_record_calls == []
            # LLM 看到的是 [语音消息] 占位（知道对方发了语音但没听懂）
            assert PLACEHOLDER_VOICE in handle.chat.calls[0].messages[-1]["content"]
            assert PLACEHOLDER_RECORD not in handle.chat.calls[0].messages[-1]["content"]
        finally:
            await handle.aclose()
            ctx.history.close()

    asyncio.run(scenario())


def test_pipeline_falls_back_on_transcribe_failure(tmp_path):
    """转写失败（HTTP 401）：降级 [语音消息] 占位，绝不阻断对话。"""
    ctx, _clock = _make_pipeline_ctx(tmp_path, chat_overrides={
        "stt_provider": "test",
        "stt_model": "test-asr",
        "stt_api_key": "test-stt-key",
        "api_base": "https://stt.example.com/api/v3",
    })
    _attach_get_record(ctx, {"sil_101.mp3": AUDIO_B64})
    client = _client(ctx.cfg.chat, _failing_urlopen({}))

    async def scenario():
        pipeline = MessagePipeline(ctx, now_fn=ctx.now_fn, stt_client=client)
        handle = PipelineHandle(pipeline, ctx, driver="direct",
                                module_name="virtuallover.brain.pipeline",
                                class_name="MessagePipeline", clock_ok=True)
        try:
            handle.chat.reply_fn = _echo_last_user_text
            await handle.emit(_voice_event(502, "sil_101.mp3"))
            await settle(handle, timeout=8.0)
            assert PLACEHOLDER_VOICE in handle.chat.calls[0].messages[-1]["content"]
            assert len(handle.chat.calls) == 1   # 对话照常进行
        finally:
            await handle.aclose()
            ctx.history.close()

    asyncio.run(scenario())


def test_pipeline_falls_back_on_missing_audio(tmp_path):
    """get_record 返回空（桥侧取音频失败）：降级 [语音消息] 占位，不触转写 API。"""
    recorded: dict = {}
    ctx, _clock = _make_pipeline_ctx(tmp_path, chat_overrides={
        "stt_provider": "test",
        "stt_model": "test-asr",
        "stt_api_key": "test-stt-key",
        "api_base": "https://stt.example.com/api/v3",
    })
    _attach_get_record(ctx, {"sil_102.mp3": None})
    client = _client(ctx.cfg.chat, _fake_urlopen_with_text("不该被调用", recorded))

    async def scenario():
        pipeline = MessagePipeline(ctx, now_fn=ctx.now_fn, stt_client=client)
        handle = PipelineHandle(pipeline, ctx, driver="direct",
                                module_name="virtuallover.brain.pipeline",
                                class_name="MessagePipeline", clock_ok=True)
        try:
            handle.chat.reply_fn = _echo_last_user_text
            await handle.emit(_voice_event(503, "sil_102.mp3"))
            await settle(handle, timeout=8.0)
            assert "request" not in recorded          # 转写 API 零调用
            assert PLACEHOLDER_VOICE in handle.chat.calls[0].messages[-1]["content"]
        finally:
            await handle.aclose()
            ctx.history.close()

    asyncio.run(scenario())


def test_pipeline_mixed_text_and_voice(tmp_path):
    """文字+语音混合消息：转写文字嵌回原文本，其余文字原样保留。"""
    recorded: dict = {}
    ctx, _clock = _make_pipeline_ctx(tmp_path, chat_overrides={
        "stt_provider": "test",
        "stt_model": "test-asr",
        "stt_api_key": "test-stt-key",
        "api_base": "https://stt.example.com/api/v3",
    })
    _attach_get_record(ctx, {"sil_103.mp3": AUDIO_B64})
    client = _client(ctx.cfg.chat, _fake_urlopen_with_text("你猜猜看", recorded))

    async def scenario():
        pipeline = MessagePipeline(ctx, now_fn=ctx.now_fn, stt_client=client)
        handle = PipelineHandle(pipeline, ctx, driver="direct",
                                module_name="virtuallover.brain.pipeline",
                                class_name="MessagePipeline", clock_ok=True)
        try:
            handle.chat.reply_fn = _echo_last_user_text
            await handle.emit(_voice_event(
                504, "sil_103.mp3", text=f"听听这个{PLACEHOLDER_RECORD}",
                extra_segments=[{"type": "text", "data": {"text": "听听这个"}}]))
            await settle(handle, timeout=8.0)
            # 占位被转写文字原位替换，前后文字保留
            assert "听听这个你猜猜看" in handle.chat.calls[0].messages[-1]["content"]
        finally:
            await handle.aclose()
            ctx.history.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 5) 配置接线：默认空 = 不支持（用户配了后开），example 同步
# ---------------------------------------------------------------------------


def test_config_defaults_empty():
    """[chat] stt_* 三键默认空串：空=不支持，用户配了后开（任务书 §3）。"""
    chat = VlConfig().chat
    assert chat.stt_provider == ""
    assert chat.stt_api_key == ""
    assert chat.stt_model == ""


def test_example_has_stt_keys():
    """vl.toml.example 与配置树机械对拍：stt 三键在场且为空占位（无敏感值）。"""
    import tomllib
    from pathlib import Path

    from virtuallover.config import load_config

    example = (Path(__file__).resolve().parents[2]
               / "deploy" / "config-samples" / "vl.toml.example")
    raw = tomllib.loads(example.read_text(encoding="utf-8"))
    chat = raw["chat"]
    assert chat["stt_provider"] == ""
    assert chat["stt_api_key"] == ""
    assert chat["stt_model"] == ""
    # example 能整体过启动校验（键集/值对拍由 test_config_example.py 把关）
    cfg = load_config(example)
    assert cfg.chat.stt_provider == ""
