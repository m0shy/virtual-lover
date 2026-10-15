# -*- coding: utf-8 -*-
"""场景自拍后处理测试：&&selfie_<scene>&& 全流程（有图/无图/概率/每日上限/残缺标记）。

形状对齐 test_memes.py：tmp_path 图库 + 注入确定性 rng（random 序列），
日期用固定 datetime 注入（不依赖墙钟），状态文件落 tmp_path 断言持久化。

本文件只出现占位场景名与临时目录，无任何账号/人设数据。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

# sys.path 引导：virtuallover/ 是 namespace 包，仓库根必须可导入。
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from virtuallover.soul.selfies import (  # noqa: E402
    DEFAULT_DAILY_LIMIT,
    DEFAULT_PROBABILITY,
    SCENES,
    find_scenes,
    pick_photo,
    process_reply,
    strip_all_markers,
)

NOW = datetime(2026, 10, 14, 12, 0, 0)


class _SeqRng:
    """确定性 rng：random() 依次返回预设序列，choice 取第一个。"""

    def __init__(self, *randoms: float):
        self.seq = list(randoms)
        self.i = 0

    def random(self) -> float:
        v = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        return v

    def choice(self, seq):
        return seq[0]


def make_library(state_dir: Path, scene: str, *names: str) -> Path:
    """在 tmp 图库的某场景目录放下若干（伪）照片，返回场景目录。"""
    d = state_dir / "selfies" / scene
    d.mkdir(parents=True, exist_ok=True)
    for name in names:
        (d / name).write_bytes(b"fake-photo")
    return d


# ---------------------------------------------------------------------------
# 场景表与标记提取
# ---------------------------------------------------------------------------


class TestScenes:
    def test_scene_table(self):
        assert SCENES == ("campus", "home", "date", "food", "night")

    def test_find_scenes_normalized(self):
        assert find_scenes("看看 &&selfie_Home&& 和 &&selfie_food&&") == ["home", "food"]
        assert find_scenes("白名单外 &&selfie_beach&&") == []
        assert find_scenes("空场景 &&selfie_&&") == []
        assert find_scenes("没有标记") == []


class TestStripAllMarkers:
    def test_complete_markers_removed(self):
        assert strip_all_markers("给你看&&selfie_home&&好嘛") == "给你看好嘛"

    def test_illegal_scene_marker_removed(self):
        assert strip_all_markers("&&selfie_beach&&海边！") == "海边！"

    def test_broken_forms_removed(self):
        """残缺形态连 selfie_ 词一起清（标记语法不是正文，与 memes 不同口径）。"""
        assert strip_all_markers("空场景 &&selfie_&& 结束") == "空场景 结束"
        out = strip_all_markers("未闭合 &&selfie_home 呀")
        assert "&&" not in out and "selfie" not in out and "未闭合 呀" == out

    def test_whitespace_converged(self):
        out = strip_all_markers("第一行&&selfie_food&&\n\n\n\n第二行&&")
        assert "\n\n\n" not in out
        assert "第一行" in out and "第二行" in out


class TestPickPhoto:
    def test_picks_from_scene_dir(self, tmp_path):
        make_library(tmp_path, "home", "a.jpg", "b.png")
        img = pick_photo(tmp_path / "selfies", "home", _SeqRng())
        assert img is not None and img.suffix.lower() in (".jpg", ".png")

    def test_missing_or_empty_scene_returns_none(self, tmp_path):
        assert pick_photo(tmp_path / "selfies", "home") is None
        (tmp_path / "selfies" / "home").mkdir(parents=True)
        assert pick_photo(tmp_path / "selfies", "home") is None

    def test_non_photo_files_ignored(self, tmp_path):
        """只有 .jpg/.png 算照片：.gif/.txt 不参与（规格口径）。"""
        d = make_library(tmp_path, "home", "readme.txt", "a.gif")
        assert pick_photo(tmp_path / "selfies", "home", _SeqRng()) is None
        (d / "a.gif").unlink()
        (d / "readme.txt").unlink()
        (d / "b.jpg").write_bytes(b"x")
        img = pick_photo(tmp_path / "selfies", "home", _SeqRng())
        assert img is not None and img.name == "b.jpg"


# ---------------------------------------------------------------------------
# process_reply 全流程
# ---------------------------------------------------------------------------


class TestProcessReply:
    def test_no_marker_returns_original(self, tmp_path):
        text, img = process_reply("普通回复", now=NOW, state_dir=tmp_path,
                                  rng=_SeqRng(0.0))
        assert text == "普通回复" and img is None
        assert not (tmp_path / "selfie_state.json").exists()  # 无标记不写状态

    def test_hit_returns_scene_photo(self, tmp_path):
        make_library(tmp_path, "home", "a.jpg", "b.png")
        text, img = process_reply("给你看看我的房间～&&selfie_home&&",
                                  now=NOW, state_dir=tmp_path, rng=_SeqRng(0.0))
        assert text == "给你看看我的房间～"
        assert img is not None
        assert img.is_absolute() and img.exists()
        assert img.parent.name == "home"
        # 计数落盘：当日第 1 张
        state = json.loads((tmp_path / "selfie_state.json").read_text(encoding="utf-8"))
        assert state == {"date": "2026-10-14", "sent": 1}

    def test_probability_miss_cleans_only(self, tmp_path):
        make_library(tmp_path, "date", "a.jpg")
        text, img = process_reply("约会照片&&selfie_date&&", now=NOW,
                                  state_dir=tmp_path, rng=_SeqRng(0.9))
        assert text == "约会照片" and img is None
        assert not (tmp_path / "selfie_state.json").exists()  # 未命中不写状态

    def test_probability_param_override(self, tmp_path):
        make_library(tmp_path, "food", "a.png")
        _, img = process_reply("&&selfie_food&&", now=NOW, state_dir=tmp_path,
                               rng=_SeqRng(0.99), probability=1.0)
        assert img is not None

    def test_daily_cap_blocks_then_next_day_resets(self, tmp_path):
        make_library(tmp_path, "home", "a.jpg")
        state_file = tmp_path / "selfie_state.json"
        state_file.write_text(json.dumps({"date": "2026-10-14", "sent": 3}),
                              encoding="utf-8")
        _, img = process_reply("&&selfie_home&&", now=NOW, state_dir=tmp_path,
                               rng=_SeqRng(0.0))
        assert img is None  # 当日满 3 张：概率命中也不出图
        assert json.loads(state_file.read_text(encoding="utf-8"))["sent"] == 3
        # 跨天自动清零：第二天同一张照片可发
        _, img = process_reply("&&selfie_home&&", now=NOW + timedelta(days=1),
                               state_dir=tmp_path, rng=_SeqRng(0.0))
        assert img is not None
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state == {"date": "2026-10-15", "sent": 1}

    def test_daily_limit_param(self, tmp_path):
        make_library(tmp_path, "home", "a.jpg")
        _, img1 = process_reply("&&selfie_home&&", now=NOW, state_dir=tmp_path,
                                rng=_SeqRng(0.0), daily_limit=1)
        _, img2 = process_reply("&&selfie_home&&", now=NOW, state_dir=tmp_path,
                                rng=_SeqRng(0.0), daily_limit=1)
        assert img1 is not None and img2 is None

    def test_second_send_accumulates_count(self, tmp_path):
        make_library(tmp_path, "campus", "a.jpg")
        for expected in (1, 2):
            _, img = process_reply("&&selfie_campus&&", now=NOW,
                                   state_dir=tmp_path, rng=_SeqRng(0.0))
            assert img is not None
            state = json.loads((tmp_path / "selfie_state.json").read_text(encoding="utf-8"))
            assert state["sent"] == expected

    def test_empty_library_returns_none(self, tmp_path):
        """库空（场景无图）→ 清理标记返回 None，软拒绝由人格规则兜底。"""
        (tmp_path / "selfies" / "night").mkdir(parents=True)
        text, img = process_reply("晚安照片&&selfie_night&&", now=NOW,
                                  state_dir=tmp_path, rng=_SeqRng(0.0))
        assert text == "晚安照片" and img is None

    def test_illegal_scene_returns_none(self, tmp_path):
        text, img = process_reply("&&selfie_beach&&", now=NOW, state_dir=tmp_path,
                                  rng=_SeqRng(0.0))
        assert text == "" and img is None

    def test_broken_marker_cleaned(self, tmp_path):
        make_library(tmp_path, "home", "a.jpg")
        text, img = process_reply("残缺 &&selfie_&& 标记", now=NOW,
                                  state_dir=tmp_path, rng=_SeqRng(0.0))
        assert text == "残缺 标记" and img is None

    def test_multiple_markers_first_with_photo_wins(self, tmp_path):
        make_library(tmp_path, "home", "a.jpg")
        text, img = process_reply("&&selfie_food&&和&&selfie_home&&",
                                  now=NOW, state_dir=tmp_path, rng=_SeqRng(0.0))
        assert text == "和"
        assert img is not None and img.parent.name == "home"

    def test_corrupt_state_starts_fresh(self, tmp_path):
        make_library(tmp_path, "home", "a.jpg")
        (tmp_path / "selfie_state.json").write_text("不是 JSON", encoding="utf-8")
        _, img = process_reply("&&selfie_home&&", now=NOW, state_dir=tmp_path,
                               rng=_SeqRng(0.0))
        assert img is not None  # 损坏状态从零开始，不阻塞发送

    def test_now_accepts_timestamp(self, tmp_path):
        make_library(tmp_path, "home", "a.jpg")
        _, img = process_reply("&&selfie_home&&", now=NOW.timestamp(),
                               state_dir=tmp_path, rng=_SeqRng(0.0))
        assert img is not None
        state = json.loads((tmp_path / "selfie_state.json").read_text(encoding="utf-8"))
        assert state["date"] == "2026-10-14"

    def test_default_probability_shape(self):
        """规格参数钉住：同一会话概率 0.35、每日上限 3 张。"""
        assert DEFAULT_PROBABILITY == 0.35
        assert DEFAULT_DAILY_LIMIT == 3
