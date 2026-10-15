# -*- coding: utf-8 -*-
"""语音子包（VOICE_SPEC §1/§2）：GPT-SoVITS 客户端 + 语音引擎栈 + 管线门面。

- gsvi.py：GSVI HTTP 客户端（/tts 合成 + /character_list 注册表缓存 + TCP 探测）；
- cache.py：磁盘 LRU 语音缓存；mapping.py：7 态情绪映射回退链；
- lifecycle.py：GSVI 进程状态机（adopt-first/熔断/PID 核验）；
- engine.py：VoiceEngine —— §1 组合体（全局单飞 synthesize），挂 ctx.voice_engine；
- manager.py：VoiceManager —— §2 管线门面（每用户队列/双轨），挂 ctx.voice_manager；
- gptsovits.py：SPEC §9 时代旧名兼容 shim（GptSoVitsClient = GsviClient）。
"""

from virtuallover.voice.gptsovits import GptSoVitsClient
from virtuallover.voice.gsvi import GsviClient
from virtuallover.voice.manager import VoiceManager

__all__ = ["GptSoVitsClient", "GsviClient", "VoiceManager"]
