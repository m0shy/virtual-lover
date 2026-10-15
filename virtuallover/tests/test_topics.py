# -*- coding: utf-8 -*-
"""soul/topics.py 每日一题契约测试（主动陪伴升级：不再重复问过的问题）。

覆盖点（任务书）：
- 同一天同一题：同日多次调用结果一致，状态文件落账可查；
- 每天推进：跨天 pos+1 话题变化；
- 用尽重洗：一轮内全覆盖不重复，重洗后 pos 归零继续覆盖；
- 确定性：同状态同结果（洗牌种子=日期+轮次，进程无关），order 恒为合法排列；
- 损坏自愈：非法 JSON / order 与池不符时重建，不炸不阻塞。

时间注入固定、状态落 tmp_path，零真实 IO/网络依赖。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from virtuallover.soul import topics as topics_mod

DAY1 = datetime(2026, 10, 14, 9, 0)
DAY2 = DAY1 + timedelta(days=1)
DAY3 = DAY1 + timedelta(days=2)


# ---------------------------------------------------------------------------
# 话题池本体
# ---------------------------------------------------------------------------

def test_default_topics_pool_is_usable():
    """内置池 ≥40 条、无空串、无重复（通用内容池的基本质量线）。"""
    pool = topics_mod.DEFAULT_TOPICS
    assert len(pool) >= 40
    assert all(isinstance(t, str) and t.strip() for t in pool)
    assert len(set(pool)) == len(pool)


# ---------------------------------------------------------------------------
# 同一天同一题 + 每天推进
# ---------------------------------------------------------------------------

def test_same_day_returns_same_topic(tmp_path):
    """同一天多次调用（含跨小时）返回同一题——主动消息一天多条都围绕同一话题。"""
    first = topics_mod.pick_topic(DAY1, tmp_path)
    later = topics_mod.pick_topic(DAY1 + timedelta(hours=5), tmp_path)
    assert first == later
    assert first in topics_mod.DEFAULT_TOPICS
    state = json.loads(
        (tmp_path / topics_mod.STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["date"] == DAY1.date().isoformat()
    assert state["pos"] == 0


def test_next_day_advances_one_topic(tmp_path):
    """跨天推进一条：话题变化，pos 落账为 1。"""
    day1 = topics_mod.pick_topic(DAY1, tmp_path)
    day2 = topics_mod.pick_topic(DAY2, tmp_path)
    assert day2 != day1  # 同一排列内不同位置必不同题
    state = json.loads(
        (tmp_path / topics_mod.STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["pos"] == 1
    # 同一天再次调用不推进
    assert topics_mod.pick_topic(DAY2 + timedelta(hours=1), tmp_path) == day2
    state = json.loads(
        (tmp_path / topics_mod.STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["pos"] == 1


# ---------------------------------------------------------------------------
# 用尽重洗（小池注入，可控推演）
# ---------------------------------------------------------------------------

@pytest.fixture()
def small_pool(monkeypatch):
    """把话题池替换为 3 条，便于穷尽一轮。"""
    pool = ["题甲", "题乙", "题丙"]
    monkeypatch.setattr(topics_mod, "DEFAULT_TOPICS", pool)
    return pool


def test_full_cycle_covers_all_then_reshuffles(tmp_path, small_pool):
    """一轮之内每条话题恰好出现一次；用尽后重洗，pos 归零继续全覆盖。"""
    seen = [topics_mod.pick_topic(DAY1 + timedelta(days=i), tmp_path)
            for i in range(3)]
    assert sorted(seen) == sorted(small_pool)      # 一轮全覆盖、无重复
    state = json.loads(
        (tmp_path / topics_mod.STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["pos"] == 2
    # 第 4 天：重洗（pos 归 0，cycle +1），继续按新顺序全覆盖
    seen2 = [topics_mod.pick_topic(DAY1 + timedelta(days=i), tmp_path)
             for i in range(3, 6)]
    assert sorted(seen2) == sorted(small_pool)
    state = json.loads(
        (tmp_path / topics_mod.STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["pos"] == 2 and state["cycle"] == 1
    assert sorted(state["order"]) == [0, 1, 2]     # order 恒为合法排列


def test_deterministic_across_state_dirs(small_pool, tmp_path):
    """确定性：两个独立状态目录（重启语义）同日序列完全一致。"""
    other = tmp_path / "other"
    for i in range(3):
        day = DAY1 + timedelta(days=i)
        assert (topics_mod.pick_topic(day, tmp_path)
                == topics_mod.pick_topic(day, other))


# ---------------------------------------------------------------------------
# 损坏自愈
# ---------------------------------------------------------------------------

def test_corrupt_state_recovers(tmp_path, small_pool):
    """非法 JSON → 视为无状态，从当天重排起步，返回合法话题。"""
    (tmp_path / topics_mod.STATE_FILENAME).write_text("{broken", encoding="utf-8")
    assert topics_mod.pick_topic(DAY1, tmp_path) in small_pool


def test_order_pool_mismatch_rebuilds(tmp_path, small_pool):
    """order 与池大小不符（话题池增删过）/非法排列 → 重建不炸。"""
    (tmp_path / topics_mod.STATE_FILENAME).write_text(
        json.dumps({"date": DAY1.date().isoformat(), "order": [0, 1], "pos": 0},
                   ensure_ascii=False),
        encoding="utf-8")
    assert topics_mod.pick_topic(DAY1, tmp_path) in small_pool
    (tmp_path / topics_mod.STATE_FILENAME).write_text(
        json.dumps({"date": DAY1.date().isoformat(),
                    "order": [0, 0, 2], "pos": 1}, ensure_ascii=False),
        encoding="utf-8")
    assert topics_mod.pick_topic(DAY1, tmp_path) in small_pool


def test_pos_out_of_range_clamped(tmp_path, small_pool):
    """pos 越界（状态残留）→ 钳回合法区间，同日不再漂移。"""
    (tmp_path / topics_mod.STATE_FILENAME).write_text(
        json.dumps({"date": DAY2.date().isoformat(),
                    "order": [2, 1, 0], "pos": 99}, ensure_ascii=False),
        encoding="utf-8")
    got = topics_mod.pick_topic(DAY2, tmp_path)
    assert got in small_pool
    assert topics_mod.pick_topic(DAY2 + timedelta(hours=1), tmp_path) == got
