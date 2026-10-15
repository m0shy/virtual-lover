# -*- coding: utf-8 -*-
"""store.py 单元测试（SPEC §7.1 / §7.3）。

覆盖：schema 全字段、库元数据 fail-fast 校验、余弦检索排序与 top_k/候选 8
重排语义、session_id 过滤、/remember 手动 API、service_state kv、
<MNEMO_META> 剥离与注入行格式。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime

import numpy as np
import pytest

from conftest import FAKE_SESSION, TEST_DIM
from virtuallover.memory.store import (
    CANDIDATES,
    EMBED_DIM,
    TOP_K,
    LLMNotWiredError,
    MemoryMetaMismatchError,
    MemoryStore,
    extract_query_keywords,
    format_memory_line,
    strip_memory_meta,
)

Q = "查询文本占位"


# ---------------------------------------------------------------------------
# schema（SPEC §7.1 全字段）
# ---------------------------------------------------------------------------

def test_schema_columns_exact(fake_store):
    """memories 表九列与 SPEC §7.1 逐列对应（名称/类型/顺序）。"""
    s = fake_store()
    path = s.db_path
    s.close()

    conn = sqlite3.connect(path)
    cols = conn.execute("PRAGMA table_info(memories)").fetchall()
    conn.close()
    got = [(c[1], c[2]) for c in cols]
    assert got == [
        ("id", "INTEGER"),
        ("session_id", "TEXT"),
        ("persona_id", "TEXT"),
        ("content", "TEXT"),
        ("metadata", "TEXT"),
        ("create_time", "INTEGER"),
        ("embedding", "BLOB"),
        ("embed_model", "TEXT"),
        ("dim", "INTEGER"),
    ]


def test_embedding_stored_as_float32_blob(fake_store):
    """embedding 以 float32 BLOB 落库，dim/embed_model 行内字段回读一致。"""
    s = fake_store()
    vec = [1.0] + [0.0] * (TEST_DIM - 1)
    mid = asyncio.run(s.remember("内容甲", FAKE_SESSION, embedding=vec))
    row = s._conn.execute(
        "SELECT embedding, embed_model, dim, metadata FROM memories WHERE id=?",
        (mid,),
    ).fetchone()
    assert len(row[0]) == TEST_DIM * 4
    back = np.frombuffer(row[0], dtype=np.float32)
    assert back[0] == pytest.approx(1.0)
    assert row[1] == "test-embedding-model"
    assert row[2] == TEST_DIM
    assert json.loads(row[3]) == {}
    s.close()


def test_default_dim_is_2048():
    """默认维度对齐 SPEC §4（model/dim=2048），常量与默认注入一致。"""
    assert EMBED_DIM == 2048
    assert TOP_K == 3 and CANDIDATES == 8  # O5 补丁：top_k 2→3


# ---------------------------------------------------------------------------
# 库元数据 fail-fast（SPEC §7.1）
# ---------------------------------------------------------------------------

def test_fresh_meta_recorded(fake_store):
    """新库写入 provider/model/endpoint/dim + schema_version 基线。"""
    s = fake_store()
    meta = s.get_meta()
    assert meta["embedding_provider"] == "占位供应商"
    assert meta["embedding_model"] == "test-embedding-model"
    assert meta["embedding_endpoint"] == "http://127.0.0.1:9"
    assert meta["embedding_dim"] == str(TEST_DIM)
    assert meta["schema_version"] == "1"
    assert s.blocked_reason is None
    s.close()


def test_reopen_with_same_meta_ok(fake_store):
    """同配置重开：校验通过，可继续写读。"""
    s = fake_store(name="same.db")
    asyncio.run(s.remember("内容甲", FAKE_SESSION, embedding=[1.0] + [0.0] * (TEST_DIM - 1)))
    s.close()

    s2 = fake_store(name="same.db")
    assert s2.blocked_reason is None
    assert s2.count() == 1
    s2.close()


def test_dim_mismatch_blocks_write_and_search(fake_store):
    """dim 不一致 → fail-fast：写入与检索均抛错，且库内容未被改动。"""
    s = fake_store(name="mismatch.db")
    asyncio.run(s.remember("内容甲", FAKE_SESSION, embedding=[1.0] + [0.0] * (TEST_DIM - 1)))
    s.close()

    s2 = fake_store(name="mismatch.db", dim=TEST_DIM - 4)
    assert s2.blocked_reason is not None
    assert "embedding_dim" in s2.blocked_reason
    with pytest.raises(MemoryMetaMismatchError):
        asyncio.run(s2.remember("新内容", FAKE_SESSION))
    with pytest.raises(MemoryMetaMismatchError):
        asyncio.run(s2.search(FAKE_SESSION, Q))
    with pytest.raises(MemoryMetaMismatchError):
        s2.delete_session(FAKE_SESSION)
    assert s2.count() == 1  # 封锁态仅开放只读统计
    s2.close()


def test_model_mismatch_blocks(fake_store):
    """换 embedding 模型 → 封锁（旧向量对新模型无意义，绝不静默混用）。"""
    s = fake_store(name="model.db")
    s.close()

    other = MemoryStore(
        s.db_path,
        embedder=None,
        provider_id="占位供应商",
        model="另一个模型",
        endpoint="http://127.0.0.1:9",
        dim=TEST_DIM,
    )
    with pytest.raises(MemoryMetaMismatchError):
        asyncio.run(other.remember("内容甲", FAKE_SESSION))
    with pytest.raises(MemoryMetaMismatchError):
        asyncio.run(other.search(FAKE_SESSION, Q))
    other.close()


def test_meta_missing_but_rows_exist_blocks(fake_store):
    """store_meta 被清空但已有记忆行 → 封锁（元数据缺失即拒写拒检索）。"""
    s = fake_store(name="orphan.db")
    asyncio.run(s.remember("内容甲", FAKE_SESSION, embedding=[1.0] + [0.0] * (TEST_DIM - 1)))
    s._conn.execute("DELETE FROM store_meta")
    s._conn.commit()
    s.close()

    s2 = fake_store(name="orphan.db")
    assert s2.blocked_reason is not None
    with pytest.raises(MemoryMetaMismatchError):
        asyncio.run(s2.search(FAKE_SESSION, Q))
    s2.close()


# ---------------------------------------------------------------------------
# 检索：余弦排序 / top_k / 候选 8 重排 / 会话过滤
# ---------------------------------------------------------------------------

def _axis(dim: int, i: int) -> list[float]:
    v = [0.0] * dim
    v[i] = 1.0
    return v


def test_search_cosine_order_and_topk(fake_store):
    """纯余弦路径：按相似度降序返回 top_k=2。"""
    q = _axis(TEST_DIM, 0)
    near = _axis(TEST_DIM, 0)                       # cos=1.0
    mid = (np.array(q) + np.array(_axis(TEST_DIM, 1))) / np.sqrt(2)  # cos≈0.707
    far = _axis(TEST_DIM, 1)                        # cos=0.0
    vectors = {"甲": near, "乙": mid.tolist(), "丙": far}
    s = fake_store(vectors=vectors)
    for content in ("甲", "乙", "丙"):
        asyncio.run(s.remember(content, FAKE_SESSION))

    hits = asyncio.run(s.search(FAKE_SESSION, Q, query_vector=q))
    assert [h.content for h in hits] == ["甲", "乙", "丙"]  # O5：top_k=3
    assert hits[0].cosine == pytest.approx(1.0, abs=1e-6)
    assert hits[1].cosine == pytest.approx(0.7071, abs=1e-3)
    assert all(h.keyword_hits == 0 for h in hits)
    s.close()


def test_search_empty_session_returns_empty(fake_store):
    s = fake_store()
    assert asyncio.run(s.search(FAKE_SESSION, Q)) == []
    s.close()


def test_keyword_rerank_boosts_within_candidates(fake_store):
    """重排：候选池内关键词命中者优先于更高余弦。"""
    q = _axis(TEST_DIM, 0)
    cos = [0.99, 0.95, 0.90, 0.85, 0.80]
    contents = ["条目一", "条目二", "樱花条目三", "条目四", "条目五"]
    vectors = {}
    for content, score in zip(contents, cos):
        v = np.array(q) * score + np.array(_axis(TEST_DIM, 1)) * np.sqrt(1 - score ** 2)
        vectors[content] = v.tolist()
    s = fake_store(vectors=vectors)
    for content in contents:
        asyncio.run(s.remember(content, FAKE_SESSION))

    hits = asyncio.run(s.search(FAKE_SESSION, "樱花", query_vector=q))
    # 「樱花」命中条目三（余弦第 3），重排后升到第 1（O5：top_k=3 全量返回）
    assert [h.content for h in hits] == ["樱花条目三", "条目一", "条目二"]
    assert hits[0].keyword_hits >= 1
    assert hits[1].keyword_hits == 0
    s.close()


def test_candidates_window_excludes_outsider(fake_store):
    """候选 8 语义：余弦第 9 名即使全中关键词也不进结果；扩大候选后可进。"""
    q = _axis(TEST_DIM, 0)
    vectors: dict[str, list[float]] = {}
    plain = [f"内容{i}" for i in range(10)]
    for i, content in enumerate(plain):
        score = 0.99 - i * 0.05  # 0.99 … 0.54 严格降序
        v = np.array(q) * score + np.array(_axis(TEST_DIM, 1)) * np.sqrt(max(1 - score ** 2, 0))
        vectors[content] = v.tolist()
    rank9 = plain[8]           # 余弦第 9：内容8，命中关键词「火锅」
    rank1 = plain[0]
    vectors["今晚吃火锅"] = (np.array(q) * 0.52
                          + np.array(_axis(TEST_DIM, 1)) * np.sqrt(1 - 0.52 ** 2)).tolist()
    s = fake_store(vectors=vectors)
    for content in plain:
        asyncio.run(s.remember(content, FAKE_SESSION))
    asyncio.run(s.remember("今晚吃火锅", FAKE_SESSION))

    hits = asyncio.run(s.search(FAKE_SESSION, "火锅", query_vector=q))
    assert len(hits) == TOP_K
    # 默认候选 8：第 9 名「今晚吃火锅」被排除，返回纯余弦前三（O5：top_k=3）
    assert rank9 not in [h.content for h in hits]
    assert [h.content for h in hits] == [rank1, plain[1], plain[2]]

    hits_wide = asyncio.run(s.search(FAKE_SESSION, "火锅", query_vector=q, candidates=11))
    assert hits_wide[0].content == "今晚吃火锅"  # 进入候选池后关键词重排登顶
    s.close()


def test_session_id_exact_filter(fake_store):
    """session_id 精确过滤：他会话的记忆不可见、不参与计数。"""
    s = fake_store()
    asyncio.run(s.remember("独属于他会话", "其他会话:FriendMessage:99999999"))
    asyncio.run(s.remember("本会话记忆", FAKE_SESSION))
    hits = asyncio.run(s.search(FAKE_SESSION, "独属于他会话"))
    assert [h.content for h in hits] == ["本会话记忆"]  # 检索域锁定本会话
    assert s.count(FAKE_SESSION) == 1
    assert s.count("其他会话:FriendMessage:99999999") == 1
    assert s.count() == 2
    s.close()


# ---------------------------------------------------------------------------
# /remember 手动 API 与参数校验
# ---------------------------------------------------------------------------

def test_remember_manual_metadata_and_create_time(fake_store):
    """手动 API：返回行 id，metadata/create_time/persona_id 按参数落库。"""
    s = fake_store()
    mid = asyncio.run(s.remember(
        "手工记忆条目", FAKE_SESSION,
        persona_id="人格占位", metadata={"source": "eval", "k": "v"},
        create_time=1700000000,
    ))
    assert isinstance(mid, int) and mid > 0
    hits = asyncio.run(s.search(FAKE_SESSION, "手工记忆条目"))
    assert hits[0].id == mid
    assert hits[0].metadata == {"source": "eval", "k": "v"}
    assert hits[0].create_time == 1700000000
    s.close()


def test_remember_rejects_empty_and_bad_dim(fake_store):
    """空内容/空会话/维度不符 → ValueError，不入库。"""
    s = fake_store()
    with pytest.raises(ValueError):
        asyncio.run(s.remember("   ", FAKE_SESSION))
    with pytest.raises(ValueError):
        asyncio.run(s.remember("内容", "  "))
    with pytest.raises(ValueError, match="维度"):
        asyncio.run(s.remember("内容", FAKE_SESSION, embedding=[0.0] * (TEST_DIM + 2)))
    assert s.count() == 0
    s.close()


def test_embedder_not_wired_raises(fake_store, tmp_path):
    """embedder 未接线：需要向量化的写入/检索抛 LLMNotWiredError（占位接口语义）。"""
    bare = MemoryStore(
        tmp_path / "bare.db", embedder=None,
        provider_id="占位供应商", model="test-embedding-model",
        endpoint="http://127.0.0.1:9", dim=TEST_DIM,
    )
    with pytest.raises(LLMNotWiredError):
        asyncio.run(bare.remember("内容", FAKE_SESSION))
    with pytest.raises(LLMNotWiredError):
        asyncio.run(bare.search(FAKE_SESSION, Q))
    bare.close()


def test_query_vector_dim_mismatch(fake_store):
    """查询向量维度不符 → ValueError。"""
    s = fake_store()
    with pytest.raises(ValueError, match="维度"):
        asyncio.run(s.search(FAKE_SESSION, Q, query_vector=[0.0] * (TEST_DIM + 1)))
    s.close()


# ---------------------------------------------------------------------------
# O8 并发面（PERF_SPEC §3 O8）：pragma / 实例锁 / 检索矩阵缓存
# ---------------------------------------------------------------------------

def test_store_pragmas_wal_busy_timeout_synchronous(fake_store):
    """O8：WAL + busy_timeout(5000) + synchronous=NORMAL（对齐 history 模式）。"""
    s = fake_store()
    assert str(s._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    assert s._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    # synchronous：0=OFF 1=NORMAL 2=FULL —— WAL 下 NORMAL 免逐次 fsync
    assert s._conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    s.close()


def test_cross_thread_access_via_instance_lock(fake_store):
    """O8：check_same_thread=False + 实例锁——另一线程可直接读写本实例。"""
    import threading

    s = fake_store()
    errors: list[Exception] = []

    def worker() -> None:
        try:
            asyncio.run(s.remember(
                "跨线程写入", FAKE_SESSION,
                embedding=[1.0] + [0.0] * (TEST_DIM - 1)))
            assert s.count() == 1
            assert s.kv_set("t", 1) is None and s.kv_get("t") == 1
        except Exception as exc:  # noqa: BLE001 —— 收集后主线程断言
            errors.append(exc)

    th = threading.Thread(target=worker)
    th.start()
    th.join(10)
    assert not errors, f"跨线程操作失败：{errors}"
    assert s.count() == 1
    s.close()


def test_matrix_cache_counters_and_invalidation(fake_store):
    """O8：缓存命中计数暴露；失效点=remember 执行与 delete_session。"""
    vec = [1.0] + [0.0] * (TEST_DIM - 1)
    s = fake_store()
    asyncio.run(s.remember("甲", FAKE_SESSION, embedding=vec))
    asyncio.run(s.remember("乙", FAKE_SESSION, embedding=[0.0, 1.0] + [0.0] * (TEST_DIM - 2)))

    assert s.cache_stats() == {"hits": 0, "misses": 0, "cached_sessions": 0}
    asyncio.run(s.search(FAKE_SESSION, Q))          # miss → 重建并缓存
    assert s.cache_stats()["misses"] == 1
    assert s.cache_stats()["cached_sessions"] == 1
    asyncio.run(s.search(FAKE_SESSION, Q))          # 命中
    assert s.cache_stats()["hits"] == 1

    # 失效点①：remember 执行 → 下次检索重建，能看到新行
    # （查询向量显式对齐「丙」的轴向量：单字无关键词，走纯余弦）
    asyncio.run(s.remember("丙", FAKE_SESSION, embedding=[0.0, 0.0, 1.0] + [0.0] * (TEST_DIM - 3)))
    hits = asyncio.run(s.search(
        FAKE_SESSION, "丙",
        query_vector=[0.0, 0.0, 1.0] + [0.0] * (TEST_DIM - 3)))
    assert hits[0].content == "丙"
    assert s.cache_stats()["misses"] == 2           # remember 后确实重建过

    # 失效点②：delete_session → 缓存失效，检索为空
    s.delete_session(FAKE_SESSION)
    assert asyncio.run(s.search(FAKE_SESSION, Q)) == []
    assert s.cache_stats()["misses"] == 3
    s.close()


def test_matrix_cache_rebuild_replaces_object_not_inplace(fake_store):
    """O8「重建不原地改」：remember 失效重建后缓存槽位换成新对象，旧快照原样。"""
    vec = [1.0] + [0.0] * (TEST_DIM - 1)
    s = fake_store()
    asyncio.run(s.remember("旧条目", FAKE_SESSION, embedding=vec))
    asyncio.run(s.search(FAKE_SESSION, Q))
    old_entry = s._matrix_cache[FAKE_SESSION]
    old_shape = old_entry.unit.shape

    asyncio.run(s.remember("新条目", FAKE_SESSION, embedding=vec))
    asyncio.run(s.search(FAKE_SESSION, Q))
    new_entry = s._matrix_cache[FAKE_SESSION]
    assert new_entry is not old_entry          # 整体替换，不是原地改
    assert old_entry.unit.shape == old_shape   # 旧快照未被读侧可见地篡改
    assert len(new_entry.rows) == 2
    s.close()


def test_cached_search_matches_rebuilt_search(fake_store):
    """O8：缓存命中路径与重建路径检索结果逐字段一致（缓存不改语义）。"""
    q = _axis(TEST_DIM, 0)
    cos = [0.99, 0.95, 0.90, 0.85, 0.80, 0.75]
    contents = ["条目一", "条目二", "樱花条目三", "条目四", "条目五", "条目六"]
    s = fake_store()
    for content, score in zip(contents, cos):
        v = np.array(q) * score + np.array(_axis(TEST_DIM, 1)) * np.sqrt(1 - score ** 2)
        asyncio.run(s.remember(content, FAKE_SESSION, embedding=v.tolist()))

    cached = asyncio.run(s.search(FAKE_SESSION, "樱花", query_vector=q))
    assert s.cache_stats()["hits"] == 0        # 首查为重建
    s._invalidate_matrix()                     # 强制下一次重建（等价失效点）
    rebuilt = asyncio.run(s.search(FAKE_SESSION, "樱花", query_vector=q))
    assert s.cache_stats()["hits"] == 0 and s.cache_stats()["misses"] >= 2
    assert cached == rebuilt                   # MemoryHit 逐字段相等
    assert [h.content for h in cached] == ["樱花条目三", "条目一", "条目二"]
    s.close()


def test_empty_session_matrix_cached(fake_store):
    """空会话同样走缓存：空矩阵条目命中，避免重复空 SELECT。"""
    s = fake_store()
    assert asyncio.run(s.search(FAKE_SESSION, Q)) == []
    assert s.cache_stats()["cached_sessions"] == 1
    assert asyncio.run(s.search(FAKE_SESSION, Q)) == []
    assert s.cache_stats()["hits"] == 1
    s.close()


# ---------------------------------------------------------------------------
# service_state kv（§7.2 持久化存储面）
# ---------------------------------------------------------------------------

def test_kv_roundtrip(fake_store):
    s = fake_store()
    assert s.kv_get("missing") is None
    assert s.kv_get("missing", default={"a": 1}) == {"a": 1}
    s.kv_set("k1", {"count": 4, "buffer": [1, 2]})
    assert s.kv_get("k1") == {"count": 4, "buffer": [1, 2]}
    s.kv_set("k1", "覆盖值")  # 同键覆盖
    assert s.kv_get("k1") == "覆盖值"
    assert s.kv_keys("k") == ["k1"]
    assert s.kv_delete("k1") is True
    assert s.kv_delete("k1") is False
    assert s.kv_keys() == []
    s.close()


def test_kv_keys_prefix_filter(fake_store):
    s = fake_store()
    s.kv_set("memsvc:v1:session:a", 1)
    s.kv_set("memsvc:v1:session:b", 2)
    s.kv_set("other", 3)
    assert s.kv_keys("memsvc:v1:session:") == [
        "memsvc:v1:session:a", "memsvc:v1:session:b",
    ]
    assert len(s.kv_keys()) == 3
    s.close()


# ---------------------------------------------------------------------------
# §7.3：元数据剥离 / 注入行格式 / 关键词抽取
# ---------------------------------------------------------------------------

def test_strip_memory_meta_cases():
    assert strip_memory_meta("普通内容") == "普通内容"
    assert strip_memory_meta("正文<MNEMO_META>x=1</MNEMO_META>") == "正文"
    assert strip_memory_meta("正文<MNEMO_META>截半残留") == "正文"
    assert strip_memory_meta("") == ""
    assert strip_memory_meta(None) == ""


def test_format_memory_line():
    """R3 相对时间分档：2023-11-15 的时刻相对 2024-01-01 落「N个月前」档。"""
    # 时刻与基准都用 datetime 构造（不依赖机器时区的魔法数字），档位确定：
    # 2023-11-15 → 2024-01-01 跨 47 天，日历月差 2 → 「2个月前」
    ts = datetime(2023, 11, 15, 12, 0).timestamp()
    now = datetime(2024, 1, 1, 12, 0).timestamp()
    line = format_memory_line(
        ts, "正文<MNEMO_META>x</MNEMO_META>\n第二行", now=now
    )
    assert line == "- [2个月前] 正文 第二行"  # 相对分档+元数据剥离+换行折叠
    assert format_memory_line(None, "无时间", now=now) == "- [较早] 无时间"


def test_extract_query_keywords():
    assert extract_query_keywords("Hello hello 世界这么大") == [
        "hello", "世界", "界这", "这么", "么大",
    ]
    assert extract_query_keywords("") == []
    assert extract_query_keywords("a") == []  # 单字符 ASCII 不成词


# ---------------------------------------------------------------------------
# PERF_SPEC §3-O5：检索相似度下限（低于阈值不注入）
# ---------------------------------------------------------------------------

def test_search_min_cosine_filters_low_hits(fake_store):
    """min_cosine：余弦低于阈值的命中不返回；None/0 不过滤（旧行为）。"""
    q = _axis(TEST_DIM, 0)
    near = _axis(TEST_DIM, 0)                        # cos=1.0
    weak = (np.array(q) + np.array(_axis(TEST_DIM, 1)) * 2) / np.sqrt(5)  # cos≈0.447
    far = _axis(TEST_DIM, 1)                         # cos=0.0
    vectors = {"相关": near, "弱相关": weak.tolist(), "无关": far}
    s = fake_store(vectors=vectors)
    for content in ("相关", "弱相关", "无关"):
        asyncio.run(s.remember(content, FAKE_SESSION))

    hits = asyncio.run(s.search(FAKE_SESSION, Q, query_vector=q, min_cosine=0.5))
    assert [h.content for h in hits] == ["相关"]  # 0.447/0.0 被滤

    hits_all = asyncio.run(s.search(FAKE_SESSION, Q, query_vector=q, min_cosine=0.4))
    assert [h.content for h in hits_all] == ["相关", "弱相关"]  # 0.447 过线

    hits_none = asyncio.run(s.search(FAKE_SESSION, Q, query_vector=q))
    assert len(hits_none) == 3  # 缺省不过滤（/eval 自管阈值不受影响）
    hits_zero = asyncio.run(s.search(FAKE_SESSION, Q, query_vector=q, min_cosine=0))
    assert len(hits_zero) == 3  # 0=关
    s.close()
