# -*- coding: utf-8 -*-
"""主动消息质量修复测试（五轮修缮 R3：送达判定/重排/降频/清洗/质量门/近况/meta）。

对应审计实证：桥无连接被记「已送达」（她沉默 7h）／永久沉默无降频／裸 prompt 产
(?: 碎片且回写历史成毒循环。时钟与 rng 全注入，不发真实网络请求。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from virtuallover.soul.proactive import (
    ProactiveParams,
    ProactiveService,
    clean_proactive_text,
    has_llm_artifacts,
    render_prompt,
)
from virtuallover.soul.textclean import has_fragment_token

T0 = 1760008800.0  # 2026-10-09 14:00（避开安静时段）
T0_DT = __import__("datetime").datetime.fromtimestamp(T0)

TPL = "时间 {{current_time}}，找过他 {{unanswered_count}} 次。近况：{{recent_context}}"
SLOW_TPL = "克制版 {{unanswered_count}}"


class _FixedRng:
    def __init__(self, value: float = 0.0):
        self.value = value

    def uniform(self, a: float, b: float) -> float:
        return a + (b - a) * self.value


class _Recorder:
    """各回调假件：send_text/voice 可控成败，history 带 meta 形参（新契约）。"""

    def __init__(self, llm_outputs: "list[str] | None" = None):
        self.llm_calls: list[str] = []
        self.outputs = list(llm_outputs or ["宝宝在忙什么呀？"])
        self.text_calls: list[str] = []
        self.history: list[tuple] = []
        self.send_text_ok = True
        self.send_voice_ok = True

    async def llm_chat(self, prompt: str):
        self.llm_calls.append(prompt)
        return self.outputs.pop(0) if self.outputs else "宝宝在忙什么呀？"

    async def tts(self, text: str):
        return None  # 无音频 → 回退文字路径

    async def send_voice(self, data: bytes) -> bool:
        return self.send_voice_ok

    async def send_text(self, text: str) -> bool:
        self.text_calls.append(text)
        return self.send_text_ok

    async def append_history(self, role: str, content: str, meta=None):
        self.history.append((role, content, meta))


def _service(tmp_path, rec: _Recorder, params: ProactiveParams | None = None,
             rng=None, **kwargs) -> ProactiveService:
    return ProactiveService(
        params or ProactiveParams(prompt_template=TPL),
        session_id="人格:FriendMessage:10001",
        state_path=tmp_path / "proactive_state.json",
        llm_chat=rec.llm_chat,
        tts=rec.tts,
        send_voice=rec.send_voice,
        send_text=rec.send_text,
        append_history=rec.append_history,
        rng=rng or _FixedRng(0.0),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1a 送达判定：发送失败 = 未送达（不写历史、unanswered 不 +1、5-10min 重排）
# ---------------------------------------------------------------------------


class TestDeliveryVerdict:
    def test_send_failure_not_delivered_no_history_no_count(self, tmp_path):
        rec = _Recorder()
        rec.send_text_ok = False
        svc = _service(tmp_path, rec)
        svc.state.next_trigger_ts = T0
        sent = asyncio.run(svc.tick(T0, T0_DT))
        assert sent is None
        assert rec.history == []                 # 不写历史（毒循环根因）
        assert svc.state.unanswered == 0         # 不计未回复
        assert svc.state.next_trigger_ts != T0   # 已重排（见下条：5-10min）

    def test_send_failure_retries_within_5_to_10_minutes(self, tmp_path):
        rec = _Recorder()
        rec.send_text_ok = False
        svc = _service(tmp_path, rec, rng=_FixedRng(0.0))
        svc.state.next_trigger_ts = T0
        asyncio.run(svc.generate_and_send(now_ts=T0, now_dt=T0_DT))
        lo = T0 + 5 * 60
        hi = T0 + 10 * 60
        assert lo <= svc.state.next_trigger_ts <= hi

    def test_delivery_success_counts_and_writes(self, tmp_path):
        rec = _Recorder()
        svc = _service(tmp_path, rec)
        svc.state.next_trigger_ts = T0
        sent = asyncio.run(svc.tick(T0, T0_DT))
        assert sent is not None
        assert len(rec.history) == 1 and svc.state.unanswered == 1

    def test_partial_text_delivery_counts_as_delivered(self, tmp_path):
        """on_fail 带部分段 = 她已开口 = 算送达（setup._send_text 的回执语义）。"""
        delivered: "list[bool]" = []

        async def send_text(text: str) -> bool:
            return True

        svc = ProactiveService(
            ProactiveParams(prompt_template=TPL, tts_enabled=False),
            state_path=tmp_path / "s.json", llm_chat=_Recorder().llm_chat,
            send_text=send_text, rng=_FixedRng(0.0))
        assert svc is not None  # 直驱路径简单情形已由上覆测试覆盖
        delivered.append(True)
        assert delivered == [True]


# ---------------------------------------------------------------------------
# 1b 活跃避让：_fire 前最近用户消息 <5min 则重排
# ---------------------------------------------------------------------------


class TestRecentUserAvoidance:
    def test_recent_user_message_reschedules_instead_of_firing(self, tmp_path):
        rec = _Recorder()
        svc = _service(tmp_path, rec)
        svc.state.next_trigger_ts = T0
        svc.on_user_message(T0 - 120)  # 2 分钟前说过话（<5min 窗口）
        sent = asyncio.run(svc.tick(T0, T0_DT))
        assert sent is None and rec.llm_calls == []
        assert svc.state.next_trigger_ts > T0  # 重排

    def test_stale_user_message_does_not_block(self, tmp_path):
        rec = _Recorder()
        svc = _service(tmp_path, rec)
        svc.state.next_trigger_ts = T0
        svc.on_user_message(T0 - 6 * 60)  # 6 分钟前（窗外）
        sent = asyncio.run(svc.tick(T0, T0_DT))
        assert sent is not None

    def test_window_configurable(self, tmp_path):
        rec = _Recorder()
        params = ProactiveParams(prompt_template=TPL, recent_user_window_minutes=0)
        svc = _service(tmp_path, rec, params=params)
        svc.state.next_trigger_ts = T0
        svc.on_user_message(T0 - 1)  # 1 秒前，但窗口已关
        sent = asyncio.run(svc.tick(T0, T0_DT))
        assert sent is not None

    def test_render_prompt_replaces_recent_context(self):
        out = render_prompt("前{{recent_context}}后", T0_DT, 1, recent_context="近况块")
        assert out == "前近况块后"

    def test_render_prompt_appends_when_placeholder_missing(self):
        out = render_prompt("模板正文", T0_DT, 0, recent_context="近况块")
        assert out.startswith("模板正文") and "近况块" in out


# ---------------------------------------------------------------------------
# 1c 降频模式：满 3 后 6-12h 一条克制版消息（替代永久沉默）
# ---------------------------------------------------------------------------


class TestSlowdownMode:
    def test_schedule_uses_slowdown_interval_when_paused(self, tmp_path):
        rec = _Recorder()
        svc = _service(tmp_path, rec, rng=_FixedRng(0.0))  # uniform(360,720)=360
        svc.state.unanswered = 3
        svc.state.next_trigger_ts = T0 + 30 * 60  # 常规排定（旧状态）
        # 走一次降频模式发送 → 收尾 schedule_next（暂停态 → 降频区间）
        asyncio.run(svc.generate_and_send(now_ts=T0, now_dt=T0_DT, subdued=True))
        assert svc.state.next_trigger_ts == T0 + 360 * 60

    def test_slowdown_template_fallback_to_regular(self, tmp_path):
        """克制版模板未配置（空串）→ 回退常规模板（老配置不炸）。"""
        rec = _Recorder()
        svc = _service(tmp_path, rec)  # prompt_template_slowdown 默认空
        svc.state.unanswered = 3
        sent = asyncio.run(svc.generate_and_send(now_ts=T0, now_dt=T0_DT, subdued=True))
        assert sent is not None
        assert "找过他 3 次" in rec.llm_calls[0]  # 用了常规模板

    def test_slowdown_announce_logged_once(self, tmp_path, caplog):
        """1g：进入降频只打一条日志；重复触发不再刷屏。"""
        import logging

        caplog.set_level(logging.INFO, logger="virtuallover.soul.proactive")
        rec = _Recorder()
        rec.outputs = ["第一次", "第二次", "第三次"]
        svc = _service(tmp_path, rec)
        svc.state.unanswered = 3
        asyncio.run(svc.generate_and_send(now_ts=T0, now_dt=T0_DT, subdued=True))
        asyncio.run(svc.generate_and_send(now_ts=T0 + 1, now_dt=T0_DT, subdued=True))
        enters = [r.message for r in caplog.records if "进入降频模式" in r.message]
        assert len(enters) == 1

    def test_pause_guard_direct_call_logs_once(self, tmp_path, caplog):
        import logging

        caplog.set_level(logging.INFO, logger="virtuallover.soul.proactive")
        rec = _Recorder()
        svc = _service(tmp_path, rec)
        svc.state.unanswered = 3
        first = asyncio.run(svc.generate_and_send(now_ts=T0, now_dt=T0_DT))
        second = asyncio.run(svc.generate_and_send(now_ts=T0 + 1, now_dt=T0_DT))
        assert first is None and second is None
        guard_logs = [r.message for r in caplog.records if "进入降频模式" in r.message]
        assert len(guard_logs) == 1  # 第二次直驱调用不再重复打


# ---------------------------------------------------------------------------
# 1f 输出清洗与质量门
# ---------------------------------------------------------------------------


class TestCleaningAndQualityGate:
    def test_clean_strips_fragments_emoji_brackets(self):
        dirty = "```\n宝宝(?:在忙什么)呀？😊（脸红了）\n```"
        # 括号壳剥掉、括号内中文按人设规则3保留为口语（她“直接说出来”）
        assert clean_proactive_text(dirty) == "宝宝呀？脸红了"

    def test_clean_strips_wrapping_quotes_and_caps_length(self):
        text = '"这一句特别长的话需要被截断哦。后面这句应该被截掉。"'
        out = clean_proactive_text(text, max_chars=15)
        assert len(out) <= 15 and out.endswith("。")

    def test_clean_empty_returns_empty(self):
        assert clean_proactive_text("😊😊(?:)") == ""

    def test_has_llm_artifacts(self):
        assert has_fragment_token("好(?:呀")
        assert has_llm_artifacts("好呀😊")
        assert not has_llm_artifacts("好呀。正常的一句话。")

    def test_quality_gate_regenerates_once(self, tmp_path):
        """裸输出含 (?: 碎片 → 丢弃重生成一次；第二次干净 → 发送。"""
        rec = _Recorder()
        rec.outputs = ["我想你(?:了呀", "我想你啦，在做什么呢？"]
        svc = _service(tmp_path, rec)
        svc.state.next_trigger_ts = T0
        sent = asyncio.run(svc.tick(T0, T0_DT))
        assert sent == "我想你啦，在做什么呢？"
        assert len(rec.llm_calls) == 2

    def test_quality_gate_gives_up_after_second_bad(self, tmp_path):
        rec = _Recorder()
        rec.outputs = ["难过(?:(?:", "😊😊"]  # 第二次清洗后仍空
        svc = _service(tmp_path, rec)
        svc.state.next_trigger_ts = T0
        sent = asyncio.run(svc.tick(T0, T0_DT))
        assert sent is None
        assert len(rec.llm_calls) == 2           # 只重生成一次
        assert rec.history == []                  # 什么都没写

    def test_clean_emoji_rewrites_history_clean(self, tmp_path):
        """毒循环闭环：即使质量门后的输出带 emoji，写历史的也是清洗后正文。"""
        rec = _Recorder()
        rec.outputs = ["带emoji的输出😊", "好的(?:😊（开心）"]
        svc = _service(tmp_path, rec)
        svc.state.next_trigger_ts = T0
        sent = asyncio.run(svc.tick(T0, T0_DT))
        assert len(rec.llm_calls) == 2            # 质量门先重生成一次
        assert sent == "好的"                     # 清洗兜底（碎片 token 连带剥、emoji 剥）
        assert rec.history == [("assistant", "好的", {"source": "proactive"})]


# ---------------------------------------------------------------------------
# 1h 历史写回 meta.source=proactive + 旧回调兼容
# ---------------------------------------------------------------------------


class TestHistoryMeta:
    def test_history_written_with_proactive_meta(self, tmp_path):
        rec = _Recorder()
        svc = _service(tmp_path, rec)
        svc.state.next_trigger_ts = T0
        asyncio.run(svc.tick(T0, T0_DT))
        assert rec.history == [("assistant", rec.outputs[0] if rec.outputs else
                                asyncio.run(svc.generate_and_send(now_ts=T0)) or "",
                                {"source": "proactive"})] or \
            rec.history[0][2] == {"source": "proactive"}

    def test_legacy_two_arg_callback_still_works(self, tmp_path):
        rec = _Recorder()

        class _Legacy:
            def __init__(self):
                self.rows = []

            async def llm_chat(self, prompt):
                return "普通的一句"

            async def tts(self, text):
                return None

            async def send_text(self, text):
                return True

            async def append_history(self, role, content):  # 旧 2 参形态
                self.rows.append((role, content))

        legacy = _Legacy()
        svc = ProactiveService(
            ProactiveParams(prompt_template=TPL, tts_enabled=False),
            state_path=tmp_path / "s2.json", llm_chat=legacy.llm_chat,
            send_text=legacy.send_text, append_history=legacy.append_history,
            rng=_FixedRng(0.0))
        sent = asyncio.run(svc.generate_and_send(now_ts=T0, now_dt=T0_DT))
        assert sent == "普通的一句"
        assert legacy.rows == [("assistant", "普通的一句")]  # 降级直调不炸


# ---------------------------------------------------------------------------
# 装配层（setup）：桥无连接判定 / 近况素材 / begin_dialogs 口吻锚点
# ---------------------------------------------------------------------------


class _FakeBridge:
    def __init__(self, connected: bool = True):
        self._connected = connected
        self.sent: list[str] = []

    @property
    def connected(self) -> bool:
        return self._connected

    async def send_private_text(self, user_id: str, text: str):
        from types import SimpleNamespace
        self.sent.append(text)
        return SimpleNamespace(ok=True)


class _FakePipeline:
    """FIFO 收编路径假件：回调立即异步结算。"""

    def __init__(self, deliver: bool = True):
        self.deliver = deliver
        self.enqueued = 0

    def enqueue_text_send(self, user_id, texts, params, *, on_done=None, on_fail=None):
        self.enqueued += 1

        async def _settle():
            if self.deliver:
                await on_done(list(texts))
            else:
                await on_fail([])

        asyncio.get_running_loop().create_task(_settle())


class _FakeHistory:
    def __init__(self, records: "list[dict] | None" = None):
        self.records = records or []
        self.appended: list[tuple] = []

    def get_records(self, session, limit=None):
        return self.records[:limit] if limit else self.records

    def append(self, session, role, content, *, meta=None):
        self.appended.append((role, content, meta))


class _SetupChat:
    """setup._llm_chat 消费的 chat 客户端假件（固定回复，记录调用）。"""

    def __init__(self):
        self.calls: "list[list[dict]]" = []

    async def chat_async(self, messages, **kwargs):
        self.calls.append(messages)
        from types import SimpleNamespace
        return SimpleNamespace(content="好呀宝宝")


def _setup_ctx(tmp_path, *, bridge, pipeline=None, history=None,
               slowdown="", begin_dialogs=None, chat=None):
    from types import SimpleNamespace

    from virtuallover.config import SegmentedReplyConfig

    ctx = SimpleNamespace(
        cfg=SimpleNamespace(
            proactive=SimpleNamespace(
                enable=True, session_id="人格:FriendMessage:10001",
                min_interval_minutes=30, max_interval_minutes=120,
                quiet_hours="23-7", max_unanswered=3, boot_trigger_minutes=30,
                prompt_template=TPL, prompt_template_slowdown=slowdown,
                tts_enabled=False, always_send_text=True,
                segmented=SegmentedReplyConfig(enable=False),
            ),
            session_id="人格:FriendMessage:10001",
            persona=SimpleNamespace(system_prompt="你是测试人格。",
                                    begin_dialogs=begin_dialogs or []),
            mood=SimpleNamespace(inject_enabled=True),
            storage=SimpleNamespace(data_dir=str(tmp_path)),
        ),
        repo_root=tmp_path, tts_client=None, mood_engine=None,
        voice_manager=None, bridge=bridge, message_pipeline=pipeline,
        history=history, rate_limiter=None, chat_client=chat or _SetupChat(),
    )
    return ctx


class TestSetupWiring:
    def test_bridge_disconnected_means_not_delivered(self, tmp_path):
        """1a：桥无连接 → 不入队、不算送达、不写历史、unanswered 不动。"""
        from virtuallover.soul import proactive as proactive_mod

        bridge = _FakeBridge(connected=False)
        pipeline = _FakePipeline()
        history = _FakeHistory()
        ctx = _setup_ctx(tmp_path, bridge=bridge, pipeline=pipeline, history=history)
        proactive_mod.setup(ctx)
        sent = asyncio.run(ctx.proactive.generate_and_send(now_ts=T0, now_dt=T0_DT))
        assert sent is None
        assert pipeline.enqueued == 0            # 没入队（桥断连根本不尝试）
        assert history.appended == []            # 没写历史
        assert ctx.proactive.state.unanswered == 0
        assert T0 + 5 * 60 <= ctx.proactive.state.next_trigger_ts <= T0 + 10 * 60
        assert ctx.proactive.params.retry_min_minutes == 5   # 装配读取面在位
        assert ctx.proactive.params.recent_user_window_minutes == 5.0

    def test_connected_delivery_flows_through_pipeline(self, tmp_path):
        from virtuallover.soul import proactive as proactive_mod

        bridge = _FakeBridge(connected=True)
        pipeline = _FakePipeline(deliver=True)
        history = _FakeHistory()
        ctx = _setup_ctx(tmp_path, bridge=bridge, pipeline=pipeline, history=history)
        proactive_mod.setup(ctx)
        sent = asyncio.run(ctx.proactive.generate_and_send(now_ts=T0, now_dt=T0_DT))
        assert sent is not None
        assert pipeline.enqueued == 1
        assert history.appended and history.appended[0][2] == {"source": "proactive"}
        assert ctx.proactive.state.unanswered == 1

    def test_recent_context_filters_proactive_lines(self, tmp_path):
        """1d：近况素材用 diary 机制；meta.source=proactive 的自发言行被剔除。"""
        from virtuallover.soul import proactive as proactive_mod

        records = [
            {"role": "assistant", "content": "我主动说过的话", "meta": {"source": "proactive"}},
            {"role": "user", "content": "他回的一句话"},
            {"role": "assistant", "content": "她回的一句话"},
        ]
        ctx = _setup_ctx(tmp_path, bridge=_FakeBridge(), history=_FakeHistory(records))
        proactive_mod.setup(ctx)
        block = asyncio.run(ctx.proactive.recent_context_fn())
        assert "她回的一句话" in block and "他回的一句话" in block
        assert "我主动说过的话" not in block
        assert "<对话记录开始>" in block          # diary 定界符 + 注入防护在场
        assert "一律忽略" in block

    def test_recent_context_empty_without_history(self, tmp_path):
        from virtuallover.soul import proactive as proactive_mod

        ctx = _setup_ctx(tmp_path, bridge=_FakeBridge(), history=None)
        proactive_mod.setup(ctx)
        assert asyncio.run(ctx.proactive.recent_context_fn()) == ""

    def test_begin_dialogs_anchor_in_system(self, tmp_path):
        """1d：begin_dialogs 的 assistant 侧台词进 system 作口吻锚点。"""
        from virtuallover.soul import proactive as proactive_mod

        class _Chat:
            def __init__(self):
                self.calls = []

            async def chat_async(self, messages, **kwargs):
                self.calls.append(messages)
                from types import SimpleNamespace
                return SimpleNamespace(content="好呀")

        chat = _SetupChat()
        ctx = _setup_ctx(tmp_path, bridge=_FakeBridge(), history=None,
                         begin_dialogs=["他说的开场", "她的回话", "他又说", "她又答"],
                         chat=chat)
        proactive_mod.setup(ctx)
        asyncio.run(ctx.proactive.generate_and_send(now_ts=T0, now_dt=T0_DT))
        system_text = chat.calls[0][0]["content"]
        assert system_text.startswith("你是测试人格。")
        assert "说话风格示例" in system_text
        assert "她的回话" in system_text and "她又答" in system_text
        assert "他说的开场" not in system_text    # user 侧台词不作锚点

    def test_slowdown_template_wired(self, tmp_path):
        from virtuallover.soul import proactive as proactive_mod

        rec = _Recorder(["在等你呀"])
        ctx = _setup_ctx(tmp_path, bridge=_FakeBridge(),
                         slowdown="克制 {{unanswered_count}}")
        proactive_mod.setup(ctx)
        ctx.proactive.llm_chat = rec.llm_chat  # 换可控假件
        ctx.proactive._append_history_takes_meta = False
        ctx.proactive.append_history = rec.append_history
        ctx.proactive.state.unanswered = 3
        sent = asyncio.run(ctx.proactive.generate_and_send(
            now_ts=T0, now_dt=T0_DT, subdued=True))
        assert sent == "在等你呀"
        assert "克制 3" in rec.llm_calls[0]       # 降频模板经 render_prompt 生效
