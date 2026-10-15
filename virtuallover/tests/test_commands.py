# -*- coding: utf-8 -*-
"""命令面测试（SPEC §8.5 / §8.6 门禁）—— 管理员判定、白名单拒绝路径、
/mood /diary /eval 命令组行为与文案等价性，/story /play /sing /poem 路由。
测试中的 user_id 全部为占位值（"10001"/"20002"），不使用任何真实 QQ 号；
人设名一律用「测试女友」占位，绝不写真实人设。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from virtuallover.commands import (
    BRAIN_MISSING_TEXT,
    EVAL_HELP_TEXT,
    GENERATE_FAIL_TEXT,
    CommandRouter,
    gate_user,
    is_admin_user,
    parse_command,
)
from virtuallover.soul import games as games_mod
from virtuallover.soul import story as story_mod
from virtuallover.soul.mood import MoodEngine, MoodState

# 占位账号（严禁写入真实 QQ 号）
ADMIN = "10001"
FRIEND = "20002"
STRANGER = "30003"
ADMINS = [ADMIN]
WHITELIST = [FRIEND]


class _FakeDiary:
    """日记存档假件（与 DiaryService 的读取面同构）。"""

    def __init__(self, files: dict[str, str]):
        self.files = files

    async def read_diary(self, date_str: str):
        return self.files.get(date_str)

    async def latest_diary_date(self):
        dates = sorted(self.files, reverse=True)
        return dates[0] if dates else None


def _engine(tmp_path) -> MoodEngine:
    """真实 MoodEngine（注入配置，不读 local/ 活配置）。"""
    return MoodEngine(
        tmp_path,
        config={
            "keyword_groups": {"sweet": r"喜欢你", "praise": r"爱你", "rude": r"闭嘴"},
            "daily_bonus_enabled": True,
            "inject_enabled": True,
        },
    )


# ---------------------------------------------------------------------------
# 管理员判定与门禁（§8.5 / §8.6）
# ---------------------------------------------------------------------------


class TestGateAndAdmin:
    def test_is_admin_user(self):
        assert is_admin_user(ADMIN, ADMINS) is True
        assert is_admin_user(FRIEND, ADMINS) is False
        assert is_admin_user("", ADMINS) is False

    def test_gate_user_admin_pass(self):
        assert gate_user(ADMIN, ADMINS, WHITELIST) is True

    def test_gate_user_whitelist_pass(self):
        assert gate_user(FRIEND, ADMINS, WHITELIST) is True

    def test_gate_user_stranger_rejected(self):
        assert gate_user(STRANGER, ADMINS, WHITELIST) is False
        assert gate_user("", ADMINS, WHITELIST) is False


class TestParseCommand:
    def test_basic(self):
        assert parse_command("/mood") == ("mood", [])
        assert parse_command("/mood stats") == ("mood", ["stats"])
        assert parse_command("/eval memory 5") == ("eval", ["memory", "5"])

    def test_non_command(self):
        assert parse_command("你好") is None
        assert parse_command("/") is None
        assert parse_command("") is None

    def test_command_name_lowercased(self):
        assert parse_command("/MOOD") == ("mood", [])


# ---------------------------------------------------------------------------
# /mood 命令组
# ---------------------------------------------------------------------------


class TestMoodCommand:
    def _router(self, tmp_path):
        return CommandRouter(mood_engine=_engine(tmp_path))

    def test_mood_query_by_whitelisted_user(self, tmp_path):
        router = self._router(tmp_path)
        out = asyncio.run(router.handle("/mood", FRIEND, admins=ADMINS, whitelist=WHITELIST))
        assert out and "她现在" in out[0] and "100" not in out[0]

    def test_mood_stats_admin_only(self, tmp_path):
        router = self._router(tmp_path)
        # 管理员：数值调试视图
        out = asyncio.run(router.handle("/mood stats", ADMIN, admins=ADMINS, whitelist=WHITELIST))
        assert out and "[调试视图]" in out[0] and "50.0/100" in out[0]
        # 白名单非管理员：拒绝
        out = asyncio.run(router.handle("/mood stats", FRIEND, admins=ADMINS, whitelist=WHITELIST))
        assert out == ["只有管理员才能查看数值统计哦。"]

    def test_mood_reset_admin_only(self, tmp_path):
        router = self._router(tmp_path)
        out = asyncio.run(router.handle("/mood reset", FRIEND, admins=ADMINS, whitelist=WHITELIST))
        assert out == ["只有管理员才能重置内心状态哦。"]
        # 管理员无数据
        out = asyncio.run(router.handle("/mood reset", ADMIN, admins=ADMINS, whitelist=WHITELIST))
        assert out == ["当前还没有需要重置的状态哦。"]
        # 管理员有数据
        router.mood_engine.storage.set(ADMIN, MoodState())
        router.mood_engine.storage.flush()
        out = asyncio.run(router.handle("/mood reset", ADMIN, admins=ADMINS, whitelist=WHITELIST))
        assert out == ["已重置你这里的内心状态，我们重新认识一下吧～"]

    def test_mood_unknown_action_usage(self, tmp_path):
        router = self._router(tmp_path)
        out = asyncio.run(router.handle("/mood foo", FRIEND, admins=ADMINS, whitelist=WHITELIST))
        assert out and out[0].startswith("用法：/mood")


# ---------------------------------------------------------------------------
# /diary 命令组（管理员）
# ---------------------------------------------------------------------------


class TestDiaryCommand:
    def _router(self, files: dict[str, str], today: str = "2026-10-10"):
        return CommandRouter(diary_service=_FakeDiary(files), today_fn=lambda: today)

    def test_diary_admin_only(self):
        router = self._router({})
        out = asyncio.run(router.handle("/diary", FRIEND, admins=ADMINS, whitelist=WHITELIST))
        assert out == ["只有管理员才能查看她的日记哦。"]

    def test_diary_today_then_latest(self):
        router = self._router({"2026-10-09": "旧日记", "2026-10-10": "今天的日记"})
        out = asyncio.run(router.handle("/diary", ADMIN, admins=ADMINS, whitelist=WHITELIST))
        assert out == ["📜 她的日记（2026-10-10）：\n今天的日记"]
        # 今天没有 → 最近一篇
        router2 = self._router({"2026-10-09": "旧日记"})
        out2 = asyncio.run(router2.handle("/diary", ADMIN, admins=ADMINS, whitelist=WHITELIST))
        assert out2 == ["📜 她的日记（最近一篇，2026-10-09）：\n旧日记"]

    def test_diary_empty_archive(self):
        router = self._router({})
        out = asyncio.run(router.handle("/diary", ADMIN, admins=ADMINS, whitelist=WHITELIST))
        assert out == ["还没有日记存档，等今晚她写完再来翻吧～"]

    def test_diary_specific_date(self):
        router = self._router({"2026-10-01": "那天的心事"})
        out = asyncio.run(
            router.handle("/diary 2026-10-01", ADMIN, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out == ["📜 她的日记（2026-10-01）：\n那天的心事"]
        out2 = asyncio.run(
            router.handle("/diary 2099-01-01", ADMIN, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out2 == ["2099-01-01 这天没有日记存档。"]

    def test_diary_bad_date_format(self):
        router = self._router({})
        out = asyncio.run(
            router.handle("/diary 2026/10/01", ADMIN, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out == ["日期格式不对哦，请用 YYYY-MM-DD，例如：/diary 2026-10-10"]


# ---------------------------------------------------------------------------
# /eval 命令组
# ---------------------------------------------------------------------------


class TestEvalCommand:
    def _router(self, runner=None):
        return CommandRouter(eval_runner=runner)

    def test_eval_help(self):
        router = self._router()
        out = asyncio.run(router.handle("/eval help", FRIEND, admins=ADMINS, whitelist=WHITELIST))
        assert out == [EVAL_HELP_TEXT]
        out2 = asyncio.run(router.handle("/eval", ADMIN, admins=ADMINS, whitelist=WHITELIST))
        assert out2 == [EVAL_HELP_TEXT]

    def test_eval_memory_admin_only(self):
        router = self._router()
        out = asyncio.run(
            router.handle("/eval memory", FRIEND, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out == ["只有管理员才能运行记忆评测哦。"]

    def test_eval_memory_invokes_runner(self):
        calls: list[int] = []

        async def runner(n: int):
            calls.append(n)
            return [f"报告（n={n}）"]

        router = self._router(runner)
        out = asyncio.run(
            router.handle("/eval memory 5", ADMIN, admins=ADMINS, whitelist=WHITELIST)
        )
        assert calls == [5]
        assert out == ["报告（n=5）"]

    def test_eval_memory_default_and_clamp(self):
        calls: list[int] = []

        async def runner(n: int):
            calls.append(n)
            return ["报告"]

        router = self._router(runner)
        # 缺省 n=10
        asyncio.run(router.handle("/eval memory", ADMIN, admins=ADMINS, whitelist=WHITELIST))
        assert calls[-1] == 10
        # 非法 n → 提示 + 默认 10
        out = asyncio.run(
            router.handle("/eval memory abc", ADMIN, admins=ADMINS, whitelist=WHITELIST)
        )
        assert calls[-1] == 10
        assert out[0] == "⚠️ 条数参数「abc」无效，已按默认 10 执行。"
        # 超上限 → 钳制到 50 并提示
        out2 = asyncio.run(
            router.handle("/eval memory 999", ADMIN, admins=ADMINS, whitelist=WHITELIST)
        )
        assert calls[-1] == 50
        assert out2[0] == "⚠️ 条数已调整：n=999 超出范围 [1, 50]，按 50 执行。"

    def test_eval_memory_runner_missing(self):
        router = self._router(runner=None)
        out = asyncio.run(
            router.handle("/eval memory", ADMIN, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out and "评测组件未装配" in out[0]


# ---------------------------------------------------------------------------
# 门禁与静默路径（§8.6）
# ---------------------------------------------------------------------------


class TestGateSilentPaths:
    def test_command_from_stranger_silently_ignored(self, tmp_path):
        router = CommandRouter(mood_engine=_engine(tmp_path))
        out = asyncio.run(
            router.handle("/mood", STRANGER, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out is None  # 静默忽略：不回复任何消息

    def test_unknown_command_silently_ignored(self, tmp_path):
        router = CommandRouter(mood_engine=_engine(tmp_path))
        out = asyncio.run(
            router.handle("/ unknown", FRIEND, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out is None
        out2 = asyncio.run(
            router.handle("/whatever", FRIEND, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out2 is None

    def test_non_command_text_silently_ignored(self, tmp_path):
        router = CommandRouter(mood_engine=_engine(tmp_path))
        out = asyncio.run(
            router.handle("今天聊点什么好", FRIEND, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out is None

    def test_engine_missing_friendly_message(self, tmp_path):
        router = CommandRouter(mood_engine=None)
        out = asyncio.run(
            router.handle("/mood reset", ADMIN, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out and "情绪引擎未装配" in out[0]


# ---------------------------------------------------------------------------
# /story 命令组（故事模式开窗）
# ---------------------------------------------------------------------------


class TestStoryCommand:
    def _router(self, tmp_path):
        return CommandRouter(state_dir=tmp_path)

    def test_story_activates_and_replies(self, tmp_path):
        """/story 开 2 轮故事窗口并回开场问句（soul.story 状态落盘）。"""
        router = self._router(tmp_path)
        out = asyncio.run(
            router.handle("/story", FRIEND, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out == ["好呀，想听什么类型的故事？"]
        state = story_mod.load_state(tmp_path)
        assert state["turns_left"] == 2
        assert story_mod.is_active(tmp_path) is True

    def test_story_state_dir_missing(self):
        """state_dir 未装配：友好提示，不炸。"""
        router = CommandRouter(state_dir=None)
        out = asyncio.run(
            router.handle("/story", FRIEND, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out and "还没装配" in out[0]

    def test_story_gated(self, tmp_path):
        """白名单外用户：静默忽略，不开窗。"""
        router = self._router(tmp_path)
        out = asyncio.run(
            router.handle("/story", STRANGER, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out is None
        assert story_mod.load_state(tmp_path) == {}


# ---------------------------------------------------------------------------
# /play 命令组（小游戏菜单 / 开局 / 收摊）
# ---------------------------------------------------------------------------


class TestPlayCommand:
    def _router(self, tmp_path):
        return CommandRouter(state_dir=tmp_path)

    def test_play_menu(self, tmp_path):
        router = self._router(tmp_path)
        out = asyncio.run(
            router.handle("/play", FRIEND, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out == [games_mod.game_menu()]
        # 未知子命令同样回菜单
        out2 = asyncio.run(
            router.handle("/play mahjong", FRIEND, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out2 == out

    def test_play_guess_starts(self, tmp_path):
        router = self._router(tmp_path)
        out = asyncio.run(
            router.handle("/play guess", FRIEND, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out and "1～100" in out[0]
        state = games_mod.load_state(tmp_path)
        assert state["current"] == "guess"
        assert 1 <= int(state["target"]) <= 100

    def test_play_idiom_starts(self, tmp_path):
        router = self._router(tmp_path)
        out = asyncio.run(
            router.handle("/play idiom", FRIEND, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out
        state = games_mod.load_state(tmp_path)
        assert state["current"] == "idiom"
        assert state["idiom_word"] in games_mod.IDIOMS

    def test_play_truth_starts(self, tmp_path):
        router = self._router(tmp_path)
        out = asyncio.run(
            router.handle("/play truth", FRIEND, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out
        state = games_mod.load_state(tmp_path)
        assert state["current"] == "truth"
        assert state["question"] in games_mod.TRUTH_QUESTIONS

    def test_play_stop_ends(self, tmp_path):
        router = self._router(tmp_path)
        asyncio.run(
            router.handle("/play guess", FRIEND, admins=ADMINS, whitelist=WHITELIST)
        )
        out = asyncio.run(
            router.handle("/play stop", FRIEND, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out and ("收" in out[0] or "没有进行中的游戏" in out[0])
        assert "current" not in games_mod.load_state(tmp_path)

    def test_play_state_dir_missing(self):
        router = CommandRouter(state_dir=None)
        out = asyncio.run(
            router.handle("/play guess", FRIEND, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out and "未装配" in out[0]

    def test_play_gated(self, tmp_path):
        router = self._router(tmp_path)
        out = asyncio.run(
            router.handle("/play guess", STRANGER, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out is None
        assert games_mod.load_state(tmp_path) == {}


# ---------------------------------------------------------------------------
# /sing /poem 命令组（大脑客户端生成 + 语音双轨）
# ---------------------------------------------------------------------------


class _FakeBrain:
    """大脑客户端假件（与 ChatClient/BrainRouter 的 chat_async 同构）。"""

    def __init__(self, content: str = "月亮不睡我不睡\n你也是小月亮", exc: Exception | None = None):
        self.content = content
        self.exc = exc
        self.messages: list | None = None

    async def chat_async(self, messages, **kwargs):
        self.messages = messages
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(content=self.content)


class _FakeVoice:
    """语音门面假件（deliver_proactive 双轨决策同构：返回是否已发出）。"""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[tuple] = []

    async def deliver_proactive(self, user_id, text, **kwargs):
        self.calls.append((user_id, text))
        return self.ok


class TestSingPoemCommand:
    def _router(self, tmp_path, *, brain=None, voice=None, persona="你是「测试女友」。"):
        return CommandRouter(
            brain_client=brain,
            voice_manager=voice,
            persona_system_prompt=persona,
            state_dir=tmp_path,
        )

    def test_sing_voice_success_sends_voice_only(self, tmp_path):
        """双轨成功只发语音：handle 返回 []（命令路径不再补发文字）。"""
        brain = _FakeBrain()
        voice = _FakeVoice(ok=True)
        router = self._router(tmp_path, brain=brain, voice=voice)
        out = asyncio.run(
            router.handle("/sing 想你", FRIEND, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out == []
        user_id, text = voice.calls[0]
        assert user_id == FRIEND
        assert "月亮不睡我不睡" in text
        assert "念" in text and "唱" in text        # 注明是念不是真唱
        # 生成 prompt：人设 system 在场 + 主题进 prompt + 放宽句长约束
        assert brain.messages[0]["role"] == "system"
        assert "测试女友" in brain.messages[0]["content"]
        prompt = brain.messages[-1]["content"]
        assert "想你" in prompt and "歌词" in prompt and "放宽" in prompt

    def test_sing_voice_failure_falls_back_to_text(self, tmp_path):
        """双轨失败回退文字：handle 返回 [文本]（命令路径补发）。"""
        voice = _FakeVoice(ok=False)
        router = self._router(tmp_path, brain=_FakeBrain(), voice=voice)
        out = asyncio.run(
            router.handle("/sing", FRIEND, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out and "月亮不睡我不睡" in out[0] and "念" in out[0]

    def test_poem_without_voice_manager_returns_text(self, tmp_path):
        """拿不到语音门面：只回文字。"""
        router = self._router(tmp_path, brain=_FakeBrain(), voice=None)
        out = asyncio.run(
            router.handle("/poem", FRIEND, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out and "月亮不睡我不睡" in out[0] and "诗" in out[0]
        # 缺省主题进 prompt
        brain_msg = router.brain_client.messages[-1]["content"]
        assert "我们的日常" in brain_msg and "情诗" in brain_msg

    def test_sing_brain_missing(self, tmp_path):
        router = self._router(tmp_path, brain=None, voice=_FakeVoice())
        out = asyncio.run(
            router.handle("/sing", FRIEND, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out == [BRAIN_MISSING_TEXT]

    def test_sing_generate_failure_degrades(self, tmp_path):
        """LLM 故障 / 空回复：卖萌降级文案，不抛异常。"""
        router = self._router(tmp_path, brain=_FakeBrain(exc=RuntimeError("网断了")),
                              voice=_FakeVoice())
        out = asyncio.run(
            router.handle("/sing", FRIEND, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out == [GENERATE_FAIL_TEXT]
        empty = self._router(tmp_path, brain=_FakeBrain(content="  "), voice=_FakeVoice())
        out2 = asyncio.run(
            empty.handle("/poem", FRIEND, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out2 == [GENERATE_FAIL_TEXT]

    def test_sing_gated(self, tmp_path):
        router = self._router(tmp_path, brain=_FakeBrain(), voice=_FakeVoice())
        out = asyncio.run(
            router.handle("/sing 想你", STRANGER, admins=ADMINS, whitelist=WHITELIST)
        )
        assert out is None
        assert router.voice_manager.calls == []
