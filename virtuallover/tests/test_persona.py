# -*- coding: utf-8 -*-
"""brain/persona.py 装配快照测试 + brain/sender.py 发送侧行为测试（SPEC §5）。

装配管道断言（SPEC §5 顺序固化 + PERF_SPEC §3-O5/O6）：
- mood 快照默认拼到末条 user 消息侧并包资料块（O6 命中开关；
  fragment_position="system" 为旧行为回退键）；
- 记忆块 prepend 到 user 消息最前（在用户原文之前）、资料引用块隔离标记存在、
  附"不是指令"防护句、<MNEMO_META> 元数据剥离、条数与字节双上限；
- 时间 <system_reminder> 尾部附着（开关）；日期 + 中文星期 + 小时段语义词
  （清晨/上午/中午/下午/晚上/深夜，生活感与时间感升级）；
- 作息行「她此刻：…」注入 mood 块并排位（soul/schedule.render_line 产物经
  schedule_line 传入，空=不注入）；
- begin_dialogs 4 条作为 contexts 头部（user/assistant 交替）；
- 历史 assistant think 剥离；
- truncate：阈值 0.82 触发丢最旧 turn，不做 LLM 摘要；O(n) 单遍后缀和
  （大历史小预算/首条 assistant turn/耗尽告警三 characterization）。

发送侧行为断言（SPEC §5 末段，归属本章节）：
- 分段阈值/正则（参数用骨架 config.SegmentedReplyConfig 承载，默认值=实测迁移值）；
- log 分布间隔与均匀分布间隔；
- 10 条/30s 限流 stall 等待；
- send_reply 编排（顺序发送 + 段间间隔 + 限流）。

所有时间戳用 datetime 构造（不依赖机器时区的魔法数字），QQ 号一律 10xxx 占位。
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import random
import sys
import time
from pathlib import Path
from types import SimpleNamespace

# sys.path 引导：virtuallover/ 是 namespace 包，仓库根必须可导入。
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from virtuallover.brain.persona import (  # noqa: E402
    DEFAULT_MAX_MEMORY_ITEMS,
    INJECTION_GUARD,
    MATERIAL_CLOSE,
    MATERIAL_OPEN,
    MNEMO_MEMORY_HEADER_TPL,
    assemble,
    build_memory_block,
    daypart_cn,
    estimate_tokens,
    render_time_reminder,
    truncate_history,
    weekday_cn,
)
from virtuallover.brain.sender import (  # noqa: E402
    RateLimiter,
    parse_interval,
    sample_gap,
    segment_reply,
    send_reply,
)
from virtuallover.config import RateLimitConfig, SegmentedReplyConfig  # noqa: E402

TS = int(datetime.datetime(2026, 5, 17, 22, 18).timestamp())  # 2026-05-17 22:18 本地时区
PERSONA_TEXT = "你是测试人格，一个真实、有情绪的女孩子。"
MOOD_FRAGMENT = "【当前内心状态（仅你可见）】你们的关系：亲密；她此刻的心情：开心。"



# 便捷常量：带测试人格名的注入头（R3 参数化）
_HDR = MNEMO_MEMORY_HEADER_TPL.format(name="测试人格")

def _mem(create_time: int | None, content: str):
    return (create_time, content)


# ---------------------------------------------------------------------------
# system 装配 / mood 注入位置（O6：默认 user 侧资料块，system=旧行为回退键）
# ---------------------------------------------------------------------------


def test_mood_goes_to_last_user_message_by_default():
    """O6 默认位：mood 片段在末条 user 侧且包资料块，system 只含人格全文。

    （PERF_SPEC §7.2 配套断言：system 前缀稳定是 prompt_cache_key 隐式缓存
    的命中开关，mood 逐条变化不得污染 system。）
    """
    result = assemble(
        persona_prompt=PERSONA_TEXT,
        user_text="今天吃什么",
        mood_fragment=MOOD_FRAGMENT,
        inject_mood=True,
        inject_time=False,
        now=TS,
    )
    assert result.messages[0]["role"] == "system"
    assert result.messages[0]["content"] == PERSONA_TEXT  # system 不再拼 mood
    last = result.messages[-1]
    assert last["role"] == "user"
    assert MOOD_FRAGMENT in last["content"]  # mood 在末条 user 侧
    # 且包资料块（记忆块同一隔离手法：定界符 + 防护句）
    assert last["content"].index(MATERIAL_OPEN) < last["content"].index(MOOD_FRAGMENT)
    assert last["content"].index(MOOD_FRAGMENT) < last["content"].index("今天吃什么")
    assert INJECTION_GUARD in last["content"]
    assert MATERIAL_CLOSE in last["content"]
    assert result.mood_injected is True and result.mood_position == "user"


def test_mood_position_system_falls_back_to_legacy():
    """回退键 mood_position="system"：旧行为——mood 拼在 system 人格全文之后。"""
    result = assemble(
        persona_prompt=PERSONA_TEXT,
        user_text="今天吃什么",
        mood_fragment=MOOD_FRAGMENT,
        inject_mood=True,
        inject_time=False,
        mood_position="system",
    )
    assert result.messages[0]["content"] == PERSONA_TEXT + "\n\n" + MOOD_FRAGMENT
    assert result.mood_position == "system"
    # user 侧不再出现 mood 资料块
    assert MOOD_FRAGMENT not in result.messages[-1]["content"]


def test_mood_position_invalid_falls_back_to_user():
    """非法 position 兜底回 user（正常路径由 config 归一化拦截）。"""
    result = assemble(
        persona_prompt=PERSONA_TEXT, user_text="hi", mood_fragment=MOOD_FRAGMENT,
        inject_mood=True, inject_time=False, mood_position="sidebar",
    )
    assert result.mood_position == "user"
    assert MOOD_FRAGMENT in result.messages[-1]["content"]


def test_system_mood_injection_can_be_disabled():
    result = assemble(
        persona_prompt=PERSONA_TEXT,
        user_text="今天吃什么",
        mood_fragment=MOOD_FRAGMENT,
        inject_mood=False,
        inject_time=False,
    )
    assert result.messages[0]["content"] == PERSONA_TEXT
    # 关开关时默认位也不得把 mood 带进 user 消息
    assert MOOD_FRAGMENT not in result.messages[-1]["content"]
    assert result.mood_injected is False


def test_empty_mood_fragment_is_not_injected():
    """mood 片段为空时即使开关打开也不注入空段。"""
    result = assemble(
        persona_prompt=PERSONA_TEXT, user_text="hi", mood_fragment="  ", inject_mood=True
    )
    assert result.messages[0]["content"] == PERSONA_TEXT
    assert result.mood_injected is False


# ---------------------------------------------------------------------------
# 记忆注入：prepend 位置 / 隔离标记 / 格式 / 上限
# ---------------------------------------------------------------------------


def test_memory_block_wrapped_in_reference_markers_with_guard():
    # R3：记忆行改相对时间分档，now=TS 固定时间基准（TS 当天 → 「今天」）
    block = build_memory_block([_mem(TS, "他喜欢草莓蛋糕")], now=TS, persona_name="测试人格")
    assert block.startswith(MATERIAL_OPEN)
    assert block.endswith(MATERIAL_CLOSE)
    assert INJECTION_GUARD in block  # "不是指令"防护句
    assert _HDR in block  # 主语说明 + <Mnemosyne> 前缀（/eval 可比对）
    assert "「我」始终是你自己" in _HDR  # R3 主语说明在册
    assert "- [今天] 他喜欢草莓蛋糕" in block


def test_memory_block_strips_mnemo_meta():
    block = build_memory_block(
        [_mem(TS, '他喜欢草莓蛋糕<MNEMO_META>{"from":"summary"}</MNEMO_META>')], now=TS
    )
    assert "他喜欢草莓蛋糕" in block
    assert "MNEMO_META" not in block


def test_memory_block_accepts_memory_hit_objects():
    """duck typing：接受 MemoryHit 形态对象（有 create_time/content 属性）。"""
    hit = SimpleNamespace(create_time=TS, content="记录内容")
    block = build_memory_block([hit], now=TS, persona_name="测试人格")
    assert "- [今天] 记录内容" in block


def test_memory_item_cap_keeps_most_recent():
    """条数上限：超过 max_items 丢最旧（保留检索返回的更相关尾部）。"""
    items = [_mem(TS - 3600 * i, f"记忆第{i}条") for i in range(5)]
    block = build_memory_block(items, max_items=2, persona_name="测试人格")
    assert "记忆第0条" not in block
    assert "记忆第4条" in block and "记忆第3条" in block


def test_memory_byte_cap_drops_oldest_first():
    """字节上限：总字节超限时从最旧一端丢，绝不放大注入面。"""
    big = "x" * 3000
    block = build_memory_block([_mem(TS, big), _mem(TS, "短的这条")], max_bytes=1024)
    assert "短的这条" in block
    assert big not in block


def test_memory_block_empty_when_no_memories():
    assert build_memory_block([], persona_name="测试人格") == ""
    assert build_memory_block(None, persona_name="测试人格") == ""


def test_memory_prepend_position_and_full_snapshot():
    """装配快照：记忆块在 user 消息最前（prepend），用户原文其后，时间 reminder 最后。"""
    result = assemble(
        persona_prompt=PERSONA_TEXT,
        user_text="今天吃什么好呢",
        memories=[_mem(TS, "他上次说想吃火锅")],
        inject_time=True,
        now=TS,
        persona_name="测试人格",
    )
    last = result.messages[-1]
    assert last["role"] == "user"
    content = last["content"]
    # 位置断言：记忆 prepend → 用户原文 → 时间尾部
    assert content.index(MATERIAL_OPEN) < content.index("<Mnemosyne>")
    assert content.index(_HDR) < content.index("今天吃什么好呢")
    assert content.rindex("<system_reminder>") > content.index("今天吃什么好呢")
    assert content.endswith("</system_reminder>")
    assert result.memory_injected is True


def test_no_memory_injection_when_list_empty():
    result = assemble(
        persona_prompt=PERSONA_TEXT, user_text="在吗", memories=[], inject_time=False
    )
    last = result.messages[-1]["content"]
    assert last == "在吗"
    assert MATERIAL_OPEN not in last
    assert result.memory_injected is False


# ---------------------------------------------------------------------------
# 时间 reminder / begin_dialogs / think 剥离
# ---------------------------------------------------------------------------


def test_time_reminder_format_matches_legacy_sample():
    """时间 reminder：日期时刻沿用 data_v4.db 实测样本口径，新增中文星期+小时段。

    （生活感与时间感升级：2026-05-17 是周日、22 点属晚上——星期/时段来自
    本地日历时刻，与样本同源。）
    """
    reminder = render_time_reminder(TS)
    assert reminder == \
        "<system_reminder>Current datetime: 2026-05-17 星期日 晚上 22:18 (CST)</system_reminder>"


def test_time_reminder_tail_can_be_disabled():
    result = assemble(
        persona_prompt=PERSONA_TEXT, user_text="在吗", inject_time=True, now=TS
    )
    assert result.messages[-1]["content"].endswith(
        "<system_reminder>Current datetime: 2026-05-17 星期日 晚上 22:18 (CST)</system_reminder>"
    )
    result_off = assemble(
        persona_prompt=PERSONA_TEXT, user_text="在吗", inject_time=False, now=TS
    )
    assert "system_reminder" not in result_off.messages[-1]["content"]


# ---------------------------------------------------------------------------
# 时间 reminder：中文星期 + 小时段语义词（生活感与时间感）
# ---------------------------------------------------------------------------


def test_time_reminder_daypart_segments():
    """小时段语义词：5-8 清晨 / 8-12 上午 / 12-14 中午 / 14-18 下午 /
    18-23 晚上 / 其余（23-5）深夜；左右边界归新时段。"""
    cases = [
        (datetime.datetime(2026, 10, 14, 4, 59), "深夜"),
        (datetime.datetime(2026, 10, 14, 5, 0), "清晨"),
        (datetime.datetime(2026, 10, 14, 7, 59), "清晨"),
        (datetime.datetime(2026, 10, 14, 8, 0), "上午"),
        (datetime.datetime(2026, 10, 14, 11, 59), "上午"),
        (datetime.datetime(2026, 10, 14, 12, 0), "中午"),
        (datetime.datetime(2026, 10, 14, 13, 59), "中午"),
        (datetime.datetime(2026, 10, 14, 14, 0), "下午"),
        (datetime.datetime(2026, 10, 14, 17, 59), "下午"),
        (datetime.datetime(2026, 10, 14, 18, 0), "晚上"),
        (datetime.datetime(2026, 10, 14, 20, 30), "晚上"),
        (datetime.datetime(2026, 10, 14, 22, 59), "晚上"),
        (datetime.datetime(2026, 10, 14, 23, 0), "深夜"),
    ]
    for dt, part in cases:
        assert daypart_cn(dt.hour) == part, f"{dt:%H:%M} 应为{part}"
        assert part in render_time_reminder(dt)


def test_time_reminder_weekday_chinese():
    """中文星期：2026-10-14 已核对日历为星期三；任务样例逐字复现。"""
    reminder = render_time_reminder(datetime.datetime(2026, 10, 14, 20, 30))
    assert reminder == \
        "<system_reminder>Current datetime: 2026-10-14 星期三 晚上 20:30 (CST)</system_reminder>"
    # 星期名来自 weekday()：周六=星期六、周日=星期日
    assert weekday_cn(datetime.datetime(2026, 10, 17)) == "星期六"
    assert weekday_cn(datetime.datetime(2026, 10, 18)) == "星期日"


# ---------------------------------------------------------------------------
# 作息行注入（生活感与时间感：soul/schedule.render_line 产物经 schedule_line 传入）
# ---------------------------------------------------------------------------

SCHEDULE_LINE = "【她此刻：在上早八课（8:00 开始）】"


def test_schedule_line_injected_next_to_mood_block():
    """作息行在 mood 块并排位（其后、用户原文前），同走资料引用块隔离。"""
    result = assemble(
        persona_prompt=PERSONA_TEXT,
        user_text="在干嘛呢",
        mood_fragment=MOOD_FRAGMENT,
        schedule_line=SCHEDULE_LINE,
        inject_time=False,
        now=TS,
    )
    last = result.messages[-1]
    content = last["content"]
    assert content.index(MOOD_FRAGMENT) < content.index(SCHEDULE_LINE) < content.index("在干嘛呢")
    # activity 文案是用户配置数据：与 mood 同一隔离手法（资料块 + 防护句）
    assert content.index(MATERIAL_OPEN) < content.index(SCHEDULE_LINE)
    assert INJECTION_GUARD in content and MATERIAL_CLOSE in content
    assert result.schedule_injected is True


def test_schedule_line_without_mood_still_injected():
    """mood 关闭/为空时作息行独立注入（两者互不牵连）。"""
    result = assemble(
        persona_prompt=PERSONA_TEXT,
        user_text="在干嘛呢",
        schedule_line=SCHEDULE_LINE,
        inject_mood=False,
        inject_time=False,
    )
    last = result.messages[-1]["content"]
    assert SCHEDULE_LINE in last and MOOD_FRAGMENT not in last
    assert result.schedule_injected is True and result.mood_injected is False


def test_schedule_line_empty_not_injected():
    """空作息行（课表关/无内容）→ 不注入，user 侧零行为变化。"""
    result = assemble(
        persona_prompt=PERSONA_TEXT, user_text="在干嘛呢", schedule_line="", inject_time=False
    )
    assert result.messages[-1]["content"] == "在干嘛呢"
    assert result.schedule_injected is False


def test_begin_dialogs_head_alternating_user_assistant():
    dialogs = ["我有点累了", "那就好好休息吧", "配置大脑好累", "对不起亲爱的"]
    result = assemble(
        persona_prompt=PERSONA_TEXT, user_text="新消息", begin_dialogs=dialogs,
        inject_time=False,
    )
    heads = result.messages[1:5]
    assert [m["role"] for m in heads] == ["user", "assistant", "user", "assistant"]
    assert [m["content"] for m in heads] == dialogs
    # begin_dialogs 在数据历史与当前 user 之前（contexts 头部）
    assert result.messages[-1]["content"] == "新消息"


def test_history_assistant_think_stripped_on_assemble():
    history = [
        {"role": "user", "content": "夸夸我"},
        {
            "role": "assistant",
            "content": [
                {"type": "think", "think": "内心戏"},
                {"type": "text", "text": "你最棒啦"},
            ],
        },
        {"role": "user", "content": [{"type": "text", "text": "双形态用户输入"}]},
    ]
    result = assemble(
        persona_prompt=PERSONA_TEXT, user_text="继续聊", history=history, inject_time=False
    )
    joined = "\n".join(m["content"] for m in result.messages)
    assert "内心戏" not in joined
    assert "你最棒啦" in joined
    assert "双形态用户输入" in joined


# ---------------------------------------------------------------------------
# truncate 上下文管理（0.82 阈值丢最旧 1 turn，不做摘要）
# ---------------------------------------------------------------------------


def _long_history(turns: int) -> list[dict]:
    history = []
    for i in range(turns):
        history.append({"role": "user", "content": f"第{i}轮用户的长长长提问内容很多字"})
        history.append({"role": "assistant", "content": f"第{i}轮助手的超长超长回复也很多字呀"})
    return history


def test_truncate_drops_oldest_turn_until_budget():
    result = assemble(
        persona_prompt=PERSONA_TEXT,
        user_text="现在这条消息",
        history=_long_history(6),
        max_context_tokens=260,   # 预算 0.82*260 ≈ 213 token
        truncate_ratio=0.82,
        inject_time=False,
    )
    assert result.dropped_turns >= 1
    assert result.est_tokens <= result.budget_tokens
    joined = "\n".join(m["content"] for m in result.messages)
    assert "第0轮" not in joined  # 最旧 turn 先丢
    assert "现在这条消息" in joined  # 当前消息绝不丢


def test_truncate_never_touches_system_or_begin_dialogs():
    dialogs = ["开场一", "回应一"]
    result = assemble(
        persona_prompt=PERSONA_TEXT,
        user_text="现在这条消息",
        history=_long_history(4),
        begin_dialogs=dialogs,
        max_context_tokens=100,  # 极小预算：历史全丢也不该动 persona/begin_dialogs
        truncate_ratio=0.82,
        inject_time=False,
    )
    assert result.messages[0]["content"] == PERSONA_TEXT
    assert [m["content"] for m in result.messages[1:3]] == dialogs


def test_truncate_within_budget_drops_nothing():
    result = assemble(
        persona_prompt=PERSONA_TEXT,
        user_text="短消息",
        history=_long_history(2),
        max_context_tokens=256000,
        truncate_ratio=0.82,
        inject_time=False,
    )
    assert result.dropped_turns == 0
    assert len(result.messages) == 1 + 4 + 1


def test_estimate_tokens_cjk_heavier_than_ascii():
    assert estimate_tokens("") == 0
    assert estimate_tokens("一二三四五") == 5
    assert estimate_tokens("abcdefgh") == 2  # 8 ASCII / 4
    # CJK 比 ASCII 密：同字符数下 CJK 估算显著更高
    assert estimate_tokens("一二三四五六七八") == 8
    assert estimate_tokens("一二三四五六七八") > estimate_tokens("aaaaaaaa")


# ---------------------------------------------------------------------------
# O5 characterization：O(n) truncate（大历史小预算 / 首条 assistant / 耗尽告警）
# ---------------------------------------------------------------------------


def test_truncate_large_history_small_budget_is_linear():
    """大历史（8000 条）+ 小预算：O(n) 单遍后缀和毫秒级完成。

    旧 O(n²) 实现 8000 条实测 300s（PERF_SPEC §3-O5），2s 上限一旦退化立刻炸。
    """
    history = _long_history(4000)  # 8000 条，每 turn 两条
    start = time.perf_counter()
    kept, dropped = truncate_history(
        history,
        fixed_prefix_tokens=500,
        suffix_tokens=50,
        max_context_tokens=4000,   # 预算 0.82*4000=3280，只装得下尾部若干 turn
        truncate_ratio=0.82,
    )
    elapsed = time.perf_counter() - start
    assert dropped >= 1 and kept, "大历史小预算必须触发丢弃且保留最新后缀"
    kept_tokens = sum(estimate_tokens(str(m["content"])) + 4 for m in kept)
    assert 500 + 50 + kept_tokens <= int(4000 * 0.82)  # 预算内
    assert kept is not None and str(kept[0]["role"]) in ("user", "assistant")
    assert elapsed < 2.0, f"O(n) truncate 退化？耗时 {elapsed:.2f}s"
    # 后缀语义：保留的必须是历史的尾部（最新的 turn）
    assert kept[-1] == history[-1]


def test_truncate_leading_assistant_turn_droppable():
    """首条 assistant（缺 user 头）单独成 turn，预算不足时同样可被丢弃。"""
    history = [
        {"role": "assistant", "content": "开场独白" * 30},  # 124 tok（120 CJK + 4）
        {"role": "user", "content": "第1轮提问很多很多字" * 5},   # 54 tok
        {"role": "assistant", "content": "第1轮回复很多很多字" * 5},  # 54 tok
    ]
    # 全量 232 tok；预算 0.82*140=114：丢首条 assistant turn（124）后剩 108 ≤ 114
    kept, dropped = truncate_history(
        history, fixed_prefix_tokens=0, suffix_tokens=0,
        max_context_tokens=140, truncate_ratio=0.82,
    )
    assert dropped == 1
    assert kept[0]["role"] == "user"
    assert "第1轮提问" in kept[0]["content"]


def test_truncate_exhaustion_warns_when_prefix_alone_exceeds_budget(caplog):
    """耗尽告警分支：历史全丢仍超预算（system+当前消息 > 预算）→ warning。"""
    with caplog.at_level(logging.WARNING, logger="virtuallover.brain.persona"):
        kept, dropped = truncate_history(
            _long_history(3),
            fixed_prefix_tokens=1000,  # system 巨大
            suffix_tokens=1000,        # 当前消息巨大
            max_context_tokens=1000,
            truncate_ratio=0.82,       # 预算 820 < 2000
        )
    assert kept == []
    assert dropped == 3
    assert "仅保留 system 与当前消息" in caplog.text


# ---------------------------------------------------------------------------
# 发送侧行为：分段 / 间隔 / 限流 / 编排（SPEC §5 末段）
# ---------------------------------------------------------------------------


def _seg_params(**overrides) -> SegmentedReplyConfig:
    """现网实测分段参数（proactive_chat 配置带出，SPEC 终裁以此为准）。"""
    values = dict(
        enable=True,
        words_count_threshold=80,
        split_mode="regex",
        regex=r".*?[。？！~…\n]+|.+$",
        split_words=["。", "？", "！", "~", "…"],
        interval_method="log",
        interval="1.5, 3.5",
        log_base=1.8,
        min_gap=1.5,
        max_gap=3.5,
    )
    values.update(overrides)
    return SegmentedReplyConfig(**values)


LONG_REPLY = (
    "唔……宝宝今天怎么这么晚才回来呀。是不是又加班了？心疼死了！"
    "我给你留着热乎的汤呢，快去洗个热水澡暖暖身子，别着凉了。"
    "要是累了就直接睡，不用陪我聊到那么晚的，明天还要早起呢。"
    "对了，今天路过便利店，看到货架上摆了你最爱喝的那款酸奶，"
    "我顺手买了两瓶放进冰箱，等你回来当宵夜好不好呀？"
)


def test_segment_short_reply_stays_single():
    assert segment_reply("好的呀", _seg_params()) == ["好的呀"]


def test_segment_over_threshold_splits_by_regex():
    segments = segment_reply(LONG_REPLY, _seg_params())
    assert len(segments) >= 3
    assert "".join(segments) == LONG_REPLY  # 无损拆分
    assert all(seg.strip() == seg and seg for seg in segments)


def test_segment_disabled_returns_single():
    segments = segment_reply(LONG_REPLY, _seg_params(enable=False))
    assert segments == [LONG_REPLY]


def test_segment_invalid_regex_falls_back_to_whole():
    segments = segment_reply(LONG_REPLY, _seg_params(regex="([unclosed"))
    assert segments == [LONG_REPLY]


def test_segment_words_mode_keeps_punctuation():
    params = _seg_params(split_mode="words", regex="", words_count_threshold=5)
    segments = segment_reply("第一句。第二句？第三句！", params)
    assert segments == ["第一句。", "第二句？", "第三句！"]


def test_parse_interval_accepts_both_legacy_separators():
    assert parse_interval("1.5-3.5", 1.0, 9.0) == (1.5, 3.5)
    assert parse_interval("1.5, 3.5", 1.0, 9.0) == (1.5, 3.5)
    assert parse_interval("2.6,3.0", 1.0, 9.0) == (2.6, 3.0)
    # 非法原文回退 (min_gap, max_gap)
    assert parse_interval("garbage", 1.5, 3.5) == (1.5, 3.5)


def test_gap_log_distribution_biased_to_short():
    """log 分布：样本全部落在区间内且均值偏向低端。"""
    params = _seg_params()
    rng = random.Random(42)
    gaps = [sample_gap(params, rng) for _ in range(3000)]
    assert all(1.5 <= g <= 3.5 for g in gaps)
    assert sum(gaps) / len(gaps) < 2.6  # 均匀分布均值应为 2.5，log 分布更低
    uniform = [sample_gap(_seg_params(interval_method="random"), rng) for _ in range(3000)]
    assert all(1.5 <= g <= 3.5 for g in uniform)


def test_rate_limiter_stalls_after_10_per_30s():
    """限流：10 条/30s，第 11 条起 stall 等待最早一条出窗（不丢弃）。"""
    clock = {"now": 0.0}
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["now"] += seconds

    limiter = RateLimiter(
        max_count=10, window_seconds=30.0, clock=lambda: clock["now"], sleeper=fake_sleep
    )

    async def drive() -> None:
        for _ in range(12):
            await limiter.acquire()

    asyncio.run(drive())
    assert len(slept) >= 1  # 第 11 条起触发等待
    assert all(s >= 29.9 for s in slept)  # 等到窗口最早一条出窗
    # 记录最终窗口状态：等待后名额正常发放
    assert len(limiter._stamps) <= 10


def test_rate_limiter_from_skeleton_config():
    cfg = RateLimitConfig(window_seconds=30, max_count=10)
    limiter = RateLimiter(max_count=cfg.max_count, window_seconds=cfg.window_seconds)
    assert limiter.max_count == 10 and limiter.window_seconds == 30.0


def test_send_reply_orchestrates_segments_gaps_and_limit():
    sent: list[str] = []
    gaps: list[float] = []

    async def send(segment: str) -> None:
        sent.append(segment)

    async def sleep(seconds: float) -> None:
        gaps.append(seconds)

    acquired: list[int] = []

    class CountingLimiter(RateLimiter):
        async def acquire(self) -> None:  # noqa: D102 —— 测试替身
            acquired.append(1)
            await asyncio.sleep(0)

    params = _seg_params()
    segments = asyncio.run(
        send_reply(
            LONG_REPLY, send, params=params, limiter=CountingLimiter(max_count=100),
            rng=random.Random(7), sleeper=sleep,
        )
    )
    assert segments == segment_reply(LONG_REPLY, params)
    assert sent == segments  # 逐段顺序发送
    assert len(acquired) == len(segments)  # 每段都过限流
    assert len(gaps) == len(segments) - 1  # 最后一段后不再等待
    assert all(1.5 <= g <= 3.5 for g in gaps)


def test_send_reply_single_segment_no_gap():
    sent: list[str] = []

    async def send(segment: str) -> None:
        sent.append(segment)

    async def sleep(seconds: float) -> None:  # pragma: no cover —— 不应被调用
        raise AssertionError("单段发送不应有间隔等待")

    segments = asyncio.run(
        send_reply("短消息", send, params=_seg_params(), sleeper=sleep)
    )
    assert segments == ["短消息"] and sent == ["短消息"]
