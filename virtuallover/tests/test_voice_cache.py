# -*- coding: utf-8 -*-
"""voice/cache.py 契约测试（VOICE_SPEC §1/§4）。

覆盖：归一化与 GSVI 服务端等价、键含全部请求参数且排序稳定、命中单请求字节
相等、LRU 双上限淘汰（条数/字节）、坏 RIFF 魔数当 miss 并删除、原子写无 tmp
残留、启动扫描重建索引、非 wav 拒绝入缓存。

不引入 pytest-asyncio（依赖仅 websockets+numpy）：缓存是同步对象，直测。

运行：python -m pytest virtuallover/tests/test_voice_cache.py -q
"""

from __future__ import annotations

import os

import pytest

from virtuallover.voice.cache import VoiceCache, normalize_text

WAV = b"RIFF" + b"\x00" * 36 + b"WAVEfmt " + b"\x00" * 16 + b"data" + b"\x00" * 8


def _key(cache: VoiceCache, text: str, emotion: str = "平静的", **kw) -> str:
    params = dict(character="Huohua", emotion=emotion,
                  text_language="zh", audio_format="wav", text=text)
    params.update(kw)
    return cache.make_key(**params)


# ---------------------------------------------------------------------------
# 文本归一化（与服务端链逐字一致）
# ---------------------------------------------------------------------------


class TestNormalizeText:
    def test_ellipsis_to_sentence_mark(self):
        # GSVI 服务端：……/… → 。（tts_backend.py:175）
        assert normalize_text("你好……再见") == "你好。再见"
        assert normalize_text("你好…再见") == "你好。再见"

    def test_newline_to_sentence_newline(self):
        assert normalize_text("你好\n再见") == "你好。\n再见"
        assert normalize_text("你好。\n再见") == "你好。\n再见"  # 。\n 往返恒定
        assert normalize_text("你好\n\n再见") == "你好。\n再见"  # \n\n 先折叠

    def test_equivalent_texts_share_key(self):
        """归一化等价的两段文本必须命中同一缓存键（键等价类=服务端等价类）。"""
        cache = VoiceCache(_tmpdir())
        assert _key(cache, "你好……再见") == _key(cache, "你好。再见")
        assert _key(cache, "你好\n再见") == _key(cache, "你好。\n再见")


def _tmpdir() -> str:
    import tempfile

    return tempfile.mkdtemp(prefix="vl_cache_test_")


# ---------------------------------------------------------------------------
# 键构造
# ---------------------------------------------------------------------------


class TestMakeKey:
    def test_key_differs_by_each_param(self):
        cache = VoiceCache(_tmpdir())
        base = _key(cache, "你好")
        assert base != _key(cache, "你好", emotion="聊天")
        assert base != _key(cache, "你好", character="Hutao")
        assert base != _key(cache, "你好", text_language="en")
        assert base != _key(cache, "你好", audio_format="mp3")
        assert base != _key(cache, "再见")

    def test_key_is_stable_sha1_hex(self):
        cache = VoiceCache(_tmpdir())
        k = _key(cache, "稳定键")
        assert k == _key(cache, "稳定键")
        assert len(k) == 40 and all(c in "0123456789abcdef" for c in k)


# ---------------------------------------------------------------------------
# 读写与校验
# ---------------------------------------------------------------------------


class TestGetPut:
    def test_roundtrip_bytes_equal(self, tmp_path):
        cache = VoiceCache(tmp_path)
        key = _key(cache, "你好呀")
        cache.put(key, WAV)
        assert cache.get(key) == WAV  # 命中单请求字节相等（§4）

    def test_miss_returns_none(self, tmp_path):
        cache = VoiceCache(tmp_path)
        assert cache.get("0" * 40) is None

    def test_bad_magic_treated_as_miss_and_removed(self, tmp_path):
        """半截写/磁盘损坏（RIFF 魔数不符）→ 当 miss 并就地删除（§4）。"""
        cache = VoiceCache(tmp_path)
        key = _key(cache, "坏条目")
        path = tmp_path / f"{key}.wav"
        path.write_bytes(b"NOTWAV-not-a-real-file")
        assert cache.get(key) is None
        assert not path.exists()  # 坏条目已删除，下次请求走重合成

    def test_put_rejects_non_wav(self, tmp_path):
        cache = VoiceCache(tmp_path)
        with pytest.raises(ValueError):
            cache.put(_key(cache, "非 wav"), b'{"error": "nope"}')

    def test_no_tmp_leftover_after_put(self, tmp_path):
        """原子写：tmp + os.replace，落盘后目录内不留 .tmp。"""
        cache = VoiceCache(tmp_path)
        cache.put(_key(cache, "原子写"), WAV)
        assert list(tmp_path.glob("*.tmp")) == []

    def test_stats(self, tmp_path):
        cache = VoiceCache(tmp_path)
        cache.put(_key(cache, "统计"), WAV)
        stats = cache.stats()
        assert stats["entries"] == 1 and stats["bytes"] == len(WAV)
        assert stats["max_entries"] == 500 and stats["max_bytes"] == 128 * 1024 * 1024


# ---------------------------------------------------------------------------
# 双上限 LRU
# ---------------------------------------------------------------------------


class TestLruEviction:
    def test_evict_by_entries(self, tmp_path):
        cache = VoiceCache(tmp_path, max_entries=3)
        keys = [_key(cache, f"第{i}条") for i in range(4)]
        for i, key in enumerate(keys):
            cache.put(key, WAV)
            cache.get(key)  # 触摸，保持全部"最近使用"
        # 第 4 条放入后，最久未用的第 1 条被淘汰
        assert cache.get(keys[0]) is None
        assert cache.get(keys[3]) == WAV
        assert cache.stats()["entries"] == 3

    def test_evict_by_bytes(self, tmp_path):
        cache = VoiceCache(tmp_path, max_bytes=len(WAV) * 2 + 1)
        k1, k2, k3 = _key(cache, "甲"), _key(cache, "乙"), _key(cache, "丙")
        cache.put(k1, WAV)
        cache.put(k2, WAV)
        assert cache.stats()["bytes"] <= len(WAV) * 2 + 1
        cache.put(k3, WAV)  # 超字节上限：最旧的 k1 出
        assert cache.get(k1) is None
        assert cache.get(k3) == WAV

    def test_oversized_single_entry_dropped(self, tmp_path):
        """单条超过字节上限：丢弃不缓存（否则淘汰环永不收敛）。"""
        cache = VoiceCache(tmp_path, max_bytes=10)
        key = _key(cache, "超大")
        cache.put(key, WAV)
        assert cache.get(key) is None
        assert cache.stats()["entries"] == 0


# ---------------------------------------------------------------------------
# 启动扫描重建索引
# ---------------------------------------------------------------------------


class TestStartupScan:
    def test_scan_rebuilds_index(self, tmp_path):
        first = VoiceCache(tmp_path)
        k1, k2 = _key(first, "重启前甲"), _key(first, "重启前乙")
        first.put(k1, WAV)
        first.put(k2, WAV)
        # 新实例（模拟框架重启）：目录即索引，无需独立索引文件
        second = VoiceCache(tmp_path)
        assert second.stats()["entries"] == 2
        assert second.get(k1) == WAV and second.get(k2) == WAV

    def test_scan_deletes_corrupted_and_tmp(self, tmp_path):
        first = VoiceCache(tmp_path)
        good = _key(first, "好条目")
        first.put(good, WAV)
        (tmp_path / ("f" * 40 + ".wav")).write_bytes(b"BAD-not-wav")
        (tmp_path / "deadbeef.tmp").write_bytes(b"half-written")
        second = VoiceCache(tmp_path)
        assert second.stats()["entries"] == 1  # 坏条目剔除
        assert second.get(good) == WAV
        assert list(tmp_path.glob("*.tmp")) == []  # tmp 残留清理

    def test_scan_respects_shrunk_limits(self, tmp_path):
        """上限收紧后重启：扫描即收敛到新上限。"""
        first = VoiceCache(tmp_path, max_entries=10)
        keys = [_key(first, f"条目{i}") for i in range(5)]
        for key in keys:
            first.put(key, WAV)
        second = VoiceCache(tmp_path, max_entries=2)
        assert second.stats()["entries"] == 2

    def test_missing_directory_is_empty(self, tmp_path):
        cache = VoiceCache(tmp_path / "不存在")
        assert cache.stats()["entries"] == 0

    def test_lru_order_survives_restart_via_mtime(self, tmp_path):
        """get 触摸 mtime：重启后扫描序仍近似 LRU（旧条目先淘汰）。"""
        first = VoiceCache(tmp_path, max_entries=5)
        old, new = _key(first, "旧"), _key(first, "新")
        first.put(old, WAV)
        # 人为把 old 的 mtime 拨早 10s，再放 new
        stamp = os.stat(tmp_path / f"{old}.wav").st_mtime - 10
        os.utime(tmp_path / f"{old}.wav", (stamp, stamp))
        first.put(new, WAV)
        second = VoiceCache(tmp_path, max_entries=1)
        assert second.get(new) == WAV      # 保留最近使用的
        assert second.get(old) is None     # 最旧的被淘汰
