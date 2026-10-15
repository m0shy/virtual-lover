# -*- coding: utf-8 -*-
"""soul/anniversary.py 契约测试（R9 功能融合：纪念日仪式感）。

覆盖点（FUSION_SPEC §5-R9-4）：
- 锚点：会话库最早 created_at 首次计算后落 anniversary.json，此后幂等不重算；
  会话库暂无消息/历史库缺失 → 宁缺勿猜（不拿时钟冒充认识第一天）；
- 时间戳量纲：毫秒（当前主库精度）与秒（旧迁移行）都能归一；
- 里程碑：第 7/30/100/365 天按本机日历日差判定（跨午夜稳定），每档只发一次；
- 年度复发：第 730/1095/… 天（每满一周年）到期，幂等键 yearly:N 与里程碑
  days:365 分开落账，同日不重发；
- 生日：[anniversary] user_birthday / her_birthday（"MM-DD"，空=该侧关闭），
  birthday:user:YYYY / birthday:her:YYYY 按年复发，幂等与豁免语义同里程碑；
- 节日：公历固定节日（HOLIDAYS 五个）当天 holiday:YYYY-MM-DD 到期，年年复发，
  幂等落账；农历节日刻意不做；
- 旧档兼容：历史 milestones_sent 纯数字键（"7"/"30"/"100"）被 days:N 判定
  回看，升级不重发；
- 导出面：relation_days_line / message_milestone_prompt / her_birthday_today；
- ProactiveService 接缝：generate_and_send(template_override=...) 用外部
  prompt、豁免暂停守卫，安静时段照旧拒绝；发送语义（dual_send/历史写回）
  与常规主动消息同源；
- 配置面：vl.toml [proactive] anniversary 默认开（配置地基翻转），false 可显式关。

时钟与时区一律注入/固定，不发真实请求、不真等待；session_id 用占位值，
敏感值绝不进测试。
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

# sys.path 引导：virtuallover/ 是 namespace 包，仓库根必须可导入。
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from virtuallover.config import (  # noqa: E402
    AnniversaryConfig,
    ProactiveConfig,
    _build_section,
)
from virtuallover.soul import anniversary as ann  # noqa: E402
from virtuallover.soul.proactive import ProactiveParams, ProactiveService  # noqa: E402

SESSION = "测试人格:FriendMessage:10000"

# 锚点基准：2025-12-06 20:00 本地时区（第 7 天 = 2025-12-13，第 30 天 = 2026-01-05，
# 第 100 天 = 2026-03-16，第 365 天 = 2026-12-06，第 730 天 = 2027-12-06；
# 时刻都避开 23-7 安静时段）
ANCHOR_DT = datetime(2025, 12, 6, 20, 0)
ANCHOR_TS = ANCHOR_DT.timestamp()


def _cfg(user_birthday: str = "", her_birthday: str = "") -> AnniversaryConfig:
    """[anniversary] 配置段构造（生日是个人信息：测试一律占位日期）。"""
    return AnniversaryConfig(user_birthday=user_birthday, her_birthday=her_birthday)


def _state(anchor_ts: float = ANCHOR_TS) -> dict:
    """内存态：锚点 + 空落账表（不上盘的判定测试用）。"""
    return {"anchor_ts": anchor_ts, "milestones_sent": {}}


class FakeHistory:
    """HistoryStore 鸭类型替身：只暴露 first_created_at（可注入返回值/异常）。"""

    def __init__(self, value=None, exc: Exception | None = None):
        self.value = value
        self.exc = exc
        self.calls: list[str] = []

    def first_created_at(self, session_id: str):
        self.calls.append(session_id)
        if self.exc is not None:
            raise self.exc
        return self.value


# ---------------------------------------------------------------------------
# first_seen_ts：量纲归一与降级
# ---------------------------------------------------------------------------

def test_first_seen_ms_scale_normalized():
    """毫秒量纲（≥1e12，当前主库写入精度）归一为秒。"""
    assert ann.first_seen_ts(FakeHistory(ANCHOR_TS * 1000), SESSION) == ANCHOR_TS


def test_first_seen_seconds_scale_kept():
    """秒量纲（旧迁移行 ~1e9）原样返回。"""
    assert ann.first_seen_ts(FakeHistory(ANCHOR_TS), SESSION) == ANCHOR_TS


def test_first_seen_missing_history_returns_none():
    """历史库缺失 / 无消息 / 无 first_created_at / 查询异常 → 一律 None。"""
    assert ann.first_seen_ts(None, SESSION) is None
    assert ann.first_seen_ts(FakeHistory(None), SESSION) is None
    assert ann.first_seen_ts(FakeHistory(0), SESSION) is None
    assert ann.first_seen_ts(SimpleNamespace(), SESSION) is None
    assert ann.first_seen_ts(FakeHistory(exc=RuntimeError("库坏了")), SESSION) is None


# ---------------------------------------------------------------------------
# ensure_anchor：首次计算落盘 + 幂等
# ---------------------------------------------------------------------------

def test_ensure_anchor_computes_once_and_persists(tmp_path):
    """首次计算锚点并落盘；此后历史库再变也不重算（认识第一天是事实）。"""
    state_path = tmp_path / "anniversary.json"
    history = FakeHistory(ANCHOR_TS * 1000)
    state = ann.ensure_anchor(history, SESSION, state_path)
    assert state["anchor_ts"] == ANCHOR_TS
    assert state["anchor_date"] == ANCHOR_DT.strftime("%Y-%m-%d")
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk["anchor_ts"] == ANCHOR_TS
    # 换一个「更新的最早消息」再算：锚点不漂移
    history2 = FakeHistory(datetime(2026, 6, 1, 9, 0).timestamp() * 1000)
    state2 = ann.ensure_anchor(history2, SESSION, state_path)
    assert state2["anchor_ts"] == ANCHOR_TS
    assert history2.calls == []  # 幂等：锚点在位时根本不查库


def test_ensure_anchor_waits_when_history_empty(tmp_path):
    """新安装会话库暂无消息：不落盘不冒充，返回无锚点 state。"""
    state_path = tmp_path / "anniversary.json"
    state = ann.ensure_anchor(FakeHistory(None), SESSION, state_path)
    assert "anchor_ts" not in state
    assert not state_path.exists()


def test_ensure_anchor_tolerant_when_state_corrupt(tmp_path):
    """状态文件损坏（非法 JSON）：视为无锚点，重新计算。"""
    state_path = tmp_path / "anniversary.json"
    state_path.write_text("{broken", encoding="utf-8")
    state = ann.ensure_anchor(FakeHistory(ANCHOR_TS * 1000), SESSION, state_path)
    assert state["anchor_ts"] == ANCHOR_TS


# ---------------------------------------------------------------------------
# 里程碑判定：日历日差 + 幂等落账
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("offset_day,expected", [
    (0, None), (1, None), (6, None), (7, 7), (8, None),
    (29, None), (30, 30), (99, None), (100, 100), (101, None),
])
def test_milestone_due_calendar_days(offset_day, expected):
    """第 7/30/100 天各命中一次，其余日子安静；日历日差（非 24h 周期）。"""
    now_ts = (ANCHOR_DT + timedelta(days=offset_day)).timestamp()
    assert ann.milestone_due(ANCHOR_TS, now_ts) == expected


def test_milestone_due_future_anchor_is_quiet():
    """锚点在未来（防御面）：负天数一律 None。"""
    future = ANCHOR_TS + 10 * 86400
    assert ann.milestone_due(future, ANCHOR_TS) is None


def test_anniversary_due_idempotent_after_mark_sent(tmp_path):
    """同一天重复判定：发送落账前 due，落账后安静（重启/重复心跳不重发）。"""
    state_path = tmp_path / "anniversary.json"
    state = ann.ensure_anchor(FakeHistory(ANCHOR_TS * 1000), SESSION, state_path)
    day7_ts = (ANCHOR_DT + timedelta(days=7)).timestamp()
    assert ann.anniversary_due(state, day7_ts) == 7
    ann.mark_sent(state, 7, state_path, now_ts=day7_ts)
    assert ann.anniversary_due(state, day7_ts + 60) is None
    # 第 8 天回看第 7 档：仍安静（按日命中，不按区间）
    assert ann.anniversary_due(state, day7_ts + 86400) is None
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk["milestones_sent"]["7"] == day7_ts


def test_anniversary_due_without_anchor_is_none():
    assert ann.anniversary_due({}, ANCHOR_TS) is None
    assert ann.anniversary_due({"anchor_ts": 0}, ANCHOR_TS) is None


def test_render_prompt_placeholders():
    out = ann.render_prompt(ann.DEFAULT_PROMPT_TEMPLATE, 30,
                            datetime(2026, 1, 5, 21, 30))
    assert "第 30 天" in out
    assert "2026-01-05 21:30" in out
    assert "{{days}}" not in out and "{{current_time}}" not in out


# ---------------------------------------------------------------------------
# ProactiveService 接缝：template_override 直驱
# ---------------------------------------------------------------------------

class Recorder:
    """主动消息假件容器（test_proactive 同款思路的本地轻量版）。"""

    def __init__(self):
        self.prompts: list[str] = []
        self.texts: list[str] = []
        self.history: list[tuple] = []

    async def llm_chat(self, prompt: str):
        self.prompts.append(prompt)
        return "今天是我们认识的第 30 天，想你了。"

    async def dual_send(self, text: str) -> bool:
        self.texts.append(text)
        return True

    async def append_history(self, role: str, content: str, meta=None):
        self.history.append((role, content, meta))


class _FixedRng:
    """确定 rng：uniform 恒返回下界（调度可预期）。"""

    def uniform(self, a: float, b: float) -> float:
        return a

    def randint(self, a: int, b: int) -> int:
        return a


def _make_service(recorder: Recorder, state_path: Path) -> ProactiveService:
    params = ProactiveParams(
        prompt_template="常规模板 {{current_time}}",
        tts_enabled=True,
        quiet_hours="23-7",
    )
    return ProactiveService(
        params,
        session_id=SESSION,
        state_path=state_path,
        llm_chat=recorder.llm_chat,
        dual_send=recorder.dual_send,
        append_history=recorder.append_history,
        rng=_FixedRng(),
    )


def test_generate_and_send_uses_template_override(tmp_path):
    """template_override 直用外部 prompt（{{days}} 已替换的纪念日语义）。"""
    rec = Recorder()
    svc = _make_service(rec, tmp_path / "proactive_state.json")
    fire_dt = datetime(2026, 1, 5, 21, 30)
    prompt = ann.render_prompt(ann.DEFAULT_PROMPT_TEMPLATE, 30, fire_dt)
    sent = asyncio.run(svc.generate_and_send(
        now_ts=fire_dt.timestamp(), template_override=prompt))
    assert sent == "今天是我们认识的第 30 天，想你了。"
    assert len(rec.prompts) == 1
    assert "第 30 天" in rec.prompts[0]
    assert "常规模板" not in rec.prompts[0]          # 常规模板未混入
    assert rec.texts == ["今天是我们认识的第 30 天，想你了。"]  # dual_send 语义照旧
    assert rec.history and rec.history[0][0] == "assistant"
    assert rec.history[0][2] == {"source": "proactive"}  # 历史写回 meta 照旧


def test_template_override_bypasses_pause_guard(tmp_path):
    """降频暂停（unanswered 满 3）不该让纪念日缺席：template_override 豁免守卫。"""
    rec = Recorder()
    svc = _make_service(rec, tmp_path / "proactive_state.json")
    svc.state.unanswered = 3  # 达到 max_unanswered=3 → paused
    fire_dt = datetime(2025, 12, 13, 20, 0)
    prompt = ann.render_prompt(ann.DEFAULT_PROMPT_TEMPLATE, 7, fire_dt)
    sent = asyncio.run(svc.generate_and_send(
        now_ts=fire_dt.timestamp(), template_override=prompt))
    assert sent is not None
    assert len(rec.prompts) == 1


def test_template_override_still_respects_quiet_hours(tmp_path):
    """安静时段内纪念日同样闭嘴（凌晨 2 点判定）：不生成、不发送。"""
    rec = Recorder()
    svc = _make_service(rec, tmp_path / "proactive_state.json")
    quiet_ts = datetime(2026, 1, 6, 2, 0).timestamp()
    sent = asyncio.run(svc.generate_and_send(
        now_ts=quiet_ts, template_override="第 30 天纪念日消息"))
    assert sent is None
    assert rec.prompts == [] and rec.texts == []


def test_no_override_uses_regular_template(tmp_path):
    """不传 override 时行为不变：暂停守卫生效（常规路径回归锚）。"""
    rec = Recorder()
    svc = _make_service(rec, tmp_path / "proactive_state.json")
    svc.state.unanswered = 3
    sent = asyncio.run(svc.generate_and_send(
        now_ts=datetime(2026, 1, 5, 21, 30).timestamp()))
    assert sent is None                       # 暂停守卫拦下
    assert rec.prompts == []


# ---------------------------------------------------------------------------
# 配置面：vl.toml [proactive] anniversary（默认开，配置地基翻转）
# ---------------------------------------------------------------------------

def test_proactive_anniversary_default_on():
    assert ProactiveConfig().anniversary is True


def test_proactive_anniversary_loads_from_toml_table():
    section = _build_section(ProactiveConfig, {"anniversary": True}, "proactive")
    assert section.anniversary is True


def test_anniversary_birthdays_default_empty():
    """[anniversary] 生日默认空（两侧关闭）；占位日期可从 vl.toml 段载入。"""
    section = AnniversaryConfig()
    assert section.user_birthday == "" and section.her_birthday == ""
    loaded = _build_section(
        AnniversaryConfig, {"user_birthday": "01-01", "her_birthday": "12-31"},
        "anniversary")
    assert loaded.user_birthday == "01-01" and loaded.her_birthday == "12-31"


# ---------------------------------------------------------------------------
# 第 365 天里程碑 + 年度复发（yearly:N）
# ---------------------------------------------------------------------------

def test_milestone_day_365_joins_milestone_tier():
    """第 365 天进里程碑档：milestone_due 命中，幂等键 days:365。"""
    day365_ts = (ANCHOR_DT + timedelta(days=365)).timestamp()
    assert ann.milestone_due(ANCHOR_TS, day365_ts) == 365
    assert ann.ceremony_due(_state(), _cfg(), day365_ts) == "days:365"


def test_yearly_recurrence_fires_beyond_milestones():
    """第 730 天（两周年）年度复发：键 yearly:730，与里程碑档分开。"""
    day730_ts = (ANCHOR_DT + timedelta(days=730)).timestamp()
    assert ann.milestone_due(ANCHOR_TS, day730_ts) is None  # 不在里程碑档
    assert ann.ceremony_due(_state(), _cfg(), day730_ts) == "yearly:730"


def test_day_365_is_milestone_not_yearly(tmp_path):
    """365 = 里程碑档（days:365）；落账后同日不再给 yearly:365（一次一句）。"""
    state_path = tmp_path / "anniversary.json"
    state = _state()
    day365_ts = (ANCHOR_DT + timedelta(days=365)).timestamp()
    assert ann.ceremony_due(state, _cfg(), day365_ts) == "days:365"
    ann.mark_key_sent(state, "days:365", state_path, now_ts=day365_ts)
    assert ann.ceremony_due(state, _cfg(), day365_ts) is None  # 无 yearly:365 双发


def test_yearly_idempotent_and_next_anniversary(tmp_path):
    """yearly:730 落账后当天安静；再满一年轮到 yearly:1095（年年复发）。"""
    state_path = tmp_path / "anniversary.json"
    state = _state()
    day730_ts = (ANCHOR_DT + timedelta(days=730)).timestamp()
    ann.mark_key_sent(state, "yearly:730", state_path, now_ts=day730_ts)
    assert ann.ceremony_due(state, _cfg(), day730_ts + 60) is None
    day1095_ts = (ANCHOR_DT + timedelta(days=1095)).timestamp()
    assert ann.ceremony_due(state, _cfg(), day1095_ts) == "yearly:1095"
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk["milestones_sent"]["yearly:730"] == day730_ts


def test_non_anniversary_day_is_quiet():
    """非里程碑/非整周年的日子安静（400 天：400 % 365 != 0）。"""
    day400_ts = (ANCHOR_DT + timedelta(days=400)).timestamp()
    assert ann.ceremony_due(_state(), _cfg(), day400_ts) is None


def test_render_yearly_prompt():
    """周年模板渲染：{{days}}/{{years}}/{{current_time}} 全部替换完毕。"""
    out = ann.render_ceremony_prompt("yearly:730", datetime(2027, 12, 6, 20, 0))
    assert "第 730 天" in out and "2 周年" in out
    assert "{{" not in out


# ---------------------------------------------------------------------------
# 生日（birthday:user:YYYY / birthday:her:YYYY）
# ---------------------------------------------------------------------------

def test_user_birthday_fires_once_per_year(tmp_path):
    """他的生日：当天 birthday:user:YYYY 到期，落账后安静，次年新键复发。"""
    state_path = tmp_path / "anniversary.json"
    state = _state()
    cfg = _cfg(user_birthday="03-14")
    bday_dt = datetime(2026, 3, 14, 20, 0)
    key = ann.ceremony_due(state, cfg, bday_dt.timestamp())
    assert key == "birthday:user:2026"
    prompt = ann.render_ceremony_prompt(key, bday_dt)
    assert "生日" in prompt and "{{" not in prompt  # 祝福+撒娇+讨约定模板渲染完毕
    ann.mark_key_sent(state, key, state_path, now_ts=bday_dt.timestamp())
    assert ann.ceremony_due(state, cfg, bday_dt.timestamp() + 60) is None
    next_year_ts = datetime(2027, 3, 14, 20, 0).timestamp()
    assert ann.ceremony_due(state, cfg, next_year_ts) == "birthday:user:2027"


def test_her_birthday_path():
    """她的生日：birthday:her:YYYY 到期，模板是撒娇求祝福求小礼物。"""
    state = _state()
    cfg = _cfg(her_birthday="07-07")
    bday_dt = datetime(2026, 7, 7, 20, 0)
    key = ann.ceremony_due(state, cfg, bday_dt.timestamp())
    assert key == "birthday:her:2026"
    prompt = ann.render_ceremony_prompt(key, bday_dt)
    assert "生日" in prompt and "{{" not in prompt
    assert ann.her_birthday_today(cfg, bday_dt) is True
    assert ann.her_birthday_today(cfg, datetime(2026, 7, 8)) is False


def test_birthday_disabled_when_unconfigured_or_invalid():
    """两侧都未配置（默认）=关闭；非法格式（13-45/abc）按未配置处理。"""
    bday_ts = datetime(2026, 3, 14, 20, 0).timestamp()
    assert ann.ceremony_due(_state(), _cfg(), bday_ts) is None
    assert ann.ceremony_due(_state(), _cfg(user_birthday="13-45"), bday_ts) is None
    assert ann.ceremony_due(_state(), _cfg(her_birthday="abc"), bday_ts) is None
    assert ann.her_birthday_today(_cfg(), datetime(2026, 7, 7)) is False
    assert ann.her_birthday_today(_cfg(her_birthday="13-45"), datetime(2026, 7, 7)) is False


def test_birthday_and_holiday_need_no_anchor():
    """生日/节日不依赖认识锚点：锚点缺席（新安装）也照常到期。"""
    cfg = _cfg(user_birthday="03-14")
    assert ann.ceremony_due({}, cfg, datetime(2026, 3, 14, 20, 0).timestamp()) \
        == "birthday:user:2026"
    assert ann.ceremony_due({}, _cfg(), datetime(2026, 2, 14, 20, 0).timestamp()) \
        == "holiday:2026-02-14"


def test_ceremony_due_accepts_section_or_full_config():
    """cfg 鸭类型：[anniversary] 段或带 .anniversary 的整体配置对象都收。"""
    bday_ts = datetime(2026, 3, 14, 20, 0).timestamp()
    full = SimpleNamespace(anniversary=_cfg(user_birthday="03-14"))
    assert ann.ceremony_due(_state(), full, bday_ts) == "birthday:user:2026"
    assert ann.her_birthday_today(full, datetime(2026, 7, 7)) is False


# ---------------------------------------------------------------------------
# 节日（holiday:YYYY-MM-DD，公历固定）
# ---------------------------------------------------------------------------

def test_holidays_constant_per_spec():
    """HOLIDAYS 五个公历固定节日（农历节日刻意不在列）。"""
    assert ann.HOLIDAYS == (
        (1, 1, "元旦"), (2, 14, "情人节"), (5, 20, "520"),
        (12, 24, "平安夜"), (12, 25, "圣诞节"),
    )


def test_holiday_fires_idempotent_and_recurs_yearly(tmp_path):
    """情人节当天 holiday:2026-02-14 到期；落账后安静，次年新键复发。"""
    state_path = tmp_path / "anniversary.json"
    state = _state()
    val_dt = datetime(2026, 2, 14, 20, 0)
    key = ann.ceremony_due(state, _cfg(), val_dt.timestamp())
    assert key == "holiday:2026-02-14"
    prompt = ann.render_ceremony_prompt(key, val_dt)
    assert "情人节" in prompt and "{{" not in prompt
    ann.mark_key_sent(state, key, state_path, now_ts=val_dt.timestamp())
    assert ann.ceremony_due(state, _cfg(), val_dt.timestamp() + 60) is None
    next_year_ts = datetime(2027, 2, 14, 20, 0).timestamp()
    assert ann.ceremony_due(state, _cfg(), next_year_ts) == "holiday:2027-02-14"


def test_520_and_christmas_templates_customized():
    """520/圣诞按节日定制模板（520 明显更黏：把「我爱你」说出口的日子）。"""
    out520 = ann.render_ceremony_prompt(
        "holiday:2026-05-20", datetime(2026, 5, 20, 20, 0))
    assert "520" in out520 and "{{" not in out520
    out_xmas = ann.render_ceremony_prompt(
        "holiday:2026-12-25", datetime(2026, 12, 25, 20, 0))
    assert "圣诞" in out_xmas and "{{" not in out_xmas


def test_non_holiday_day_is_quiet():
    """非节日安静（4 月 1 日不在 HOLIDAYS）。"""
    assert ann.ceremony_due(_state(), _cfg(),
                            datetime(2026, 4, 1, 20, 0).timestamp()) is None


def test_same_day_ceremonies_drain_one_per_tick(tmp_path):
    """同日多项不并发：锚点 2025-12-25 → 第 365 天恰逢圣诞节，先里程碑后节日。"""
    state_path = tmp_path / "anniversary.json"
    anchor_dt = datetime(2025, 12, 25, 20, 0)
    state = _state(anchor_ts=anchor_dt.timestamp())
    xmas_ts = datetime(2026, 12, 25, 20, 0).timestamp()
    assert ann.ceremony_due(state, _cfg(), xmas_ts) == "days:365"
    ann.mark_key_sent(state, "days:365", state_path, now_ts=xmas_ts)
    assert ann.ceremony_due(state, _cfg(), xmas_ts) == "holiday:2026-12-25"


# ---------------------------------------------------------------------------
# 导出面：relation_days_line / message_milestone_prompt
# ---------------------------------------------------------------------------

def test_relation_days_line_counts_first_day_as_one():
    """『你们已经认识 N 天』：认识当天即第 1 天（N≥1）；接受 datetime 与 epoch 秒。"""
    assert ann.relation_days_line(ANCHOR_TS, ANCHOR_DT) == "你们已经认识 1 天"
    assert ann.relation_days_line(
        ANCHOR_TS, ANCHOR_DT + timedelta(days=6)) == "你们已经认识 7 天"
    assert ann.relation_days_line(
        ANCHOR_TS, ANCHOR_TS + 6 * 86400) == "你们已经认识 7 天"


def test_relation_days_line_invalid_inputs_are_none():
    """锚点非法（0/负数）或在未来 → 宁缺勿猜返回 None。"""
    assert ann.relation_days_line(0, ANCHOR_DT) is None
    assert ann.relation_days_line(-5, ANCHOR_DT) is None
    assert ann.relation_days_line(ANCHOR_TS + 86400, ANCHOR_DT) is None


def test_message_milestone_prompt_boundaries():
    """100/500/1000/5000 返回庆祝模板（{{current_time}} 留给链路渲染），其余 None。"""
    for n in (100, 500, 1000, 5000):
        out = ann.message_milestone_prompt(n)
        assert out is not None and str(n) in out
        assert "{{count}}" not in out and "{{current_time}}" in out
    for n in (0, 1, 7, 30, 99, 101, 499, 999, 4999, 5001, -100, None, "x"):
        assert ann.message_milestone_prompt(n) is None


# ---------------------------------------------------------------------------
# 旧 milestones_sent 兼容（历史档纯数字键）
# ---------------------------------------------------------------------------

def test_legacy_numeric_keys_still_respected(tmp_path):
    """旧档 {"7": ts} 纯数字键：days:7 判定回看旧键，升级不重发；新键同义互认。"""
    state_path = tmp_path / "anniversary.json"
    day7_ts = (ANCHOR_DT + timedelta(days=7)).timestamp()
    legacy = {"anchor_ts": ANCHOR_TS, "milestones_sent": {"7": day7_ts}}
    assert ann.ceremony_due(legacy, _cfg(), day7_ts) is None
    assert ann.anniversary_due(legacy, day7_ts) is None
    modern = {"anchor_ts": ANCHOR_TS, "milestones_sent": {"days:7": day7_ts}}
    assert ann.ceremony_due(modern, _cfg(), day7_ts) is None
    assert ann.anniversary_due(modern, day7_ts) is None
    fresh = _state()
    assert ann.anniversary_due(fresh, day7_ts) == 7
    ann.mark_key_sent(fresh, "days:7", state_path, now_ts=day7_ts)
    assert ann.anniversary_due(fresh, day7_ts) is None  # 新键也被旧接口认账


def test_mark_sent_legacy_writer_and_mark_key_sent_roundtrip(tmp_path):
    """mark_sent（旧整数写法，__main__ tick 在用）与 mark_key_sent 落账都防重发。"""
    state_path = tmp_path / "anniversary.json"
    state = _state()
    day365_ts = (ANCHOR_DT + timedelta(days=365)).timestamp()
    assert ann.anniversary_due(state, day365_ts) == 365
    ann.mark_sent(state, 365, state_path, now_ts=day365_ts)
    assert ann.anniversary_due(state, day365_ts) is None
    # 旧整数键 "365" 落账后，365 当天不再给任何键
    # （elif 语义：里程碑档命中时周年档不参与，同日一次一句）
    assert ann.ceremony_due(state, _cfg(), day365_ts) is None
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk["milestones_sent"]["365"] == day365_ts
