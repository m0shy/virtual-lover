# -*- coding: utf-8 -*-
"""soul/textclean.strip_mood_tags 测试 —— 出站剥 ``<<mood:...>>`` 自评情绪标签。

配套：soul.mood.parse_mood_tag 负责严格解析（合法心情名 + 数值强度），
本模块验证宽松剥除兜底——凡是标签形态的残片都不该发给用户。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from virtuallover.soul.textclean import (
    MOOD_TAG_RE,
    clean_proactive_text,
    clean_tts_text,
    strip_mood_tags,
)


class TestStripMoodTags:
    def test_tail_tag_removed(self):
        assert strip_mood_tags("今天也要开心哦<<mood:happy:0.8>>") == "今天也要开心哦"

    def test_middle_tag_removed(self):
        assert strip_mood_tags("他刚才<<mood:WRONGED:0.6>>哼") == "他刚才哼"

    def test_multiple_tags_removed(self):
        assert strip_mood_tags("<<mood:sad:0.2>>先这样<<mood:jealous:0.9>>再说") == (
            "先这样再说"
        )

    def test_missing_intensity_removed(self):
        assert strip_mood_tags("哼<<mood:tsundere>>") == "哼"

    def test_malformed_intensity_still_removed(self):
        """解析层不认的残缺标签（强度非数值）也必须剥——出站兜底只认形态。"""
        assert strip_mood_tags("哼<<mood:xx:yy>>") == "哼"

    def test_illegal_mood_name_still_removed(self):
        assert strip_mood_tags("哦<<mood:ecstatic:0.9>>") == "哦"

    def test_no_tag_untouched(self):
        text = "普通的一句话，不收敛空白。"
        assert strip_mood_tags(text) == text

    def test_empty_text_safe(self):
        assert strip_mood_tags("") == ""

    def test_case_insensitive(self):
        assert strip_mood_tags("好<<MOOD:SECURE:1>>了") == "好了"

    def test_unclosed_shape_not_removed(self):
        """没有 >> 收尾不是标签形态：不误剥正文。"""
        text = "看看这个 <<mood:happy"
        assert strip_mood_tags(text) == text

    def test_cross_tag_not_swallowed(self):
        """长度上限内的独立标签逐个剥；两个标签之间的正文保留。"""
        assert strip_mood_tags("a<<mood:happy>>b<<mood:lost:0.1>>c") == "abc"

    def test_every_parseable_tag_is_strippable(self):
        """与 soul.mood.parse_mood_tag 交叉一致：能解析的形态必能被完整剥掉。"""
        from virtuallover.soul.mood import parse_mood_tag

        sample = "文本<<mood:shy:0.4>>中<<MOOD:secure:1.2>>尾"
        parsed = parse_mood_tag(sample)
        assert parsed is not None
        cleaned, _, _ = parsed
        assert strip_mood_tags(sample).strip() == cleaned


class TestMoodTagRegexShape:
    def test_regex_is_bounded(self):
        """剥除形态有长度上限与换行隔离：无灾难回溯面。"""
        assert MOOD_TAG_RE.search("<<" + "mood:" + "x" * 200 + ">>") is None
        assert MOOD_TAG_RE.fullmatch("<<mood:" + "x" * 100 + ">>") is not None

    def test_regex_ignores_multiline_inner(self):
        assert MOOD_TAG_RE.search("<<mood:happy\n:0.5>>") is None


class TestCleaningPrimitivesRegression:
    """本模块既有出口的轻量回归（详测在 test_voice_polish.py）。"""

    def test_clean_proactive_text_still_works(self):
        assert clean_proactive_text("宝宝(?:在忙什么)呀？😊") == "宝宝呀？"

    def test_clean_tts_text_still_works(self):
        assert clean_tts_text("(｡･ω･｡)ﾉ❤ 早上好呀") == "早上好呀"
