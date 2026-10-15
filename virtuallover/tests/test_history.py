# -*- coding: utf-8 -*-
"""brain/history.py 契约测试（SPEC §6）。

覆盖点：
- messages 表读写：双形态 content 入参 → content 列存规整纯文本，
  镜像保留原格式（回退脚本据此合并回 data_v4.db）；
- 双写镜像 mirror_v4.json：JSONL 逐行 append，行结构 {"role","content","ts"}，
  content 原格式（list 保持 list）；镜像可关（mirror=False / mirror_path=None）；
- 新消息必带时间戳：缺省取注入 clock（毫秒精度，PERF_SPEC §7.2），显式传入
  优先；每轮 user+assistant 成对；
- 读取：升序、limit 取最近 N 条语义不变、meta JSON 回读；
- 多会话隔离、count/clear、非法参数拒绝。

时间戳一律用固定 clock 注入（2026-05-17 22:18 本地时区，秒浮点 → 毫秒入库），
session_id 用 `人格名:FriendMessage:10xxx` 模板形态（QQ 号 10xxx 占位，
绝无真实账号）。
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

# sys.path 引导：virtuallover/ 是 namespace 包，仓库根必须可导入。
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from virtuallover.brain.history import HistoryStore  # noqa: E402

SESSION = "人格A:FriendMessage:10000"
OTHER_SESSION = "人格A:FriendMessage:10001"
TS = int(datetime.datetime(2026, 5, 17, 22, 18).timestamp())
TS_MS = TS * 1000  # 新写入行的入库精度（PERF_SPEC §7.2：毫秒）


@pytest.fixture
def store(tmp_path):
    """默认夹具：临时目录 + 固定 clock 的 store（含镜像）。"""
    s = HistoryStore(
        tmp_path / "history.db",
        mirror_path=tmp_path / "mirror_v4.json",
        clock=lambda: TS_MS / 1000.0,  # 秒浮点 clock，入库 ×1000 = TS_MS
    )
    yield s
    s.close()


def _read_mirror_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------------------
# 写入与读取
# ---------------------------------------------------------------------------


def test_append_returns_ids_and_get_messages_roundtrip(store):
    id1 = store.append(SESSION, "user", "你好呀")
    id2 = store.append(SESSION, "assistant", "在的在的，怎么啦？")
    assert id2 == id1 + 1  # 自增主键
    assert store.get_messages(SESSION) == [
        {"role": "user", "content": "你好呀"},
        {"role": "assistant", "content": "在的在的，怎么啦？"},
    ]


def test_content_dual_form_text_column_vs_mirror_raw(store):
    """双形态：content 列规整为纯文本；镜像保留原格式（SPEC §6）。"""
    raw_segments = [
        {"type": "text", "text": "文本部分"},
        {"type": "think", "think": "内心戏"},
    ]
    store.append(SESSION, "assistant", raw_segments)
    # content 列：只取 text 段（think 已剥离）
    assert store.get_messages(SESSION)[0]["content"] == "文本部分"
    # 镜像：原格式 list 原样保留
    mirror_lines = _read_mirror_lines(store.mirror_path)
    assert mirror_lines[0]["content"] == raw_segments


def test_mirror_jsonl_appends_one_line_per_message(store):
    store.append(SESSION, "user", "第一条")
    store.append(SESSION, "assistant", "第二条")
    store.append(SESSION, "user", "第三条")
    lines = _read_mirror_lines(store.mirror_path)
    assert len(lines) == 3  # append 语义：每条消息一行，不重写文件
    assert lines[0] == {"role": "user", "content": "第一条", "ts": TS_MS}
    assert lines[1]["content"] == "第二条"
    assert lines[2]["content"] == "第三条"


def test_mirror_can_be_disabled_per_call_and_per_store(tmp_path):
    db = tmp_path / "h.db"
    mirror = tmp_path / "m.json"
    no_mirror_store = HistoryStore(db, mirror_path=None, clock=lambda: TS)
    no_mirror_store.append(SESSION, "user", "不镜像")
    no_mirror_store.append(SESSION, "assistant", "跳过", mirror=False)
    assert not mirror.exists()  # mirror_path=None：文件根本不创建
    no_mirror_store.close()

    with_mirror = HistoryStore(db, mirror_path=mirror, clock=lambda: TS)
    with_mirror.append(SESSION, "user", "正常镜像", mirror=False)
    assert not mirror.exists()  # mirror=False：单条跳过
    with_mirror.append(SESSION, "assistant", "这条写")
    assert len(_read_mirror_lines(mirror)) == 1
    with_mirror.close()


def test_created_at_defaults_to_clock_and_explicit_wins(store):
    row = store.append(SESSION, "user", "默认时间戳")
    assert store.get_records(SESSION)[0]["created_at"] == TS_MS  # 新消息必带时间戳（毫秒）

    explicit = TS_MS + 3_600_000
    store.append(SESSION, "assistant", "显式时间戳", created_at=explicit)
    assert store.get_records(SESSION)[1]["created_at"] == explicit


def test_append_pair_writes_user_assistant_with_same_timestamp(store):
    user_id, assistant_id = store.append_pair(SESSION, "早安", "早安呀，今天也要元气满满哦")
    records = store.get_records(SESSION)
    assert [r["role"] for r in records] == ["user", "assistant"]  # 每轮 user+assistant
    assert records[0]["created_at"] == records[1]["created_at"] == TS_MS
    mirror_lines = _read_mirror_lines(store.mirror_path)
    assert [line["role"] for line in mirror_lines] == ["user", "assistant"]
    assert user_id < assistant_id


def test_meta_json_roundtrip(store):
    meta = {"source": "test", "msg_id": 12345}
    store.append(SESSION, "user", "带元数据", meta=meta)
    record = store.get_records(SESSION)[0]
    assert record["meta"] == meta
    # 未传 meta 默认空对象
    store.append(SESSION, "assistant", "无元数据")
    assert store.get_records(SESSION)[1]["meta"] == {}


# ---------------------------------------------------------------------------
# 读取语义
# ---------------------------------------------------------------------------


def test_get_messages_limit_returns_recent_in_ascending_order(store):
    for i in range(5):
        store.append(SESSION, "user", f"消息{i}")
    recent = store.get_messages(SESSION, limit=2)
    assert recent == [{"role": "user", "content": "消息3"}, {"role": "user", "content": "消息4"}]


def test_get_records_includes_ids_and_timestamps(store):
    store.append(SESSION, "user", "带全字段")
    record = store.get_records(SESSION)[0]
    assert set(record.keys()) == {"id", "role", "content", "created_at", "meta"}
    assert record["id"] == 1


def test_multi_session_isolation(store):
    store.append(SESSION, "user", "A 的消息")
    store.append(OTHER_SESSION, "user", "B 的消息")
    store.append(SESSION, "user", "A 的第二条")
    assert store.count(SESSION) == 2
    assert store.count(OTHER_SESSION) == 1
    assert store.count() == 3
    assert [m["content"] for m in store.get_messages(SESSION)] == ["A 的消息", "A 的第二条"]


def test_count_and_clear(store):
    store.append_pair(SESSION, "问", "答")
    store.append_pair(SESSION, "再问", "再答")
    store.append(OTHER_SESSION, "user", "别的会话")
    assert store.count(SESSION) == 4
    assert store.clear(SESSION) == 4
    assert store.count(SESSION) == 0
    assert store.count(OTHER_SESSION) == 1  # 清理只作用于目标会话
    assert store.clear(SESSION) == 0  # 幂等


# ---------------------------------------------------------------------------
# 参数校验与持久化
# ---------------------------------------------------------------------------


def test_invalid_role_and_empty_session_rejected(store):
    with pytest.raises(ValueError):
        store.append(SESSION, "tool", "非法 role")
    with pytest.raises(ValueError):
        store.append("", "user", "空会话")


def test_reopen_persists_messages(tmp_path):
    db = tmp_path / "persist.db"
    mirror = tmp_path / "mirror.json"
    first = HistoryStore(db, mirror_path=mirror, clock=lambda: TS)
    first.append(SESSION, "user", "重启前写入")
    first.close()
    second = HistoryStore(db, mirror_path=mirror, clock=lambda: TS)
    assert second.get_messages(SESSION) == [{"role": "user", "content": "重启前写入"}]
    second.append(SESSION, "assistant", "重启后写入")
    assert second.count(SESSION) == 2
    second.close()


def test_wal_mode_enabled(tmp_path):
    store = HistoryStore(tmp_path / "wal.db", mirror_path=None, clock=lambda: TS)
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"
    store.close()


def test_synchronous_normal_enabled(tmp_path):
    """O8（PERF_SPEC §3）：synchronous=NORMAL，与 memory/store.py 同批对齐。"""
    store = HistoryStore(tmp_path / "sync.db", mirror_path=None, clock=lambda: TS)
    # synchronous：0=OFF 1=NORMAL 2=FULL —— WAL 下 NORMAL 免逐次 fsync
    assert store._conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    store.close()
