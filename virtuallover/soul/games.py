# -*- coding: utf-8 -*-
"""soul/games.py —— 本地小游戏三类（不经 LLM 的本地判定，独立小模块）。

三类游戏（判定全部本地完成，回复用固定模板 + 轻微变体，口吻可爱）：

- **guess 猜数字**：开局随机 1～100（注入 rng）；从用户消息提取数字判定
  高了/低了/对了，对了记分庆祝、本局结束；
- **idiom 成语接龙**：内置 :data:`IDIOMS` 60 个常用成语；她出题 → 用户接
  （四字且首字等于她尾字即接上）→ 她再从词库找首字等于用户尾字的成语，
  找不到就认输卖萌并重新出题；用户接错首字她得一分；
- **truth 真心话**：内置 :data:`TRUTH_QUESTIONS` 30 个题目常量轮抽，
  只出题不判答案（用户的回答交给正常聊天，说「下一题」换题）。

状态 ``<state_dir>/game_state.json``（默认 local/vl_data/ 下，与
proactive_state.json 同目录）记当前游戏 / 双方比分 / 题面 / 答案，
**跨天可续**（无过期）；``/play stop`` 或对话里说「不玩了」随时收摊。

接缝：管线侧对每条非命令消息调 :func:`handle_game_message`——属于当前
游戏的输入返回本地判定文案，否则返回 None 交给正常聊天（绝不吞话）。
依赖注入：状态目录 / rng / 时钟全部由调用方传入（测试 tmp_path + 固定
rng + 固定 now），本模块只依赖标准库，不发任何请求。状态读写沿用
soul/anniversary.py 的容错模式（损坏视为无状态）与原子落盘。
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from pathlib import Path

logger = logging.getLogger("virtuallover.soul.games")

# 状态落盘目录（相对运行根，与 proactive_state.json 同目录）；
# 实际文件由调用方传 state_dir，本模块在其下固定文件名落盘。
DEFAULT_STATE_DIR = "local/vl_data"
STATE_FILENAME = "game_state.json"

# 支持的游戏类型（/play 子命令与状态 current 字段共用）
GAME_KINDS: "tuple[str, ...]" = ("guess", "idiom", "truth")

# guess 猜数字参数
GUESS_MIN, GUESS_MAX = 1, 100

# ---------------------------------------------------------------------------
# 题库常量
# ---------------------------------------------------------------------------

# 成语接龙词库：60 个常用成语（链式衔接友好：一心一意→意气风发→发扬光大→…）
IDIOMS: "tuple[str, ...]" = (
    "一心一意", "意气风发", "发扬光大", "大快人心", "心想事成", "成千上万",
    "万无一失", "失而复得", "得心应手", "手到擒来", "来日方长", "长年累月",
    "月明千里", "里应外合", "合情合理", "理直气壮", "壮志凌云", "云淡风轻",
    "轻而易举", "举世闻名", "名不虚传", "传为佳话", "花好月圆", "满面春风",
    "春暖花开", "开门见山", "山清水秀", "秀外慧中", "中流砥柱", "画蛇添足",
    "足智多谋", "谋事在人", "人山人海", "海阔天空", "空前绝后", "后来居上",
    "上善若水", "水到渠成", "成竹在胸", "胸有成竹", "金石为开", "开诚布公",
    "公私分明", "明察秋毫", "毫不留情", "情深似海", "海誓山盟", "平步青云",
    "云开见日", "日新月异", "异想天开", "开天辟地", "地久天长", "长风破浪",
    "浪子回头", "头头是道", "道听途说", "说一不二", "二话不说", "一诺千金",
)

# 真心话题库：30 个题目常量（只出题不判答案）
TRUTH_QUESTIONS: "tuple[str, ...]" = (
    "你第一眼注意到我的是什么呀？",
    "如果只能带一样东西去无人岛，你带什么？",
    "你做过最幼稚的一件事是什么？",
    "你最近一次偷偷开心是因为什么？",
    "你手机里最舍不得删的照片是哪一张？",
    "你最喜欢我怎么称呼你？",
    "小时候最怕的是什么呀？",
    "你觉得我们俩像哪两种小动物的组合？",
    "你有没有为一个人生闷气很久？说说是为了什么～",
    "你最想改掉自己的哪个小习惯？",
    "如果明天不用上班上课，你最想干嘛？",
    "你最近做的梦还记得吗？梦到谁了？",
    "你会因为我吃醋吗？说一次印象最深的～",
    "你觉得自己最可爱的地方是哪里？",
    "你想和我穿情侣装吗？想要什么颜色的？",
    "你的手机壁纸背后有什么故事？",
    "你最想和我一起去哪个城市？",
    "你被夸过最离谱的一句话是什么？",
    "你现在最想吃的一样东西是什么？",
    "你遇到过最让人心动的一句情话是什么？",
    "你深夜 emo 的时候一般会做什么？",
    "你觉得我们俩谁更黏人一点？",
    "你给我起过外号吗？是什么呀？",
    "如果中了一百万，你会怎么花？",
    "你最近学会的一个新技能是什么？",
    "你希望我们下次约会去哪里呀？",
    "你自己的优点里，你最喜欢哪一个？",
    "你有没有偷偷模仿过我的口头禅？",
    "你最害怕失去的东西是什么？",
    "如果能拥有一个超能力，你选什么？",
)

# ---------------------------------------------------------------------------
# 回复模板（固定模板 + 轻微变体；rng.choice 抽取，口吻可爱）
# ---------------------------------------------------------------------------

_GUESS_HIGH_TEMPLATES = (
    "嗯…{n} 有点高了哦，往小一点猜～",
    "哎呀，{n} 大啦！再小一点～",
    "{n}？高了高了，往下猜猜看～",
)
_GUESS_LOW_TEMPLATES = (
    "嗯…{n} 有点低了哦，往大一点猜～",
    "哎呀，{n} 小啦！再大一点～",
    "{n}？低了低了，往上猜猜看～",
)
_GUESS_HIT_TEMPLATES = (
    "哇！！就是 {n}！你猜对啦～一共猜了 {count} 次，这局你赢！🎉",
    "对啦对啦，就是 {n}！猜了 {count} 次就中了，好厉害～记你 1 分！",
)
_GUESS_RANGE_TEXT = (
    f"要在 {GUESS_MIN}～{GUESS_MAX} 之间猜哦，超出范围的不算啦～"
)
_IDIOM_WRONG_HEAD_TEMPLATES = (
    "「{w}」可不是以「{head}」开头的哦～要以「{head}」开头才算接上，这分我先收下咯（偷笑）",
    "唔…「{w}」没接上啦，要以「{head}」开头才行的～我记 1 分，再给你一次机会！",
)
_IDIOM_CONNECTED_TEMPLATES = (
    "「{w}」接得好！我接：「{hers}」，到你啦，要以「{hers_last}」开头哦～",
    "哇，接上了！那我接「{hers}」，该你啦，以「{hers_last}」开头接一个～",
)
_IDIOM_GIVE_UP_TEMPLATES = (
    "「{w}」…呜，我接不上了，这局你赢啦！我重新出：「{new}」，"
    "请你以「{head}」开头接哦～",
    "「{w}」把我难住啦，认输认输～换一题：我出「{new}」，"
    "以「{head}」开头接一个吧！",
)
_TRUTH_NEXT_TEMPLATES = (
    "好，换一个！{q}",
    "再来一题～{q}",
)
_STOP_FAREWELL_TEMPLATES = (
    "好呀，那游戏先收起来啦，想玩随时喊我～",
    "收摊收摊～今天先玩到这里，下次再找我玩哦！",
)

# 结束当前游戏的对白触发（handle 内置，用户不必非用 /play stop）
STOP_RE = re.compile(r"不玩|不猜|不接|不问|结束|退出|停止|算了|stop", re.IGNORECASE)
# 真心话「换一题」触发
NEXT_RE = re.compile(r"下一题|换一题|换一个|再来一题|换题|next", re.IGNORECASE)
# 成语候选清洗：只保留汉字（用户可能带标点/引号发来）
_CJK_KEEP_RE = re.compile(r"[^\u4e00-\u9fff]+")
_NUM_RE = re.compile(r"\d+")


# ---------------------------------------------------------------------------
# 状态读写（anniversary.py 同款容错与原子落盘）
# ---------------------------------------------------------------------------


def state_path(state_dir: "str | Path") -> Path:
    """状态文件路径：``<state_dir>/game_state.json``。"""
    return Path(state_dir) / STATE_FILENAME


def load_state(state_dir: "str | Path") -> dict:
    """读游戏状态；缺失/损坏返回空 dict（不阻塞、不抛异常）。

    形态::

        {"current": "guess",              # guess|idiom|truth；无游戏时缺省
         "target": 42,                    # guess：答案数字
         "guess_count": 3,                # guess：已猜次数
         "idiom_word": "一心一意",         # idiom：当前桌上待接的词
         "used": ["一心一意"],             # idiom：本轮已用词（防自链自）
         "question": "……",                # truth：当前题目
         "scores": {"user": 1, "her": 0}, # 双方比分（跨局累计）
         "last_active_ts": 1765000000.0}  # 最近一次游戏交互时刻（观测面）
    """
    try:
        raw = state_path(state_dir).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state_dir: "str | Path", state: dict) -> None:
    """原子落盘（tmp + os.replace）；OSError 只告警不抛（状态丢失宁重开）。"""
    path = state_path(state_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.error("游戏状态写入失败: %s", exc)


def _score_line(scores: dict) -> str:
    """比分播报行；双方都为零时不播（避免开局空播报）。"""
    user = int(scores.get("user") or 0)
    her = int(scores.get("her") or 0)
    if user == 0 and her == 0:
        return ""
    return f"（目前比分 你 {user} : 我 {her}）"


# ---------------------------------------------------------------------------
# 命令入口：菜单 / 开局 / 收摊
# ---------------------------------------------------------------------------


def game_menu() -> str:
    """/play 菜单文案。"""
    return (
        "🎮 和我玩点什么？\n"
        "▪ /play guess —— 猜数字（1～100，我说高了低了）\n"
        "▪ /play idiom —— 成语接龙（四字接四字，接不上算我输）\n"
        "▪ /play truth —— 真心话（我出题你来答）\n"
        "▪ /play stop —— 结束当前游戏"
    )


def start_game(
    kind: str,
    state_dir: "str | Path",
    *,
    rng: "random.Random | None" = None,
    now: "float | None" = None,
) -> "list[str]":
    """开局一个游戏并落盘状态；返回开场消息列表。

    - 比分跨局保留（换游戏不清比分）；进行中的旧游戏被直接替换；
    - kind 非法（防御面，commands 层已保证）回退返回菜单。
    """
    kind = str(kind or "").strip().lower()
    if kind not in GAME_KINDS:
        return [game_menu()]
    rng = rng if rng is not None else random.Random()
    now_ts = time.time() if now is None else float(now)

    old = load_state(state_dir)
    scores = old.get("scores") if isinstance(old.get("scores"), dict) else {}
    scores = {"user": int(scores.get("user") or 0), "her": int(scores.get("her") or 0)}

    state: dict = {"current": kind, "scores": scores, "last_active_ts": now_ts}
    if kind == "guess":
        state["target"] = int(rng.randint(GUESS_MIN, GUESS_MAX))
        state["guess_count"] = 0
        opening = (
            f"来玩猜数字！我心里想了一个 {GUESS_MIN}～{GUESS_MAX} 的数，"
            "你直接发数字来猜，我告诉你高了低了，猜对有奖励哦～"
        )
    elif kind == "idiom":
        word = str(rng.choice(list(IDIOMS)))
        state["idiom_word"] = word
        state["used"] = [word]
        opening = (
            f"成语接龙！我先出：「{word}」，"
            f"请你接一个以「{word[-1]}」开头的四字成语～"
        )
    else:  # truth
        question = str(rng.choice(list(TRUTH_QUESTIONS)))
        state["question"] = question
        opening = (
            f"真心话时间！{question}"
            "（答完想换一题就说「下一题」，不想玩了就 /play stop 哦）"
        )
    save_state(state_dir, state)
    logger.info("[games] 游戏开局：%s", kind)
    return [opening]


def stop_game(
    state_dir: "str | Path",
    *,
    now: "float | None" = None,
    rng: "random.Random | None" = None,
) -> "list[str]":
    """/play stop：收摊并保留比分；无进行中游戏时轻提示。"""
    now_ts = time.time() if now is None else float(now)
    state = load_state(state_dir)
    if not state or not state.get("current"):
        return ["现在没有进行中的游戏哦，发 /play 看看菜单？"]
    scores = state.get("scores") if isinstance(state.get("scores"), dict) else {}
    leftover = {"scores": {
        "user": int(scores.get("user") or 0), "her": int(scores.get("her") or 0),
    }, "last_active_ts": now_ts}
    save_state(state_dir, leftover)
    logger.info("[games] 游戏收摊：%s", state.get("current"))
    line = _score_line(leftover["scores"])
    farewell = (rng.choice(_STOP_FAREWELL_TEMPLATES) if rng is not None
                else _STOP_FAREWELL_TEMPLATES[0])
    return [line + farewell] if line else [farewell]


# ---------------------------------------------------------------------------
# 对话内判定：handle_game_message（管线侧对每条非命令消息调用）
# ---------------------------------------------------------------------------


def handle_game_message(
    text: str,
    *,
    state_dir: "str | Path",
    rng: "random.Random | None" = None,
    now: "float | None" = None,
) -> "str | None":
    """游戏输入判定：属于当前游戏的输入返回本地判定文案，否则 None。

    - 无进行中游戏 → None（交给正常聊天）；
    - 「不玩/结束/stop」类对白 → 收摊并回告别文案；
    - guess：文本含数字才判定（纯聊天不吞）；idiom：清洗后恰为四个汉字
      才判定；truth：只响应换题请求，回答交给正常聊天（只出题不判答案）；
    - rng/now 可注入（测试固定）；任何交互都刷新 last_active_ts。
    """
    rng = rng if rng is not None else random.Random()
    now_ts = time.time() if now is None else float(now)
    text = str(text or "").strip()
    if not text:
        return None
    state = load_state(state_dir)
    kind = state.get("current")
    if kind not in GAME_KINDS:
        return None
    if kind == "idiom" and len(str(state.get("idiom_word") or "")) != 4:
        # 状态残缺（防御面）：收摊落盘，不卡死也绝不误判
        state.pop("current", None)
        save_state(state_dir, state)
        return None

    # 收摊对白（三类游戏通用；/play stop 之外的对话内出口）
    if STOP_RE.search(text):
        return stop_game(state_dir, now=now_ts, rng=rng)[0]

    if kind == "guess":
        reply = _handle_guess(text, state, rng)
    elif kind == "idiom":
        reply = _handle_idiom(text, state, rng)
    else:
        reply = _handle_truth(text, state, rng)
    if reply is None:
        return None
    state["last_active_ts"] = now_ts
    save_state(state_dir, state)
    return reply


def _handle_guess(text: str, state: dict, rng: random.Random) -> "str | None":
    """猜数字判定：从消息提取第一个数字；高了/低了/对了（对了记分收官）。"""
    match = _NUM_RE.search(text)
    if match is None:
        return None  # 没数字：不是游戏输入，交给正常聊天
    target = int(state.get("target") or 0)
    n = int(match.group())
    if not (GUESS_MIN <= n <= GUESS_MAX):
        return _GUESS_RANGE_TEXT
    state["guess_count"] = int(state.get("guess_count") or 0) + 1
    if n == target:
        scores = state.setdefault("scores", {})
        scores["user"] = int(scores.get("user") or 0) + 1
        state.pop("current", None)  # 本局结束（比分保留）
        return rng.choice(_GUESS_HIT_TEMPLATES).format(n=n, count=state["guess_count"])
    if n > target:
        return rng.choice(_GUESS_HIGH_TEMPLATES).format(n=n)
    return rng.choice(_GUESS_LOW_TEMPLATES).format(n=n)


def _handle_idiom(text: str, state: dict, rng: random.Random) -> "str | None":
    """成语接龙判定：清洗后恰为四个汉字才算游戏输入，判首字衔接。"""
    word = str(state.get("idiom_word") or "")
    candidate = _CJK_KEEP_RE.sub("", text)
    if len(candidate) != 4:
        return None  # 非四字输入：交给正常聊天
    scores = state.setdefault("scores", {})
    if candidate[0] != word[-1]:
        scores["her"] = int(scores.get("her") or 0) + 1
        return rng.choice(_IDIOM_WRONG_HEAD_TEMPLATES).format(
            w=candidate, head=word[-1]) + _score_line(scores)
    # 接上了：用户 +1，她找尾字可接的词
    scores["user"] = int(scores.get("user") or 0) + 1
    used = [str(u) for u in (state.get("used") or []) if str(u)]
    used.extend([word, candidate])
    follow = [w for w in IDIOMS if w[0] == candidate[-1] and w not in used]
    if follow:
        hers = str(rng.choice(follow))
        used.append(hers)
        state["idiom_word"] = hers
        state["used"] = used[-20:]  # 防长局无限增长
        return rng.choice(_IDIOM_CONNECTED_TEMPLATES).format(
            w=candidate, hers=hers, hers_last=hers[-1]) + _score_line(scores)
    # 她接不上：认输卖萌，重新出题（链重开）
    fresh = [w for w in IDIOMS if w not in used] or list(IDIOMS)
    new = str(rng.choice(fresh))
    state["idiom_word"] = new
    state["used"] = [new]
    return rng.choice(_IDIOM_GIVE_UP_TEMPLATES).format(
        w=candidate, new=new, head=new[0]) + _score_line(scores)


def _handle_truth(text: str, state: dict, rng: random.Random) -> "str | None":
    """真心话判定：只出题不判答案——只响应换题请求，回答交给正常聊天。"""
    if not NEXT_RE.search(text):
        return None
    current = str(state.get("question") or "")
    fresh = [q for q in TRUTH_QUESTIONS if q != current] or list(TRUTH_QUESTIONS)
    question = str(rng.choice(fresh))
    state["question"] = question
    return rng.choice(_TRUTH_NEXT_TEMPLATES).format(q=question)
