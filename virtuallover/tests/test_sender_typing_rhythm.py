# -*- coding: utf-8 -*-
"""brain/sender.py 打字节奏（typing_cps）契约测试。

覆盖 sample_gap / sample_first_gap 新签名（可选 next_len 参数）的边界：
- 向后兼容：next_len=None 或 typing_cps=0（或缺 typing_cps 属性的旧参数对象）
  完全保持现行分布（1.5-3.5 采样域、1.2s 硬下限、first_gap 关闭语义零变化）；
- 打字节奏：间隔 = clamp(base + 段长 × typing_cps × [0.8,1.2] 抖动, 1.2s, 10s)；
- 短段（≤SHORT_SEGMENT_CHARS 字）直接 1.2s 秒回感；长段封顶 10s；
- 首段抖动同样吃打字节奏，但 first_gap 关闭语义不被越过。

运行：python -m pytest virtuallover/tests/test_sender_typing_rhythm.py -q
"""

from __future__ import annotations

import random
import types

from virtuallover.brain import sender
from virtuallover.brain.sender import (
    MAX_SEGMENT_GAP_SECONDS,
    MIN_SEGMENT_GAP_SECONDS,
    SHORT_SEGMENT_CHARS,
    sample_first_gap,
    sample_gap,
)
from virtuallover.config import SegmentedReplyConfig


def _gap_params(**kw) -> SegmentedReplyConfig:
    """1.5-3.5 均匀区间 + 可覆盖字段（typing_cps 默认取配置地基值 0.08）。"""
    kw.setdefault("interval", "1.5,3.5")
    kw.setdefault("min_gap", 1.5)
    kw.setdefault("max_gap", 3.5)
    kw.setdefault("interval_method", "random")  # 均匀分布：边界可精确推算
    return SegmentedReplyConfig(**kw)


# ---------------------------------------------------------------------------
# 向后兼容：next_len=None / typing_cps=0 / 旧参数对象 → 现行分布零变化
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_none_next_len_keeps_current_distribution(self):
        """next_len=None（既有调用方形态）：1.5-3.5 采样域原样，1.2s 下限兜底。"""
        rng = random.Random(7)
        gaps = [sample_gap(_gap_params(), rng) for _ in range(2000)]
        assert all(1.5 <= g <= 3.5 for g in gaps)
        assert min(gaps) < 1.6 and max(gaps) > 3.4  # 区间两端都被采到

    def test_zero_cps_ignores_next_len(self):
        """typing_cps=0（用户显式关闭）：即便给了段长也完全保持现行分布。"""
        rng = random.Random(11)
        params = _gap_params(typing_cps=0.0)
        gaps = [sample_gap(params, rng, next_len=20) for _ in range(2000)]
        assert all(1.5 <= g <= 3.5 for g in gaps)

    def test_legacy_params_object_without_typing_cps_attr(self):
        """缺 typing_cps 属性的旧参数对象（duck typing 契约）按关闭处理。"""
        legacy = types.SimpleNamespace(
            interval="1.5,3.5", min_gap=1.5, max_gap=3.5, interval_method="random")
        rng = random.Random(3)
        gaps = [sample_gap(legacy, rng, next_len=30) for _ in range(1000)]
        assert all(1.5 <= g <= 3.5 for g in gaps)

    def test_first_gap_without_next_len_unchanged(self):
        """首段：next_len=None 保持现行均匀采样域（0.8-2.2，无 1.2s 下限）。"""
        rng = random.Random(5)
        params = SegmentedReplyConfig(first_gap="0.8-2.2")
        gaps = [sample_first_gap(params, rng) for _ in range(2000)]
        assert all(0.8 <= g <= 2.2 for g in gaps)
        assert min(gaps) < 1.0  # 现行行为允许低于 1.2（打字节奏才引入下限）


# ---------------------------------------------------------------------------
# 打字节奏：短段秒回 / 长段按 cps 叠加 / 封顶
# ---------------------------------------------------------------------------


class TestTypingGap:
    def test_short_segment_returns_exact_floor(self):
        """≤4 字短段：不叠加打字耗时，直接 1.2s 底线（秒回感，确定性）。"""
        rng = random.Random(1)
        for length in (0, 1, 3, SHORT_SEGMENT_CHARS):
            assert sample_gap(_gap_params(), rng, next_len=length) == \
                MIN_SEGMENT_GAP_SECONDS

    def test_typing_time_added_with_jitter_bounds(self):
        """固定 base=1.5、段长 20、cps=0.08：叠加区间 [1.28,1.92] → [2.78, 3.42]。"""
        rng = random.Random(13)
        params = _gap_params(interval="1.5-1.5")
        gaps = [sample_gap(params, rng, next_len=20) for _ in range(2000)]
        lo, hi = 1.5 + 20 * 0.08 * 0.8, 1.5 + 20 * 0.08 * 1.2
        assert all(lo - 1e-9 <= g <= hi + 1e-9 for g in gaps)
        assert min(gaps) < lo + 0.05 and max(gaps) > hi - 0.05  # 抖动两端都出现

    def test_longer_segment_waits_longer(self):
        """段长越长等越久（80 字下界 > 20 字上界）：拟人手打节奏单调性。"""
        rng = random.Random(17)
        params = _gap_params(interval="1.5-1.5")
        short = min(sample_gap(params, rng, next_len=20) for _ in range(500))
        long_min = min(sample_gap(params, rng, next_len=80) for _ in range(500))
        assert long_min > short

    def test_very_long_segment_caps_at_ten_seconds(self):
        """超长段（200 字，cps=0.08）：任何抖动下都封顶 10s（不掉线感）。"""
        rng = random.Random(19)
        gaps = [sample_gap(_gap_params(), rng, next_len=200) for _ in range(500)]
        assert all(g == MAX_SEGMENT_GAP_SECONDS for g in gaps)

    def test_moderately_long_segment_not_capped(self):
        """80 字（cps=0.08）：叠加区间 [6.62, 9.18] 全部低于 10s 封顶（贴线不触顶）。"""
        rng = random.Random(23)
        params = _gap_params(interval="1.5-1.5")
        gaps = [sample_gap(params, rng, next_len=80) for _ in range(500)]
        assert all(6.62 - 1e-9 <= g <= 9.18 + 1e-9 for g in gaps)
        assert all(g < MAX_SEGMENT_GAP_SECONDS for g in gaps)

    def test_floor_applies_when_base_below_1_2(self):
        """配置写了极小间隔区间（0.1-0.2）：打字节奏下仍压到 1.2s 下限。"""
        rng = random.Random(29)
        params = _gap_params(interval="0.1-0.2", min_gap=0.1, max_gap=0.2)
        gaps = [sample_gap(params, rng, next_len=6) for _ in range(500)]
        assert all(g >= MIN_SEGMENT_GAP_SECONDS for g in gaps)


# ---------------------------------------------------------------------------
# 首段抖动 × 打字节奏
# ---------------------------------------------------------------------------


class TestTypingFirstGap:
    def test_first_gap_typing_bounds(self):
        """first_gap 0.8-2.2、段长 20、cps=0.08：叠加后 [2.08, 4.12]（下限不咬合）。"""
        rng = random.Random(31)
        params = SegmentedReplyConfig(first_gap="0.8-2.2")
        gaps = [sample_first_gap(params, rng, next_len=20) for _ in range(2000)]
        assert all(2.08 - 1e-9 <= g <= 4.12 + 1e-9 for g in gaps)

    def test_first_gap_short_segment_floor(self):
        """首段 ≤4 字：直接 1.2s（比现行 0.8 下限略抬，秒回但非瞬发）。"""
        assert sample_first_gap(
            SegmentedReplyConfig(first_gap="0.8-2.2"), next_len=2
        ) == MIN_SEGMENT_GAP_SECONDS

    def test_first_gap_fixed_base_with_typing(self):
        """first_gap 钉死 1.5：叠加区间 [2.78, 3.42]（与段间同款数学）。"""
        rng = random.Random(37)
        params = SegmentedReplyConfig(first_gap="1.5-1.5")
        gaps = [sample_first_gap(params, rng, next_len=20) for _ in range(500)]
        assert all(2.78 - 1e-9 <= g <= 3.42 + 1e-9 for g in gaps)

    def test_disabled_first_gap_not_revived_by_typing(self):
        """first_gap 关闭语义不被打字节奏越过：空串 + 段长仍返回 0.0。"""
        assert sample_first_gap(
            SegmentedReplyConfig(first_gap=""), next_len=30) == 0.0

    def test_zero_cps_first_gap_unchanged(self):
        """typing_cps=0：首段即便给了段长也保持现行分布。"""
        rng = random.Random(41)
        params = SegmentedReplyConfig(first_gap="0.8-2.2", typing_cps=0.0)
        gaps = [sample_first_gap(params, rng, next_len=20) for _ in range(1000)]
        assert all(0.8 <= g <= 2.2 for g in gaps)
