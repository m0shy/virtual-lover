# -*- coding: utf-8 -*-
"""表情包后处理测试（SPEC §8.4 / §12 行为断言：meme 白名单）——
严格匹配、白名单校验、命中率、单条上限、独立消息、残缺清洗、图库目录。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from virtuallover.soul.memes import (
    DEFAULT_WHITELIST,
    HISTORY_TAGS,
    build_whitelist,
    find_tags,
    pick_image,
    process_reply,
    strip_all_markers,
)

# 真实图库目录（实测 19 个 tag 目录 398 张图 + R3 补缺占位 3 目录 9 张；测试只读不写）
LIVE_MEMES_DIR = REPO_ROOT / "local/shared_plugin_data/meme_manager/memes"
# R3 图库补缺的占位目录（历史 tag 补齐，占位图文件名带 PLACEHOLDER_ 前缀）
PLACEHOLDER_TAGS = frozenset({"blush", "heart", "hug"})


class _SeqRng:
    """确定性 rng：randint 依次返回预设序列，choice 取第一个。"""

    def __init__(self, *randints: int):
        self.seq = list(randints)
        self.i = 0

    def randint(self, a: int, b: int) -> int:
        v = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        return v

    def choice(self, seq):
        return seq[0]


# ---------------------------------------------------------------------------
# 严格匹配 &&[^&]+&& 与提取
# ---------------------------------------------------------------------------


class TestFindTags:
    def test_basic_and_multiple(self):
        assert find_tags("你好&&happy&&呀") == ["happy"]
        assert find_tags("&&happy&&和&&sad&&") == ["happy", "sad"]

    def test_chinese_and_long_tag(self):
        assert find_tags("&&开心&&") == ["开心"]

    def test_empty_marker_not_matched(self):
        """&&&&（空标记）不满足 &&[^&]+&&，属残缺形态。"""
        assert find_tags("&&&&") == []

    def test_unclosed_not_matched(self):
        assert find_tags("心情 &&happy 呀") == []

    def test_nested_ampersand_not_matched(self):
        """&&a&&b&& 中间段含 & 的形态：严格正则只取 &&[^&]+&&。"""
        assert find_tags("&&a&&b&&") == ["a"]  # b 段前面是 && 但后面无 && 配对


class TestTagNormalize:
    """R3 归一化：历史 _jpg 后缀变体提取即剥（白名单校验与取图同享）。"""

    def test_jpg_suffix_stripped(self):
        assert find_tags("&&blush_jpg&&") == ["blush"]
        assert find_tags("&&happy_jpg&&和&&sad&&") == ["happy", "sad"]

    def test_plain_tag_untouched(self):
        assert find_tags("&&happy&&") == ["happy"]
        assert find_tags("&&givemoney&&") == ["givemoney"]

    def test_empty_after_normalize_dropped(self):
        """&&_jpg&& 归一化后为空，按残迹丢弃。"""
        assert find_tags("&&_jpg&&") == []

    def test_whitespace_stripped(self):
        assert find_tags("&& happy_jpg &&") == ["happy"]


# ---------------------------------------------------------------------------
# 白名单（三份实测来源）
# ---------------------------------------------------------------------------


class TestWhitelist:
    def test_default_matches_live_image_dirs(self):
        """默认白名单的图库部分与实际目录一一对应（19 tag + R3 占位 3 tag）。"""
        if not LIVE_MEMES_DIR.is_dir():
            pytest.skip("本地图库目录不存在（本地环境缺失）")
        dirs = {p.name for p in LIVE_MEMES_DIR.iterdir() if p.is_dir()}
        assert dirs == set(DEFAULT_WHITELIST) | set(PLACEHOLDER_TAGS)
        n_imgs = sum(1 for d in dirs for f in (LIVE_MEMES_DIR / d).iterdir() if f.is_file())
        assert n_imgs == 407  # 398 张原图 + R3 占位 9 张（blush/heart/hug 各 3）
        for tag in PLACEHOLDER_TAGS:
            assert all(f.name.startswith("PLACEHOLDER_")
                       for f in (LIVE_MEMES_DIR / tag).iterdir() if f.is_file())

    def test_history_tags_included(self):
        """历史实测 10 个 base tag 全部进白名单（其中 3 个仍无图库目录，命中只清理）。"""
        wl = build_whitelist()
        for tag in ("blush", "hug", "wronged", "heart", "kawaii", "kiss"):
            assert tag in wl
        # _jpg 变体不进白名单（归一化前形态；R3 起提取层剥后缀，见 TestTagNormalize）
        for bad in ("blush_jpg", "happyjpg", "jpg_test"):
            assert bad not in wl

    def test_extra_tags_mergeable(self):
        wl = build_whitelist(extra_tags=["custom_tag"])
        assert "custom_tag" in wl


# ---------------------------------------------------------------------------
# 取图
# ---------------------------------------------------------------------------


class TestPickImage:
    def test_live_dir_pick(self):
        if not LIVE_MEMES_DIR.is_dir():
            pytest.skip("本地图库目录不存在")
        img = pick_image(LIVE_MEMES_DIR, "happy", random.Random(1))
        assert img is not None and img.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp")
        assert img.parent.name == "happy"

    def test_missing_tag_returns_none(self, tmp_path):
        assert pick_image(tmp_path, "happy") is None

    def test_empty_dir_returns_none(self, tmp_path):
        (tmp_path / "happy").mkdir()
        assert pick_image(tmp_path, "happy") is None

    def test_non_image_files_ignored(self, tmp_path):
        d = tmp_path / "happy"
        d.mkdir()
        (d / "readme.txt").write_text("x", encoding="utf-8")
        assert pick_image(tmp_path, "happy") is None


# ---------------------------------------------------------------------------
# process_reply 全链路：命中率 / 上限 / 清理 / 独立消息
# ---------------------------------------------------------------------------


class TestProcessReply:
    MEMES = LIVE_MEMES_DIR if LIVE_MEMES_DIR.is_dir() else None

    @pytest.mark.skipif(MEMES is None, reason="本地图库目录不存在")
    def test_hit_sends_one_image_as_separate_message(self):
        """命中：取 1 张图（独立消息语义），文本标记全部清除。"""
        plan = process_reply(
            "给你看&&happy&&好玩吧", memes_dir=self.MEMES,
            whitelist=DEFAULT_WHITELIST, rng=_SeqRng(1),
        )
        assert plan.image_path is not None and plan.image_path.parent.name == "happy"
        assert plan.tag == "happy"
        assert "&&" not in plan.text and "给你看" in plan.text and "好玩吧" in plan.text

    @pytest.mark.skipif(MEMES is None, reason="本地图库目录不存在")
    def test_miss_cleans_tags_only(self):
        """概率未命中（60% 之外的 40%）：只清理标记，不发图。"""
        plan = process_reply(
            "哈哈&&happy&&", memes_dir=self.MEMES,
            whitelist=DEFAULT_WHITELIST, rng=_SeqRng(100),
        )
        assert plan.image_path is None and plan.tag is None
        assert plan.text == "哈哈"

    def test_probability_bounds(self, tmp_path):
        """probability=100 必中、=0 必不中。"""
        (tmp_path / "happy").mkdir()
        (tmp_path / "happy" / "a.jpg").write_bytes(b"x")
        always = process_reply("&&happy&&", memes_dir=tmp_path,
                               probability=100, rng=_SeqRng(1))
        never = process_reply("&&happy&&", memes_dir=tmp_path,
                              probability=0, rng=_SeqRng(1))
        assert always.image_path is not None
        assert never.image_path is None

    @pytest.mark.skipif(MEMES is None, reason="本地图库目录不存在")
    def test_single_image_cap(self):
        """多个合法 tag 也只取第一个（单条上限 1 张）。"""
        plan = process_reply(
            "&&happy&&&&sad&&", memes_dir=self.MEMES,
            whitelist=DEFAULT_WHITELIST, rng=_SeqRng(1, 100),
        )
        assert plan.image_path is not None and plan.tag == "happy"
        assert plan.dropped_tags == ["sad"]  # 第二个被上限挡下（仍从文本清理）

    @pytest.mark.skipif(MEMES is None, reason="本地图库目录不存在")
    def test_illegal_tag_silently_cleaned(self):
        """非法 tag（白名单外）静默清理：不发图、不报错、文本净化。

        &&blush_jpg&& 经 R3 归一化按基础 tag blush 校验：blush 不在
        DEFAULT_WHITELIST（19 图库 tag）内，故以 blush 名义落 dropped。
        """
        plan = process_reply(
            "&&foo&&与&&blush_jpg&&", memes_dir=self.MEMES,
            whitelist=DEFAULT_WHITELIST, rng=_SeqRng(1),
        )
        assert plan.image_path is None
        assert set(plan.dropped_tags) == {"foo", "blush"}
        assert "&&" not in plan.text

    @pytest.mark.skipif(MEMES is None, reason="本地图库目录不存在")
    def test_history_jpg_suffix_normalized_hits_gallery(self):
        """R3 修复实证：历史形态 &&happy_jpg&& 剥后缀即命中图库出图。"""
        plan = process_reply(
            "开心～&&happy_jpg&&", memes_dir=self.MEMES,
            whitelist=build_whitelist(), rng=_SeqRng(1),
        )
        assert plan.image_path is not None and plan.image_path.parent.name == "happy"
        assert plan.tag == "happy"
        assert "&&" not in plan.text

    @pytest.mark.skipif(MEMES is None, reason="本地图库目录不存在")
    def test_placeholder_tag_blush_now_picks_image(self):
        """R3 图库补缺实证：历史 tag blush 从「白名单但无图」变为能出占位图。"""
        plan = process_reply(
            "脸红了&&blush&&", memes_dir=self.MEMES,
            whitelist=build_whitelist(), rng=_SeqRng(1),
        )
        assert plan.image_path is not None
        assert plan.image_path.parent.name == "blush"
        assert plan.image_path.name.startswith("PLACEHOLDER_")
        assert plan.tag == "blush"

    @pytest.mark.skipif(MEMES is None, reason="本地图库目录不存在")
    def test_whitelisted_but_no_image_cleaned(self):
        """白名单内但图库无图（历史 tag wronged）：清理标记只发文字。"""
        plan = process_reply(
            "委屈委屈&&wronged&&", memes_dir=self.MEMES,
            whitelist=build_whitelist(), rng=_SeqRng(1),
        )
        assert plan.image_path is None and plan.tag is None
        assert "wronged" in plan.dropped_tags
        assert "&&" not in plan.text and "委屈委屈" in plan.text

    def test_no_tags_no_image(self, tmp_path):
        plan = process_reply("普通回复没有标记", memes_dir=tmp_path, rng=_SeqRng(1))
        assert plan.image_path is None and plan.text == "普通回复没有标记"
        assert plan.dropped_tags == []


# ---------------------------------------------------------------------------
# 残缺 && 清洗
# ---------------------------------------------------------------------------


class TestStripResidue:
    @pytest.mark.parametrize("raw,expected", [
        # 只清 && 残迹；标记内的词是正文文字，保留不删（保守清洗）
        ("未闭合 &&happy 标记", "未闭合 happy 标记"),
        ("孤立 && 符号", "孤立 符号"),
        ("空标记 &&&& 结束", "空标记 结束"),
        ("连续 &&&&happy&&&& 标记", "连续 标记"),
        ("正常 &&happy&& 清理", "正常 清理"),
    ])
    def test_residue_forms(self, raw, expected):
        assert strip_all_markers(raw) == expected

    def test_multiline_converged(self):
        text = "第一行&&sad&&\n\n\n\n第二行&&"
        out = strip_all_markers(text)
        assert "\n\n\n" not in out
        assert "第一行" in out and "第二行" in out
