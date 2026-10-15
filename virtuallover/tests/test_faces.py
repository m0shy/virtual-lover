# -*- coding: utf-8 -*-
"""QQ 小黄脸模块测试：face 表、发送白名单、{{face:id}} 标记协议、face 段结构。

- FACE_NAMES / SEND_WHITELIST：与公开 OneBot v11 face 表核对过的锚点
  id（13=呲牙、14=微笑、32=疑问、85=飞吻、106=委屈），以及白名单⊆表、
  id 17 空缺（两份公开来源均无此 id）等硬约束；
- extract_face_markers：剥出/白名单外丢弃/残缺花括号清洗/空白收敛；
- faces_cheatsheet：一行速查、只含白名单 id；
- _seg_face：face 数组段结构（发送侧永不拼 CQ 码）。

本文件只出现占位 id 与表情名称，无任何账号/人设数据。
"""

from __future__ import annotations

import sys
from pathlib import Path

# sys.path 引导：virtuallover/ 是 namespace 包，仓库根必须可导入。
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from virtuallover.bridge.faces import (  # noqa: E402
    FACE_MARKER_RE,
    FACE_NAMES,
    SEND_WHITELIST,
    extract_face_markers,
    faces_cheatsheet,
)
from virtuallover.bridge.onebot_server import _seg_face  # noqa: E402


# ---------------------------------------------------------------------------
# face 表与发送白名单（数据出处见 faces.py docstring，锚点逐一钉住）
# ---------------------------------------------------------------------------


class TestFaceTables:
    def test_sizes(self):
        """常用表约 40 个、发送白名单 20 个（本任务规格）。"""
        assert len(FACE_NAMES) == 40
        assert len(SEND_WHITELIST) == 20

    def test_verified_anchor_ids(self):
        """与公开表情表联网核对的锚点 id（icqq facemap × coolq wiki id 集）。"""
        assert FACE_NAMES[0] == "惊讶"
        assert FACE_NAMES[13] == "呲牙"
        assert FACE_NAMES[14] == "微笑"
        assert FACE_NAMES[21] == "可爱"
        assert FACE_NAMES[32] == "疑问"
        assert FACE_NAMES[66] == "爱心"
        assert FACE_NAMES[85] == "飞吻"
        assert FACE_NAMES[106] == "委屈"
        assert FACE_NAMES[122] == "爱你"
        assert FACE_NAMES[182] == "笑哭"

    def test_known_gap_absent(self):
        """id 17 在两份公开来源（coolq wiki / icqq）中均不存在，绝不编造。"""
        assert 17 not in FACE_NAMES
        assert 999 not in FACE_NAMES

    def test_names_nonempty_strings(self):
        assert all(
            isinstance(name, str) and name.strip()
            for name in FACE_NAMES.values()
        )

    def test_whitelist_subset_of_names(self):
        """白名单必须是常用表的子集（表内命名、表内可发，口径一致）。"""
        assert SEND_WHITELIST <= set(FACE_NAMES)


# ---------------------------------------------------------------------------
# extract_face_markers：剥出 / 丢弃 / 残缺清洗 / 空白收敛
# ---------------------------------------------------------------------------


class TestExtractFaceMarkers:
    def test_no_markers_returns_original(self):
        text = "今晚吃什么呀"
        assert extract_face_markers(text) == (text, [])

    def test_single_marker_extracted(self):
        cleaned, ids = extract_face_markers("么么哒{{face:85}}")
        assert cleaned == "么么哒"
        assert ids == [85]

    def test_multiple_markers_in_order(self):
        cleaned, ids = extract_face_markers("{{face:14}}早呀{{face:85}}{{face:21}}")
        assert cleaned == "早呀"
        assert ids == [14, 85, 21]

    def test_duplicate_ids_kept(self):
        _, ids = extract_face_markers("{{face:14}}{{face:14}}")
        assert ids == [14, 14]

    def test_non_whitelist_id_dropped_but_cleaned(self):
        """22（白眼）在 FACE_NAMES 但不在发送白名单：标记清除、id 不放行。"""
        cleaned, ids = extract_face_markers("哼{{face:22}}")
        assert cleaned == "哼"
        assert ids == []

    def test_unknown_id_dropped_silently(self):
        """幻想 id（表外）只丢弃标记，不报错、不编造。"""
        cleaned, ids = extract_face_markers("嘿{{face:999}}嘿{{face:12345678901234567890}}")
        assert cleaned == "嘿嘿"
        assert ids == []

    def test_mixed_valid_and_invalid(self):
        cleaned, ids = extract_face_markers("A{{face:999}}B{{face:13}}C{{face:22}}")
        assert cleaned == "ABC"
        assert ids == [13]

    def test_leading_zeros_normalized(self):
        _, ids = extract_face_markers("{{face:014}}")
        assert ids == [14]

    def test_malformed_unclosed_kept_as_text(self):
        """未闭合的 {{face:14 只清花括号残迹，词按正文保留（memes 同口径）。"""
        cleaned, ids = extract_face_markers("未闭合 {{face:14 标记")
        assert ids == []
        assert "{{" not in cleaned and "}}" not in cleaned
        assert "face:14" in cleaned

    def test_isolated_braces_cleaned(self):
        cleaned, _ = extract_face_markers("孤立 {{ 与 }} 残迹")
        assert "{" not in cleaned and "}" not in cleaned
        assert "孤立" in cleaned and "残迹" in cleaned

    def test_whitespace_converged(self):
        cleaned, ids = extract_face_markers("哈哈 {{face:14}} 世界")
        assert cleaned == "哈哈 世界"
        assert ids == [14]

    def test_empty_input(self):
        assert extract_face_markers("") == ("", [])

    def test_marker_regex_shape(self):
        """完整标记严格为 {{face:数字}}；花括号内不容空白。"""
        assert FACE_MARKER_RE.fullmatch("{{face:14}}")
        assert FACE_MARKER_RE.fullmatch("{{face:0}}")
        assert FACE_MARKER_RE.fullmatch("{{ face:14 }}") is None


# ---------------------------------------------------------------------------
# faces_cheatsheet：给 LLM 的一行速查
# ---------------------------------------------------------------------------


class TestFacesCheatsheet:
    def test_single_line(self):
        sheet = faces_cheatsheet()
        assert "\n" not in sheet
        assert sheet.strip() == sheet

    def test_contains_verified_entries(self):
        sheet = faces_cheatsheet()
        assert "{{face:14}}=微笑" in sheet
        assert "{{face:85}}=飞吻" in sheet

    def test_exactly_whitelist_ids(self):
        """速查表逐项解析后必须与发送白名单一一对应（不多列、不少列）。"""
        pairs = {}
        for item in faces_cheatsheet().split(" "):
            left, name = item.split("=", 1)
            assert left.startswith("{{face:") and left.endswith("}}")
            pairs[int(left.removeprefix("{{face:").removesuffix("}}"))] = name
        assert set(pairs) == set(SEND_WHITELIST)
        assert all(pairs[fid] == FACE_NAMES[fid] for fid in pairs)


# ---------------------------------------------------------------------------
# _seg_face：face 数组段结构（契约 8：一律数组段，永不拼 CQ 码）
# ---------------------------------------------------------------------------


class TestSegFace:
    def test_segment_shape_int_id(self):
        assert _seg_face(14) == {"type": "face", "data": {"id": "14"}}

    def test_segment_shape_string_id(self):
        assert _seg_face("85") == {"type": "face", "data": {"id": "85"}}

    def test_id_is_string_on_wire(self):
        """规范形态：face 段 id 为字符串（OneBot v11 segment.md 示例口径）。"""
        seg = _seg_face(13)
        assert isinstance(seg["data"]["id"], str)
