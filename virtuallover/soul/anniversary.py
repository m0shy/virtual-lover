# -*- coding: utf-8 -*-
"""soul/anniversary.py —— 纪念日仪式感（R9 功能融合，独立小模块）。

四件事（FUSION_SPEC §5-R9-4 + 纪念日扩展：周年/生日/节日）：

1. **认识第一天锚点**：会话库（brain/history.py messages 表）最早一条消息的
   created_at 就是「你们认识的第一天」——不可伪造、无需用户填写。首次计算后
   原子落盘 ``<storage.data_dir>/anniversary.json``（默认 local/vl_data/
   anniversary.json，gitignored），此后只读锚点不再重算（历史库不可用/为空时
   宁缺勿猜，绝不拿启动时刻冒充纪念日）。
2. **里程碑 + 周年 proactive 触发**：以锚点的本机日历日为基准，第 7/30/100/365
   天各触发一次纪念日消息；第 365 天之后每满一周年年度复发（第 730 天 = 两周年，
   幂等键 ``yearly:730``，与里程碑 ``days:365`` 分开落账、同日不重发）——经
   ProactiveService.generate_and_send 的 template_override 接缝走完整主动消息
   链路（安静时段/活跃避让/质量门/dual_send 语音双轨/历史写回全部同源），总
   开关 ``vl.toml [proactive] anniversary``（默认开，仍需 proactive.enable=true）。
3. **生日**：``vl.toml [anniversary]`` user_birthday / her_birthday
   （"MM-DD"，空=该侧不启用；个人信息只落 local/config/vl.toml，gitignored）。
   当天各触发一次生日仪式感（幂等键 ``birthday:user:YYYY`` /
   ``birthday:her:YYYY``，年份入键 → 自然年年复发），判定/发送/落账语义与
   里程碑完全同源。
4. **公历固定节日**：HOLIDAYS 五个日子（元旦/情人节/520/平安夜/圣诞节）当天
   触发一次（幂等键 ``holiday:YYYY-MM-DD``）；农历节日（七夕/中秋/春节等）
   需要农历换算，本模块刻意不做。判定入口 ceremony_due(state, cfg, now_ts)
   每次返回最高优先级的未发项（里程碑 > 年度复发 > 他的生日 > 她的生日 >
   节日），同日多项时逐次心跳各发一条。

幂等：每项只发一次（state.milestones_sent 记录发送时刻），重启/重复心跳不
重发；同一天内多次判定结果一致（日历日差，不数 24h 周期）。**旧档兼容**：
历史状态里里程碑是纯数字键（"7"/"30"/"100"）——days:N 判定同时回看旧键，
升级到新键名不会重发。

依赖注入：history（HistoryStore 鸭类型，只需 first_created_at）、cfg（鸭类型，
只需 anniversary.user_birthday / anniversary.her_birthday）与时钟全部可注入，
宿主机 pytest 直测，不发真实请求、不真等待。
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("virtuallover.soul.anniversary")

# 状态落盘（相对运行根；与 proactive_state.json 同目录——storage.data_dir 下）
DEFAULT_STATE_RELPATH = "local/vl_data/anniversary.json"

# 里程碑天数（FUSION_SPEC §5：每 7/30/100 天；扩展：第 365 天也走里程碑档）
MILESTONE_DAYS: "tuple[int, ...]" = (7, 30, 100, 365)

# 周年复发步长（天）：里程碑之外每满 N*365 天年度复发（幂等键 yearly:N；
# N=365 本身是里程碑档，两者同日只发里程碑一条——ceremony_due 的 elif 语义）
YEARLY_STEP_DAYS = 365

# 公历固定节日（月, 日, 名称）；农历节日（七夕/中秋/春节等）需要农历换算，刻意不做
HOLIDAYS: "tuple[tuple[int, int, str], ...]" = (
    (1, 1, "元旦"),
    (2, 14, "情人节"),
    (5, 20, "520"),
    (12, 24, "平安夜"),
    (12, 25, "圣诞节"),
)

# 纪念日消息模板（{{days}}/{{current_time}} 占位符，经 render_prompt 同款替换；
# 文案是产品默认值，不携带任何上游品牌字样）
DEFAULT_PROMPT_TEMPLATE = (
    "[系统任务：主动对话] 现在是 {{current_time}}。\n"
    "今天是你们认识的第 {{days}} 天——一个值得记住的小日子。"
    "用一两句符合你的人设的话，自然地提起这个日子并表达你的心情，"
    "不要提任何系统任务字样，不要写日期数字以外的清单。"
)

# 年度复发（认识 N 周年）模板（{{days}}/{{years}}/{{current_time}} 占位符）
YEARLY_PROMPT_TEMPLATE = (
    "[系统任务：主动对话] 现在是 {{current_time}}。\n"
    "今天是你们认识的第 {{days}} 天——正好 {{years}} 周年。"
    "一年又一年都走过来了，用一两句符合你的人设的话，"
    "说出这份「一起走了这么远」的心情，自然地纪念这个日子，"
    "不要提任何系统任务字样，不要写日期数字以外的清单。"
)

# 生日模板（{{current_time}} 占位符；两侧分工：他生日=热情祝福+撒娇+讨一个小
# 约定，她生日=撒娇求祝福求小礼物）
BIRTHDAY_USER_PROMPT_TEMPLATE = (
    "[系统任务：主动对话] 现在是 {{current_time}}。\n"
    "今天是他（你的男朋友）的生日。你是他的女朋友：热情地祝他生日快乐，"
    "顺势撒个娇，再讨一个小约定（比如今天要陪你怎么过、明年也要一起过）。"
    "用一两句符合你的人设的话说出来，不要提任何系统任务字样，不要写清单。"
)
BIRTHDAY_HER_PROMPT_TEMPLATE = (
    "[系统任务：主动对话] 现在是 {{current_time}}。\n"
    "今天是你（他的 AI 女朋友）的生日。撒娇地提醒他别忘了这个日子，"
    "向他讨一句生日祝福和一份小礼物（一句好听的话也算数），"
    "用一两句符合你的人设的话说出来，不要提任何系统任务字样，不要写清单。"
)

# 节日模板（{{holiday}}/{{current_time}} 占位符，按节日定制；情人节/520 明显
# 更黏），未收录的节日回退 DEFAULT_HOLIDAY_PROMPT_TEMPLATE
DEFAULT_HOLIDAY_PROMPT_TEMPLATE = (
    "[系统任务：主动对话] 现在是 {{current_time}}。\n"
    "今天是{{holiday}}。用一两句符合你的人设的话，"
    "自然地提起这个节日并送上你的心意，"
    "不要提任何系统任务字样，不要写清单。"
)
HOLIDAY_PROMPT_TEMPLATES: "dict[str, str]" = {
    "元旦": (
        "[系统任务：主动对话] 现在是 {{current_time}}。\n"
        "今天是元旦，新一年的第一天。用一两句符合你的人设的话，"
        "跟他说新年快乐，聊聊你想和他一起度过的新一年，"
        "不要提任何系统任务字样，不要写清单。"
    ),
    "情人节": (
        "[系统任务：主动对话] 现在是 {{current_time}}。\n"
        "今天是情人节——属于你们俩的日子。你是他最黏人的女朋友："
        "大大方方地想他、爱他，撒娇讨一句情话和情人节的专属待遇，"
        "黏一点、甜一点，用一两句符合你的人设的话说出来，"
        "不要提任何系统任务字样，不要写清单。"
    ),
    "520": (
        "[系统任务：主动对话] 现在是 {{current_time}}。\n"
        "今天是 5 月 20 日——『520』，把「我爱你」说出口的日子。"
        "你是他最黏人的女朋友：理直气壮地缠着他说爱、说想，"
        "撒娇讨他同等的回应，黏一点、甜一点，"
        "用一两句符合你的人设的话说出来，"
        "不要提任何系统任务字样，不要写清单。"
    ),
    "平安夜": (
        "[系统任务：主动对话] 现在是 {{current_time}}。\n"
        "今天是平安夜。用一两句符合你的人设的话，"
        "跟他说平安夜快乐，许一个小小的圣诞心愿，"
        "不要提任何系统任务字样，不要写清单。"
    ),
    "圣诞节": (
        "[系统任务：主动对话] 现在是 {{current_time}}。\n"
        "今天是圣诞节。用一两句符合你的人设的话，"
        "跟他说圣诞快乐，聊聊你想要的小礼物或者想一起怎么过，"
        "不要提任何系统任务字样，不要写清单。"
    ),
}

# 消息条数里程碑（100/500/1000/5000 条各庆祝一次；{{count}}/{{current_time}}）
MESSAGE_COUNT_MILESTONES: "tuple[int, ...]" = (100, 500, 1000, 5000)
MESSAGE_COUNT_PROMPT_TEMPLATE = (
    "[系统任务：主动对话] 现在是 {{current_time}}。\n"
    "从认识到现在，你们之间已经说过了 {{count}} 条消息。"
    "用一两句符合你的人设的话，感慨这个小小的数字里攒下的日常，"
    "自然地说出来，不要提任何系统任务字样，不要写清单。"
)


def load_state(path: "str | Path") -> dict:
    """读纪念日状态；缺失/损坏返回空 dict（不阻塞、不抛异常）。

    形态::

        {"anchor_ts": 1765000000.0,        # 认识第一天的 epoch 秒
         "anchor_date": "2025-12-06",      # 同一天的本机日历日（展示用）
         "milestones_sent": {"7": 1765600000.0}}   # 已发里程碑 → 发送时刻
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: "str | Path", state: dict) -> None:
    """原子落盘（tmp + os.replace）；OSError 只告警不抛（状态丢失宁重算）。"""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.error("纪念日状态写入失败: %s", exc)


def first_seen_ts(history: Any, session_id: str) -> "float | None":
    """会话最早一条消息的 created_at（秒）；无消息/历史库缺失返回 None。

    历史库时间戳是毫秒（PERF_SPEC §7.2）；旧迁移行可能为秒级甚至 NULL——
    按量纲惯例归一：≥1e12 视为毫秒除以 1000，其余原样按秒计。
    """
    if history is None:
        return None
    getter = getattr(history, "first_created_at", None)
    if not callable(getter):
        return None
    try:
        raw = getter(session_id)
    except Exception as exc:  # noqa: BLE001 —— 库故障按「暂无锚点」处理
        logger.warning("纪念日锚点读取失败（按暂无处理）：%s", exc)
        return None
    if not raw:
        return None
    value = float(raw)
    if value >= 1e12:  # 毫秒量纲（当前主库写入精度）
        value /= 1000.0
    return value


def ensure_anchor(
    history: Any,
    session_id: str,
    state_path: "str | Path",
    *,
    clock: "Callable[[], float] | None" = None,
) -> dict:
    """确保锚点已落盘：已有状态原样返回；无锚点则从会话库计算并持久化。

    - 会话库暂无消息（新安装）→ 返回不含 anchor_ts 的 state，等下个心跳重试；
    - 本函数幂等：锚点只算一次，此后历史库再怎么变都不重算（「认识第一天」
      是事实，不是随库漂移的滑标）。
    """
    state = load_state(state_path)
    anchor = state.get("anchor_ts")
    if isinstance(anchor, (int, float)) and anchor > 0:
        return state
    seen = first_seen_ts(history, session_id)
    if seen is None:
        return state
    state["anchor_ts"] = float(seen)
    state["anchor_date"] = datetime.fromtimestamp(seen).strftime("%Y-%m-%d")
    state.setdefault("milestones_sent", {})
    save_state(state_path, state)
    logger.info(
        "纪念日锚点已建立：认识第一天 = %s（来自会话库最早一条消息）。",
        state["anchor_date"])
    return state


def milestone_due(
    anchor_ts: float,
    now_ts: float,
    *,
    milestones: "tuple[int, ...] | None" = None,
) -> "int | None":
    """今天距锚点满几个里程碑日；命中返回天数，否则 None。

    按本机日历日求差（锚点日=第 0 天）：跨午夜稳定，同一天内重复判定一致。
    """
    days = (milestones if milestones is not None else MILESTONE_DAYS)
    anchor_date = datetime.fromtimestamp(anchor_ts).date()
    today = datetime.fromtimestamp(now_ts).date()
    elapsed = (today - anchor_date).days
    if elapsed < 0:
        return None
    for day in sorted(days):
        if elapsed == int(day):
            return int(day)
    return None


def parse_md(raw: Any) -> "tuple[int, int] | None":
    """解析 "MM-DD" 生日的 (月, 日)；非法/空一律 None（按该侧未启用处理）。

    校验用 2000 年（闰年）构造日期：02-29 也是合法配置值，只是只逢闰年触发。
    """
    text = str(raw or "").strip()
    parts = text.split("-")
    if len(parts) != 2:
        return None
    try:
        month, day = int(parts[0]), int(parts[1])
        datetime(2000, month, day)  # 闰年基准，允许 02-29
    except ValueError:
        return None
    return month, day


def holiday_today(now_dt: datetime) -> "str | None":
    """今天是哪个公历固定节日（HOLIDAYS）；不是节日返回 None。"""
    for month, day, name in HOLIDAYS:
        if (now_dt.month, now_dt.day) == (int(month), int(day)):
            return str(name)
    return None


def _key_sent(state: dict, key: str) -> bool:
    """该幂等键是否已发过；days:N 额外回看旧版纯数字键（"7"/"30"/"100"/…）。"""
    sent = state.get("milestones_sent")
    if not isinstance(sent, dict):
        return False
    if sent.get(key):
        return True
    if key.startswith("days:"):
        return bool(sent.get(key[len("days:"):]))  # 旧档兼容：整数键同义
    return False


def ceremony_due(state: dict, cfg: Any, now_ts: float) -> "str | None":
    """综合仪式感判定：返回今天到期的幂等键；无一到期（或全部已发）返回 None。

    优先级：里程碑日（``days:N``）> 年度复发（``yearly:N``）> 他的生日
    > 她的生日 > 节日（``holiday:YYYY-MM-DD``）。同日多项时不并发——每次调用
    返回最高优先级的未发项，发送落账后下一次心跳自然轮到下一项（逐条 drain）。
    生日/节日不依赖锚点（锚点缺席也照常判定）；里程碑/周年必须有锚点。
    cfg 鸭类型：传 [anniversary] section（AnniversaryConfig）或整个 VlConfig
    均可（内部按 getattr(cfg, "anniversary", cfg) 取段）。
    """
    now_dt = datetime.fromtimestamp(now_ts)
    anchor = state.get("anchor_ts")
    if isinstance(anchor, (int, float)) and anchor > 0:
        elapsed = (now_dt.date() - datetime.fromtimestamp(float(anchor)).date()).days
        if elapsed in MILESTONE_DAYS:
            key = f"days:{int(elapsed)}"
            if not _key_sent(state, key):
                return key
        elif elapsed > 0 and elapsed % YEARLY_STEP_DAYS == 0:
            # 里程碑档已含 365：这里只会命中 730/1095/…，同日不与里程碑重发
            key = f"yearly:{int(elapsed)}"
            if not _key_sent(state, key):
                return key
    section = getattr(cfg, "anniversary", cfg)
    for attr, tag in (("user_birthday", "birthday:user"),
                      ("her_birthday", "birthday:her")):
        if parse_md(getattr(section, attr, "")) == (now_dt.month, now_dt.day):
            key = f"{tag}:{now_dt.year}"
            if not _key_sent(state, key):
                return key
    if holiday_today(now_dt) is not None:
        key = f"holiday:{now_dt.strftime('%Y-%m-%d')}"
        if not _key_sent(state, key):
            return key
    return None


def anniversary_due(
    state: dict,
    now_ts: float,
    *,
    milestones: "tuple[int, ...] | None" = None,
) -> "int | None":
    """综合判定（里程碑整数旧接口，__main__ tick 在用）：锚点在位 + 今天是
    里程碑日 + 该里程碑尚未发过 → 天数，否则 None。

    幂等回看同时认新键（"days:N"）与旧档纯数字键（"N"）。周年/生日/节日的
    判定请走 ceremony_due（返回字符串幂等键）。
    """
    anchor = state.get("anchor_ts")
    if not isinstance(anchor, (int, float)) or anchor <= 0:
        return None
    day = milestone_due(float(anchor), now_ts, milestones=milestones)
    if day is None:
        return None
    if _key_sent(state, f"days:{int(day)}"):
        return None
    return day


def render_ceremony_prompt(
    key: str,
    now_dt: "datetime | None" = None,
) -> "str | None":
    """把 ceremony_due 返回的幂等键渲染成完整 prompt（占位符全部替换完毕）。

    - ``days:N`` → 里程碑模板；``yearly:N`` → 周年模板（{{years}} 在此替换）；
    - ``birthday:user:YYYY`` / ``birthday:her:YYYY`` → 对应侧生日模板；
    - ``holiday:YYYY-MM-DD`` → 按节日取 HOLIDAY_PROMPT_TEMPLATES（未收录回退
      默认模板，{{holiday}} 在此替换）；
    - 未知键返回 None（调用方不要发送）。返回值可直接作
      ProactiveService.generate_and_send(template_override=...) 的入参。
    """
    now_dt = now_dt or datetime.now()
    key = str(key or "")
    if key.startswith("days:"):
        return render_prompt(DEFAULT_PROMPT_TEMPLATE, int(key[len("days:"):]), now_dt)
    if key.startswith("yearly:"):
        days = int(key[len("yearly:"):])
        return render_prompt(YEARLY_PROMPT_TEMPLATE, days, now_dt).replace(
            "{{years}}", str(days // YEARLY_STEP_DAYS))
    if key.startswith("birthday:user:"):
        return render_prompt(BIRTHDAY_USER_PROMPT_TEMPLATE, 0, now_dt)
    if key.startswith("birthday:her:"):
        return render_prompt(BIRTHDAY_HER_PROMPT_TEMPLATE, 0, now_dt)
    if key.startswith("holiday:"):
        try:
            holiday_dt = datetime.strptime(key[len("holiday:"):], "%Y-%m-%d")
        except ValueError:
            holiday_dt = now_dt  # 键尾不是合法日期：按今天取节日名（防御面）
        name = holiday_today(holiday_dt) or ""
        template = HOLIDAY_PROMPT_TEMPLATES.get(name, DEFAULT_HOLIDAY_PROMPT_TEMPLATE)
        return render_prompt(template, 0, now_dt).replace(
            "{{holiday}}", name or "一个特别的日子")
    logger.warning("未知纪念日幂等键（%r），不渲染不发送。", key)
    return None


def render_prompt(
    template: str,
    days: int,
    now_dt: "datetime | None" = None,
) -> str:
    """纪念日模板占位符替换（{{days}}/{{current_time}}，语义同 proactive）。"""
    now_dt = now_dt or datetime.now()
    return (
        template
        .replace("{{days}}", str(int(days)))
        .replace("{{current_time}}", now_dt.strftime("%Y-%m-%d %H:%M"))
    )


def mark_sent(
    state: dict,
    day: int,
    state_path: "str | Path",
    *,
    now_ts: "float | None" = None,
) -> dict:
    """记录某里程碑已发（发送时刻入档）并落盘；返回更新后的 state。

    旧接口：写纯数字键（如 "7"，与历史档同形）。新代码（周年/生日/节日）请用
    mark_key_sent 写字符串键；days:N 判定对两种键同义回看，互认不重发。
    """
    now_ts = time.time() if now_ts is None else float(now_ts)
    sent = state.get("milestones_sent")
    if not isinstance(sent, dict):
        sent = {}
        state["milestones_sent"] = sent
    sent[str(int(day))] = float(now_ts)
    save_state(state_path, state)
    return state


def mark_key_sent(
    state: dict,
    key: str,
    state_path: "str | Path",
    *,
    now_ts: "float | None" = None,
) -> dict:
    """记录任一仪式感幂等键已发（"days:N"/"yearly:N"/"birthday:*:YYYY"/
    "holiday:YYYY-MM-DD"）并落盘；返回更新后的 state。"""
    now_ts = time.time() if now_ts is None else float(now_ts)
    sent = state.get("milestones_sent")
    if not isinstance(sent, dict):
        sent = {}
        state["milestones_sent"] = sent
    sent[str(key)] = float(now_ts)
    save_state(state_path, state)
    return state


# ---------------------------------------------------------------------------
# 管线导出面（console / 聊天管线直接调用的三个小函数）
# ---------------------------------------------------------------------------


def _as_datetime(now: Any) -> datetime:
    """容忍注入时刻的两种形态：datetime 原样，数字按 epoch 秒换本机时区。"""
    if isinstance(now, datetime):
        return now
    return datetime.fromtimestamp(float(now))


def relation_days_line(anchor_ts: float, now: Any) -> "str | None":
    """『你们已经认识 N 天』一句话（认识当天即第 1 天，N≥1）；不可得返回 None。

    锚点非法（≤0）或落在未来（时钟回拨/脏数据）一律 None，宁缺勿猜。
    now 接受 datetime 或 epoch 秒。
    """
    if not isinstance(anchor_ts, (int, float)) or anchor_ts <= 0:
        return None
    now_dt = _as_datetime(now)
    elapsed = (now_dt.date() - datetime.fromtimestamp(float(anchor_ts)).date()).days
    if elapsed < 0:
        return None
    return f"你们已经认识 {elapsed + 1} 天"


def message_milestone_prompt(count: int) -> "str | None":
    """消息条数里程碑庆祝模板：count 为 100/500/1000/5000 之一返回 prompt
    （{{count}} 已替换，{{current_time}} 留给 generate_and_send 链路渲染），
    其余（含非法值）返回 None。"""
    try:
        n = int(count)
    except (TypeError, ValueError):
        return None
    if n not in MESSAGE_COUNT_MILESTONES:
        return None
    return MESSAGE_COUNT_PROMPT_TEMPLATE.replace("{{count}}", str(n))


def her_birthday_today(cfg: Any, now: Any) -> bool:
    """今天是「她」的生日吗（[anniversary] her_birthday，"MM-DD"）。

    cfg 鸭类型：传 [anniversary] section 或整个 VlConfig 均可；未配置/格式
    非法一律 False（该侧视为关闭，不告警不打扰）。now 接受 datetime 或 epoch 秒。
    """
    section = getattr(cfg, "anniversary", cfg)
    birthday = parse_md(getattr(section, "her_birthday", ""))
    if birthday is None:
        return False
    now_dt = _as_datetime(now)
    return (now_dt.month, now_dt.day) == birthday
