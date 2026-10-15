# -*- coding: utf-8 -*-
"""textutil 单元测试：CQ 码/消息段 → 纯文本单向降级。

覆盖 SPEC §3 第 6 条的段类型降级表与容错边界：
- 六类已知段（text/face/at/reply/image/record）的降级映射；
  其中 face 段按 faces.FACE_NAMES 命名（id 14 → ``[微笑]``），
  未知 id 回落通用占位 ``[表情]``（绝不编造名称）；
- 未知段类型占位 + 不抛异常；
- Number|String 双形态 id/qq 兼容；
- 字符串形态 CQ 码解析、转义单趟解码、残缺码容错；
- reply 段 id 提取（get_msg 回查入口）；
- 模块单向性守卫：不存在任何"构造回去"的函数（禁写 CQ 构造器）。

本文件只允许出现占位 QQ 号（10xxx），不出现任何真实账号数据。
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
from pathlib import Path

# sys.path 引导：virtuallover/ 是 namespace 包，仓库根必须可导入。
# （骨架 tests/conftest.py 就绪后此块幂等无害）
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from virtuallover.bridge import textutil  # noqa: E402
from virtuallover.bridge.textutil import (  # noqa: E402
    PLACEHOLDER_AT_ALL,
    PLACEHOLDER_FACE,
    PLACEHOLDER_IMAGE,
    PLACEHOLDER_RECORD,
    PLACEHOLDER_REPLY,
    cq_to_plain,
    find_reply_id,
    segments_to_plain,
)

# ---------------------------------------------------------------------------
# 字符串形态 CQ 码 → 纯文本
# ---------------------------------------------------------------------------


class TestCqToPlain:
    def test_plain_text_unchanged(self):
        """无 CQ 码的普通文本原样保留。"""
        assert cq_to_plain("今晚吃什么呀") == "今晚吃什么呀"

    def test_empty_and_none_like(self):
        assert cq_to_plain("") == ""

    def test_face_named_by_id_table(self):
        """face 段 id 在常用表情表内 → [名称]（她看得懂小黄脸了）。"""
        assert cq_to_plain("哈哈[CQ:face,id=12]") == "哈哈[调皮]"
        assert cq_to_plain("[CQ:face,id=85]") == "[飞吻]"

    def test_face_unknown_id_falls_back(self):
        """表外/残缺 id 回落通用占位 [表情]，绝不编造名称。"""
        assert cq_to_plain("[CQ:face,id=999]") == PLACEHOLDER_FACE
        assert cq_to_plain("[CQ:face,id=abc]") == PLACEHOLDER_FACE
        assert cq_to_plain("[CQ:face]") == PLACEHOLDER_FACE

    def test_image_placeholder(self):
        assert cq_to_plain("[CQ:image,file=abc.jpg,url=http://x/y.jpg]") == PLACEHOLDER_IMAGE

    def test_record_placeholder(self):
        assert cq_to_plain("[CQ:record,file=abc.amr]") == PLACEHOLDER_RECORD

    def test_reply_placeholder(self):
        assert cq_to_plain("[CQ:reply,id=999]楼上说得对") == PLACEHOLDER_REPLY + "楼上说得对"

    def test_at_numeric(self):
        assert cq_to_plain("[CQ:at,qq=10001] 在吗") == "@10001 在吗"

    def test_at_all(self):
        assert cq_to_plain("[CQ:at,qq=all]") == PLACEHOLDER_AT_ALL

    def test_unknown_type_generic_placeholder(self):
        assert cq_to_plain("[CQ:json,data=%7B%7D]") == "[CQ:json]"

    def test_mixed_text_and_codes(self):
        src = "看[CQ:image,file=a.jpg]和[CQ:face,id=2]，[CQ:at,qq=10002]快回"
        want = "看" + PLACEHOLDER_IMAGE + "和[色]，@10002快回"
        assert cq_to_plain(src) == want

    def test_multiple_codes_consecutive(self):
        assert cq_to_plain("[CQ:face,id=1][CQ:face,id=2]") == "[撇嘴][色]"

    def test_unclosed_code_kept_as_text(self):
        """残缺未闭合的 [CQ:... 不被误解析，按普通文本保留。"""
        assert cq_to_plain("你好[CQ:face,id=1") == "你好[CQ:face,id=1"

    def test_literal_bracket_escaped(self):
        """wire 上的字面 [ 由 &#91; 编码，解码后也不会被误判成 CQ 码。"""
        assert cq_to_plain("&#91;CQ:face,id=1&#93;") == "[CQ:face,id=1]"

    def test_unescape_single_pass(self):
        """单趟解码：&amp;#91; 必须停在字面量 &#91;，不得二次解码成 [。"""
        assert cq_to_plain("&amp;#91;") == "&#91;"

    def test_unescape_all_entities(self):
        assert cq_to_plain("a&amp;b &#91;x&#93; y&#44;z") == "a&b [x] y,z"

    def test_param_value_escaped_comma_not_split(self):
        """参数值里的转义逗号（&#44;）不会分裂参数：先按逗号 split、后解码。
        因此 qq=100&#44;01 的原始意图值是字面量 "100,01"。"""
        assert cq_to_plain("[CQ:at,qq=100&#44;01]") == "@100,01"

    def test_code_without_params(self):
        assert cq_to_plain("[CQ:face]") == PLACEHOLDER_FACE


# ---------------------------------------------------------------------------
# array 段 → 纯文本（主路径）
# ---------------------------------------------------------------------------


class TestSegmentsToPlain:
    def test_none_and_empty(self):
        assert segments_to_plain(None) == ""
        assert segments_to_plain([]) == ""

    def test_text_segment(self):
        assert segments_to_plain([{"type": "text", "data": {"text": "晚安"}}]) == "晚安"

    def test_text_without_data_tolerated(self):
        """缺 data 的 text 段按空文本容错，不抛异常。"""
        assert segments_to_plain([{"type": "text"}]) == ""

    def test_known_placeholders(self):
        """face 段按表情表命名，其余已知段仍降级占位。"""
        segs = [
            {"type": "face", "data": {"id": 1}},
            {"type": "reply", "data": {"id": 2}},
            {"type": "image", "data": {"file": "a.png"}},
            {"type": "record", "data": {"file": "a.amr"}},
        ]
        assert segments_to_plain(segs) == (
            "[撇嘴]" + PLACEHOLDER_REPLY + PLACEHOLDER_IMAGE + PLACEHOLDER_RECORD
        )

    def test_face_segment_naming_and_fallback(self):
        """array face 段：表内 id → 名称（Number|String 双形态一致），表外 → [表情]。"""
        named_int = segments_to_plain([{"type": "face", "data": {"id": 14}}])
        named_str = segments_to_plain([{"type": "face", "data": {"id": "28"}}])
        assert named_int == "[微笑]"
        assert named_str == "[憨笑]"
        unknown = segments_to_plain([{"type": "face", "data": {"id": 999}}])
        no_data = segments_to_plain([{"type": "face"}])
        empty_data = segments_to_plain([{"type": "face", "data": {}}])
        assert unknown == no_data == empty_data == PLACEHOLDER_FACE

    def test_at_number_and_string(self):
        """Number|String 兼容：qq 传 int 与 str 产出一致。"""
        assert segments_to_plain([{"type": "at", "data": {"qq": 10001}}]) == "@10001"
        assert segments_to_plain([{"type": "at", "data": {"qq": "10001"}}]) == "@10001"

    def test_at_all(self):
        assert segments_to_plain([{"type": "at", "data": {"qq": "all"}}]) == PLACEHOLDER_AT_ALL

    def test_unknown_segment_degrades(self):
        assert segments_to_plain([{"type": "json", "data": {"data": "{}"}}]) == "[CQ:json]"
        assert segments_to_plain([{"type": "forward", "data": {"id": 1}}]) == "[CQ:forward]"

    def test_non_dict_segment_skipped(self):
        """非 dict 的残缺段跳过，不抛异常。"""
        assert segments_to_plain(["oops", {"type": "text", "data": {"text": "ok"}}]) == "ok"

    def test_typical_private_event(self):
        """贴近 NapCat 实际下行的完整事件段列表。"""
        segs = [
            {"type": "reply", "data": {"id": 12345}},
            {"type": "text", "data": {"text": "这条语音笑死我了 "}},
            {"type": "record", "data": {"file": "xxx.amr", "url": "http://example.invalid/a.amr"}},
        ]
        assert segments_to_plain(segs) == PLACEHOLDER_REPLY + "这条语音笑死我了 " + PLACEHOLDER_RECORD

    def test_text_is_raw_no_unescape(self):
        """array 格式的 text 段是原始文本，不做 CQ 反转义（&#91; 原样保留）。"""
        assert segments_to_plain([{"type": "text", "data": {"text": "&#91;"}}]) == "&#91;"


# ---------------------------------------------------------------------------
# reply 段 id 提取（get_msg 回查入口）
# ---------------------------------------------------------------------------


class TestFindReplyId:
    def test_none_when_absent(self):
        assert find_reply_id(None) is None
        assert find_reply_id([{"type": "text", "data": {"text": "x"}}]) is None

    def test_numeric_id(self):
        assert find_reply_id([{"type": "reply", "data": {"id": 555}}]) == "555"

    def test_string_id(self):
        assert find_reply_id([{"type": "reply", "data": {"id": "555"}}]) == "555"

    def test_first_reply_wins(self):
        segs = [
            {"type": "text", "data": {"text": "前缀"}},
            {"type": "reply", "data": {"id": 1}},
            {"type": "reply", "data": {"id": 2}},
        ]
        assert find_reply_id(segs) == "1"

    def test_reply_without_id(self):
        assert find_reply_id([{"type": "reply", "data": {}}]) is None


# ---------------------------------------------------------------------------
# 单向性守卫：禁写 CQ 构造器（SPEC §3 第 8 条"永不拼 CQ 码"）
# ---------------------------------------------------------------------------


class TestOneWayGuard:
    def test_no_constructor_functions(self):
        """模块源码里不允许出现任何 纯文本→CQ/段 构造方向的函数。"""
        source = inspect.getsource(textutil)
        assert re.search(r"def\s+(build|encode|make|to_cq|plain_to)\w*\s*\(", source) is None

    @staticmethod
    def _suspicious_cq_strings() -> list[str]:
        """tokenize 扫描模块源码，找出"疑似 CQ 码构造模板"的字符串。

        合法白名单（降级方向的 [CQ: 出现形态）：
        - 解析正则的 pattern（以 ``\\[CQ:`` 转义开头）；
        - f-string 占位片段（FSTRING_MIDDLE，形如 ``[CQ:{...}``）；
        - 模块/类/函数 docstring（用 AST 行号跨度精确豁免）。
        其余任何含 ``[CQ:`` 的字符串常量即视为构造/硬编码模板。
        """
        import io
        import tokenize

        source = inspect.getsource(textutil)
        tree = ast.parse(source)
        # 1) 收集全部作用域 docstring 的行号跨度（tokenize 与 ast 行号一致）
        scopes = [tree] + [
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        doc_spans: list[tuple[int, int]] = []
        for scope in scopes:
            body = getattr(scope, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                doc = body[0]
                doc_spans.append((doc.lineno, doc.end_lineno))

        def in_docstring(start_line: int) -> bool:
            return any(lo <= start_line <= hi for lo, hi in doc_spans)

        # 2) tokenize 扫描字符串字面量
        suspicious: list[str] = []
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.STRING and not in_docstring(tok.start[0]):
                literal = str(ast.literal_eval(tok.string))
                # 解析正则（\[CQ: 转义开头）合法；其余含 [CQ: 的常量即违例
                if "[CQ:" in literal and not literal.startswith("\\[CQ:"):
                    suspicious.append(literal)
            elif tok.type == tokenize.FSTRING_MIDDLE and "[CQ:" in tok.string:
                continue  # f-string 占位片段（如 [CQ:{ctype}]）是降级输出，合法
        return suspicious

    def test_no_cq_code_templates(self):
        """除解析正则/f-string 占位/docstring 外，不允许任何含 [CQ: 的字符串。"""
        assert self._suspicious_cq_strings() == []
