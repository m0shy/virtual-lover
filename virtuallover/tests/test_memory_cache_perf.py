# -*- coding: utf-8 -*-
"""O8 万条命中检索基准（PERF_SPEC §3 O8 / §5：缓存命中 p50 ≤ 10ms）。

口径（PERF_SPEC §3 O8 原文）：
- 万条命中 p50 ≤10ms 进基准测试；
- 确定性合成语料，不依赖本机 local/ 数据（md5 派生伪随机单位向量，重跑恒定）；
- 量化/mmap/分块为实测负优化已否决，本基准钉住「缓存命中」路径的性能回归。

构成：10000 条 ×2048 维（对齐现网 dim）写入独立临时库 → 预热一次触发矩阵
重建（miss 路径）→ 连续 50 次检索取 p50。写入走 remember() 公开路径
（嵌入预算好，原子块一次提交），顺带覆盖 O8「remember 失效 + atomic 提交
整体失效」的写路径。缓存命中计数一并断言（PERF_SPEC §3 O8：计数暴露）。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path

import numpy as np
import pytest

from conftest import FAKE_SESSION
from virtuallover.memory.store import TOP_K, MemoryStore

N_ROWS = 10_000      # 万条口径（PERF_SPEC §5「万条检索」）
DIM = 2048           # 对齐现网嵌入维度（SPEC §4）
ROUNDS = 50          # 采样次数（p50 取中位）
P50_BUDGET_MS = 10.0  # 验收预算（PERF_SPEC §5：缓存命中 p50 ≤10ms）

SESSION = FAKE_SESSION  # 占位会话标识（conftest），不触碰任何真实数据


def deterministic_unit(text: str, dim: int = DIM) -> np.ndarray:
    """按文本 md5 生成确定性伪随机单位向量（与 conftest.deterministic_vector 同手法）。"""
    seed = int.from_bytes(hashlib.md5(text.encode("utf-8")).digest()[:4], "little")
    rng = np.random.default_rng(seed)
    vec = rng.standard_normal(dim).astype(np.float32)
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm else vec


@pytest.fixture(scope="module")
def big_store(tmp_path_factory):
    """万条确定性语料的临时库（module 级：构建一次，本模块用例共享）。"""
    tmp: Path = tmp_path_factory.mktemp("mem_perf")
    store = MemoryStore(
        tmp / "perf.db",
        embedder=None,  # 向量全部预先算好随 remember 传入，无需嵌入回调
        provider_id="占位供应商",
        model="perf-embedding-model",
        endpoint="http://127.0.0.1:9",
        dim=DIM,
    )

    async def build() -> None:
        t0 = time.perf_counter()
        with store.atomic():
            for i in range(1, N_ROWS + 1):
                text = f"记忆条目{i}：关于主题{(i - 1) % 97}的确定性合成事实"
                vec = deterministic_unit(text)
                await store.remember(
                    text, SESSION, embedding=vec, create_time=1_700_000_000 + i,
                    commit=False,
                )
        build_seconds = time.perf_counter() - t0
        print(f"\n[perf] 万条语料构建 {build_seconds:.2f}s（{N_ROWS} 行 ×{DIM} 维）")

    asyncio.run(build())
    assert store.count(SESSION) == N_ROWS
    yield store
    store.close()


def test_wan_rows_cache_hit_p50_within_budget(big_store):
    """★ 验收断言：万条会话缓存命中检索 p50 ≤ 10ms（PERF_SPEC §5）。"""
    store = big_store

    async def run() -> tuple[float, float]:
        # 预热：首查触发矩阵重建（miss 路径，不计入命中口径）
        warm = await store.search(SESSION, "预热查询", query_vector=deterministic_unit("预热查询"))
        assert len(warm) == TOP_K           # top_k 语义不受缓存影响（O5 后默认 3）
        stats = store.cache_stats()
        assert stats["misses"] == 1 and stats["cached_sessions"] == 1

        latencies_ms: list[float] = []
        for i in range(ROUNDS):
            qtext = f"基准查询变体{i}"
            qvec = deterministic_unit(qtext)  # 确定性查询向量（免嵌入回调）
            t0 = time.perf_counter()
            hits = await store.search(SESSION, qtext, query_vector=qvec)
            latencies_ms.append((time.perf_counter() - t0) * 1000.0)
            assert len(hits) == TOP_K
        p50 = sorted(latencies_ms)[ROUNDS // 2]
        return p50, max(latencies_ms)

    p50, worst = asyncio.run(run())
    stats = store.cache_stats()
    assert stats["hits"] >= ROUNDS          # 全程缓存命中（PERF_SPEC §3 O8 计数暴露）
    assert p50 <= P50_BUDGET_MS, (
        f"万条缓存命中 p50={p50:.2f}ms 超出预算 {P50_BUDGET_MS}ms"
        f"（最差 {worst:.2f}ms，hits={stats['hits']}）"
    )
    print(f"\n[perf] 万条命中检索 p50={p50:.2f}ms / max={worst:.2f}ms"
          f"（预算 {P50_BUDGET_MS}ms，{ROUNDS} 次采样）")


def test_wan_rows_cache_hit_results_stable(big_store):
    """缓存命中与强制重建的检索结果一致（缓存不改变检索语义）。"""
    store = big_store
    qvec = deterministic_unit("一致性核对查询")

    async def run():
        cached = await store.search(SESSION, "一致性核对查询", query_vector=qvec)
        store._invalidate_matrix()          # 失效点等价（与 remember 执行同函数）
        rebuilt = await store.search(SESSION, "一致性核对查询", query_vector=qvec)
        return cached, rebuilt

    cached, rebuilt = asyncio.run(run())
    assert cached == rebuilt                # MemoryHit 逐字段相等
    assert [h.id for h in cached]           # 万条语料非空，必有命中
