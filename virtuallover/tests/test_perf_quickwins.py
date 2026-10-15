# -*- coding: utf-8 -*-
"""性能快赢批契约测试（PERF_SPEC §2 O1 + O2a + §7.2 毫秒埋点，快赢工程师职责面）。

覆盖点（全部离线：mock HTTP server / 注入时钟，绝不触真实 API 与密钥）：

O1 thinking 双实例（§2 O1 + §7.1-4 + §7.2 O1 拆两层·离线层）：
- build_chat_clients 装配语义：主客户端 thinking=disabled + timeout_main=30s，
  bg 客户端 thinking=enabled + timeout=120s；旧配置对象缺新字段也获默认
  （load_config 归一化之前的 dataclass 默认兜底路径）；
- 请求体断言：主请求体 thinking.type=disabled、bg 请求体 thinking.type=enabled
  （tmp/probe_ark_thinking.py 实证格式，走既有 extra_body 通道）；
- fallback 不继承 ark 专属字段：fallback 收到的请求体无 thinking 键
  （合并在 _request_once 实例层，payload 对 fallback 共享而 extra_body 不共享）；
- 调用方 extra_body 覆盖客户端默认；非法 thinking 值 → 空 extra_body。

O2a 分段阈值 + 首段抖动（§2 O2a + §7.1-1 并行语义）：
- sample_first_gap：区间均匀采样、空串/缺属性/非法原文=关闭（0.0）；
- send_reply first_send_at：首段发送时刻 = max(调用时刻, first_send_at)
  ——抖动不叠加延迟、只保拟人下限；
- 段间隔 ≥1.2s 硬下限（MIN_SEGMENT_GAP_SECONDS 对任何配置生效）；
- 管线级：主回复首段受 first_gap 抖动约束；first_gap 空串=立即发；
  命令回复路径不受抖动影响。

§7.1-3 存量归一化：
- 旧默认 words_count_threshold=100 → 30 + 告警明示；thinking 非法值回退默认；
  timeout_main/timeout 越界 clamp；启动日志打印生效值。

§7.2 毫秒埋点：
- created_at 新写入行毫秒精度（0.5s 间隔不再坍缩同秒）；镜像 ts 同精度；
- 迁移旧行 created_at NULL 原样保留，不回填不伪造。

运行：python -m pytest virtuallover/tests/test_perf_quickwins.py -q
本文件不含任何真实敏感值；QQ 号一律 10000 占位号段。
"""

from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

# sys.path 引导：virtuallover/ 是 namespace 包，仓库根必须可导入。
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from virtuallover.brain import sender as sender_mod  # noqa: E402
from virtuallover.brain.history import HistoryStore  # noqa: E402
from virtuallover.brain.llm import (  # noqa: E402
    ChatClient,
    build_chat_clients,
    setup as llm_setup,
    thinking_extra_body,
)
from virtuallover.config import (  # noqa: E402
    ChatProviderConfig,
    SegmentedReplyConfig,
    VlConfig,
    dump_config,
    load_config,
)
from virtuallover.events import EventBus  # noqa: E402

# 复用 test_llm 的脚本化 mock server（conftest 已把本目录加进 sys.path）
from test_llm import _chat_ok, _make_server  # noqa: E402


# ---------------------------------------------------------------------------
# O1：双客户端装配语义
# ---------------------------------------------------------------------------


def test_build_chat_clients_defaults_main_off_bg_on():
    """默认装配：主客户端 thinking disabled + timeout_main=30s；bg enabled + 120s。"""
    cfg = ChatProviderConfig(api_base="https://api.test/v1", api_key="sk-TEST",
                             model="test-model", timeout=120)
    main_client, bg_client = build_chat_clients(cfg)
    assert main_client.extra_body == {"thinking": {"type": "disabled"}}
    assert main_client.timeout == 30.0  # §7.1-4：30s 只降主客户端
    assert bg_client.extra_body == {"thinking": {"type": "enabled"}}
    assert bg_client.timeout == 120.0  # 后台保持 120s（后台思考实测 11.5s+）
    assert main_client.provider_id == "chat"
    assert bg_client.provider_id == "chat_bg"


def test_build_chat_clients_legacy_cfg_without_new_fields_gets_defaults():
    """旧配置对象（缺 thinking/bg_thinking/timeout_main 字段）也获得 O1 默认。"""
    legacy = SimpleNamespace(api_base="https://api.test/v1", api_key="sk-TEST",
                             model="test-model", timeout=120)
    main_client, bg_client = build_chat_clients(legacy)
    assert main_client.extra_body == {"thinking": {"type": "disabled"}}
    assert main_client.timeout == 30.0
    assert bg_client.extra_body == {"thinking": {"type": "enabled"}}
    assert bg_client.timeout == 120.0


def test_build_chat_clients_respects_config_overrides():
    """配置覆写生效：thinking/bg_thinking/timeout_main 都可显式调整。"""
    cfg = ChatProviderConfig(api_base="https://api.test/v1", api_key="sk-TEST",
                             model="test-model", timeout=200,
                             timeout_main=45, thinking="enabled",
                             bg_thinking="disabled")
    main_client, bg_client = build_chat_clients(cfg)
    assert main_client.extra_body == {"thinking": {"type": "enabled"}}
    assert main_client.timeout == 45.0
    assert bg_client.extra_body == {"thinking": {"type": "disabled"}}
    assert bg_client.timeout == 200.0


def test_thinking_extra_body_invalid_mode_returns_empty():
    """非法/空 thinking 值不带字段（交上游默认，绝不发坏请求体）。"""
    assert thinking_extra_body("disabled") == {"thinking": {"type": "disabled"}}
    assert thinking_extra_body("auto") == {"thinking": {"type": "auto"}}
    assert thinking_extra_body("on") == {}
    assert thinking_extra_body("") == {}
    assert thinking_extra_body(None) == {}


# ---------------------------------------------------------------------------
# O1：请求体 extra_body 断言（脚本化 mock server）
# ---------------------------------------------------------------------------


def test_main_client_request_body_carries_thinking_disabled():
    base, server = _make_server([_chat_ok("来啦~")])
    try:
        cfg = SimpleNamespace(api_base=base, api_key="sk-TEST", model="test-model",
                              timeout=30, max_retries=3, retry_on_429=True)
        client = ChatClient(cfg, provider_id="chat",
                            extra_body=thinking_extra_body("disabled"))
        reply = client.chat([{"role": "user", "content": "在吗"}])
        assert reply.content == "来啦~"
        body = server.requests[0]["body"]
        assert body["thinking"] == {"type": "disabled"}
    finally:
        server.shutdown()
        server.server_close()


def test_bg_client_request_body_carries_thinking_enabled():
    base, server = _make_server([_chat_ok("日记写好了")])
    try:
        cfg = SimpleNamespace(api_base=base, api_key="sk-TEST", model="test-model",
                              timeout=120, max_retries=3, retry_on_429=True)
        client = ChatClient(cfg, provider_id="chat_bg",
                            extra_body=thinking_extra_body("enabled"))
        client.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}])
        assert server.requests[0]["body"]["thinking"] == {"type": "enabled"}
    finally:
        server.shutdown()
        server.server_close()


def test_fallback_request_does_not_inherit_thinking():
    """fallback 不继承 ark 专属字段（PERF_SPEC §2 O1）。

    主客户端带 thinking extra_body 且重试耗尽 → fallback 接管：fallback 的
    请求体不得出现主实例的 thinking 字段（fallback 用自己的空 extra_body）。
    """
    base, server = _make_server([
        {"status": 503, "body": {"error": "down-1"}},
        {"status": 503, "body": {"error": "down-2"}},
        _chat_ok("fallback 接住了"),
    ])
    try:
        main_cfg = SimpleNamespace(api_base=base, api_key="sk-TEST",
                                   model="test-model", timeout=5,
                                   max_retries=1, retry_on_429=True)
        fallback_cfg = SimpleNamespace(api_base=base, api_key="sk-TEST",
                                       model="backup-model", timeout=5,
                                       max_retries=1, retry_on_429=True)
        main_client = ChatClient(main_cfg, provider_id="chat",
                                 extra_body={"thinking": {"type": "disabled"}})
        fallback = ChatClient(fallback_cfg, provider_id="backup")
        main_client.fallbacks.append(fallback)

        reply = main_client.chat([{"role": "user", "content": "hi"}])
        assert reply.content == "fallback 接住了"
        assert reply.provider_id == "backup"
        assert len(server.requests) == 3  # 主 2 次（首次+1 重试）+ fallback 1 次
        # 主请求体带 thinking；fallback 请求体绝不继承
        assert server.requests[0]["body"]["thinking"] == {"type": "disabled"}
        assert "thinking" not in server.requests[-1]["body"]
    finally:
        server.shutdown()
        server.server_close()


def test_caller_extra_body_overrides_client_default():
    """调用方每次调用的 extra_body 优先于客户端级默认字段。"""
    base, server = _make_server([_chat_ok("好")])
    try:
        cfg = SimpleNamespace(api_base=base, api_key="sk-TEST", model="test-model",
                              timeout=5, max_retries=1, retry_on_429=True)
        client = ChatClient(cfg, extra_body={"thinking": {"type": "disabled"}})
        client.chat([{"role": "user", "content": "hi"}],
                    extra_body={"thinking": {"type": "enabled"}})
        assert server.requests[0]["body"]["thinking"] == {"type": "enabled"}
    finally:
        server.shutdown()
        server.server_close()


def test_setup_wires_main_and_bg_clients_on_ctx():
    """setup(ctx)：ctx.chat_client / ctx.chat_client_bg 双实例按配置装配。"""
    cfg = VlConfig()
    cfg.chat = ChatProviderConfig(api_base="https://api.test/v1", api_key="sk-TEST",
                                  model="test-model", timeout=120)
    ctx = SimpleNamespace(cfg=cfg)
    llm_setup(ctx)
    assert ctx.chat_client.extra_body == {"thinking": {"type": "disabled"}}
    assert ctx.chat_client.timeout == 30.0
    assert ctx.chat_client_bg.extra_body == {"thinking": {"type": "enabled"}}
    assert ctx.chat_client_bg.timeout == 120.0
    assert ctx.embedding_client is not None


# ---------------------------------------------------------------------------
# §7.1-3 存量归一化
# ---------------------------------------------------------------------------


def _write_cfg(tmp_path: Path, cfg: VlConfig) -> Path:
    path = tmp_path / "vl.toml"
    path.write_text(dump_config(cfg), encoding="utf-8")
    return path


def _build_min_cfg() -> VlConfig:
    """最小合法配置（必填项齐、不含任何真实敏感值；新键一律不设=走默认）。"""
    from virtuallover.config import EmbeddingConfig, PersonaConfig
    return VlConfig(
        session_id="测试人格:FriendMessage:10000",
        admins=["10000"],
        persona=PersonaConfig(name="测试人格", system_prompt="你是「她」。"),
        chat=ChatProviderConfig(api_base="https://api.test/v1", api_key="sk-TEST",
                                model="test-model", timeout=120),
        embedding=EmbeddingConfig(api_base="https://emb.test/v1", api_key="sk-TEST",
                                  model="emb-model", dim=8, timeout=20),
    )


def test_normalize_legacy_words_count_threshold_100_to_30(tmp_path, caplog):
    """存量显式 100（旧默认）→ 归一化 30 + 告警明示（O2a 裁决落地）。"""
    cfg = _build_min_cfg()
    cfg.reply.segmented.words_count_threshold = 100
    path = _write_cfg(tmp_path, cfg)
    import logging
    with caplog.at_level(logging.WARNING, logger="virtuallover.config"):
        got = load_config(path)
    assert got.reply.segmented.words_count_threshold == 30
    assert any("words_count_threshold" in r.message for r in caplog.records)


def test_normalize_minimal_config_gets_o1_and_o2a_defaults(tmp_path):
    """旧配置缺新字段：thinking/bg_thinking/timeout_main/first_gap 全走新默认。"""
    cfg = _build_min_cfg()  # 不设任何新键（dataclass 默认即"缺字段"路径）
    path = _write_cfg(tmp_path, cfg)
    got = load_config(path)
    assert got.chat.thinking == "disabled"
    assert got.chat.bg_thinking == "enabled"
    assert got.chat.timeout_main == 30
    assert got.chat.timeout == 120
    assert got.reply.segmented.first_gap == "0.8-2.2"
    assert got.reply.segmented.words_count_threshold == 30


def test_normalize_invalid_thinking_falls_back_with_warning(tmp_path, caplog):
    """thinking 非法取值回退默认并告警（三态之外不进请求体）。"""
    cfg = _build_min_cfg()
    cfg.chat.thinking = "on"
    cfg.chat.bg_thinking = "true"
    path = _write_cfg(tmp_path, cfg)
    import logging
    with caplog.at_level(logging.WARNING, logger="virtuallover.config"):
        got = load_config(path)
    assert got.chat.thinking == "disabled"
    assert got.chat.bg_thinking == "enabled"
    assert any("thinking" in r.message for r in caplog.records)


def test_normalize_timeout_main_clamped(tmp_path, caplog):
    """timeout_main / timeout 越界 clamp + 告警（防 0/负数把请求瞬间掐死）。"""
    cfg = _build_min_cfg()
    cfg.chat.timeout_main = 0
    cfg.chat.timeout = 9999
    path = _write_cfg(tmp_path, cfg)
    import logging
    with caplog.at_level(logging.WARNING, logger="virtuallover.config"):
        got = load_config(path)
    assert got.chat.timeout_main == 5
    assert got.chat.timeout == 300
    assert any("timeout_main" in r.message for r in caplog.records)


def test_startup_log_prints_effective_values(tmp_path, caplog):
    """§7.1-3：启动日志打印生效值（部署后核对口径）。"""
    path = _write_cfg(tmp_path, _build_min_cfg())
    import logging
    with caplog.at_level(logging.INFO, logger="virtuallover.config"):
        got = load_config(path)
    assert any(
        "生效值" in r.message
        and str(got.chat.timeout_main) in r.message
        and str(got.reply.segmented.words_count_threshold) in r.message
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# O2a：首段抖动 / 段间隔下限
# ---------------------------------------------------------------------------


def test_sample_first_gap_uniform_in_range():
    params = SegmentedReplyConfig(first_gap="0.8-2.2")
    rng = random.Random(7)
    gaps = [sender_mod.sample_first_gap(params, rng) for _ in range(2000)]
    assert all(0.8 <= g <= 2.2 for g in gaps)
    assert min(gaps) < 1.1 and max(gaps) > 1.9  # 区间两端都被采到


def test_sample_first_gap_fixed_value_is_deterministic():
    assert sender_mod.sample_first_gap(SegmentedReplyConfig(first_gap="1.5-1.5")) == 1.5


def test_sample_first_gap_disabled_paths_return_zero():
    """空串（用户显式关）/ 缺属性（旧参数对象）/ 非法原文 → 0（关闭抖动）。"""
    assert sender_mod.sample_first_gap(SegmentedReplyConfig(first_gap="")) == 0.0
    assert sender_mod.sample_first_gap(SimpleNamespace()) == 0.0
    assert sender_mod.sample_first_gap(SegmentedReplyConfig(first_gap="garbage")) == 0.0
    # 抖动是增益项：区间里只有一个数也成立
    assert sender_mod.sample_first_gap(SegmentedReplyConfig(first_gap="2.2-0.8")) >= 0.8


class _RecordingClock:
    """手动推进的 monotonic 替身：记录每次读取值。"""

    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _SleepRecorder:
    """记录 asyncio.sleep 等价调用的替身（同时推进 clock）。"""

    def __init__(self, clock: _RecordingClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


def test_send_reply_waits_until_first_send_at():
    """首段发送时刻 = max(调用时刻, first_send_at)（§7.1-1 并行语义）。"""
    clock = _RecordingClock(start=100.0)
    sleep = _SleepRecorder(clock)
    sent: list[str] = []

    async def send(segment: str) -> None:
        sent.append(segment)

    async def drive() -> None:
        await sender_mod.send_reply(
            "第一句！第二句？", send,
            params=SegmentedReplyConfig(words_count_threshold=3,
                                        regex=r"[^！？]+[！？]"),
            first_send_at=101.5, clock=clock, sleeper=sleep)

    asyncio.run(drive())
    # LLM 若在 100s 时刻完成，首段等到 101.5 才发：恰一次 1.5s 的首段等待
    assert sleep.calls and abs(sleep.calls[0] - 1.5) < 1e-9
    assert sent == ["第一句！", "第二句？"]
    # 段间间隔照旧采样（1.5-3.5 默认区间，被 1.2s 下限覆盖后 ≥1.2）
    assert all(g >= sender_mod.MIN_SEGMENT_GAP_SECONDS for g in sleep.calls[1:])


def test_send_reply_skips_wait_when_first_send_at_already_passed():
    """LLM 完成晚于抖动窗口（first_send_at 已过）：零等待直接发。"""
    clock = _RecordingClock(start=200.0)
    sleep = _SleepRecorder(clock)
    sent: list[str] = []

    async def send(segment: str) -> None:
        sent.append(segment)

    async def drive() -> None:
        await sender_mod.send_reply(
            "一句话", send, params=SegmentedReplyConfig(),
            first_send_at=199.0, clock=clock, sleeper=sleep)

    asyncio.run(drive())
    assert sent == ["一句话"]
    assert sleep.calls == []  # 无任何等待


def test_segment_gap_clamped_to_min_1_2_seconds():
    """段间隔硬下限：配置写 0.3-0.8 也压到 ≥1.2s（O2a 拟人底线）。"""
    params = SegmentedReplyConfig(interval="0.3,0.8", min_gap=0.3, max_gap=0.8,
                                  interval_method="random")
    rng = random.Random(11)
    gaps = [sender_mod.sample_gap(params, rng) for _ in range(2000)]
    assert all(g >= sender_mod.MIN_SEGMENT_GAP_SECONDS for g in gaps)


def test_default_gap_distribution_unaffected_by_clamp():
    """默认 1.5-3.5 区间不受 1.2s 下限影响（区间本来就高于下限）。"""
    params = SegmentedReplyConfig(interval="1.5,3.5", min_gap=1.5, max_gap=3.5)
    rng = random.Random(3)
    gaps = [sender_mod.sample_gap(params, rng) for _ in range(1000)]
    assert all(1.5 <= g <= 3.5 for g in gaps)


# ---------------------------------------------------------------------------
# O2a：管线级——主回复抖动生效 / 命令路径不受抖动影响
# ---------------------------------------------------------------------------


class FakeBridge:
    """记录每次私聊文本发送时刻的假桥。"""

    def __init__(self) -> None:
        self.sent: list[tuple[float, str]] = []

    async def send_private_text(self, user_id: str, text: str):
        self.sent.append((time.monotonic(), text))
        return SimpleNamespace(ok=True, status="ok")


class FakeChat:
    """固定短延迟的假 chat 客户端（模拟 LLM 完成）。"""

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.calls = 0

    async def chat_async(self, messages, **kwargs):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return SimpleNamespace(content="好呀，我在呢！想你了呀~")


class FakeRouter:
    """命令直答假路由：/ping → 固定回复。"""

    def __init__(self) -> None:
        self.handled = 0

    async def handle(self, text, user_id, admins=None, whitelist=None):
        self.handled += 1
        if text == "/ping":
            return ["pong"]
        return None


def _pipeline_ctx(tmp_path, *, first_gap: str) -> SimpleNamespace:
    """最小管线 ctx：只有桥/chat/分段配置是活的，其余全部关闭。"""
    cfg = VlConfig(session_id="测试人格:FriendMessage:10000", admins=["10000"])
    cfg.persona.system_prompt = "你是「她」（测试占位人格）。"
    cfg.memes.enable = False
    cfg.reply.segmented.first_gap = first_gap
    ctx = SimpleNamespace()
    ctx.cfg = cfg
    ctx.bus = EventBus()
    ctx.repo_root = Path(tmp_path)
    ctx.bridge = FakeBridge()
    ctx.chat_client = FakeChat()
    ctx.chat_client_bg = None
    ctx.rate_limiter = None
    ctx.router = None
    ctx.mood_engine = None
    ctx.memory_store = None
    ctx.memory_service = None
    ctx.history = None
    ctx.proactive = None
    ctx.voice_manager = None
    return ctx


def _payload(text: str) -> dict:
    return {"type": "message", "user_id": "10000", "text": text}


def test_main_reply_first_segment_respects_jitter(tmp_path):
    """主回复首段发送时刻 ≥ 消息到达 + first_gap（抖动并行下限生效）。"""
    from virtuallover.__main__ import _handle_user_message

    ctx = _pipeline_ctx(tmp_path, first_gap="0.2-0.2")  # 固定 0.2s 抖动（快）
    t0 = time.monotonic()
    asyncio.run(_handle_user_message(ctx, _payload("在吗")))
    assert ctx.bridge.sent, "主回复应发出至少一段"
    first_send_at, first_text = ctx.bridge.sent[0]
    assert first_text.startswith("好呀")
    # LLM 0.05s 即完成，抖动 0.2s 是下限：首段不得早于 t0+0.2
    assert first_send_at - t0 >= 0.2 - 0.01


def test_main_reply_jitter_disabled_sends_right_after_llm(tmp_path):
    """first_gap 空串=关闭抖动：LLM 完成即发（无抖动等待）。"""
    from virtuallover.__main__ import _handle_user_message

    ctx = _pipeline_ctx(tmp_path, first_gap="")
    t0 = time.monotonic()
    asyncio.run(_handle_user_message(ctx, _payload("在吗")))
    assert ctx.bridge.sent
    first_send_at, _text = ctx.bridge.sent[0]
    assert first_send_at - t0 < 0.2  # 未触发抖动等待（LLM 0.05s + 调度余量）


def test_command_reply_bypasses_first_gap_jitter(tmp_path):
    """命令回复路径不注入抖动：first_gap 再大也立即直答（抖动只作用主回复）。"""
    from virtuallover.__main__ import _handle_user_message

    ctx = _pipeline_ctx(tmp_path, first_gap="5-5")  # 若误注入会等 5s
    ctx.router = FakeRouter()
    t0 = time.monotonic()
    asyncio.run(_handle_user_message(ctx, _payload("/ping")))
    assert ctx.router.handled == 1
    assert ctx.bridge.sent == [(ctx.bridge.sent[0][0], "pong")]
    assert ctx.bridge.sent[0][0] - t0 < 1.0  # 命令直答不受抖动影响
    assert ctx.chat_client.calls == 0  # 命令旁路：不进 LLM 轮


# ---------------------------------------------------------------------------
# §7.2 毫秒埋点
# ---------------------------------------------------------------------------


def test_created_at_millisecond_precision(tmp_path):
    """新写入行毫秒精度：0.5s 间隔的两条消息时间戳差 500ms（秒级会坍缩同秒）。"""
    ticks = iter([1770000000.0, 1770000000.5])
    store = HistoryStore(tmp_path / "h.db", mirror_path=None, clock=lambda: next(ticks))
    try:
        store.append("测试人格:FriendMessage:10000", "user", "第一条")
        store.append("测试人格:FriendMessage:10000", "assistant", "第二条")
        records = store.get_records("测试人格:FriendMessage:10000")
        assert records[0]["created_at"] == 1770000000000
        assert records[1]["created_at"] == 1770000000500
        assert records[1]["created_at"] - records[0]["created_at"] == 500
    finally:
        store.close()


def test_append_pair_shares_same_millisecond_stamp(tmp_path):
    store = HistoryStore(tmp_path / "h.db", mirror_path=None,
                         clock=lambda: 1770000000.25)
    try:
        store.append_pair("测试人格:FriendMessage:10000", "问", "答")
        records = store.get_records("测试人格:FriendMessage:10000")
        assert records[0]["created_at"] == records[1]["created_at"] == 1770000000250
    finally:
        store.close()


def test_mirror_ts_millisecond_precision(tmp_path):
    mirror = tmp_path / "mirror_v4.json"
    store = HistoryStore(tmp_path / "h.db", mirror_path=mirror,
                         clock=lambda: 1770000000.75)
    try:
        store.append("测试人格:FriendMessage:10000", "user", "带镜像")
    finally:
        store.close()
    line = json.loads(mirror.read_text(encoding="utf-8").splitlines()[0])
    assert line["ts"] == 1770000000750


def test_migrated_null_created_at_rows_preserved(tmp_path):
    """迁移旧行 created_at NULL 原样保留（§7.2：不回填不伪造）；新行照常毫秒。"""
    db = tmp_path / "h.db"
    # 先按迁移工具的 schema 建库（created_at 可空）并插 NULL 行——
    # 真实迁移库由 tools/history_migrate.py 建，HistoryStore 的
    # CREATE TABLE IF NOT EXISTS 不会覆盖它
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS messages ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,"
        " created_at INTEGER, meta TEXT)"
    )
    conn.execute(
        "INSERT INTO messages(session_id, role, content, created_at, meta) "
        "VALUES (?, ?, ?, ?, ?)",
        ("测试人格:FriendMessage:10000", "user", "迁移来的旧消息", None, "{}"),
    )
    conn.commit()
    conn.close()

    store = HistoryStore(db, mirror_path=None, clock=lambda: 1770000000.1)
    try:
        # 新消息照常写入（毫秒）；旧行 NULL 不被触碰
        store.append("测试人格:FriendMessage:10000", "user", "新消息")
        records = store.get_records("测试人格:FriendMessage:10000")
        assert records[0]["created_at"] is None  # 旧行 NULL 保留
        assert records[1]["created_at"] == 1770000000100  # 新行毫秒
    finally:
        store.close()
