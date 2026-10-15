# -*- coding: utf-8 -*-
"""主动消息测试（SPEC §8.3 八条行为逐条 + §12 行为断言）—— 时钟与 rng 全部注入，
不真等待、不发真实网络请求；发送/TTS/LLM/历史写回均为假件。
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from virtuallover.soul.proactive import (
    DEFAULT_QUIET_END,
    DEFAULT_QUIET_START,
    ProactiveParams,
    ProactiveService,
    ProactiveState,
    humanize_gap,
    is_quiet,
    parse_quiet_hours,
    render_prompt,
)

# 基准时刻（避开安静时段 23-7 的白昼时刻）
T0 = datetime(2026, 10, 10, 14, 0).timestamp()
T0_DT = datetime(2026, 10, 10, 14, 0)

SPECMPL = (
    "[系统任务：主动对话]\n当前时间是: {{current_time}}。\n"
    "他没有回复的次数是: {{unanswered_count}} 次。"
)


class _FixedRng:
    """确定 rng：uniform 恒返回 mid 值（调度测试用）。"""

    def __init__(self, value: float = 0.0):
        self.value = value

    def uniform(self, a: float, b: float) -> float:
        return a + (b - a) * self.value

    def randint(self, a: int, b: int) -> int:
        return a


class _Recorder:
    """记录各回调调用的假件容器。"""

    def __init__(self, llm_text: str = "好想你了，在做什么呀？", audio: bytes | None = b"WAV"):
        self.llm_calls: list[str] = []
        self.tts_calls: list[str] = []
        self.voice_calls: list[bytes] = []
        self.text_calls: list[str] = []
        self.history: list[tuple[str, str]] = []
        self.llm_text = llm_text
        self.audio = audio
        self.send_voice_ok = True
        self.send_text_ok = True

    async def llm_chat(self, prompt: str):
        self.llm_calls.append(prompt)
        return self.llm_text

    async def tts(self, text: str):
        self.tts_calls.append(text)
        return self.audio

    async def send_voice(self, data: bytes) -> bool:
        self.voice_calls.append(data)
        return self.send_voice_ok

    async def send_text(self, text: str) -> bool:
        self.text_calls.append(text)
        return self.send_text_ok

    async def append_history(self, role: str, content: str) -> None:
        self.history.append((role, content))


def _service(tmp_path, rec: _Recorder, params: ProactiveParams | None = None,
             rng=None) -> ProactiveService:
    return ProactiveService(
        params or ProactiveParams(prompt_template=SPECMPL),
        session_id="人格:FriendMessage:10001",
        state_path=tmp_path / "proactive_state.json",
        llm_chat=rec.llm_chat,
        tts=rec.tts,
        send_voice=rec.send_voice,
        send_text=rec.send_text,
        append_history=rec.append_history,
        rng=rng or _FixedRng(0.0),  # uniform(30,120)=30 分钟
    )


# ---------------------------------------------------------------------------
# ① 30-120min 随机间隔 + next_trigger_time 持久化重启恢复
# ---------------------------------------------------------------------------


class TestRule1IntervalAndPersistence:
    def test_interval_bounds(self, tmp_path):
        """随机间隔落在 [30, 120] 分钟区间内（rng 注入验证上下界）。"""
        rec = _Recorder()
        lo = _service(tmp_path, rec, rng=_FixedRng(0.0))
        next_ts = lo.schedule_next(T0)
        assert next_ts == T0 + 30 * 60
        hi = _service(tmp_path, _Recorder(), rng=_FixedRng(1.0))
        assert hi.schedule_next(T0) == T0 + 120 * 60

    def test_state_persisted_and_restored(self, tmp_path):
        """schedule_next 落盘；新实例（重启语义）读回 next_trigger_ts 与 unanswered。"""
        rec = _Recorder()
        svc = _service(tmp_path, rec)
        svc.state.unanswered = 2
        svc.schedule_next(T0)
        state_file = tmp_path / "proactive_state.json"
        assert state_file.exists()
        saved = json.loads(state_file.read_text(encoding="utf-8"))
        assert saved["next_trigger_ts"] == T0 + 30 * 60
        assert saved["unanswered"] == 2

        svc2 = ProactiveService(  # 不注入任何回调：仅测状态恢复
            ProactiveParams(), state_path=state_file, rng=_FixedRng(0.0)
        )
        assert svc2.state.next_trigger_ts == T0 + 30 * 60
        assert svc2.state.unanswered == 2

    def test_corrupted_state_starts_clean(self, tmp_path):
        path = tmp_path / "proactive_state.json"
        path.write_text("{broken", encoding="utf-8")
        svc = ProactiveService(ProactiveParams(), state_path=path, rng=_FixedRng(0.0))
        assert svc.state == ProactiveState()


# ---------------------------------------------------------------------------
# ② 安静时段 23-7 跳过重排（跨午夜语义）
# ---------------------------------------------------------------------------


class TestRule2QuietHours:
    def test_quiet_crosses_midnight(self):
        assert is_quiet(datetime(2026, 10, 10, 23, 0)) is True
        assert is_quiet(datetime(2026, 10, 10, 23, 59)) is True
        assert is_quiet(datetime(2026, 10, 11, 3, 0)) is True
        assert is_quiet(datetime(2026, 10, 11, 6, 59)) is True
        assert is_quiet(datetime(2026, 10, 11, 7, 0)) is False
        assert is_quiet(datetime(2026, 10, 10, 22, 59)) is False
        assert is_quiet(datetime(2026, 10, 10, 12, 0)) is False

    def test_quiet_same_start_end_disables(self):
        assert is_quiet(datetime(2026, 10, 10, 23, 0), 7, 7) is False

    def test_parse_quiet_hours(self):
        assert parse_quiet_hours("23-7") == (23, 7)
        assert parse_quiet_hours("2-6") == (2, 6)
        assert parse_quiet_hours("bad") == (23, 7)
        assert parse_quiet_hours(None) == (23, 7)

    async def _tick_at(self, tmp_path, dt: datetime):
        rec = _Recorder()
        svc = _service(tmp_path, rec)
        svc.state.next_trigger_ts = dt.timestamp()  # 直接到点
        sent = await svc.tick(dt.timestamp(), dt)
        return svc, rec, sent

    def test_quiet_skip_and_reschedule(self, tmp_path):
        """到点处于安静时段：不发送，重排下次（跳过重排）。"""
        svc, rec, sent = asyncio.run(self._tick_at(tmp_path, datetime(2026, 10, 10, 23, 30)))
        assert sent is None
        assert rec.llm_calls == [] and rec.text_calls == []
        assert svc.state.next_trigger_ts > datetime(2026, 10, 10, 23, 30).timestamp()

    def test_daytime_fires(self, tmp_path):
        """非安静时段到点：正常生成发送。"""
        svc, rec, sent = asyncio.run(self._tick_at(tmp_path, T0_DT))
        assert sent == rec.llm_text


# ---------------------------------------------------------------------------
# ③ unanswered>=3 暂停、回复清零
# ---------------------------------------------------------------------------


class TestRule3Unanswered:
    def test_pause_at_limit_enters_slowdown_not_silence(self, tmp_path):
        """R3-1c：unanswered 达 3 到点 → 不再永久沉默，发一条克制版消息（subdued）。"""
        rec = _Recorder()
        params = ProactiveParams(
            prompt_template=SPECMPL,
            prompt_template_slowdown="克制版：{{current_time}}，找过他 {{unanswered_count}} 次。",
        )
        svc = _service(tmp_path, rec, params=params)
        svc.state.unanswered = 3
        svc.state.next_trigger_ts = T0
        sent = asyncio.run(svc.tick(T0, T0_DT))
        assert sent == rec.llm_text          # 降频模式：克制版照发
        assert rec.llm_calls and "克制版：" in rec.llm_calls[0]  # 用了克制版模板
        assert svc.state.unanswered == 4     # R10：不再封顶，{{unanswered_count}} 展示真实值
        assert svc.state.next_trigger_ts > T0  # 降频区间重排（6-12h）

    def test_reply_resets_and_resumes(self, tmp_path):
        """R3-1b：用户回复 → 清零 + 按回复时刻重排；活跃避让窗过后到点可正常触发。"""
        rec = _Recorder()
        svc = _service(tmp_path, rec)
        svc.state.unanswered = 3
        svc.on_user_message(T0)
        assert svc.state.unanswered == 0
        # 回复时刻起算重排（FixedRng(0.0)：uniform(30,120)=30 分钟）
        assert svc.state.next_trigger_ts == T0 + 30 * 60
        # 回复后 1 分钟到点：活跃避让（<5min）→ 重排不发送
        svc.state.next_trigger_ts = T0 + 60
        assert asyncio.run(svc.tick(T0 + 60, T0_DT)) is None
        # 避让窗（5min）过后：正常触发
        svc.state.next_trigger_ts = T0 + 301
        sent = asyncio.run(svc.tick(T0 + 301, T0_DT))
        assert sent == rec.llm_text

    def test_unanswered_increments_after_send(self, tmp_path):
        """发送成功后 unanswered +1（主动找过他还没回复）。"""
        rec = _Recorder()
        svc = _service(tmp_path, rec)
        svc.state.next_trigger_ts = T0
        asyncio.run(svc.tick(T0, T0_DT))
        assert svc.state.unanswered == 1


# ---------------------------------------------------------------------------
# ④ 启动 30min 无消息触发一次
# ---------------------------------------------------------------------------


class TestRule4BootTrigger:
    def test_boot_trigger_fires_once_when_silent(self, tmp_path):
        """启动武装后 30min 无消息：到点触发一次，之后不重复。"""
        rec = _Recorder()
        svc = _service(tmp_path, rec)
        svc.arm_boot_trigger(T0)  # 启动武装
        # 29min59s：还没到点
        sent_early = asyncio.run(svc.tick(T0 + 30 * 60 - 1, T0_DT))
        assert sent_early is None and rec.llm_calls == []
        # 30min：触发
        sent = asyncio.run(svc.tick(T0 + 30 * 60, T0_DT))
        assert sent == rec.llm_text
        assert len(rec.llm_calls) == 1
        # 再次 tick：不重复触发（武装已解除）
        asyncio.run(svc.tick(T0 + 31 * 60, T0_DT))
        assert len(rec.llm_calls) == 1

    def test_boot_trigger_disarmed_by_user_message(self, tmp_path):
        """启动 30min 内来了用户消息：解除武装，不触发。"""
        rec = _Recorder()
        svc = _service(tmp_path, rec)
        svc.arm_boot_trigger(T0)
        svc.on_user_message(T0 + 60)  # 启动 1 分钟后来消息
        sent = asyncio.run(svc.tick(T0 + 30 * 60, T0_DT))
        assert sent is None and rec.llm_calls == []

    def test_boot_trigger_suppressed_in_quiet(self, tmp_path):
        """启动触发到点但处于安静时段：不发送（与 ② 一致）。"""
        rec = _Recorder()
        svc = _service(tmp_path, rec)
        svc.arm_boot_trigger(datetime(2026, 10, 10, 23, 0).timestamp())
        sent = asyncio.run(
            svc.tick(datetime(2026, 10, 10, 23, 30).timestamp(),
                     datetime(2026, 10, 10, 23, 30))
        )
        assert sent is None and rec.llm_calls == []


# ---------------------------------------------------------------------------
# ⑤ prompt 模板 {{current_time}}/{{unanswered_count}} 替换
# ---------------------------------------------------------------------------


class TestRule5PromptTemplate:
    def test_render_prompt(self):
        out = render_prompt(SPECMPL, datetime(2026, 10, 10, 14, 5), 2)
        assert "2026-10-10 14:05" in out
        assert "2 次" in out
        assert "{{" not in out and "}}" not in out

    def test_template_is_user_data_not_hardcoded(self):
        """模板文本由配置带入（用户数据），未配置时替换为空串不炸。"""
        assert render_prompt("", T0_DT, 0) == ""

    def test_prompt_used_in_generation(self, tmp_path):
        rec = _Recorder()
        svc = _service(tmp_path, rec)
        svc.state.unanswered = 1
        svc.state.next_trigger_ts = T0
        asyncio.run(svc.tick(T0, T0_DT))
        assert rec.llm_calls and "2026-10-10 14:00" in rec.llm_calls[0]
        assert "1 次" in rec.llm_calls[0]


# ---------------------------------------------------------------------------
# ⑥ LLM 生成中来新消息丢弃本次
# ---------------------------------------------------------------------------


class TestRule6DropOnNewMessage:
    def test_drop_when_message_arrives_during_generation(self, tmp_path):
        rec = _Recorder()

        class SlowLlm:
            def __init__(self, svc_ref):
                self.svc_ref = svc_ref

            async def __call__(self, prompt: str):
                # 模拟生成中途用户消息到达
                rec.llm_calls.append(prompt)
                self.svc_ref.on_user_message(T0 + 5)
                return rec.llm_text

        svc = _service(tmp_path, rec)
        svc.llm_chat = SlowLlm(svc)  # 生成期间注入新消息
        svc.state.next_trigger_ts = T0
        sent = asyncio.run(svc.tick(T0, T0_DT))
        assert sent is None
        assert rec.voice_calls == [] and rec.text_calls == []  # 未发送
        assert rec.history == []  # ⑦写回也不发生
        assert svc.state.next_trigger_ts > T0  # 仍排定下次


# ---------------------------------------------------------------------------
# ⑦ 发送后写回历史
# ---------------------------------------------------------------------------


class TestRule7WriteBackHistory:
    def test_appended_as_assistant_after_send(self, tmp_path):
        rec = _Recorder()
        svc = _service(tmp_path, rec)
        svc.state.next_trigger_ts = T0
        sent = asyncio.run(svc.tick(T0, T0_DT))
        assert sent is not None
        assert rec.history == [("assistant", rec.llm_text)]

    def test_no_writeback_when_send_fails(self, tmp_path):
        rec = _Recorder()
        rec.send_voice_ok = False
        rec.send_text_ok = False
        rec.audio = None  # TTS 也无产出 → 文字发送失败
        svc = _service(tmp_path, rec)
        svc.state.next_trigger_ts = T0
        sent = asyncio.run(svc.tick(T0, T0_DT))
        assert sent is None
        assert rec.history == []


# ---------------------------------------------------------------------------
# ⑧ TTS 优先（成功只发语音，dual_output=false）
# ---------------------------------------------------------------------------


class TestRule8TtsFirst:
    def test_tts_success_sends_voice_only(self, tmp_path):
        """TTS 成功且 always_send_text=false：只发语音，不发文字。"""
        rec = _Recorder()
        svc = _service(tmp_path, rec)
        svc.state.next_trigger_ts = T0
        asyncio.run(svc.tick(T0, T0_DT))
        assert rec.tts_calls == [rec.llm_text]
        assert rec.voice_calls == [b"WAV"]
        assert rec.text_calls == []  # 不发文字

    def test_tts_failure_falls_back_to_text(self, tmp_path):
        """TTS 失败（无音频产出）→ 回退纯文字。"""
        rec = _Recorder()
        rec.audio = None
        svc = _service(tmp_path, rec)
        svc.state.next_trigger_ts = T0
        asyncio.run(svc.tick(T0, T0_DT))
        assert rec.voice_calls == []
        assert rec.text_calls == [rec.llm_text]

    def test_tts_exception_falls_back_to_text(self, tmp_path):
        """TTS 抛异常 → 回退纯文字（不中断流程）。"""
        rec = _Recorder()

        async def bad_tts(text: str):
            raise RuntimeError("TTS 服务不可用")

        svc = _service(tmp_path, rec)
        svc.tts = bad_tts
        svc.state.next_trigger_ts = T0
        sent = asyncio.run(svc.tick(T0, T0_DT))
        assert sent == rec.llm_text
        assert rec.text_calls == [rec.llm_text]

    def test_tts_disabled_sends_text(self, tmp_path):
        """tts_enabled=false（群聊配置语义）：直接发文字。"""
        rec = _Recorder()
        params = ProactiveParams(prompt_template=SPECMPL, tts_enabled=False)
        svc = _service(tmp_path, rec, params=params)
        svc.state.next_trigger_ts = T0
        asyncio.run(svc.tick(T0, T0_DT))
        assert rec.tts_calls == []
        assert rec.text_calls == [rec.llm_text]

    def test_always_send_text_true_sends_both(self, tmp_path):
        """always_send_text=true：语音 + 文字双发（dual_output=true 语义）。"""
        rec = _Recorder()
        params = ProactiveParams(prompt_template=SPECMPL, always_send_text=True)
        svc = _service(tmp_path, rec, params=params)
        svc.state.next_trigger_ts = T0
        asyncio.run(svc.tick(T0, T0_DT))
        assert rec.voice_calls == [b"WAV"]
        assert rec.text_calls == [rec.llm_text]


# ---------------------------------------------------------------------------
# 调度循环与复入保护
# ---------------------------------------------------------------------------


class TestRunForever:
    def test_run_forever_boot_armed_and_scheduled(self, tmp_path):
        """启动即武装 ④ 并排定下次 ①；tick 推过 30min 后启动触发一次。"""
        rec = _Recorder()
        svc = _service(tmp_path, rec)
        stop = asyncio.Event()
        current = {"t": T0}
        rounds = {"n": 0}

        async def fake_sleep(seconds):
            rounds["n"] += 1
            if rounds["n"] == 1:
                # 第一次 tick（启动 tick，未到点）后把时钟直接推过启动触发点
                current["t"] = T0 + 31 * 60
            else:
                stop.set()

        asyncio.run(svc.run_forever(clock=lambda: current["t"], sleep=fake_sleep, stop=stop))
        assert svc._boot_fired is True   # ④启动触发已发生（武装随之解除）
        assert svc._boot_armed is False
        assert svc.state.next_trigger_ts >= T0 + 30 * 60  # ①发送成功后已排定下次
        assert len(rec.llm_calls) == 1  # ④只触发一次

    def test_generate_reentry_guard(self, tmp_path):
        """复入保护：同一服务同时只有一个生成流程。"""
        rec = _Recorder()
        svc = _service(tmp_path, rec)
        svc._generating = True
        sent = asyncio.run(svc.generate_and_send(now_ts=T0, now_dt=T0_DT))
        assert sent is None and rec.llm_calls == []


# ---------------------------------------------------------------------------
# 参数默认值与现网实测对齐
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_params_match_live_config(self):
        """默认参数=现网实测（30/120min、23-7、3 次、30min、TTS on、只发语音）。"""
        p = ProactiveParams()
        assert p.min_interval_minutes == 30
        assert p.max_interval_minutes == 120
        assert p.quiet_hours == "23-7"
        assert p.max_unanswered == 3
        assert p.boot_trigger_minutes == 30
        assert p.tts_enabled is True
        assert p.always_send_text is False


# ---------------------------------------------------------------------------
# PERF_SPEC §3-O2b：proactive 的 LLM 调用补 system=persona+mood 快照（装配层）
# ---------------------------------------------------------------------------

class _FakeBridge:
    """文字发送假件：任何段都成功。"""

    async def send_private_text(self, user_id: str, text: str):
        from types import SimpleNamespace
        return SimpleNamespace(ok=True)


class _SetupCtx:
    """proactive.setup(ctx) 的最小装配上下文假件（O2b 断言用）。"""

    def __init__(self, tmp_path, chat_client, mood_engine):
        from types import SimpleNamespace
        from virtuallover.config import SegmentedReplyConfig
        self.cfg = SimpleNamespace(
            proactive=SimpleNamespace(
                enable=True, session_id="人格:FriendMessage:10001",
                min_interval_minutes=30, max_interval_minutes=120,
                quiet_hours="23-7", max_unanswered=3, boot_trigger_minutes=30,
                prompt_template=SPECMPL, tts_enabled=False, always_send_text=True,
                segmented=SegmentedReplyConfig(enable=False),
            ),
            session_id="人格:FriendMessage:10001",
            persona=SimpleNamespace(system_prompt="你是测试人格，温柔的女生。",
                                    name="测试人格"),
            mood=SimpleNamespace(inject_enabled=True),
            storage=SimpleNamespace(data_dir=tmp_path.name),
        )
        self.repo_root = tmp_path
        self.chat_client = chat_client
        self.tts_client = None
        self.mood_engine = mood_engine
        self.voice_manager = None
        self.history = None
        self.bridge = _FakeBridge()
        self.rate_limiter = None


class _FakeChat:
    def __init__(self):
        self.calls: list[list[dict]] = []

    async def chat_async(self, messages, **kwargs):
        self.calls.append(messages)
        from types import SimpleNamespace
        return SimpleNamespace(content="宝宝在忙什么呀")


class _FakeMood:
    """render_fragment 尊重 inject_enabled 的最小假件（与 MoodEngine 同接口）。"""

    def __init__(self, fragment: str = "【当前内心状态】关系：亲密｜心情：开心（中等）｜连续互动 3 天"):
        self.fragment = fragment

    def render_fragment(self, user_id: str) -> str:
        return self.fragment


def test_proactive_llm_sends_persona_plus_mood_system(tmp_path):
    """O2b：LLM 请求补 system=persona 原文 + mood 快照（与主管线同源）。"""
    from virtuallover.soul import proactive as proactive_mod

    chat = _FakeChat()
    ctx = _SetupCtx(tmp_path, chat, _FakeMood())
    proactive_mod.setup(ctx)
    sent = asyncio.run(ctx.proactive.generate_and_send(now_ts=T0, now_dt=T0_DT))
    assert sent == "宝宝在忙什么呀"
    messages = chat.calls[0]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"].startswith("你是测试人格")
    assert "【当前内心状态】" in messages[0]["content"]  # mood 快照（R3 纯数据行）在场
    assert messages[1]["role"] == "user"
    assert "主动对话" in messages[1]["content"]  # user 侧仍是模板渲染 prompt
    assert "{{current_time}}" not in messages[1]["content"]  # 占位符已替换


def test_proactive_llm_mood_failure_degrades_to_persona_only(tmp_path):
    """mood 源故障降级为仅 persona（不阻断主动消息）。"""
    from virtuallover.soul import proactive as proactive_mod

    class _BrokenMood:
        def render_fragment(self, user_id: str) -> str:
            raise RuntimeError("mood db down")

    chat = _FakeChat()
    ctx = _SetupCtx(tmp_path, chat, _BrokenMood())
    proactive_mod.setup(ctx)
    asyncio.run(ctx.proactive.generate_and_send(now_ts=T0, now_dt=T0_DT))
    messages = chat.calls[0]
    assert messages[0]["content"] == "你是测试人格，温柔的女生。"  # 无 mood 段
    assert len(messages) == 2


def test_proactive_llm_without_mood_engine_still_has_persona(tmp_path):
    """无情绪引擎（未装配）时也必须有人格在 system（O2b 底线）。"""
    from virtuallover.soul import proactive as proactive_mod

    chat = _FakeChat()
    ctx = _SetupCtx(tmp_path, chat, None)
    proactive_mod.setup(ctx)
    asyncio.run(ctx.proactive.generate_and_send(now_ts=T0, now_dt=T0_DT))
    messages = chat.calls[0]
    assert messages[0] == {"role": "system", "content": "你是测试人格，温柔的女生。"}


# ---------------------------------------------------------------------------
# R10 主动陪伴升级：沉默递进三档 / {{last_user_gap}} 人话 / 表情包通道 / 深度素材
# ---------------------------------------------------------------------------


class TestProactiveStateNewFields:
    """R10 状态新字段：持久化 roundtrip + 旧状态文件缺字段兼容。"""

    def test_to_dict_roundtrip(self):
        state = ProactiveState(next_trigger_ts=1.5, unanswered=2,
                               last_user_ts=3.5, gave_up_date="2026-10-14")
        assert ProactiveState.from_dict(state.to_dict()) == state

    def test_old_state_file_without_new_fields(self, tmp_path):
        """旧栈状态文件没有 last_user_ts/gave_up_date：缺字段回退默认值。"""
        path = tmp_path / "proactive_state.json"
        path.write_text(json.dumps({"next_trigger_ts": 1.0, "unanswered": 1}),
                        encoding="utf-8")
        svc = ProactiveService(ProactiveParams(), state_path=path, rng=_FixedRng(0.0))
        assert svc.state.next_trigger_ts == 1.0
        assert svc.state.last_user_ts == 0.0
        assert svc.state.gave_up_date == ""

    def test_garbage_new_fields_fall_back(self):
        state = ProactiveState.from_dict({"last_user_ts": "abc", "gave_up_date": 123})
        assert state.last_user_ts == 0.0
        assert state.gave_up_date == "123"  # 非字符串宽容 str() 化，不炸

    def test_on_user_message_persists_last_user_ts(self, tmp_path):
        """on_user_message 落盘 last_user_ts（重启后沉默分档不丢）。"""
        svc = ProactiveService(ProactiveParams(), state_path=tmp_path / "s.json",
                               rng=_FixedRng(0.0))
        svc.on_user_message(T0)
        saved = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
        assert saved["last_user_ts"] == T0
        svc2 = ProactiveService(ProactiveParams(), state_path=tmp_path / "s.json",
                                rng=_FixedRng(0.0))
        assert svc2.state.last_user_ts == T0


class TestHumanizeGap:
    """{{last_user_gap}} 人话口径：<1h / N 小时 / 一天~三天 / N 天。"""

    @pytest.mark.parametrize("seconds,expected", [
        (0, "不到一小时"),
        (3599, "不到一小时"),
        (3600, "1 小时"),
        (6 * 3600, "6 小时"),
        (23 * 3600 + 59 * 60, "23 小时"),
        (24 * 3600, "一天"),
        (26 * 3600, "一天"),
        (48 * 3600, "两天"),
        (74 * 3600, "三天"),
        (96 * 3600, "4 天"),
        (25 * 86400, "25 天"),
    ])
    def test_cases(self, seconds, expected):
        assert humanize_gap(seconds) == expected


class TestRenderPromptLastUserGap:
    def test_placeholder_replaced(self):
        out = render_prompt("已经 {{last_user_gap}} 了", T0_DT, 1,
                            last_user_gap="6 小时")
        assert out == "已经 6 小时 了"

    def test_missing_placeholder_left_alone(self):
        assert render_prompt("正文", T0_DT, 0, last_user_gap="一天") == "正文"


class _Rng0:
    """确定 rng：uniform 恒取下界（担心档 → 12h 整）。"""

    def uniform(self, a: float, b: float) -> float:
        return a

    def randint(self, a: int, b: int) -> int:
        return a


class TestSilenceProgression:
    """沉默递进三档：<24h 常规 / ≥24h 担心型 + 12-24h 间隔 / ≥72h 告别后停排程。"""

    def _svc(self, tmp_path, rec, **overrides) -> ProactiveService:
        kwargs = dict(
            prompt_template=SPECMPL,
            prompt_template_worried="担心型：已经 {{last_user_gap}} 了",
        )
        kwargs.update(overrides)
        return ProactiveService(
            ProactiveParams(**kwargs),
            session_id="人格:FriendMessage:10001",
            state_path=tmp_path / "proactive_state.json",
            llm_chat=rec.llm_chat, tts=rec.tts,
            send_voice=rec.send_voice, send_text=rec.send_text,
            append_history=rec.append_history, rng=_Rng0(),
        )

    def test_below_worried_keeps_regular_band(self, tmp_path):
        """沉默 6 小时（<24h）：常规模板 + 常规 30-120min 间隔（现状不变）。"""
        rec = _Recorder()
        svc = self._svc(tmp_path, rec)
        svc.state.last_user_ts = T0 - 6 * 3600
        assert svc.schedule_next(T0) == T0 + 30 * 60
        svc.state.next_trigger_ts = T0
        asyncio.run(svc.tick(T0, T0_DT))
        assert "主动对话" in rec.llm_calls[0]              # 常规模板在用
        assert "担心型" not in rec.llm_calls[0]

    def test_worried_band_schedules_12h(self, tmp_path):
        """沉默 30 小时（≥24h）：担心档间隔放宽到 12-24h（rng 下界=12h）。"""
        rec = _Recorder()
        svc = self._svc(tmp_path, rec)
        svc.state.last_user_ts = T0 - 30 * 3600
        assert svc.schedule_next(T0) == T0 + 12 * 3600

    def test_worried_template_with_humanized_gap(self, tmp_path):
        """沉默 30 小时到点：担心型模板 + 「一天」人话 + 真实 unanswered。"""
        rec = _Recorder()
        svc = self._svc(tmp_path, rec)
        svc.state.last_user_ts = T0 - 30 * 3600
        svc.state.unanswered = 2
        svc.state.next_trigger_ts = T0
        sent = asyncio.run(svc.tick(T0, T0_DT))
        assert sent == rec.llm_text
        assert "担心型：已经 一天 了" in rec.llm_calls[0]
        assert svc.state.unanswered == 3

    def test_give_up_sends_final_goodbye_then_stops(self, tmp_path):
        """沉默 80 小时（≥72h）：担心模板追告别语气发最后一条 → 落账停排程。"""
        rec = _Recorder()
        svc = self._svc(tmp_path, rec)
        svc.state.last_user_ts = T0 - 80 * 3600
        svc.state.next_trigger_ts = T0
        sent = asyncio.run(svc.tick(T0, T0_DT))
        assert sent == rec.llm_text
        assert "最后一条主动消息" in rec.llm_calls[0]   # 告别语气指令在场
        assert svc.state.gave_up_date == datetime.fromtimestamp(T0).date().isoformat()
        assert svc.state.next_trigger_ts == 0.0          # 停止排程
        # 之后的 tick 一律安静（不再触发、不再调 LLM、不再重排）
        calls_after = len(rec.llm_calls)
        assert asyncio.run(svc.tick(T0 + 60, T0_DT)) is None
        assert len(rec.llm_calls) == calls_after
        assert svc.state.next_trigger_ts == 0.0

    def test_gave_up_survives_restart(self, tmp_path):
        """放弃期持久化：重启（新实例）后 tick 与 template_override 全部静默。"""
        rec = _Recorder()
        svc = self._svc(tmp_path, rec)
        svc.state.last_user_ts = T0 - 80 * 3600
        svc.state.next_trigger_ts = T0
        asyncio.run(svc.tick(T0, T0_DT))
        svc2 = self._svc(tmp_path, rec)
        assert svc2.state.gave_up_date == datetime.fromtimestamp(T0).date().isoformat()
        assert asyncio.run(svc2.tick(T0 + 120, T0_DT)) is None
        assert asyncio.run(svc2.generate_and_send(
            now_ts=T0 + 120, now_dt=T0_DT, template_override="外部仪式感")) is None
        assert rec.llm_calls  # 只有第一段的告别那一次

    def test_user_return_resumes_scheduling(self, tmp_path):
        """他回来（on_user_message）：放弃期解除、unanswered 清零、按回复时刻重排。"""
        rec = _Recorder()
        svc = self._svc(tmp_path, rec)
        svc.state.last_user_ts = T0 - 80 * 3600
        svc.state.unanswered = 3
        svc.state.gave_up_date = "2026-10-01"
        svc.on_user_message(T0 + 60)
        assert svc.state.gave_up_date == ""
        assert svc.state.unanswered == 0
        assert svc.state.next_trigger_ts == T0 + 60 + 30 * 60  # 常规档（刚回复）
        # 恢复后到点可正常触发
        svc.state.next_trigger_ts = T0 + 60 + 301
        sent = asyncio.run(svc.tick(T0 + 60 + 301, T0_DT))
        assert sent == rec.llm_text


class TestProactiveMemesChannel:
    """R10 表情包通道：&&tag&& 剥除 + 独立图片消息，与主回复管线同语义。"""

    def test_markers_stripped_and_image_sent(self, tmp_path):
        rec = _Recorder()

        class _Plan:
            text = "早安呀"
            image_path = Path("stub.jpg")

        images: "list[Path]" = []

        async def send_image(path):
            images.append(path)
            return True

        svc = ProactiveService(
            ProactiveParams(prompt_template=SPECMPL),
            session_id="人格:FriendMessage:10001",
            state_path=tmp_path / "proactive_state.json",
            llm_chat=rec.llm_chat, tts=rec.tts,
            send_voice=rec.send_voice, send_text=rec.send_text,
            append_history=rec.append_history,
            process_memes=lambda text: _Plan(), send_image=send_image,
            rng=_Rng0(),
        )
        svc.state.next_trigger_ts = T0
        sent = asyncio.run(svc.tick(T0, T0_DT))
        assert sent == "早安呀"                       # 文字按剥标记正文发送
        assert rec.tts_calls == ["早安呀"]            # TTS 念的也是干净正文
        assert rec.history == [("assistant", "早安呀")]
        assert images == [Path("stub.jpg")]           # 命中图片追加独立消息

    def test_without_memes_injection_text_untouched(self, tmp_path):
        """未注入 process_memes：正文原样（直驱测试桩/旧装配兼容）。"""
        rec = _Recorder()
        svc = _service(tmp_path, rec)
        svc.state.next_trigger_ts = T0
        sent = asyncio.run(svc.tick(T0, T0_DT))
        assert sent == rec.llm_text

    def test_empty_plan_text_abandons_send(self, tmp_path):
        """剥标记后正文为空（LLM 只输出了标记）：本次放弃，不发送不写历史。"""
        rec = _Recorder()

        class _EmptyPlan:
            text = ""
            image_path = None

        svc = ProactiveService(
            ProactiveParams(prompt_template=SPECMPL),
            session_id="人格:FriendMessage:10001",
            state_path=tmp_path / "proactive_state.json",
            llm_chat=rec.llm_chat, tts=rec.tts,
            send_voice=rec.send_voice, send_text=rec.send_text,
            append_history=rec.append_history,
            process_memes=lambda text: _EmptyPlan(),
            rng=_Rng0(),
        )
        svc.state.next_trigger_ts = T0
        sent = asyncio.run(svc.tick(T0, T0_DT))
        assert sent is None
        assert rec.text_calls == [] and rec.voice_calls == []
        assert rec.history == []


# ---------------------------------------------------------------------------
# R10 深度素材：每日一题 / 状态行 / 记忆检索（setup 装配面）
# ---------------------------------------------------------------------------


class _FakeMemoryStore:
    """MemoryStore 鸭类型替身：记录检索参数，返回固定命中。"""

    def __init__(self):
        self.calls: "list[tuple]" = []

    async def search(self, session_id, query, top_k=1, **kwargs):
        from types import SimpleNamespace
        self.calls.append((session_id, query, top_k))
        return [SimpleNamespace(
            id=1, session_id=session_id, content="他喜欢在深夜听歌",
            metadata={}, create_time=int(T0), cosine=0.9, keyword_hits=1)]


class _DeepCtx:
    """深度素材测试的装配上下文假件（在 _SetupCtx 基础上补 memory_store）。"""

    def __init__(self, tmp_path, chat, memory_store=None):
        from types import SimpleNamespace
        from virtuallover.config import SegmentedReplyConfig
        self.cfg = SimpleNamespace(
            proactive=SimpleNamespace(
                enable=True, session_id="人格:FriendMessage:10001",
                min_interval_minutes=30, max_interval_minutes=120,
                quiet_hours="23-7", max_unanswered=3, boot_trigger_minutes=30,
                prompt_template=SPECMPL, tts_enabled=False, always_send_text=True,
                segmented=SegmentedReplyConfig(enable=False),
                daily_topic=True,
            ),
            session_id="人格:FriendMessage:10001",
            persona=SimpleNamespace(system_prompt="你是测试人格，温柔的女生。",
                                    name="测试人格"),
            mood=SimpleNamespace(inject_enabled=True),
            storage=SimpleNamespace(data_dir=str(tmp_path)),
        )
        self.repo_root = tmp_path
        self.chat_client = chat
        self.tts_client = None
        self.mood_engine = None
        self.voice_manager = None
        self.history = None
        self.bridge = _FakeBridge()
        self.rate_limiter = None
        self.memory_store = memory_store


class TestSetupDeepMaterial:
    """_llm_chat 的 prompt 深度素材：(a) 每日一题 (b) 状态行 (c) 记忆 top-2。"""

    def test_daily_topic_and_memory_injected(self, tmp_path):
        from virtuallover.soul import proactive as proactive_mod

        chat = _FakeChat()
        store = _FakeMemoryStore()
        ctx = _DeepCtx(tmp_path, chat, memory_store=store)
        proactive_mod.setup(ctx)
        sent = asyncio.run(ctx.proactive.generate_and_send(now_ts=T0, now_dt=T0_DT))
        assert sent == "宝宝在忙什么呀"
        user_content = chat.calls[0][1]["content"]
        assert "想跟他聊的话题" in user_content          # (a) 每日一题在场
        assert "- [" in user_content and "他喜欢在深夜听歌" in user_content  # (c)
        assert store.calls and store.calls[0][2] == 2    # top-2 检索
        # 话题状态落 vl_data 目录（当天同题的幂等基础）
        assert (tmp_path / "daily_topic_state.json").exists()

    def test_schedule_line_injected_when_module_present(self, tmp_path, monkeypatch):
        """(b) soul/schedule 在场时状态行注入 prompt（模块缺席时静默跳过）。"""
        import types

        import virtuallover.soul as soul_pkg
        from virtuallover.soul import proactive as proactive_mod

        fake_schedule = types.ModuleType("virtuallover.soul.schedule")

        def _render_line(now, entries, *, enabled=True):
            # 签名对齐真实 soul/schedule.render_line(now, entries, *, enabled)
            assert enabled is True
            return "在图书馆自习"

        fake_schedule.render_line = _render_line
        monkeypatch.setattr(soul_pkg, "schedule", fake_schedule, raising=False)
        monkeypatch.setitem(sys.modules, "virtuallover.soul.schedule", fake_schedule)

        chat = _FakeChat()
        ctx = _DeepCtx(tmp_path, chat)
        proactive_mod.setup(ctx)
        asyncio.run(ctx.proactive.generate_and_send(now_ts=T0, now_dt=T0_DT))
        assert "在图书馆自习" in chat.calls[0][1]["content"]

    def test_schedule_real_module_end_to_end(self, tmp_path):
        """(b) 与真实 soul/schedule.render_line 联跑：全周全天课表条目命中。"""
        from types import SimpleNamespace

        from virtuallover.soul import proactive as proactive_mod

        chat = _FakeChat()
        ctx = _DeepCtx(tmp_path, chat)
        ctx.cfg.schedule = SimpleNamespace(
            enabled=True,
            entries=[{"days": "0,1,2,3,4,5,6", "start": "00:00", "end": "23:59",
                      "activity": "在宿舍摸鱼"}],
        )
        proactive_mod.setup(ctx)
        asyncio.run(ctx.proactive.generate_and_send(now_ts=T0, now_dt=T0_DT))
        assert "她此刻的状态" in chat.calls[0][1]["content"]
        assert "在宿舍摸鱼" in chat.calls[0][1]["content"]

    def test_schedule_module_missing_is_skipped(self, tmp_path, monkeypatch):
        """(b) soul/schedule 缺席（ImportError）：不注入也不炸。"""
        import virtuallover.soul as soul_pkg
        from virtuallover.soul import proactive as proactive_mod

        monkeypatch.setattr(soul_pkg, "schedule", None, raising=False)
        monkeypatch.setitem(sys.modules, "virtuallover.soul.schedule", None)

        chat = _FakeChat()
        ctx = _DeepCtx(tmp_path, chat)
        proactive_mod.setup(ctx)
        sent = asyncio.run(ctx.proactive.generate_and_send(now_ts=T0, now_dt=T0_DT))
        assert sent == "宝宝在忙什么呀"
        assert "她此刻的状态" not in chat.calls[0][1]["content"]

    def test_daily_topic_disabled_keeps_prompt_clean(self, tmp_path):
        """(a) daily_topic=false：不读话题池，prompt 不含话题行。"""
        from virtuallover.soul import proactive as proactive_mod

        chat = _FakeChat()
        ctx = _DeepCtx(tmp_path, chat)
        ctx.cfg.proactive.daily_topic = False
        proactive_mod.setup(ctx)
        asyncio.run(ctx.proactive.generate_and_send(now_ts=T0, now_dt=T0_DT))
        assert "想跟他聊的话题" not in chat.calls[0][1]["content"]
        assert not (tmp_path / "daily_topic_state.json").exists()

    def test_memory_store_missing_is_skipped(self, tmp_path):
        """(c) memory_store 缺席：跳过回忆注入，生成照常。"""
        from virtuallover.soul import proactive as proactive_mod

        chat = _FakeChat()
        ctx = _DeepCtx(tmp_path, chat, memory_store=None)
        proactive_mod.setup(ctx)
        asyncio.run(ctx.proactive.generate_and_send(now_ts=T0, now_dt=T0_DT))
        assert "共同回忆" not in chat.calls[0][1]["content"]

    def test_memory_search_failure_is_skipped(self, tmp_path):
        """(c) 检索抛异常：降级为不注入，生成照常（素材故障不阻断消息）。"""
        from virtuallover.soul import proactive as proactive_mod

        class _BrokenStore:
            async def search(self, *args, **kwargs):
                raise RuntimeError("embedding down")

        chat = _FakeChat()
        ctx = _DeepCtx(tmp_path, chat, memory_store=_BrokenStore())
        proactive_mod.setup(ctx)
        sent = asyncio.run(ctx.proactive.generate_and_send(now_ts=T0, now_dt=T0_DT))
        assert sent == "宝宝在忙什么呀"
        assert "共同回忆" not in chat.calls[0][1]["content"]
