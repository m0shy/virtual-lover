# -*- coding: utf-8 -*-
"""R3 记忆质量测试（五轮修缮第三轮）：人设锚 / 主语硬约束 / 弃权出口 / 入库查重 /
相对时间分档 / 注入头主语说明 / /eval 会话隔离 / memory_clean 保守规则 / time_anchor 死配置移除。

背景（R3 审计实证）：总结产物曾把男性恋人写成「她」（存量 id24）；注入行绝对
时间戳让她把当天聊的当上周聊的；功能测试窗口的调试痕迹被写进长期记忆。
全程注入假 chat/假嵌入（conftest 共用夹具），结果确定，不碰真实库与真实 LLM。
"""

from __future__ import annotations

import asyncio
import datetime
import sys
from pathlib import Path

import numpy as np
import pytest

from conftest import FAKE_SESSION, FakeChat, deterministic_vector, make_fake_embedder
from virtuallover.config import MnemosyneConfig
from virtuallover.memory.service import (
    DEDUP_COSINE,
    DEDUP_KEYWORD_RATIO,
    SOURCE_MANUAL,
    MemoryService,
    build_summary_prompts,
    is_abstention,
    _SUMMARY_SYSTEM_TPL,
    _SUMMARY_TEMPLATE,
)
from virtuallover.memory.store import format_memory_line, relative_time_label
from virtuallover.brain.persona import MNEMO_MEMORY_HEADER_TPL, build_memory_block
from virtuallover.tools.memory_clean import _is_candidate

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 时间基准固定（与 test_persona 同手法：datetime 构造，不依赖机器时区魔法数字）
NOW = datetime.datetime(2026, 10, 12, 12, 0).timestamp()


_SYS = _SUMMARY_SYSTEM_TPL.format(persona="测试人格")
_HDR = MNEMO_MEMORY_HEADER_TPL.format(name="测试人格")


def _ts(days: float, hour: int = 9) -> int:
    """距 NOW 前 days 天的时刻（days 为负=未来）。"""
    base = datetime.datetime(2026, 10, 12, hour, 0).timestamp()
    return int(base - days * 86400)


def make_service(fake_store, chat, **kwargs):
    return MemoryService(
        fake_store(), chat_fn=chat, persona_id="测试人格",
        now_fn=lambda: NOW, **kwargs,
    )


async def feed(svc: MemoryService, texts, session: str = FAKE_SESSION):
    """user/assistant 交替喂入（对齐每轮双方各计一次），内联等待总结完成。"""
    for i, text in enumerate(texts):
        role = "user" if i % 2 == 0 else "assistant"
        await svc.on_message(session, role, text, background=False)


# ---------------------------------------------------------------------------
# 人设锚 + 主语硬约束（审计实证：id24 性别漂移、主客倒置）
# ---------------------------------------------------------------------------

def test_summary_system_has_persona_anchor():
    """system 锚三要素：「我」=测试人格本人、对方=男性恋人、绝不改写性别。"""
    assert "你是测试人格" in _SYS
    assert "「我」永远是你自己" in _SYS
    assert "对方是你唯一的恋人（男性）" in _SYS
    assert "绝不改写对方性别" in _SYS


def test_summary_template_has_subject_and_abstention_rules():
    """模板硬约束：主语先行 + 工具调试禁写 + 弃权出口，全部随模板送进模型。"""
    assert "每个事实先明确主语" in _SUMMARY_TEMPLATE
    assert "绝不混写" in _SUMMARY_TEMPLATE
    assert "工具调用/系统调试/命令与路径" in _SUMMARY_TEMPLATE
    assert "绝不写进记忆" in _SUMMARY_TEMPLATE
    assert "（这段不值得记住）" in _SUMMARY_TEMPLATE


def test_build_summary_prompts_carries_guards():
    """经 build_summary_prompts 组装后三要素仍在（真送模型的形态）。"""
    system, user = build_summary_prompts(["对方：你好", "我：你好呀"], persona="测试人格")
    assert "绝不改写对方性别" in system
    assert "每个事实先明确主语" in user
    assert "（这段不值得记住）" in user
    assert "工具调用/系统调试/命令与路径" in user


# ---------------------------------------------------------------------------
# 弃权出口（审计实证：功能测试窗口的调试痕迹入库成毒）
# ---------------------------------------------------------------------------

def test_is_abstention_cases():
    """弃权判定：含标记的短输出算弃权；长记忆/正常记忆不误杀。"""
    assert is_abstention("（这段不值得记住）")
    assert is_abstention(" 这段不值得记住。 ")
    assert is_abstention("（这段不值得记住）——只是寒暄")  # 标记外的极短附加语
    assert not is_abstention("他今天生日，我记住了要送他礼物")  # 正常记忆
    assert not is_abstention("我们聊了很久，他抱怨说这段代码不值得记住，还得改命令行参数")  # 长文引用不误杀
    assert not is_abstention("")
    assert not is_abstention(None)


def test_abstention_clears_buffer_without_storing(fake_store):
    """弃权出口服务级语义：清缓冲、不入库、不走失败退避，后续窗口照常。"""
    chat = FakeChat(replies=["（这段不值得记住）", "我记得他周五要交周报。"])
    svc = make_service(fake_store, chat)

    asyncio.run(feed(svc, ["111", "收到啦", "再测试一下", "好呀在测试表情包"]))
    assert svc._store.count(FAKE_SESSION) == 0   # 弃权：不入库
    assert svc.pending_count(FAKE_SESSION) == 0  # 弃权：缓冲照清（不无限重试）
    state = svc._load_state(FAKE_SESSION)
    assert state.next_retry_ts is None           # 弃权是成功语义：不进退避
    assert state.fail_count == 0

    asyncio.run(feed(svc, ["我周五要交周报", "好哒我记着", "别忘了提醒我", "放心吧"]))
    assert svc._store.count(FAKE_SESSION) == 1   # 后续正常窗口照常入库
    hits = asyncio.run(svc._store.search(FAKE_SESSION, "周报"))
    assert hits and "周报" in hits[0].content


# ---------------------------------------------------------------------------
# 入库查重（同会话近邻 cos>0.85 且关键词高重叠 → 跳过入库只清缓冲）
# ---------------------------------------------------------------------------

def test_dedup_skips_duplicate_summary(fake_store):
    """同会话重复总结：第二窗产出与已入库记忆相同的正文 → 跳过入库、缓冲照清。"""
    summary = "他喜欢草莓蛋糕，我们约好周五一起去吃。"
    # 向量表钉死该正文的向量：入库与查重两次嵌入同向量 → cos=1.0 必超阈值
    store = fake_store(vectors={summary: deterministic_vector(summary)})
    chat = FakeChat(replies=[summary, summary])  # 两窗产出完全相同的总结
    svc = MemoryService(store, chat_fn=chat, persona_id="测试人格", now_fn=lambda: NOW)

    asyncio.run(feed(svc, ["我超爱草莓蛋糕", "那周五去吃呀", "好耶拉钩", "拉钩上吊一百年不许变"]))
    assert store.count(FAKE_SESSION) == 1
    assert svc.pending_count(FAKE_SESSION) == 0

    # 第二窗：模型又总结出同一句话——查重应命中并跳过
    asyncio.run(feed(svc, ["周五记得哦", "当然", "蛋糕店六点关门", "那五点半见"]))
    assert store.count(FAKE_SESSION) == 1, "重复总结不应二次入库"
    assert svc.pending_count(FAKE_SESSION) == 0, "查重命中后缓冲必须照清（防无限重总结）"
    # 模型确实被调用了两次（跳过的是入库，不是总结）
    assert len(chat.calls) == 2


def test_dedup_thresholds_documented():
    """查重阈值在册且语义正确：cos 严格大于、重叠率下限。"""
    assert DEDUP_COSINE == 0.85
    assert 0 < DEDUP_KEYWORD_RATIO <= 1.0


def test_dedup_negative_different_summary_stored(fake_store):
    """不同内容的总结照常入库（查重不误伤正常新记忆）。"""
    chat = FakeChat(replies=["他喜欢草莓蛋糕，约好周五去吃。",
                             "他提到下周三要交项目报告，我会提醒他。"])
    svc = make_service(fake_store, chat)
    asyncio.run(feed(svc, ["我爱草莓蛋糕", "周五去吃呀", "好耶", "拉钩"]))
    asyncio.run(feed(svc, ["我下周三交报告", "我会提醒你的", "谢谢宝宝", "叫我干嘛"]))
    assert svc._store.count(FAKE_SESSION) == 2


# ---------------------------------------------------------------------------
# 相对时间分档（审计实证：绝对时间戳让她把当天当上周）
# ---------------------------------------------------------------------------

def test_relative_time_label_full_tiers():
    """全档位：今天/昨天/N天前/上周/N周前/N个月前，基准固定不随机器漂移。"""
    cases = [
        (_ts(0), "今天"),
        (_ts(1), "昨天"),
        (_ts(2), "2天前"),
        (_ts(6), "6天前"),
        (_ts(7), "上周"),
        (_ts(13), "上周"),
        (_ts(14), "2周前"),
        (_ts(27), "3周前"),
        (_ts(28), "1个月前"),
        (_ts(150), "5个月前"),
    ]
    for create_time, expect in cases:
        assert relative_time_label(create_time, NOW) == expect, f"days={create_time}"


def test_relative_time_label_null_and_distorted_to_earlier():
    """NULL/失真（未来超 1 天、早于 2000 年）统一「较早」档，绝不伪造时刻。"""
    assert relative_time_label(None, NOW) == "较早"
    assert relative_time_label(0, NOW) == "较早"
    assert relative_time_label(_ts(-2), NOW) == "较早"   # 未来 2 天：失真
    assert relative_time_label(_ts(-1), NOW) == "今天"   # 未来 1 天内：时钟漂移余量
    assert relative_time_label(100, NOW) == "较早"        # 1970 年：脏时间戳


def test_format_memory_line_relative_and_meta_strip():
    """注入行：相对分档 + 元数据剥离 + 换行折叠。"""
    line = format_memory_line(_ts(0), "正文<MNEMO_META>x</MNEMO_META>\n第二行", now=NOW)
    assert line == "- [今天] 正文 第二行"


def test_memory_header_carries_subject_note():
    """注入头主语说明：防主客倒置/性别漂移在注入侧也说破；保留 <Mnemosyne> 前缀。"""
    assert _HDR.startswith("<Mnemosyne>")
    assert "「我」（测试人格）" in _HDR
    assert "「我」始终是你自己" in _HDR
    assert "「他/老公/亲爱的」是对方" in _HDR


def test_memory_block_last_week_scenario():
    """「我们上周聊了什么」场景复现：当天标「今天」、10 天前标「上周」，档位分明。"""
    memories = [
        (_ts(0), "他今天说晚饭吃了火锅"),
        (_ts(10), "上周他说要养一只柯基"),
    ]
    block = build_memory_block(memories, now=NOW, persona_name="测试人格")
    assert "- [今天] 他今天说晚饭吃了火锅" in block
    assert "- [上周] 上周他说要养一只柯基" in block
    assert _HDR in block


# ---------------------------------------------------------------------------
# /eval 会话隔离（回归：评测记忆绝不混入主会话检索）
# ---------------------------------------------------------------------------

def test_eval_session_isolated_from_main_session(fake_store):
    """/eval 写入 <session>:EvalMemory（__main__._EVAL_SESSION_SUFFIX 同构），
    store.search 按 session_id 精确过滤——评测事实不出现在主会话检索里，反之亦然。"""
    store = fake_store()
    eval_session = FAKE_SESSION + ":EvalMemory"
    svc = MemoryService(store, chat_fn=None, persona_id="测试人格", now_fn=lambda: NOW)
    asyncio.run(svc.remember(FAKE_SESSION, "主会话记忆：他喜欢吃糖醋排骨。",
                             source=SOURCE_MANUAL))
    asyncio.run(svc.remember(eval_session, "【评测#001】我最喜欢的食物是麻婆豆腐。",
                             source="eval"))

    main_hits = asyncio.run(store.search(FAKE_SESSION, "最喜欢的食物 麻婆豆腐"))
    assert all("#001" not in h.content and "麻婆豆腐" not in h.content for h in main_hits)
    eval_hits = asyncio.run(store.search(eval_session, "最喜欢的食物 麻婆豆腐"))
    assert any("#001" in h.content for h in eval_hits)
    assert all("糖醋排骨" not in h.content for h in eval_hits)


# ---------------------------------------------------------------------------
# memory_clean 保守规则（存量修正只动「上下文明确」的行）
# ---------------------------------------------------------------------------

def test_memory_clean_candidate_rules():
    """准入四则：有她+有恋人指代+无第三方女性语境+非 eval，缺一不修。"""
    id24 = ("对方说最近觉得很累，我们现在没法见面，我碰不到她。我让她快停下来歇一歇，"
            "还想给她揉揉肩，我告诉她我们的心一直靠在一起。")
    assert _is_candidate(id24, {"source": "backfill"})          # 实证 id24 形态：修
    assert not _is_candidate(id24.replace("她", "他"), {})      # 已修正：幂等无命中
    assert not _is_candidate("她今天很累了，我陪着她。", {})     # 无恋人指代：不碰
    assert not _is_candidate("老公说他室友她最近搬家了，我说她辛苦了。", {})  # 第三方女性：不碰
    assert not _is_candidate(id24, {"source": "eval"})          # 评测事实：永不触碰
    assert not _is_candidate("老公说他今天很累。", {})           # 无她：无可修


# ---------------------------------------------------------------------------
# time_anchor 死配置移除（R3：从未被读取的键，删除并留迁移注释）
# ---------------------------------------------------------------------------

def test_time_anchor_dead_config_removed():
    """MnemosyneConfig 不再有 time_anchor 字段；现网 vl.toml 残留行已清。"""
    assert not hasattr(MnemosyneConfig(), "time_anchor")
    vl_toml = ROOT / "local" / "config" / "vl.toml"
    if vl_toml.exists():  # 现网配置在场时校验（无 local/ 的裸 checkout 跳过）
        assert "time_anchor" not in vl_toml.read_text(encoding="utf-8")
