# -*- coding: utf-8 -*-
"""兼容 shim：SPEC §9 时代的 GPT-SoVITS 客户端已改造为 :mod:`virtuallover.voice.gsvi`。

VOICE_SPEC §1 把该模块重组为 gsvi.py（+character_list 注册表缓存、TCP 探测），
本文件仅为旧引用（tests/test_gptsovits.py、文档）保留 ``GptSoVitsClient`` 名字——
同 :class:`virtuallover.voice.gsvi.GsviClient` 的别名，行为契约（GET /tts、
别名参数、3 次重试、60s、wav 校验、失败回退 None）不变。装配入口移至
:mod:`virtuallover.voice.manager`（全局单飞锁 + 缓存 + 生命周期）。
"""

from __future__ import annotations

from virtuallover.voice.gsvi import GsviClient

__all__ = ["GptSoVitsClient", "GsviClient"]

# 旧名别名（SPEC §9 契约不变：synthesize(text) -> wav | None）
GptSoVitsClient = GsviClient
