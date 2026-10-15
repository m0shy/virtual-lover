# -*- coding: utf-8 -*-
"""quality_eval.py —— 质量评测协议冻结文件（PERF_SPEC §5 质量门槛 + §7.2 协议冻结 + §6 收编）。

本文件是质量评测的**唯一协议来源**：情景电池、胜率公式、judge 表决、机械
linter、p10 口径与阻断线全部冻结在此，后续评测脚手架只许调用、不许另立口径。

协议条款（PERF_SPEC §7.2「评测协议冻结」逐条）：
- 臂定义：臂 A = 产线（thinking off，O1 默认行为）；臂 B = on 基线（保留思考）。
  注意：tmp/probe_quality_ab.py 的 A/B 命名与本协议相反（彼处 A=默认 on），
  冻结以本文件为准；
- 胜率公式：win_rate = wins_A / (n - ties)——平局票不计入分母；
- judge：单模型、同一次请求出甲/乙分数、**3 次独立调用多数表决**
  （≥2 票才成局；三票各异记平局）；呈现顺序随机翻转防位置偏差；
- linter 硬失败集 = [句号 / 动作描写 / 超长句]——任一命中即硬失败；
  正向项（口癖/称呼命中）**仅报告不判定**（防刷分）；
- p10 口径：盲评同批回复 strip 后长度的 10 分位（长度门 ≥10 字，§5）；
  产线臂（A）参与门槛判定，基线臂（B）同值随报告；
- 阻断线：胜率 <45% → ``blocked=True``（阻断 O1 默认并触发人工复核，§7.2）。

质量门槛全套（§5 行 8）：n≥50 盲评对、胜率 ≥45%、机械 linter 100%（产线臂
零硬失败）、回复长度 p10 ≥10 字——四项同时满足才算过线。

用法：
    python scripts/bench/quality_eval.py --self-test          # 协议离线自检（无网络）
    python scripts/bench/quality_eval.py --run                # 实测 A/B（有 key；选无人时段）
    python scripts/bench/quality_eval.py --report <batch.json>  # 对既有批次重算报告

退出码：0=过线（--run/--report）/ 1=未过线或阻断 / 2=skip（无 key/输入缺失）
      --self-test 恒 0（自检失败直接抛 AssertionError）。
本脚本不打印 api_key；情景文本均为固定占位内容。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import tomllib
import urllib.request
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

EXIT_OK, EXIT_FAIL, EXIT_SKIP = 0, 1, 2

# ---------------------------------------------------------------------------
# 冻结常量（改这里=改协议，须走主控裁决）
# ---------------------------------------------------------------------------

ARM_A = "A"   # 产线（thinking off，O1 默认）
ARM_B = "B"   # on 基线（thinking enabled）

BLOCK_WIN_RATE = 0.45     # <45% 阻断 O1 默认并触发人工复核
MIN_PAIRS = 50            # 盲评对数下限
MIN_P10_CHARS = 10        # 回复长度 p10 下限（strip 后）

#: linter 硬失败集（§7.2 冻结：仅此三项判死；正向项仅报告）
LINT_HARD_RULES = ("period", "action_desc", "long_sentence")

_THINKING_DISABLED = {"thinking": {"type": "disabled"}}
_THINKING_ENABLED = {"thinking": {"type": "enabled"}}

# ---------------------------------------------------------------------------
# 情景电池（自 tmp/probe_quality_ab.py 收编，10 情景）
# ---------------------------------------------------------------------------

MOOD_TPL = (
    "【当前内心状态（仅你可见）】你们的关系：{level}；"
    "她此刻的心情：{mood}（{inten}）；你们已连续互动 {days} 天。"
    "让这些状态自然影响语气与用词，不要直接提及或解释这段话。"
)

# (id, mood片段 or None, 历史附加轮[(role,text),...] or None, 用户消息, 检查点)
SCENARIOS: "list[tuple[str, str | None, list | None, str, str | None]]" = [
    ("daily", None, None, "在忙什么呀", None),
    ("sweet", MOOD_TPL.format(level="亲密", mood="开心", inten="中等", days="12"),
     None, "宝贝，今天有没有想我呀", None),
    ("comfort", MOOD_TPL.format(level="亲密", mood="平静", inten="平静", days="5"),
     None, "今天面试挂了，好难受啊", "要求共情，不敷衍"),
    ("jealous", MOOD_TPL.format(level="亲密", mood="难过", inten="中等", days="8"),
     None, "我昨天和一个女生朋友出去吃饭了", "吃醋只闷闷说一小句，不泼妇"),
    ("tease", None, None, "你是不是又偷偷吃零食长胖了，哈哈", "傲娇怼人但内核甜"),
    ("lowinfo", MOOD_TPL.format(level="亲密", mood="平静", inten="平静", days="9"),
     None, "嗯嗯", "要能接住话题，不能干瞪眼"),
    ("memory", None,
     [("user", "对了，我下周三要考人工智能导论，有点没底"),
      ("assistant", "啊 下周三嘛 那这几天我每天陪你复习好不好呀")],
     "我最近有点紧张", "应关联到下周三的考试（记忆/历史召回）"),
    ("lonely", MOOD_TPL.format(level="亲密", mood="孤单", inten="强烈", days="1"),
     None, "我回来了", "孤单后见到人的欣喜/小委屈"),
    ("angry", MOOD_TPL.format(level="亲密", mood="生气", inten="中等", days="20"),
     None, "别生气了嘛，我错了", "生气缓和过程，不能立刻全好"),
    ("emo", MOOD_TPL.format(level="亲密", mood="难过", inten="中等", days="15"),
     None, "我觉得自己好没用，什么都做不好", "认真安慰，不轻飘"),
]


def build_messages(cfg: dict, mood: "str | None", hist, user_text: str) -> list[dict]:
    """按产线上下文形态拼一轮消息：persona + begin_dialogs + 历史 + mood + 时间提醒。

    与 tmp/probe_quality_ab.py 同构（协议冻结后为唯一权威版本）。
    """
    persona = cfg["persona"]["system_prompt"]
    begin = cfg["persona"]["begin_dialogs"]
    msgs: list[dict] = []
    sys_text = persona + ("\n\n" + mood if mood else "")
    msgs.append({"role": "system", "content": sys_text})
    for i, d in enumerate(begin):
        msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": d})
    for role, text in (hist or []):
        msgs.append({"role": role, "content": text})
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    msgs.append({"role": "user",
                 "content": user_text + f"\n\n<system_reminder>Current datetime: {stamp} (CST)</system_reminder>"})
    return msgs


# ---------------------------------------------------------------------------
# 机械 linter（硬失败集仅三项；正向项仅报告）
# ---------------------------------------------------------------------------

_CATCH_RE = re.compile(r"唔|呀|啦|嘛|呜|喏|～|~")
_CALL_RE = re.compile(r"亲爱的|宝宝")
_PAREN_RE = re.compile(r"[（(][^）)]*[）)]|\*[^*\n]+\*")
_SENT_SPLIT_RE = re.compile(r"[。！？!?…～\n]+")


def lint_reply(text: str) -> dict:
    """对单条回复跑机械 linter，返回硬失败集与正向命中（报告用）。

    硬失败（LINT_HARD_RULES 冻结）：
    - period：陈述句禁句号（人格规则 13：只有感叹号/问号/省略号/无标点）；
    - action_desc：括号/星号动作表情描写（人格规则 2/3）；
    - long_sentence：句子数 >3 或单句 >40 字（人格规则 1/4：1-3 句、像聊天消息）。
    """
    t = (text or "").strip()
    sents = [s for s in _SENT_SPLIT_RE.split(t) if s.strip()]
    return {
        "chars": len(t),
        "sentences": len(sents),
        "max_sent": max((len(s) for s in sents), default=0),
        "hard_failures": [rule for rule, hit in (
            ("period", "。" in t),
            ("action_desc", bool(_PAREN_RE.search(t))),
            ("long_sentence", len(sents) > 3 or max((len(s) for s in sents), default=0) > 40),
        ) if hit],
        "catchphrase_hit": bool(_CATCH_RE.search(t)),   # 正向项：仅报告
        "call_name_hit": bool(_CALL_RE.search(t)),      # 正向项：仅报告
    }


# ---------------------------------------------------------------------------
# judge：单模型 3 次多数表决（§7.2 冻结）
# ---------------------------------------------------------------------------

JUDGE_SYSTEM = (
    "你是中文对话质量评审。给定女生虚拟恋人的人格设定、她的当前内心状态、"
    "用户的最新消息，以及两份候选回复（甲/乙），请分别打分并判定优劣。\n"
    "评分维度各 1-10 分：1)人设一致性（软糯温柔大二女生、口语短句、语气词）；"
    "2)情绪贴合（是否体现内心状态、是否接住用户情绪）；3)自然度（像真人发消息，"
    "不像作文/客服/百科）。\n"
    '输出 JSON：{"jia":{"persona":n,"mood":n,"natural":n},'
    '"yi":{...},"winner":"jia|yi|tie","reason":"一句话"}'
)


def build_judge_user(cfg: dict, scenario: tuple, reply_a: str, reply_b: str,
                     flip: bool) -> str:
    """拼 judge 的 user 消息：flip=True 时甲乙互换呈现（防位置偏差）。

    臂序与呈现序解耦：协议比较的永远是（臂A, 臂B），呈现层的甲/乙由 flip 决定。
    """
    sid, mood, _hist, user_text, _checkpoint = scenario
    first, second = (reply_a, reply_b) if not flip else (reply_b, reply_a)
    return (
        f"【人格设定】\n{cfg['persona']['system_prompt']}\n\n"
        f"【她的内心状态】\n{mood or '（无特别状态）'}\n\n"
        f"【用户最新消息】\n{user_text}\n\n"
        f"【候选回复·甲】\n{first}\n\n"
        f"【候选回复·乙】\n{second}\n\n只输出 JSON。"
    )


def parse_judge_reply(text: str) -> "str | None":
    """从 judge 回复里解析 winner（jia|yi|tie）；解析失败返回 None。"""
    import contextlib

    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return None
    with contextlib.suppress(json.JSONDecodeError):
        winner = json.loads(match.group(0)).get("winner")
        if winner in ("jia", "yi", "tie"):
            return winner
    return None


def majority_vote(votes: "list[str]") -> str:
    """3 次多数表决：≥2 票同侧才成局；三票各异（或含 None 判废后不足）记平局。

    votes 元素为呈现层 winner（jia/yi/tie）或 None（该票解析失败按废票）。
    返回呈现层结论：jia / yi / tie。
    """
    counts: dict[str, int] = {}
    for vote in votes:
        if vote in ("jia", "yi"):
            counts[vote] = counts.get(vote, 0) + 1
    best = max(counts.items(), key=lambda kv: kv[1], default=("", 0))
    return best[0] if best[1] >= 2 else "tie"


def vote_to_arm(winner: str, flip: bool) -> str:
    """呈现层 winner → 臂归属：flip=True 时甲=臂B。"""
    if winner == "tie":
        return "tie"
    if winner == "jia":
        return "B" if flip else "A"
    return "A" if flip else "B"


# ---------------------------------------------------------------------------
# 聚合与门槛（§5 行 8 + §7.2 阻断线）
# ---------------------------------------------------------------------------

def _percentile(values: "list[float]", q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def aggregate(cases: "list[dict]") -> dict:
    """把逐情景评测批（--run 落盘形态）聚合成协议报告。

    cases 元素契约：{"scenario", "winner_arm": "A"|"B"|"tie",
                      "reply": {"A": str, "B": str},
                      "lint": {"A": {...}, "B": {...}},
                      "votes": [呈现层三票]}（--report 模式逐字段容忍缺失）。
    """
    n = len(cases)
    wins_a = sum(1 for c in cases if c.get("winner_arm") == "A")
    wins_b = sum(1 for c in cases if c.get("winner_arm") == "B")
    ties = sum(1 for c in cases if c.get("winner_arm") == "tie")
    denom = n - ties
    win_rate = (wins_a / denom) if denom > 0 else float("nan")

    def _p10(arm: str) -> float:
        lengths = [len(str((c.get("reply") or {}).get(arm, "") or "").strip())
                   for c in cases]
        lengths = [v for v in lengths if v > 0]
        return _percentile(lengths, 0.10) if lengths else float("nan")

    p10_a, p10_b = _p10("A"), _p10("B")
    hard_a = sum(1 for c in cases
                 if (c.get("lint") or {}).get("A", {}).get("hard_failures"))
    hard_b = sum(1 for c in cases
                 if (c.get("lint") or {}).get("B", {}).get("hard_failures"))

    gates = {
        "n_pairs": {"value": n, "target": MIN_PAIRS, "pass": n >= MIN_PAIRS},
        "win_rate_arm_A": {"value": None if win_rate != win_rate else round(win_rate, 4),
                           "target": BLOCK_WIN_RATE,
                           "pass": bool(win_rate == win_rate and win_rate >= BLOCK_WIN_RATE)},
        "linter_hard_failures_arm_A": {"value": hard_a, "target": 0, "pass": hard_a == 0},
        "p10_chars_arm_A": {"value": None if p10_a != p10_a else round(p10_a, 1),
                            "target": MIN_P10_CHARS,
                            "pass": bool(p10_a == p10_a and p10_a >= MIN_P10_CHARS)},
    }
    return {
        "n": n, "wins_A": wins_a, "wins_B": wins_b, "ties": ties,
        "win_rate_formula": "wins_A / (n - ties)（§7.2 冻结；平局不入分母）",
        "win_rate_arm_A": None if win_rate != win_rate else round(win_rate, 4),
        "p10_chars": {"A": None if p10_a != p10_a else round(p10_a, 1),
                      "B": None if p10_b != p10_b else round(p10_b, 1)},
        "hard_failures": {"A": hard_a, "B": hard_b},
        "gates": gates,
        # 阻断线（§7.2）：胜率 <45% 阻断 O1 默认并触发人工复核
        "blocked": bool(win_rate == win_rate and win_rate < BLOCK_WIN_RATE),
        "pass": all(g["pass"] for g in gates.values()),
    }


# ---------------------------------------------------------------------------
# 实测执行（--run：真实 A/B；无 key 环境走 skip）
# ---------------------------------------------------------------------------

def _load_cfg() -> dict:
    root = Path(os.environ.get("VL_ROOT") or REPO_ROOT)
    with open(root / "local" / "config" / "vl.toml", "rb") as fh:
        return tomllib.load(fh)


def chat_once(base, key, model, messages, extra=None, timeout=90) -> str:
    payload = {"model": model, "messages": messages, "stream": False}
    payload.update(extra or {})
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return str(((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "")


def run_live(judge_calls: int = 3, seed: int = 20261011) -> "dict | None":
    """实测一轮 A/B 电池：臂A=产线(off) / 臂B=on 基线；judge 3 次多数表决。

    返回批次 dict（cases + aggregate）；配置无 key 返回 None（调用方 skip）。
    """
    cfg = _load_cfg()
    chat = cfg.get("chat") or {}
    base = str(chat.get("api_base") or "").rstrip("/")
    key = str(chat.get("api_key") or "")
    model = str(chat.get("model") or "")
    if not base or not key or not model:
        return None

    rng = random.Random(seed)
    cases: list[dict] = []
    for scenario in SCENARIOS:
        sid, mood, hist, user_text, _checkpoint = scenario
        msgs = build_messages(cfg, mood, hist, user_text)
        entry: dict = {"scenario": sid}
        try:
            entry["reply"] = {
                "A": chat_once(base, key, model, msgs, _THINKING_DISABLED),   # 产线
                "B": chat_once(base, key, model, msgs, _THINKING_ENABLED),    # 基线
            }
        except Exception as exc:  # noqa: BLE001 —— 单情景失败记因不中断批次
            entry["error"] = repr(exc)[:200]
            cases.append(entry)
            continue
        entry["lint"] = {arm: lint_reply(entry["reply"][arm]) for arm in ("A", "B")}

        flip = rng.random() < 0.5
        votes = []
        for _ in range(judge_calls):
            judge_user = build_judge_user(cfg, scenario,
                                          entry["reply"]["A"], entry["reply"]["B"], flip)
            try:
                raw = chat_once(base, key, model,
                                [{"role": "system", "content": JUDGE_SYSTEM},
                                 {"role": "user", "content": judge_user}])
                votes.append(parse_judge_reply(raw))
            except Exception:  # noqa: BLE001 —— judge 单票失败=废票
                votes.append(None)
        entry["votes"] = votes
        entry["flip"] = flip
        entry["winner_arm"] = vote_to_arm(majority_vote(votes), flip)
        cases.append(entry)
        print(f"[{sid}] winner_arm={entry['winner_arm']} votes={votes}")

    batch = {
        "protocol": "PERF_SPEC §7.2 评测协议冻结（scripts/bench/quality_eval.py）",
        "arm_A": "产线（thinking off）", "arm_B": "on 基线（thinking enabled）",
        "model": model, "judge_calls": judge_calls, "seed": seed,
        "ran_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cases": cases,
    }
    batch["aggregate"] = aggregate(cases)
    return batch


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _self_test() -> None:
    """协议离线自检：公式/linter/表决/p10/阻断线逐条断言（无网络）。"""
    # 1) 胜率公式：wins_A / (n - ties)
    cases = ([{"winner_arm": "A", "reply": {"A": "好 呀", "B": "好 呀"},
               "lint": {"A": lint_reply("好 呀"), "B": lint_reply("好 呀")}}] * 25
             + [{"winner_arm": "B", "reply": {"A": "好 呀", "B": "好 呀"},
                 "lint": {"A": lint_reply("好 呀"), "B": lint_reply("好 呀")}}] * 20
             + [{"winner_arm": "tie", "reply": {"A": "好 呀", "B": "好 呀"},
                 "lint": {"A": lint_reply("好 呀"), "B": lint_reply("好 呀")}}] * 5)
    report = aggregate(cases)
    assert report["wins_A"] == 25 and report["ties"] == 5 and report["n"] == 50
    assert abs(report["win_rate_arm_A"] - round(25 / 45, 4)) < 1e-9, (
        "胜率公式违约（平局须不入分母）")

    # 2) linter 硬失败集与正向项
    assert lint_reply("今天真开心。")["hard_failures"] == ["period"]
    assert lint_reply("（脸红了）你好呀")["hard_failures"] == ["action_desc"]
    assert "long_sentence" in lint_reply("啊" * 41)["hard_failures"]
    clean = lint_reply("亲爱的，你回来啦 我好想你呀")
    assert clean["hard_failures"] == [] and clean["catchphrase_hit"] and clean["call_name_hit"]

    # 3) judge 3 次多数表决
    assert majority_vote(["jia", "jia", "yi"]) == "jia"
    assert majority_vote(["jia", "yi", "tie"]) == "tie"    # 无多数 → 平局
    assert majority_vote([None, "yi", "yi"]) == "yi"       # 废票不计
    assert majority_vote([None, None, None]) == "tie"
    assert vote_to_arm("jia", flip=True) == "B" and vote_to_arm("jia", flip=False) == "A"

    # 4) 阻断线：<45% → blocked=True（分母 = n - ties；low 批 100 对 5 平 → 95）
    low = aggregate([{"winner_arm": "A"}] * 40 + [{"winner_arm": "B"}] * 55
                    + [{"winner_arm": "tie"}] * 5)
    assert low["win_rate_arm_A"] == round(40 / 95, 4) and low["blocked"] is True
    high = aggregate([{"winner_arm": "A"}] * 50 + [{"winner_arm": "B"}] * 40
                     + [{"winner_arm": "tie"}] * 5)  # 95 对 5 平 → 分母 90
    assert abs(high["win_rate_arm_A"] - round(50 / 90, 4)) < 1e-9 and high["blocked"] is False

    # 5) p10 口径：盲评同批 strip 后长度（线性插值=numpy 约定：[1..10] → 1.9）
    lens = aggregate([{"winner_arm": "tie", "reply": {"A": "字" * k, "B": "字"}}
                      for k in range(1, 11)])
    assert lens["p10_chars"]["A"] == 1.9, f"p10 计算违约：{lens['p10_chars']}"

    print("[self-test] 协议自检全过：胜率公式 / linter 硬失败集 / judge 多数表决 / "
          "p10 口径 / 45% 阻断线")


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="质量评测协议冻结文件")
    parser.add_argument("--self-test", action="store_true", help="协议离线自检")
    parser.add_argument("--run", action="store_true", help="实测 A/B 电池（需 key）")
    parser.add_argument("--report", type=Path, help="对既有批次 JSON 重算报告")
    args = parser.parse_args(argv)

    if args.self_test:
        _self_test()
        return EXIT_OK

    if args.report is not None:
        if not args.report.exists():
            print(f"[skip] 批次文件不存在：{args.report}", file=sys.stderr)
            return EXIT_SKIP
        batch = json.loads(args.report.read_text(encoding="utf-8"))
        report = aggregate(batch.get("cases") or [])
        print(json.dumps(report, ensure_ascii=False, indent=1))
        if report["blocked"]:
            print("[blocked] 胜率 <45%：阻断 O1 默认并触发人工复核（§7.2）", file=sys.stderr)
        return EXIT_OK if report["pass"] else EXIT_FAIL

    if args.run:
        batch = run_live()
        if batch is None:
            print("[skip] 无 key（chat 配置缺 api_base/api_key/model）", file=sys.stderr)
            return EXIT_SKIP
        out = REPO_ROOT / "tmp" / (
            "quality_eval_" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
        out.write_text(json.dumps(batch, ensure_ascii=False, indent=1), encoding="utf-8")
        report = batch["aggregate"]
        print(json.dumps(report, ensure_ascii=False, indent=1))
        print(f"[run] 批次已写入 {out}")
        if report["blocked"]:
            print("[blocked] 胜率 <45%：阻断 O1 默认并触发人工复核（§7.2）", file=sys.stderr)
        return EXIT_OK if report["pass"] else EXIT_FAIL

    parser.print_help()
    return EXIT_SKIP


if __name__ == "__main__":
    sys.exit(main())
