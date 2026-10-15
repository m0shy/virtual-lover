# -*- coding: utf-8 -*-
"""语音体验修缮测试（五轮修缮 R3：TTS 清洗/语音并行/映射表/情绪灵敏度/送达队列）。

对应审计实证：Huohua 音库情绪语义错配（calm→冷御姐而 calm 是常态 15/16）、
emoji/颜文字/(?: 残留念进语音、语音总在全文发完才合成。
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from virtuallover.brain.pipeline import MessagePipeline, _SendJob, _UserRuntime
from virtuallover.config import SegmentedReplyConfig, load_config
from virtuallover.soul.mood import (
    EVENT_KEYWORD_PRAISE,
    EVENT_KEYWORD_SWEET,
    EVENT_MESSAGE,
    Event,
    MoodState,
    MoodEngine,
    apply_event,
)
from virtuallover.soul.textclean import clean_proactive_text, clean_tts_text
from virtuallover.voice.manager import VoiceManager
from virtuallover.voice.mapping import DEFAULT_EMOTION_MAP, resolve_emotion

# ---------------------------------------------------------------------------
# 2b TTS 文本清洗
# ---------------------------------------------------------------------------


class TestTtsTextClean:
    def test_strips_emoji_fragments_brackets(self):
        assert clean_tts_text("宝宝(?:在忙什么)呀？😊") == "宝宝呀？"
        assert clean_tts_text("(｡･ω･｡)ﾉ❤ 早上好呀") == "早上好呀"

    def test_keeps_sentence_punctuation(self):
        """句末标点是 TTS 韵脚载体——清洗绝不剥。"""
        assert clean_tts_text("好想你呀！在做什么呢？") == "好想你呀！在做什么呢？"

    def test_no_truncation(self):
        long = "这一段很长。" * 20
        assert clean_tts_text(long) == long

    def test_empty_after_clean_returns_empty(self):
        assert clean_tts_text("😊😊(?:)") == ""
        assert clean_tts_text("   ") == ""

    def test_proactive_clean_is_stricter(self):
        """proactive 正文清洗额外收敛长度（发出去的文字 vs 念出来的语音）。"""
        out = clean_proactive_text("一句话。" * 30, max_chars=40)
        assert len(out) <= 40


class TestManagerDirtySkip:
    def _manager(self, synth_log, voice_log):
        async def synth(text, snapshot=None):
            synth_log.append(text)
            return b"WAV"

        async def send_voice(user_id, audio):
            voice_log.append(audio)
            return True

        return VoiceManager(send_voice=send_voice, synth=synth,
                            trigger_probability=1.0)

    def test_dirty_segment_skips_synthesis(self):
        """清洗后为空的段直接跳过合成（宁可不念，不念乱码）。"""
        synth_log: list = []
        voice_log: list = []
        manager = self._manager(synth_log, voice_log)

        async def run():
            manager.enqueue_reply("10001", ["😊😊(?:)", "正常的一句呀"])
            await manager._queues["10001"].join()
            await manager.aclose()

        asyncio.run(run())
        assert synth_log == ["正常的一句呀"]
        assert manager.stats["dirty_skips"] == 1
        assert manager.stats["segments_sent"] == 1

    def test_proactive_dirty_text_falls_back_to_text(self):
        """主动消息整条清洗后为空 → 跳过合成、回退文字（原文交给文字轨）。"""
        synth_log: list = []
        text_log: list = []

        async def synth(text, snapshot=None):
            synth_log.append(text)
            return b"WAV"

        async def send_text(text):
            text_log.append(text)
            return True

        manager = VoiceManager(send_voice=lambda u, a: asyncio.sleep(0, True),
                               synth=synth, trigger_probability=1.0)

        async def run():
            sent = await manager.deliver_proactive(
                "10001", "😊(?:)", send_text=send_text)
            await manager.aclose()
            return sent

        assert asyncio.run(run()) is True
        assert synth_log == []          # 没合成
        assert text_log == ["😊(?:)"]   # 文字轨拿原文（proactive 侧另有清洗/质量门）

    def test_proactive_clean_text_goes_to_synth(self):
        synth_log: list = []
        voice_log: list = []
        text_log: list = []

        async def synth(text, snapshot=None):
            synth_log.append(text)
            return b"WAV"

        async def send_voice(user_id, audio):
            voice_log.append(audio)
            return True

        manager = VoiceManager(send_voice=send_voice, synth=synth,
                               trigger_probability=1.0)

        async def run():
            sent = await manager.deliver_proactive("10001", "想你了😊，在忙吗？")
            await manager.aclose()
            return sent

        assert asyncio.run(run()) is True
        assert synth_log == ["想你了，在忙吗？"]  # 清洗后合成
        assert voice_log and text_log == []


# ---------------------------------------------------------------------------
# 2c 语音并行：首段文字出帧即入队合成（不等全文）；失败短路收走排队作业
# ---------------------------------------------------------------------------


class _ParallelBridge:
    """可控成败的桥假件：ok_until 之后每段都失败。"""

    def __init__(self, ok_until: int = 99):
        self.ok_until = ok_until
        self.sent: list[str] = []

    async def send_private_text(self, user_id, text):
        from types import SimpleNamespace
        if len(self.sent) >= self.ok_until:
            return SimpleNamespace(ok=False, status="send-failed")
        self.sent.append(text)
        return SimpleNamespace(ok=True)


class _FakeVoiceManager:
    def __init__(self):
        self.enqueued: list = []
        self.dropped = 0

    def enqueue_reply(self, user_id, segments, mood_snapshot=None):
        self.enqueued.append((user_id, list(segments)))

    def drop_queued(self, user_id):
        self.dropped += 1
        return self.dropped


def _pipeline_ctx(bridge, voice):
    return SimpleNamespace(bridge=bridge, rate_limiter=None,
                           voice_manager=voice,
                           cfg=SimpleNamespace(session_id="测试:FriendMessage:10001"))


@pytest.fixture
def fast_gap(monkeypatch):
    """段间隔压到 0.01s（真实间隔 ≥1.2s 下限，测试不等它）。

    签名跟随管线新调用形态（打字节奏：调用方传下一段长度 next_len，
    brain/sender.sample_gap docstring）——本组测试不消费段长，吞掉即可。
    """
    from virtuallover.brain import sender

    monkeypatch.setattr(
        sender, "sample_gap", lambda params, rng=None, next_len=None: 0.01)


class TestParallelVoice:
    def test_on_first_sent_fires_before_finish(self, fast_gap):
        """首段出帧即触发 on_first_sent（完整段列表），早于发送收尾。"""
        bridge = _ParallelBridge()
        voice = _FakeVoiceManager()
        pipeline = MessagePipeline(_pipeline_ctx(bridge, voice))
        events: list = []

        async def on_first_sent(segments):
            events.append(("first", list(segments)))

        async def on_done(segments):
            events.append(("done", list(segments)))

        job = _SendJob(user_id="10001", segments=["一", "二", "三"],
                       params=SegmentedReplyConfig(first_gap=""),
                       on_done=on_done, on_first_sent=on_first_sent)
        st = _UserRuntime("10001")

        async def run():
            await pipeline._send_job(st, job)

        asyncio.run(run())
        assert bridge.sent == ["一", "二", "三"]
        assert [name for name, _ in events] == ["first", "done"]
        assert events[0][1] == ["一", "二", "三"]  # 完整计划段列表（并行合成全部段）

    def test_run_round_enqueues_voice_at_first_segment(self, fast_gap, monkeypatch):
        """端到端：一轮回复里，语音在第 1 段出帧时入队（不等全文发完）。"""
        class _Chat:
            async def chat_async(self, messages, **kwargs):
                return SimpleNamespace(content="第一句话在这里呀。第二句话也在这里呢。第三句话还是在这里哦。")

        bridge = _ParallelBridge()
        voice = _FakeVoiceManager()
        cfg = SimpleNamespace(
            session_id="测试:FriendMessage:10001",
            reply=SimpleNamespace(segmented=SegmentedReplyConfig(
                enable=True, words_count_threshold=5, first_gap="",
                split_mode="regex", regex=r"[^\n]+?[。？！…]|[^\n]+")),
            persona=SimpleNamespace(system_prompt="测试人格。", begin_dialogs=[], name="测试人格"),
            context=SimpleNamespace(max_context_tokens=4096, truncate_ratio=0.82),
            mnemosyne=SimpleNamespace(inject_enabled=False, top_k=3),
            mood=SimpleNamespace(inject_enabled=False, fragment_position="user"),
            memes=SimpleNamespace(enable=False, probability=0, max_images=0,
                                  memes_dir="", extra_tags=[]),
        )
        ctx = SimpleNamespace(bridge=bridge, rate_limiter=None, voice_manager=voice,
                              chat_client=_Chat(), history=None, memory_service=None,
                              mood_engine=None, cfg=cfg, now_fn=lambda: 1000.0,
                              repo_root=REPO_ROOT)
        pipeline = MessagePipeline(ctx)

        async def run():
            st = pipeline._user("10001")
            st.gen = 1
            st.state = "RUNNING"
            st.round_msgs = [SimpleNamespace(
                text="在吗", reply_id=None, reply_text=None, message_id="-",
                arrived_ts=1000.0, arrived_mono=0.0, row_id=None,
                display_text="在吗")]
            await pipeline._run_round("10001", 1)
            # 发送 worker 不调用 task_done()，join() 不可用——轮询出帧结果
            async def _wait_sent():
                while len(bridge.sent) < 3:
                    await asyncio.sleep(0.02)
            await asyncio.wait_for(_wait_sent(), timeout=5)
            await asyncio.sleep(0.05)  # 收尾回调落地
            await pipeline.aclose()

        asyncio.run(run())
        assert len(voice.enqueued) == 1
        user_id, segments = voice.enqueued[0]
        assert user_id == "10001"
        assert len(segments) == 3  # 完整计划段一次入队（合成全程并行）

    def test_fail_shortcut_drops_queued_voice(self, fast_gap):
        """发送失败短路：on_fail 收到已出帧段；排队语音作业被收走。"""
        bridge = _ParallelBridge(ok_until=1)  # 第 2 段起失败
        voice = _FakeVoiceManager()
        pipeline = MessagePipeline(_pipeline_ctx(bridge, voice))
        events: list = []

        async def on_first_sent(segments):
            events.append(("first", list(segments)))

        async def on_fail(segments):
            events.append(("fail", list(segments)))
            voice.drop_queued("10001")

        job = _SendJob(user_id="10001", segments=["一", "二", "三"],
                       params=SegmentedReplyConfig(first_gap=""),
                       on_fail=on_fail, on_first_sent=on_first_sent)
        st = _UserRuntime("10001")

        async def run():
            await pipeline._send_job(st, job)

        asyncio.run(run())
        assert bridge.sent == ["一"]
        assert [name for name, _ in events] == ["first", "fail"]
        assert voice.dropped == 1

    def test_drop_queued_clears_only_queue_not_inflight(self):
        manager = VoiceManager(send_voice=lambda u, a: asyncio.sleep(0, True),
                               synth=None, trigger_probability=1.0)

        async def run():
            assert manager.enqueue_reply("10001", ["一", "二"]) is True
            assert manager.enqueue_reply("10001", ["三"]) is True
            dropped = manager.drop_queued("10001")
            assert dropped == 2
            assert manager._queues["10001"].empty()
            assert manager.drop_queued("unknown") == 0
            await manager.aclose()

        asyncio.run(run())
        assert manager.stats["queue_dropped"] == 2


# ---------------------------------------------------------------------------
# 2a 映射表（参谋听感结论）+ vl.toml/控制台单一来源同步
# ---------------------------------------------------------------------------


class TestEmotionMapR3:
    def test_new_default_table(self):
        assert DEFAULT_EMOTION_MAP["calm"] == "聊天"
        assert DEFAULT_EMOTION_MAP["shy"] == "傻白甜"
        assert DEFAULT_EMOTION_MAP["excited"] == "傻白甜"
        # 未涉及行保持终裁值
        assert DEFAULT_EMOTION_MAP["happy"] == "傻白甜"
        assert DEFAULT_EMOTION_MAP["angry"] == "疯批"
        assert DEFAULT_EMOTION_MAP["sad"] == "慢速病娇"
        assert DEFAULT_EMOTION_MAP["lonely"] == "平常的"
        # 情绪丰富化 7 态并入默认表（与 soul.mood.MOOD_ENRICHED / 运行态 vl.toml 一致）
        assert DEFAULT_EMOTION_MAP["wronged"] == "慢速病娇"
        assert DEFAULT_EMOTION_MAP["lost"] == "慢速病娇"
        assert DEFAULT_EMOTION_MAP["jealous"] == "疯批"
        assert DEFAULT_EMOTION_MAP["expect"] == "傻白甜"
        assert DEFAULT_EMOTION_MAP["sweet"] == "傻白甜"
        assert DEFAULT_EMOTION_MAP["tsundere"] == "傻白甜"
        assert DEFAULT_EMOTION_MAP["secure"] == "聊天"

    def test_all_values_registered_for_huohua(self):
        registry = {"default", "傻白甜", "疯批", "慢速病娇", "平常的", "聊天", "平静的"}
        for mood, emotion in DEFAULT_EMOTION_MAP.items():
            assert emotion in registry, f"{mood}→{emotion} 未注册"

    @pytest.mark.skipif(not (REPO_ROOT / "local" / "config" / "vl.toml").exists(), reason="live local config not present in clean clone")
    def test_vl_toml_layer_synced(self):
        """vl.toml 的 emotion_map 与代码默认表一致（控制台映射卡=默认表←vl.toml←覆盖层）。"""
        from virtuallover.console import voice as console_voice

        cfg = load_config(REPO_ROOT / "local" / "config" / "vl.toml")
        assert cfg.tts.emotion_map == DEFAULT_EMOTION_MAP
        assert console_voice.effective_emotion_map(cfg.tts, None) == DEFAULT_EMOTION_MAP

    def test_calm_resolves_to_chat(self):
        got = resolve_emotion("calm", registry=list(DEFAULT_EMOTION_MAP.values()) + ["default"])
        assert got.emotion == "聊天" and not got.degraded


# ---------------------------------------------------------------------------
# 2d 情绪灵敏度：_calm_down 0.1→0.04（可配）+ keyword_groups 扩充
# ---------------------------------------------------------------------------


class TestMoodSensitivity:
    def test_default_calm_down_is_004(self):
        s1 = apply_event(MoodState(mood_intensity=0.6), Event(EVENT_MESSAGE, timestamp=1.0))
        assert s1.mood_intensity == pytest.approx(0.56)

    def test_calm_down_configurable(self, tmp_path):
        engine = MoodEngine(tmp_path, config={"calm_down_per_message": 0.5})
        assert engine.calm_down_per_message == 0.5
        s1 = apply_event(MoodState(mood_intensity=0.6),
                         Event(EVENT_MESSAGE, timestamp=1.0),
                         calm_down_per_message=engine.calm_down_per_message)
        assert s1.mood_intensity == pytest.approx(0.1)

    def test_invalid_config_falls_back(self, tmp_path):
        engine = MoodEngine(tmp_path, config={"calm_down_per_message": "abc"})
        assert engine.calm_down_per_message == 0.04

    def test_expanded_keyword_groups(self, tmp_path):
        groups = {
            "sweet": "亲爱的|宝贝|喜欢你|抱抱|亲亲|想你啦",
            "praise": "好看|厉害|想你|爱你|贴心|温柔|厉害了|棒",
            "rude": "闭嘴|烦|滚",
        }
        engine = MoodEngine(tmp_path, config={"keyword_groups": groups})
        assert engine.classify("抱抱我呀") == Event(EVENT_KEYWORD_SWEET)
        assert engine.classify("你真温柔") == Event(EVENT_KEYWORD_PRAISE)
        assert engine.classify("你好贴心") == Event(EVENT_KEYWORD_PRAISE)
        assert engine.classify("厉害了") == Event(EVENT_KEYWORD_PRAISE)
        assert engine.classify("今天天气") is None

    def test_calm_down_default_does_not_extinguish_arousal(self):
        """审计动机回归：一次甜言唤起（+0.3）后，普通消息不再一步浇灭。"""
        s = apply_event(MoodState(), Event(EVENT_KEYWORD_SWEET, timestamp=1.0))
        assert s.mood_intensity == pytest.approx(0.6)  # 0.3 默认 + 0.3 唤起
        s2 = apply_event(s, Event(EVENT_MESSAGE, timestamp=2.0))
        assert s2.mood_intensity == pytest.approx(0.56)  # 0.04 回落，仍是「中等」强度


# ---------------------------------------------------------------------------
# 2e 控制台语音页：trigger_probability 滑杆（真实生效的旋钮）后端面
# ---------------------------------------------------------------------------


class TestTriggerProbabilityKnob:
    def _write_vl_toml(self, root: Path) -> Path:
        from virtuallover.config import (
            BridgeConfig, ChatProviderConfig, EmbeddingConfig, PersonaConfig,
            VlConfig, dump_config,
        )

        cfg = VlConfig(session_id="测试:FriendMessage:10000", admins=["10000"],
                       whitelist=["10000"])
        cfg.bridge = BridgeConfig(host="127.0.0.1", port=6199, token="a" * 43,
                                  self_id="10000")
        cfg.chat = ChatProviderConfig(api_base="https://chat.test/v1",
                                      api_key="sk-TEST", model="test-model")
        cfg.embedding = EmbeddingConfig(api_base="https://emb.test/v1",
                                        api_key="sk-TEST", model="emb-model")
        cfg.persona = PersonaConfig(name="测试人格", system_prompt="占位人格。")
        path = root / "local" / "config" / "vl.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_config(cfg), encoding="utf-8")
        return path

    def _service(self, tmp_path, manager):
        from virtuallover.console import voice as console_voice

        ctx = types.SimpleNamespace(
            cfg=types.SimpleNamespace(tts=types.SimpleNamespace(
                enabled=True, base_url="http://127.0.0.1:5000",
                trigger_probability=1.0)),
            repo_root=tmp_path, voice_manager=manager)
        return console_voice.VoicePageService(runtime=ctx)

    def test_invalid_value_rejected(self, tmp_path):
        service = self._service(tmp_path, manager=None)
        with pytest.raises(Exception) as exc:
            service.set_trigger_probability({"value": 1.5})
        assert "区间" in str(exc.value)
        with pytest.raises(Exception):
            service.set_trigger_probability({"value": "abc"})

    def test_runtime_applied_and_persisted(self, tmp_path):
        """运行时热改（manager 实时读）+ vl.toml 持久化（.bak 备份）。"""
        manager = SimpleNamespace(trigger_probability=1.0)
        vl = self._write_vl_toml(tmp_path)
        service = self._service(tmp_path, manager)
        out = service.set_trigger_probability({"value": 0.35})
        assert out["ok"] and out["runtime_applied"] and out["persisted"]
        assert manager.trigger_probability == 0.35   # 运行时立即生效
        assert load_config(vl).tts.trigger_probability == 0.35  # 重启不变
        backups = list(vl.parent.glob("vl.toml.bak-*"))
        assert backups, "改写前必须有 .bak 备份"

    def test_status_exposes_current_value(self, tmp_path):
        manager = SimpleNamespace(trigger_probability=0.4)
        service = self._service(tmp_path, manager)
        assert service.status()["trigger_probability"] == 0.4
        assert service.get_trigger_probability() == {
            "ok": True, "value": 0.4, "source": "manager"}
