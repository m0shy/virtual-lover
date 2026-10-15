# -*- coding: utf-8 -*-
"""soul/games.py 契约测试（三类本地小游戏：guess / idiom / truth）。

覆盖点：
- guess 猜数字：开局随机（注入 rng 固定）、高了/低了/对了三态判定、
  对了记分庆祝并收官、非数字输入不吞话（None）、越界提示不计数；
- idiom 成语接龙：她出题 → 用户四字且首字衔接判上 → 她从词库找尾字可接的
  词回接；找不到认输卖萌重新出题；接错首字她记分；非四字输入不吞话；
- truth 真心话：只出题不判答案——换题请求生效，回答类输入交回聊天（None）；
- 状态持久化：game_state.json 落盘、比分跨局/跨调用保留、跨天可续
  （now 大幅前推仍可玩）、stop 收摊保留比分；
- 命令入口语义：菜单 / 开局 / stop / 非法 kind 回菜单。

rng/now 全部注入固定值（SeqRng 顺序取值），不发任何请求；题库常量规模校验
（60 成语 / 30 题目）；不写任何真实人设名/QQ 号。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# sys.path 引导：virtuallover/ 是 namespace 包，仓库根必须可导入。
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from virtuallover.soul import games  # noqa: E402

T0 = 1765000000.0


class SeqRng:
    """确定性 rng 桩：randint 取「值本身」（钳在 [a,b]）、choice 按索引取
    （溢出回绕）——开局目标数/题面完全可预期。"""

    def __init__(self, *values: int):
        self.values = list(values)
        self.i = 0

    def _next(self) -> int:
        value = self.values[self.i % len(self.values)]
        self.i += 1
        return value

    def randint(self, a: int, b: int) -> int:
        return min(max(a + self._next() - 1, a), b)

    def choice(self, seq):
        return seq[self._next() % len(seq)]


# ---------------------------------------------------------------------------
# 题库常量规模
# ---------------------------------------------------------------------------


def test_idiom_bank_has_60_unique_idioms():
    """成语词库 60 个、全部四字、无重复。"""
    assert len(games.IDIOMS) == 60
    assert all(len(w) == 4 for w in games.IDIOMS)
    assert len(set(games.IDIOMS)) == 60


def test_truth_bank_has_30_questions():
    """真心话题库 30 个、非空、无重复。"""
    assert len(games.TRUTH_QUESTIONS) == 30
    assert all(q.strip() for q in games.TRUTH_QUESTIONS)
    assert len(set(games.TRUTH_QUESTIONS)) == 30


# ---------------------------------------------------------------------------
# guess 猜数字
# ---------------------------------------------------------------------------


class TestGuess:
    def _start(self, tmp_path, target: int):
        return games.start_game("guess", tmp_path, rng=SeqRng(target), now=T0)

    def test_start_persists_target(self, tmp_path):
        """开局落盘 current/target/guess_count，开场白报 1～100 范围。"""
        out = self._start(tmp_path, 42)
        assert len(out) == 1 and "1～100" in out[0]
        state = games.load_state(tmp_path)
        assert state["current"] == "guess"
        assert state["target"] == 42
        assert state["guess_count"] == 0

    def test_high_low_hit_verdicts(self, tmp_path):
        """三态判定：小了→低了、大了→高了、对了→记分庆祝并收官。"""
        self._start(tmp_path, 42)
        low = games.handle_game_message("20", state_dir=tmp_path, rng=SeqRng(0), now=T0 + 1)
        assert low is not None and "低了" in low
        high = games.handle_game_message("88", state_dir=tmp_path, rng=SeqRng(0), now=T0 + 2)
        assert high is not None and "高了" in high
        hit = games.handle_game_message("是42吗", state_dir=tmp_path, rng=SeqRng(0), now=T0 + 3)
        assert hit is not None and "42" in hit and ("对" in hit or "赢" in hit)
        state = games.load_state(tmp_path)
        assert "current" not in state            # 收官
        assert state["scores"]["user"] == 1      # 对了记分

    def test_non_number_input_passes_through(self, tmp_path):
        """没数字的聊天不吞（None 交回正常聊天）。"""
        self._start(tmp_path, 42)
        assert games.handle_game_message("你猜猜我想说啥", state_dir=tmp_path,
                                         rng=SeqRng(0), now=T0 + 1) is None

    def test_out_of_range_hint_not_counted(self, tmp_path):
        """越界数字给提示、不消耗次数。"""
        self._start(tmp_path, 42)
        out = games.handle_game_message("250", state_dir=tmp_path,
                                        rng=SeqRng(0), now=T0 + 1)
        assert out is not None and "1～100" in out
        assert games.load_state(tmp_path)["guess_count"] == 0

    def test_no_game_returns_none(self, tmp_path):
        """无进行中游戏：数字输入也交回正常聊天。"""
        assert games.handle_game_message("42", state_dir=tmp_path,
                                         rng=SeqRng(0), now=T0) is None


# ---------------------------------------------------------------------------
# idiom 成语接龙
# ---------------------------------------------------------------------------


class TestIdiom:
    def _start(self, tmp_path, bank_index: int = 0):
        """开局固定她出 IDIOMS[bank_index]（SeqRng choice 按索引取）。"""
        return games.start_game("idiom", tmp_path, rng=SeqRng(bank_index), now=T0)

    def test_start_asks_user_to_chain(self, tmp_path):
        """开局她出题并告知接龙首字。"""
        out = self._start(tmp_path, 0)   # 一心一意
        assert "一心一意" in out[0] and "意" in out[0]
        assert games.load_state(tmp_path)["idiom_word"] == "一心一意"

    def test_user_connects_and_she_counters(self, tmp_path):
        """用户四字接上（首字=她尾字）→ 用户记分，她接尾字可接的词。"""
        self._start(tmp_path, 0)                          # 一心一意
        reply = games.handle_game_message("意气风发", state_dir=tmp_path,
                                          rng=SeqRng(2), now=T0 + 1)  # 发扬光大
        assert reply is not None and "发扬光大" in reply
        state = games.load_state(tmp_path)
        assert state["idiom_word"] == "发扬光大"
        assert state["scores"]["user"] == 1

    def test_she_concedes_when_stuck(self, tmp_path):
        """她找不到可接的词：认输卖萌并重新出题（链重开）。"""
        self._start(tmp_path, 0)                          # 一心一意 → 尾字 意
        reply = games.handle_game_message("意在笔先", state_dir=tmp_path,
                                          rng=SeqRng(10), now=T0 + 1)
        assert reply is not None and "接不上" in reply
        state = games.load_state(tmp_path)
        assert state["current"] == "idiom"                # 游戏继续
        assert len(state["idiom_word"]) == 4              # 新题已出
        assert state["used"] == [state["idiom_word"]]

    def test_wrong_head_scores_her(self, tmp_path):
        """用户四字但首字不衔接：她记分并要求重接（游戏继续）。"""
        self._start(tmp_path, 0)                          # 一心一意（尾字 意）
        reply = games.handle_game_message("嘻嘻哈哈", state_dir=tmp_path,
                                          rng=SeqRng(0), now=T0 + 1)
        assert reply is not None and "意" in reply
        state = games.load_state(tmp_path)
        assert state["scores"]["her"] == 1
        assert state["idiom_word"] == "一心一意"          # 原词重接

    def test_non_four_char_input_passes_through(self, tmp_path):
        """非四字输入不吞（None 交回正常聊天）。"""
        self._start(tmp_path, 0)
        assert games.handle_game_message("今天天气真不错呀", state_dir=tmp_path,
                                         rng=SeqRng(0), now=T0 + 1) is None

    def test_punctuation_stripped(self, tmp_path):
        """带标点/引号的四字成语照常判定（清洗后恰四字）。"""
        self._start(tmp_path, 0)
        reply = games.handle_game_message("「意气风发」！", state_dir=tmp_path,
                                          rng=SeqRng(2), now=T0 + 1)
        assert reply is not None and "发扬光大" in reply


# ---------------------------------------------------------------------------
# truth 真心话
# ---------------------------------------------------------------------------


class TestTruth:
    def test_start_asks_question(self, tmp_path):
        """开局抽题落盘，开场白含题面与玩法提示。"""
        out = games.start_game("truth", tmp_path, rng=SeqRng(3), now=T0)
        question = games.TRUTH_QUESTIONS[3]
        assert question in out[0] and "下一题" in out[0]
        assert games.load_state(tmp_path)["question"] == question

    def test_next_question_rotates(self, tmp_path):
        """「下一题」换题（不与当前重复）并落盘。"""
        games.start_game("truth", tmp_path, rng=SeqRng(3), now=T0)
        current = games.load_state(tmp_path)["question"]
        reply = games.handle_game_message("下一题", state_dir=tmp_path,
                                          rng=SeqRng(7), now=T0 + 1)
        assert reply is not None
        state = games.load_state(tmp_path)
        assert state["question"] in games.TRUTH_QUESTIONS
        assert state["question"] != current
        assert state["question"] in reply

    def test_answer_passes_through_to_chat(self, tmp_path):
        """回答类输入不判答案（None 交回正常聊天——只出题不判答案）。"""
        games.start_game("truth", tmp_path, rng=SeqRng(3), now=T0)
        assert games.handle_game_message("我最爱吃草莓啦", state_dir=tmp_path,
                                         rng=SeqRng(0), now=T0 + 1) is None


# ---------------------------------------------------------------------------
# 状态持久化 / 收摊 / 菜单
# ---------------------------------------------------------------------------


def test_scores_persist_across_games(tmp_path):
    """比分跨局保留：guess 赢一局后换 idiom，比分不清零。"""
    games.start_game("guess", tmp_path, rng=SeqRng(42), now=T0)
    games.handle_game_message("42", state_dir=tmp_path, rng=SeqRng(0), now=T0 + 1)
    games.start_game("idiom", tmp_path, rng=SeqRng(0), now=T0 + 2)
    assert games.load_state(tmp_path)["scores"]["user"] == 1


def test_game_survives_across_days(tmp_path):
    """跨天可续：now 大幅前推（3 天后）游戏照常判定。"""
    games.start_game("guess", tmp_path, rng=SeqRng(42), now=T0)
    reply = games.handle_game_message("10", state_dir=tmp_path,
                                      rng=SeqRng(0), now=T0 + 3 * 86400)
    assert reply is not None and "低了" in reply
    assert games.load_state(tmp_path)["guess_count"] == 1


def test_stop_keeps_scores_and_clears_current(tmp_path):
    """stop 收摊：current 清空、比分保留在档；再 stop 提示无游戏。"""
    games.start_game("guess", tmp_path, rng=SeqRng(42), now=T0)
    games.handle_game_message("42", state_dir=tmp_path, rng=SeqRng(0), now=T0 + 1)
    games.start_game("idiom", tmp_path, rng=SeqRng(0), now=T0 + 2)
    out = games.stop_game(tmp_path, now=T0 + 3)
    assert out and "比分" in out[0]
    state = games.load_state(tmp_path)
    assert "current" not in state and state["scores"]["user"] == 1
    again = games.stop_game(tmp_path, now=T0 + 4)
    assert again and "没有进行中的游戏" in again[0]


def test_stop_phrase_in_chat_ends_game(tmp_path):
    """对话里说「不玩了」同样收摊（不非得用 /play stop）。"""
    games.start_game("guess", tmp_path, rng=SeqRng(42), now=T0)
    reply = games.handle_game_message("不玩了不玩了", state_dir=tmp_path,
                                      rng=SeqRng(0), now=T0 + 1)
    assert reply is not None
    assert "current" not in games.load_state(tmp_path)


def test_game_menu_lists_all_kinds():
    """菜单含三类游戏与 stop 说明。"""
    menu = games.game_menu()
    for hint in ("/play guess", "/play idiom", "/play truth", "/play stop"):
        assert hint in menu


def test_start_unknown_kind_falls_back_to_menu(tmp_path):
    """非法 kind（防御面）：回菜单、不落任何游戏状态。"""
    out = games.start_game("mahjong", tmp_path, rng=SeqRng(0), now=T0)
    assert out == [games.game_menu()]
    assert games.load_state(tmp_path) == {}


def test_state_file_named_game_state_json(tmp_path):
    """状态文件固定名 game_state.json（vl_data 目录口径）。"""
    games.start_game("guess", tmp_path, rng=SeqRng(42), now=T0)
    on_disk = json.loads((tmp_path / "game_state.json").read_text(encoding="utf-8"))
    assert on_disk["current"] == "guess"
    assert games.DEFAULT_STATE_DIR == "local/vl_data"
