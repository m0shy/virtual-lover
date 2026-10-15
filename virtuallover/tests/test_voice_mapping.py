# -*- coding: utf-8 -*-
"""voice/mapping.py 契约测试（VOICE_SPEC §1/§4）+ 配置扩展/归一化联动测试。

覆盖：默认 7 态表与 soul.mood.VALID_MOODS 对账、表外情绪走 fallback 键、
注册表回退链两跳全路径（映射值∉注册表→配置默认情绪→"default"）、每跳
degraded_emotion 打点、注册表不可用不校验、大小写不敏感、intensity 仅经
effective_mood 衰减语义参与（与文字注入同源，不解析注入字符串）、
config.TtsConfig 扩展字段与 load 归一化（base_url/character/emotion_map）。

运行：python -m pytest virtuallover/tests/test_voice_mapping.py -q
"""

from __future__ import annotations

import logging

import pytest

from virtuallover.voice.mapping import (
    DEFAULT_DEFAULT_EMOTION,
    DEFAULT_EMOTION_MAP,
    MOOD_KEYS,
    emotion_for_snapshot,
    resolve_emotion,
)

# Huohua 实测注册情绪（voice-engine/trained/character_info.json）
HUOHUA_EMOTIONS = ["default", "傻白甜", "疯批", "慢速病娇", "平常的", "聊天", "平静的"]


# ---------------------------------------------------------------------------
# 默认表结构
# ---------------------------------------------------------------------------


class TestDefaultMap:
    def test_covers_all_moods_plus_fallback(self):
        from virtuallover.soul.mood import ALL_MOODS  # 14 态枚举来源对账（原 7 态+丰富化 7 态）

        assert MOOD_KEYS == ALL_MOODS
        assert set(DEFAULT_EMOTION_MAP) == MOOD_KEYS | {"fallback"}

    def test_all_values_registered_for_huohua(self):
        """终裁表的每个映射值都必须在 Huohua 注册表内（否则默认配置即联动哑火）。"""
        registry = {e.lower() for e in HUOHUA_EMOTIONS}
        for mood, emotion in DEFAULT_EMOTION_MAP.items():
            assert emotion.lower() in registry, f"{mood}→{emotion} 未注册"

    def test_sad_maps_to_slow_yandere(self):
        """sad→慢速病娇（终裁：多数意见；表可配置控制台可改）。"""
        assert DEFAULT_EMOTION_MAP["sad"] == "慢速病娇"


# ---------------------------------------------------------------------------
# 回退链
# ---------------------------------------------------------------------------


class TestResolveEmotion:
    def test_direct_hits(self):
        cases = {
            "happy": "傻白甜", "excited": "傻白甜", "angry": "疯批",
            "sad": "慢速病娇", "lonely": "平常的", "shy": "傻白甜",
            "calm": "聊天",
        }
        for mood, want in cases.items():
            got = resolve_emotion(mood, registry=HUOHUA_EMOTIONS, character="Huohua")
            assert got.emotion == want
            assert got.source == "map" and not got.degraded and got.hops == ()

    def test_unknown_mood_uses_fallback_key(self, caplog):
        got = resolve_emotion("惊恐", registry=HUOHUA_EMOTIONS, character="Huohua")
        assert got.emotion == "聊天"
        assert got.source == "map_fallback" and got.degraded
        assert len(got.hops) == 1
        assert "degraded_emotion" in caplog.text  # 每跳打点

    def test_unregistered_value_falls_to_config_default(self, caplog):
        """映射值 ∉ 注册表 → 配置默认情绪（回退链第一跳）。"""
        caplog.set_level(logging.WARNING)
        got = resolve_emotion(
            "happy", emotion_map={"happy": "傲娇"},  # 注册表没有"傲娇"
            default_emotion="聊天", registry=HUOHUA_EMOTIONS, character="Huohua")
        assert got.emotion == "聊天"
        assert got.source == "config_default" and got.degraded
        assert len(got.hops) == 1
        assert caplog.text.count("degraded_emotion") == 1

    def test_config_default_unregistered_falls_to_default(self, caplog):
        """配置默认情绪也 ∉ 注册表 → "default"（第二跳；GSVI 一定注册）。"""
        caplog.set_level(logging.WARNING)
        got = resolve_emotion(
            "happy", emotion_map={"happy": "傲娇"},
            default_emotion="也能量", registry=HUOHUA_EMOTIONS, character="Huohua")
        assert got.emotion == "default"
        assert got.source == "default" and got.degraded
        assert len(got.hops) == 2
        assert caplog.text.count("degraded_emotion") == 2

    def test_registry_none_means_no_validation(self):
        """注册表不可用（未拉取/失败）：不校验成员，映射值直接发送（GSVI 静默兜底）。"""
        got = resolve_emotion("happy", emotion_map={"happy": "傲娇"}, registry=None)
        assert got.emotion == "傲娇" and not got.degraded

    def test_registry_match_is_case_insensitive(self):
        """GSVI 对 string 参数统一 .lower()（tts_backend.py:125）：比较必须小写化。"""
        got = resolve_emotion("calm", registry=["聊天".upper()], character="X")
        assert got.emotion == "聊天" and not got.degraded

    def test_empty_mood_treated_as_calm(self):
        assert resolve_emotion("", registry=HUOHUA_EMOTIONS).emotion == "聊天"


class TestEmotionForSnapshot:
    def test_snapshot_tuple_uses_mood_only(self):
        """intensity 不在映射层消费——终裁：仅经衰减语义参与（getter 内完成）。"""
        got = emotion_for_snapshot(("happy", 0.99), registry=HUOHUA_EMOTIONS)
        assert got.emotion == "傻白甜"

    def test_none_snapshot_treated_as_calm(self):
        assert emotion_for_snapshot(None, registry=HUOHUA_EMOTIONS).emotion == "聊天"


# ---------------------------------------------------------------------------
# 与 soul.mood.effective_mood 同源（强度只经衰减语义参与）
# ---------------------------------------------------------------------------


class TestEffectiveMoodSource:
    def test_decayed_intensity_flips_to_calm_emotion(self):
        """5h 前的 happy（强度 0.5）经 τ=4h 衰减 0.5·e^(-5/4)≈0.14<0.15 → calm
        → 映射到「聊天」（R3-2a：calm→聊天）；与文字注入片段渲染同一条衰减数学。"""
        from virtuallover.soul.mood import MoodState, effective_mood, render_system_fragment

        updated = 1_700_000_000.0
        state = MoodState(mood="happy", mood_intensity=0.5, updated_at=updated)
        now = updated + 5 * 3600
        snapshot = effective_mood(state, now=now)
        assert snapshot[0] == "calm"  # getter 已回落
        got = emotion_for_snapshot(snapshot, registry=HUOHUA_EMOTIONS)
        assert got.emotion == "聊天"
        # 同源校验：同一时刻注入片段也按平静渲染（getter 复用衰减数学的动机）；
        # R3 起注入片段是纯数据行（【当前内心状态】关系：…｜心情：平静…）
        assert "平静" in render_system_fragment(state, now=now)

    def test_fresh_happy_stays_happy_mapping(self):
        from virtuallover.soul.mood import MoodState, effective_mood

        state = MoodState(mood="happy", mood_intensity=0.8, updated_at=1_700_000_000.0)
        snapshot = effective_mood(state, now=1_700_000_000.0 + 60)
        assert snapshot[0] == "happy"
        assert emotion_for_snapshot(snapshot, registry=HUOHUA_EMOTIONS).emotion == "傻白甜"


# ---------------------------------------------------------------------------
# config.TtsConfig 扩展 + load 归一化（VOICE_SPEC §1/§5）
# ---------------------------------------------------------------------------


def _write_full_config(tmp_path, **tts_kw) -> "Path":
    from virtuallover.config import (
        ChatProviderConfig, EmbeddingConfig, PersonaConfig, TtsConfig,
        VlConfig, dump_config,
    )
    from pathlib import Path

    cfg = VlConfig(session_id="测试人格:FriendMessage:10000", admins=["10000"])
    cfg.chat = ChatProviderConfig(api_base="https://chat.test/v1",
                                  api_key="sk-TEST-占位", model="test-model")
    cfg.embedding = EmbeddingConfig(api_base="https://emb.test/v1",
                                    api_key="sk-TEST-占位", model="emb-model")
    cfg.persona = PersonaConfig(name="测试人格", system_prompt="占位人格。")
    cfg.tts = TtsConfig(**tts_kw)
    path = tmp_path / "local" / "config" / "vl.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_config(cfg), encoding="utf-8")
    return Path(path)


class TestTtsConfigExtension:
    def test_defaults(self):
        from virtuallover.config import TtsConfig

        t = TtsConfig()
        assert t.character == "Huohua"          # §0 终裁：默认 Huohua
        assert t.base_url == "http://127.0.0.1:5000"
        assert t.auto_start is True
        assert t.cache_max_entries == 500 and t.cache_max_bytes == 128 * 1024 * 1024
        assert t.default_emotion == DEFAULT_DEFAULT_EMOTION == "聊天"
        assert t.emotion_map == DEFAULT_EMOTION_MAP
        # 每次实例化拿到独立拷贝（default_factory），互不串改
        t.emotion_map["happy"] = "改了"
        assert TtsConfig().emotion_map["happy"] == "傻白甜"

    def test_load_normalizes_host_docker_internal(self, tmp_path, caplog):
        """host.docker.internal 残留 → 127.0.0.1（旧栈容器直连死路的代码面修复）。"""
        from virtuallover.config import load_config

        path = _write_full_config(tmp_path, base_url="http://host.docker.internal:5000")
        cfg = load_config(path)
        assert cfg.tts.base_url == "http://127.0.0.1:5000"
        assert "base_url 已归一化" in caplog.text

    def test_load_normalizes_localhost_and_missing_scheme(self, tmp_path):
        from virtuallover.config import load_config

        path = _write_full_config(tmp_path, base_url="localhost:9100")
        cfg = load_config(path)
        assert cfg.tts.base_url == "http://127.0.0.1:9100"

    def test_load_switches_hutao_to_huohua(self, tmp_path, caplog):
        """旧栈单情绪音色 Hutao → Huohua，warning 明示声音变化（§0 终裁）。"""
        from virtuallover.config import load_config

        path = _write_full_config(tmp_path, character="Hutao")
        cfg = load_config(path)
        assert cfg.tts.character == "Huohua"
        assert "声音变化明示" in caplog.text

    def test_load_merges_partial_emotion_map_over_defaults(self, tmp_path):
        """vl.toml 只写部分行：其余行保持默认表（不落入 fallback 键）。"""
        from virtuallover.config import load_config

        path = _write_full_config(tmp_path, emotion_map={"happy": "疯批"})
        cfg = load_config(path)
        assert cfg.tts.emotion_map["happy"] == "疯批"     # 用户覆盖
        assert cfg.tts.emotion_map["sad"] == "慢速病娇"   # 其余默认

    def test_roundtrip_keeps_emotion_map(self, tmp_path):
        """dump→load 往返：dict 字段经 inline table 序列化无损。"""
        from virtuallover.config import load_config

        path = _write_full_config(tmp_path, emotion_map={"angry": "平静的"})
        cfg = load_config(path)
        assert cfg.tts.emotion_map["angry"] == "平静的"
        assert cfg.tts.emotion_map["happy"] == "傻白甜"
