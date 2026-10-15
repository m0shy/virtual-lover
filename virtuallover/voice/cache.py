# -*- coding: utf-8 -*-
"""语音产物磁盘缓存（VOICE_SPEC §1）。

契约逐条：
- **键 = sha1(排序后全部请求参数)**：character | emotion | text_language | format |
  归一化 text | speed（可选维度）全部参与；参数按名排序后 urlencode 成规范串再
  sha1（稳定、可复算、文件名安全）；speed 按两位小数字符串参与（1.10 与 1.0999
  同键）——不同语速不共缓存；speed=None（旧调用方/不限语速）不参与，键与既有
  条目完全兼容；
- **文本归一化与 GSVI 服务端逐字同链**：``normalize_text`` 复刻
  voice-engine/Inference/src/tts_backend.py:175 的 text 预处理
  （……→。、…→。、\\n\\n→\\n、。\\n→\\n、\\n→。\\n）——服务端对同参数同文本的
  两次请求等价 ⟺ 本缓存判等价，键的等价类与服务端行为一致；
- **双上限 LRU**：条数 500 + 字节 128MB（vl.toml [tts] 可改），任一超限按最近
  最少使用淘汰；内存序用 OrderedDict，落盘序用文件 mtime（get 命中即
  os.utime 触摸，重启后扫描仍能还原最近使用序）；
- **原子写**：tmp 文件 + ``os.replace``（同目录原子替换，与 storage/mood 的
  落盘模式一致）；非 RIFF 数据拒绝入缓存；
- **RIFF 魔数校验**：读回时校验 b"RIFF" 帧头，坏条目（半截写/磁盘损坏）当 miss
  并就地删除——下次请求重合成；
- **启动扫描重建索引**：目录内 ``<40位sha1>.wav`` 全量扫描（校验魔数、清 tmp
  残留），无独立索引文件——目录即索引。

线程约定：缓存操作只发生在 VoiceManager 的事件循环内（全局单飞锁保证串行），
不做额外加锁。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import urllib.parse
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Optional

__all__ = ["VoiceCache", "normalize_text", "WAV_MAGIC",
           "DEFAULT_MAX_ENTRIES", "DEFAULT_MAX_BYTES"]

logger = logging.getLogger("virtuallover.voice.cache")

WAV_MAGIC = b"RIFF"
DEFAULT_MAX_ENTRIES = 500                 # VOICE_SPEC §1：条数上限
DEFAULT_MAX_BYTES = 128 * 1024 * 1024     # VOICE_SPEC §1：字节上限 128MB

# 合法缓存文件名：<40位 sha1 hex>.wav（其余文件一律视为垃圾/残留）
_KEY_FILE_RE = re.compile(r"^([0-9a-f]{40})\.wav$")


def normalize_text(text: str) -> str:
    """文本归一化：与 GSVI /tts 服务端的 text 预处理链逐字一致。

    出处：voice-engine/Inference/src/tts_backend.py:175
    ``text.replace("……","。").replace("…","。").replace("\\n\\n","\\n")
    .replace("。\\n","\\n").replace("\\n","。\\n")``
    （VOICE_SPEC §1 概述为「……→。、\\n→。\\n」，此处按服务端全链实现，
    保证缓存键等价类与服务端真实行为等价）。
    """
    return (
        text.replace("……", "。")
        .replace("…", "。")
        .replace("\n\n", "\n")
        .replace("。\n", "\n")
        .replace("\n", "。\n")
    )


class VoiceCache:
    """磁盘 LRU 缓存：get/put + 双上限淘汰 + 启动扫描。"""

    def __init__(
        self,
        directory: "str | Path",
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        clock: "Callable[[], float]" = time.time,
    ) -> None:
        self.directory = Path(directory)
        if int(max_entries) < 1 or int(max_bytes) < 1:
            raise ValueError("缓存上限必须为正（max_entries/max_bytes）")
        self.max_entries = int(max_entries)
        self.max_bytes = int(max_bytes)
        self._clock = clock
        # key → 字节数；OrderedDict 尾部=最近使用
        self._entries: "OrderedDict[str, int]" = OrderedDict()
        self._bytes = 0
        self._scan()

    # ------------------------------------------------------------------ 键

    def make_key(
        self,
        *,
        character: str,
        emotion: str,
        text_language: str,
        audio_format: str,
        text: str,
        speed: "Optional[float]" = None,
    ) -> str:
        """sha1(排序后全部请求参数)。文本先经 normalize_text 归一化。

        speed：语速倍率维度（mapping.mood_speed 产出，可选）。给定时按两位
        小数字符串参与键——不同语速不共缓存；None 不参与（键与既有条目兼容）。
        """
        params = {
            "character": str(character),
            "emotion": str(emotion),
            "text_language": str(text_language),
            "format": str(audio_format),
            "text": normalize_text(str(text)),
        }
        if speed is not None:
            params["speed"] = f"{float(speed):.2f}"  # 两位小数：等价类稳定
        canonical = urllib.parse.urlencode(sorted(params.items()))
        return hashlib.sha1(canonical.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ 读写

    def get(self, key: str) -> "Optional[bytes]":
        """命中返回 wav 字节（并触摸 LRU 序）；未命中/坏条目返回 None。

        坏条目（RIFF 魔数不符）就地删除当 miss——下次请求走重合成。
        """
        path = self._path(key)
        try:
            if not path.is_file():
                return None
            with open(path, "rb") as fh:
                data = fh.read()
            if not data.startswith(WAV_MAGIC):
                raise ValueError("RIFF 魔数不符")
            if len(data) <= 44:
                raise ValueError("条目过短（不足 wav 帧头）")
        except (OSError, ValueError) as exc:
            logger.warning("[voice_cache] 条目损坏，按 miss 处理并删除 %s：%s", key, exc)
            self._drop(key)
            return None
        # 命中：内存序 + 落盘 mtime 双触摸（重启后扫描序仍近似 LRU）
        self._entries.move_to_end(key)
        try:
            stamp = self._clock()
            os.utime(path, (stamp, stamp))
        except OSError:
            pass  # 触摸失败不影响命中
        return data

    def put(self, key: str, data: bytes) -> None:
        """原子写入并按双上限淘汰；非 RIFF 数据拒绝入缓存（防御性校验）。

        单条超过字节上限时：先淘汰全部旧条目仍装不下 → 丢弃本条（只告警，
        不影响合成主流程）。
        """
        if not data or not data.startswith(WAV_MAGIC):
            raise ValueError("拒绝缓存非 wav（缺少 RIFF 帧头）数据")
        path = self._path(key)
        tmp = self.directory / f"{key}.tmp"
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)  # 同目录原子替换
        except OSError as exc:
            logger.error("[voice_cache] 写入失败（不影响本次合成返回）：%s", exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return
        self._record(key, len(data))
        self._evict()

    def stats(self) -> dict:
        """缓存面数据（控制台状态卡用）：条数/字节/上限。"""
        return {
            "entries": len(self._entries),
            "bytes": int(self._bytes),
            "max_entries": self.max_entries,
            "max_bytes": self.max_bytes,
            "directory": str(self.directory),
        }

    # ------------------------------------------------------------------ 内部

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.wav"

    def _record(self, key: str, size: int) -> None:
        """登记一条（幂等：重复 put 同 key 先撤旧账再记新账，置于最近端）。"""
        self._drop_memory(key)
        self._entries[key] = size
        self._bytes += size

    def _drop_memory(self, key: str) -> None:
        size = self._entries.pop(key, None)
        if size is not None:
            self._bytes -= size

    def _drop(self, key: str) -> None:
        """内存登记 + 磁盘文件一并移除。"""
        self._drop_memory(key)
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("[voice_cache] 删除损坏条目失败 %s：%s", key, exc)

    def _evict(self) -> None:
        """双上限淘汰：条数与字节任一超限即从最旧端出（保底保留 1 条）。"""
        while len(self._entries) > 1 and (
            len(self._entries) > self.max_entries or self._bytes > self.max_bytes
        ):
            victim, size = self._entries.popitem(last=False)
            try:
                self._path(victim).unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("[voice_cache] 淘汰删除失败 %s：%s", victim, exc)
            self._bytes -= size
        # 仅剩 1 条仍超字节上限 = 单条本身超限：丢弃（拒绝缓存超大产物）
        if len(self._entries) == 1 and self._bytes > self.max_bytes:
            only, size = next(iter(self._entries.items()))
            logger.warning(
                "[voice_cache] 单条 %d 字节超过字节上限 %d，丢弃不缓存",
                size, self.max_bytes,
            )
            self._drop(only)

    def _scan(self) -> None:
        """启动扫描重建索引：合法 <sha1>.wav 按 mtime 升序装入（旧→新），
        校验 RIFF 魔数（坏的删除），清理 .tmp 残留。目录不存在即空表。"""
        self._entries = OrderedDict()
        self._bytes = 0
        if not self.directory.is_dir():
            return
        stale_tmp: "list[Path]" = []
        found: "list[tuple[float, str, int]]" = []
        try:
            names = list(os.scandir(self.directory))
        except OSError as exc:
            logger.error("[voice_cache] 启动扫描失败（空表继续）：%s", exc)
            return
        for entry in names:
            if not entry.is_file():
                continue
            match = _KEY_FILE_RE.match(entry.name)
            if match is None:
                if entry.name.endswith(".tmp"):
                    stale_tmp.append(Path(entry.path))
                continue
            path = Path(entry.path)
            try:
                stat = path.stat()
                with open(path, "rb") as fh:
                    if fh.read(4) != WAV_MAGIC:
                        raise ValueError("RIFF 魔数不符")
                found.append((stat.st_mtime, match.group(1), stat.st_size))
            except (OSError, ValueError) as exc:
                logger.warning("[voice_cache] 扫描发现损坏条目 %s，已删除：%s",
                               entry.name, exc)
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        found.sort()  # mtime 升序 = 最旧在前（LRU 淘汰端）
        for _mtime, key, size in found:
            self._entries[key] = size
            self._bytes += size
        for tmp in stale_tmp:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        if self._entries:
            logger.info("[voice_cache] 启动扫描完成：%d 条 / %d 字节（%s）",
                        len(self._entries), self._bytes, self.directory)
        self._evict()  # 上限收紧过的情况（配置改小）启动即收敛
