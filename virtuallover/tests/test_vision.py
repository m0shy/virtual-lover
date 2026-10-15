# -*- coding: utf-8 -*-
"""brain/vision.py 图片识别测试（多模态能力①：云 vision 模型 + 占位降级）。

覆盖面（多模态任务书第 5 条）：
- 纯函数：download_image（成功/超限）、MIME 嗅探、data URL 构造、
  OpenAI 多模态消息格式、supports_vision 开关读取、图片引用提取；
- process_image 全路径：视觉开（多模态消息进 chat_async）/ 视觉关
  （零调用零网络 + 一次性指引）/ 下载失败降级 / 空回复降级 / 客户端异常降级；
- llm 透传：ChatClient.chat 保留 image_url 段（mock urlopen 捕获请求体）；
- 管线集成（O3/O4 契约 harness）：带图事件 → 「[图片]」占位被描述替换，
  history user 行同步落描述；开关关=零变化；
- 控制台集成：URL / base64 粘贴 → 同链路重写消息；
- 配置装载：[chat].supports_vision=true 能被 load_config 读到。

测试全部用占位值与假件，绝不触网、不含任何真实敏感值。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from types import SimpleNamespace

import pytest

from virtuallover.brain import vision
from virtuallover.brain.llm import ChatClient, LLMReply
from virtuallover.console import core as console_core
from virtuallover.config import load_config

# ---------------------------------------------------------------------------
# 假件与工具
# ---------------------------------------------------------------------------

#: 合法 PNG 魔数开头的假图（嗅探测试只看前 8 字节）
FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16

USER_PLACEHOLDER = "10001"  # 占位用户（禁真实 QQ 号）


class _RecordingChat:
    """假 chat 客户端（契约面 = ChatClient.chat_async 子集）：记录调用与回复。"""

    def __init__(self, reply: str = "一只橘猫趴在桌上") -> None:
        self.reply = reply
        self.calls: list[tuple[list, dict]] = []
        self.error: Exception | None = None

    async def chat_async(self, messages: list, **kwargs):
        self.calls.append((messages, kwargs))
        if self.error is not None:
            raise self.error
        return LLMReply(content=self.reply)


class _FakeResp:
    """urlopen 假响应（_post_json / download_image 消费面：read/headers/with）。"""

    def __init__(self, payload: bytes, status: int = 200) -> None:
        self._payload = payload
        self.status = status
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, amount: int = -1) -> bytes:
        if amount is None or amount < 0:
            return self._payload
        return self._payload[:amount]


def _run(coro):
    """测试内驱动协程（仓库测试不引入 pytest-asyncio，同款手法）。"""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _reset_vision_hint():
    """每条用例重置一次性指引标志（用例间隔离）。"""
    vision._vision_hint_logged = False
    yield
    vision._vision_hint_logged = False


# ---------------------------------------------------------------------------
# ① 纯函数：下载 / MIME / data URL / 多模态消息 / 开关 / 引用提取
# ---------------------------------------------------------------------------

def test_download_image_success_and_limit():
    """下载成功返回 bytes；超过 max_bytes 抛 ValueError（32MiB 上限同桥）。"""
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return _FakeResp(FAKE_PNG)

    data = vision.download_image("http://media.example/cat.jpg",
                                 urlopen=fake_urlopen, timeout=7.5)
    assert data == FAKE_PNG
    assert captured["url"] == "http://media.example/cat.jpg"
    assert captured["timeout"] == 7.5

    big = b"x" * 64
    with pytest.raises(ValueError):
        vision.download_image("http://media.example/big.jpg", max_bytes=10,
                              urlopen=lambda request, timeout=None: _FakeResp(big))


def test_sniff_image_mime_by_magic_then_ext_then_default():
    """魔数优先 → URL 后缀兜底 → image/jpeg 最终兜底。"""
    assert vision.sniff_image_mime(FAKE_PNG) == "image/png"
    assert vision.sniff_image_mime(FAKE_JPEG) == "image/jpeg"
    assert vision.sniff_image_mime(b"GIF89a....") == "image/gif"
    assert vision.sniff_image_mime(b"RIFF1234WEBPVP8 ") == "image/webp"
    assert vision.sniff_image_mime(b"BM....") == "image/bmp"
    # 未知魔数：按 URL 后缀
    assert vision.sniff_image_mime(b"????", "http://e.com/a.webp") == "image/webp"
    # 后缀也不认识：默认 jpeg
    assert vision.sniff_image_mime(b"????", "http://e.com/a") == "image/jpeg"


def test_build_vision_message_openai_multimodal_format():
    """多模态消息形态（任务书钉死）：text 段 + image_url 段的 data URL。"""
    data_url = vision.make_image_data_url(FAKE_JPEG, "http://e.com/a.jpg")
    assert data_url.startswith("data:image/jpeg;base64,")
    # 载荷是合法 base64 且可还原
    payload = data_url.split(",", 1)[1]
    assert base64.b64decode(payload) == FAKE_JPEG

    msg = vision.build_vision_message("这是什么", data_url)
    assert msg["role"] == "user"
    assert isinstance(msg["content"], list) and len(msg["content"]) == 2
    assert msg["content"][0] == {"type": "text", "text": "这是什么\n" + vision.CAPTION_SUFFIX}
    assert msg["content"][1]["type"] == "image_url"
    assert msg["content"][1]["image_url"] == {"url": data_url}

    # 无附言：默认描述指令
    msg2 = vision.build_vision_message("", data_url)
    assert msg2["content"][0]["text"] == vision.DESCRIBE_PROMPT


def test_supports_vision_flag_reading():
    """开关读取：true/false/缺配置/缺键一律安全（缺省=False=占位降级）。"""
    assert vision.supports_vision(SimpleNamespace(chat=SimpleNamespace(supports_vision=True)))
    assert not vision.supports_vision(SimpleNamespace(chat=SimpleNamespace(supports_vision=False)))
    assert not vision.supports_vision(SimpleNamespace(chat=SimpleNamespace()))  # 缺键
    assert not vision.supports_vision(SimpleNamespace())                        # 缺 [chat]
    assert not vision.supports_vision(None)                                     # 缺配置


def test_extract_image_refs_url_data_uri_and_bare_b64():
    """引用提取：URL / data URI / 裸 base64 三形态；清理文本可作附言。"""
    text, refs = vision.extract_image_refs("看看 https://e.com/x/cat.jpg?v=1 好看吗")
    assert refs == [("url", "https://e.com/x/cat.jpg?v=1")]
    assert text == "看看 好看吗"

    payload = base64.b64encode(b"hello-image").decode()
    text2, refs2 = vision.extract_image_refs(f"data:image/png;base64,{payload}")
    assert refs2 == [("b64", payload)]
    assert text2 == ""

    # 裸 base64（≥256 连续字符）被识别；短串不被误伤
    long_b64 = "A" * 300
    text3, refs3 = vision.extract_image_refs(f"前缀 {long_b64} 后缀")
    assert refs3 == [("b64", long_b64)]
    assert "前缀" in text3 and "后缀" in text3

    text4, refs4 = vision.extract_image_refs("普通一句话，没有图片")
    assert refs4 == [] and text4 == "普通一句话，没有图片"


# ---------------------------------------------------------------------------
# ② process_image 全路径（视觉开 / 关 / 失败降级）
# ---------------------------------------------------------------------------

def test_process_image_vision_on_builds_multimodal_and_returns_desc():
    """开关开：下载→多模态消息进 chat_async→返回描述；caption 进 text 段。"""
    client = _RecordingChat(reply="橘猫")
    downloads = []

    def fake_download(url, timeout=30.0):
        downloads.append(url)
        return FAKE_JPEG

    desc = _run(vision.process_image(
        "http://media.example/cat.jpg", "看这只",
        chat_client=client, supports_vision_flag=True,
        download_fn=fake_download))
    assert desc == "橘猫"
    assert downloads == ["http://media.example/cat.jpg"]
    assert len(client.calls) == 1
    messages, kwargs = client.calls[0]
    content = messages[0]["content"]
    assert content[0]["type"] == "text" and "看这只" in content[0]["text"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert kwargs.get("extra_body") == {"prompt_cache_key": "vl-vision"}


def test_process_image_vision_off_is_zero_call_placeholder():
    """开关关：占位返回、chat 零调用（下载函数都不该被触）。"""
    client = _RecordingChat()

    def _must_not_download(url, timeout=30.0):
        raise AssertionError("视觉关闭时绝不下载")

    desc = _run(vision.process_image(
        "http://media.example/cat.jpg", "",
        chat_client=client, supports_vision_flag=False,
        download_fn=_must_not_download))
    assert desc == vision.PLACEHOLDER_IMAGE
    assert client.calls == []


def test_process_image_hints_once_when_vision_off(caplog):
    """关开关的一次性指引：首张图提示一次「换视觉模型后可开」，之后不再刷。"""
    client = _RecordingChat()
    with caplog.at_level(logging.INFO, logger="virtuallover.brain.vision"):
        _run(vision.process_image("http://e.com/a.jpg", "", chat_client=client,
                                  supports_vision_flag=False, download_fn=lambda u, timeout=30.0: b""))
        _run(vision.process_image("http://e.com/b.jpg", "", chat_client=client,
                                  supports_vision_flag=False, download_fn=lambda u, timeout=30.0: b""))
    hints = [r for r in caplog.records if "supports_vision" in r.getMessage()]
    assert len(hints) == 1, "指引只提示一次（防刷屏）"


def test_process_image_download_failure_degrades_to_placeholder():
    """下载失败（网络/超限）→ 占位，不抛异常、不进 chat。"""
    client = _RecordingChat()

    def bad_download(url, timeout=30.0):
        raise OSError("connection reset")

    desc = _run(vision.process_image("http://media.example/x.jpg", "",
                                     chat_client=client, supports_vision_flag=True,
                                     download_fn=bad_download))
    assert desc == vision.PLACEHOLDER_IMAGE
    assert client.calls == []


def test_process_image_bytes_empty_and_error_and_blank_reply():
    """字节路径：空字节占位；chat 异常占位；模型空回复占位。"""
    client = _RecordingChat()
    assert _run(vision.process_image_bytes(b"", "", chat_client=client)) == \
        vision.PLACEHOLDER_IMAGE

    client.error = RuntimeError("upstream 500")
    assert _run(vision.process_image_bytes(FAKE_PNG, "", chat_client=client)) == \
        vision.PLACEHOLDER_IMAGE

    client.error = None
    client.reply = "   "
    assert _run(vision.process_image_bytes(FAKE_PNG, "", chat_client=client)) == \
        vision.PLACEHOLDER_IMAGE


def test_process_image_defaults_flag_from_cfg():
    """不显式传开关时从 cfg 读取（管线同款入口；cfg=None 安全视为关）。"""
    client = _RecordingChat()
    cfg_on = SimpleNamespace(chat=SimpleNamespace(supports_vision=True))
    desc = _run(vision.process_image(
        "http://e.com/a.jpg", "", chat_client=client, cfg=cfg_on,
        download_fn=lambda u, timeout=30.0: FAKE_PNG))
    assert desc == client.reply
    # cfg 缺省（None）= 关：零调用
    desc2 = _run(vision.process_image(
        "http://e.com/a.jpg", "", chat_client=client, cfg=None,
        download_fn=lambda u, timeout=30.0: FAKE_PNG))
    assert desc2 == vision.PLACEHOLDER_IMAGE
    assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# ③ llm 透传：多模态 content 不被扁平化吞掉
# ---------------------------------------------------------------------------

def test_chat_client_preserves_image_url_segment():
    """ChatClient.chat 发出的请求体保留 image_url 段（多模态消息原样透传）。

    mock urlopen 捕获请求体；修前 _flatten_messages 会把分段 content 扁成
    纯文本、图片段被丢成空串——本用例钉死该回归。
    """
    captured: dict = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResp(json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "看到了"},
                         "finish_reason": "stop"}]
        }).encode("utf-8"))

    cfg = SimpleNamespace(api_base="http://llm.example/v1", api_key="sk-test-placeholder",
                          model="test-model", timeout=5, max_retries=1, retry_on_429=True)
    client = ChatClient(cfg, urlopen=fake_urlopen)
    data_url = vision.make_image_data_url(FAKE_PNG, "http://e.com/a.png")
    reply = client.chat([vision.build_vision_message("这是什么", data_url)])
    assert reply.content == "看到了"
    sent = captured["body"]["messages"][0]
    assert isinstance(sent["content"], list), "多模态 content 必须原样透传"
    assert sent["content"][1]["image_url"]["url"] == data_url


def test_content_to_text_still_flattens_text_only_lists():
    """回归：纯 text/think 分段列表照旧扁平化（历史库/主链路零变化）。"""
    from virtuallover.brain.llm import content_to_text
    assert content_to_text([{"type": "text", "text": "你好"},
                            {"type": "think", "text": "私密"}]) == "你好"
    assert content_to_text("纯字符串") == "纯字符串"


# ---------------------------------------------------------------------------
# ④ 管线集成：带图事件 → 占位被描述替换（O3/O4 契约 harness）
# ---------------------------------------------------------------------------

def _image_event(user: str, text: str, message_id: int, url: str) -> dict:
    """带 image 段的桥事件（与 onebot_server._handle_message_event 同形）。"""
    from virtuallover.tests._o34_contract import make_event
    ev = make_event(user, text, message_id)
    ev["segments"] = [
        {"type": "text", "data": {"text": text}},
        {"type": "image", "data": {"url": url}},
    ]
    return ev


def _split_reply(call):
    """reply_fn：vision 调用（末条消息 content 是 list）回描述，主调用回正常回复。"""
    last = call.messages[-1]
    if isinstance(last.get("content"), list):
        return "一只橘猫趴在桌上"
    return "哇，猫咪好可爱呀。"


def _vision_cfg(ctx, on: bool) -> None:
    ctx.cfg.chat.supports_vision = on


def test_pipeline_replaces_image_placeholder_with_description(tmp_path, monkeypatch):
    """supports_vision=true：带图消息的「[图片]」被视觉描述替换（多模态②）。

    链路：vision 调用（第 1 次 chat）→ 描述替换占位 → 主 LLM 轮（第 2 次
    chat）user 文本含描述不含占位 → history user 行落描述。
    下载 monkeypatch 成假字节（测试绝不触网）。
    """
    monkeypatch.setattr(vision, "download_image",
                        lambda url, timeout=30.0, **kw: FAKE_JPEG)
    from virtuallover.tests._o34_contract import (
        FakeClock, attach_pipeline, make_ctx, settle,
    )
    from virtuallover.events import EVENT_MESSAGE

    clock = FakeClock()

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        _vision_cfg(ctx, True)
        handle, why = await attach_pipeline(ctx)
        if handle is None:
            pytest.skip(why)
        try:
            handle.chat.auto_release = True
            handle.chat.reply_fn = _split_reply
            ev = _image_event("10001", "看[图片]", 901,
                              "http://media.example/cat.jpg")
            await handle.emit(ev)
            await settle(handle, timeout=8.0)

            assert len(handle.chat.calls) == 2, "应有一次 vision 调用 + 一次主 LLM 轮"
            vision_call, main_call = handle.chat.calls
            # vision 调用是多模态消息
            v_content = vision_call.messages[0]["content"]
            assert v_content[1]["type"] == "image_url"
            # 主调用 user 文本：占位已被描述替换
            main_user = [m for m in main_call.messages if m.get("role") == "user"]
            joined = "".join(str(m.get("content") or "") for m in main_user)
            assert "一只橘猫趴在桌上" in joined
            assert vision.PLACEHOLDER_IMAGE not in joined
            # history user 行落的是描述后的文本
            rows = ctx.history.get_records(ctx.cfg.session_id, limit=10)
            user_rows = [r for r in rows if r.get("role") == "user"]
            assert any("一只橘猫趴在桌上" in str(r.get("content")) for r in user_rows)
            assert all(vision.PLACEHOLDER_IMAGE not in str(r.get("content"))
                       for r in user_rows)
        finally:
            await handle.aclose()

    asyncio.run(scenario())


def test_pipeline_image_without_vision_keeps_placeholder_zero_extra_call(tmp_path):
    """supports_vision=false（默认）：行为与旧版完全一致——占位保留、单次调用。"""
    from virtuallover.tests._o34_contract import (
        FakeClock, attach_pipeline, make_ctx, settle,
    )

    clock = FakeClock()

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        _vision_cfg(ctx, False)
        handle, why = await attach_pipeline(ctx)
        if handle is None:
            pytest.skip(why)
        try:
            handle.chat.auto_release = True

            def _echo(call):
                for msg in reversed(call.messages):
                    if str(msg.get("role")) == "user":
                        return str(msg.get("content") or "")
                return "嗯嗯。"

            handle.chat.reply_fn = _echo
            ev = _image_event("10001", "看[图片]", 902,
                              "http://media.example/cat.jpg")
            await handle.emit(ev)
            await settle(handle, timeout=8.0)

            assert len(handle.chat.calls) == 1, "视觉关闭时不发起 vision 调用"
            main_user = [m for m in handle.chat.calls[0].messages
                         if m.get("role") == "user"]
            joined = "".join(str(m.get("content") or "") for m in main_user)
            assert vision.PLACEHOLDER_IMAGE in joined
        finally:
            await handle.aclose()

    asyncio.run(scenario())


def test_pipeline_no_image_segments_is_zero_overhead(tmp_path):
    """纯文本消息：不触发 vision 链路（不下载、不多调用）——回归护栏。"""
    from virtuallover.tests._o34_contract import (
        FakeClock, attach_pipeline, make_ctx, settle,
    )

    clock = FakeClock()

    async def scenario():
        ctx = make_ctx(tmp_path, now_fn=clock)
        _vision_cfg(ctx, True)
        handle, why = await attach_pipeline(ctx)
        if handle is None:
            pytest.skip(why)
        try:
            handle.chat.auto_release = True

            def _echo(call):
                assert not (len(call.messages) == 1
                            and isinstance(call.messages[-1].get("content"), list)), \
                    "纯文本消息绝不应触发 vision 多模态调用"
                return "好呀。"

            handle.chat.reply_fn = _echo
            ev = dict(_image_event("10001", "今天好累", 903, "unused"))
            ev["segments"] = [{"type": "text", "data": {"text": "今天好累"}}]
            await handle.emit(ev)
            await settle(handle, timeout=8.0)
            assert len(handle.chat.calls) == 1
        finally:
            await handle.aclose()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# ⑤ 控制台集成：URL / base64 粘贴 → 同链路
# ---------------------------------------------------------------------------

def test_console_images_url_resolved_via_vision(monkeypatch):
    """控制台粘贴图片 URL：识别描述并入消息（多模态③，QQ 同链路）。"""
    monkeypatch.setattr(vision, "download_image",
                        lambda url, timeout=30.0, **kw: FAKE_JPEG)
    cfg = SimpleNamespace(chat=SimpleNamespace(supports_vision=True))
    client = _RecordingChat(reply="海边日落")

    merged, used = _run(console_core._resolve_console_images(
        cfg, client, "看看 https://example.com/sunset.jpg 怎么样"))
    assert used is True
    assert "海边日落" in merged
    assert "sunset.jpg" not in merged, "URL 应被描述替换而不是透传"
    # chat 收到多模态消息
    content = client.calls[0][0][0]["content"]
    assert content[1]["type"] == "image_url"
    assert content[0]["text"].startswith("看看")


def test_console_images_b64_resolved_via_vision():
    """控制台粘贴 base64（data URI）：解码后走内存字节路径。"""
    cfg = SimpleNamespace(chat=SimpleNamespace(supports_vision=True))
    client = _RecordingChat(reply="一只柴犬")
    payload = base64.b64encode(FAKE_JPEG).decode()

    merged, used = _run(console_core._resolve_console_images(
        cfg, client, f"data:image/jpeg;base64,{payload} 这是谁呀"))
    assert used is True
    assert "一只柴犬" in merged and payload not in merged
    content = client.calls[0][0][0]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_console_images_off_keeps_message_intact():
    """控制台未开视觉：消息原样返回（URL/数据不被静默吃掉）、零调用。"""
    cfg = SimpleNamespace(chat=SimpleNamespace(supports_vision=False))
    client = _RecordingChat()
    original = "看看 https://example.com/sunset.jpg"
    merged, used = _run(console_core._resolve_console_images(cfg, client, original))
    assert merged == original and used is False
    assert client.calls == []


# ---------------------------------------------------------------------------
# ⑥ 配置装载：[chat].supports_vision
# ---------------------------------------------------------------------------

def test_config_loads_supports_vision_switch(tmp_path):
    """vl.toml [chat].supports_vision=true 能被 load_config 读到；缺省=false。"""
    from virtuallover.tests.test_config_example import EXAMPLE

    raw = EXAMPLE.read_text(encoding="utf-8")
    assert "supports_vision = false" in raw, "example 应含 supports_vision 默认关"
    enabled = tmp_path / "vl.toml"
    enabled.write_text(raw.replace("supports_vision = false", "supports_vision = true"),
                       encoding="utf-8")
    cfg = load_config(enabled)
    assert cfg.chat.supports_vision is True

    # example 原样（默认关）装载 → False
    cfg_default = load_config(EXAMPLE)
    assert cfg_default.chat.supports_vision is False
