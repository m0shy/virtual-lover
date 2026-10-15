# -*- coding: utf-8 -*-
"""语音引擎启动 wrapper（vl 自有入口，R8）。

为什么要有这个 wrapper（而不是直接跑上游 Inference/src/tts_backend.py）：

1. **网络安全**：上游脚本只能在 ``__main__`` 里启动，且硬编码
   ``app.run(host='0.0.0.0', port=tts_port)``（tts_backend.py:224）——监听所有
   网卡，同一局域网内的设备可调用你训练的音色合成任意文本。wrapper 改为
   ``app.run(host='127.0.0.1')`` 只绑回环，不动上游源码一行；
2. **进程命名中性化**：进程命令行从 ``...python.exe Inference/src/tts_backend.py``
   变为 ``...python.exe scripts/voice_engine_entry.py``——生命周期管理器的
   击杀核验标记（voice/lifecycle.py ``_CMDLINE_MARKER``）与 ``一键停止.bat``
   看护（scripts/service.py ``gsvi_stop``）据此同步。

cwd 契约（不可省）：上游 tts_backend.py 以 ``os.getcwd()`` 拼接 sys.path
（模块头 6-8 行，``GPT_SoVITS/TTS_infer_pack`` 只在引擎根下可导入），故本
wrapper 必须以**引擎根为 cwd** 启动——voice/lifecycle.py 的 ``_spawn`` 已固定
``cwd=引擎根（vl.toml [tts].gsvi_root）``。本文件自身不依赖所在目录，放在
scripts/（vl 自有代码区）只是为了随仓库入库。

运行方式：``<引擎根>/runtime/python.exe <仓库根>/scripts/voice_engine_entry.py``
（cwd=引擎根）。日常用户无需手动运行——``tts.auto_start=true`` 时框架自动拉起。
"""

from __future__ import annotations

import os
import sys


def _fail(msg: str):
    print(f"[voice-engine] 启动失败：{msg}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    cwd = os.getcwd()
    src_dir = os.path.join(cwd, "Inference", "src")
    backend = os.path.join(src_dir, "tts_backend.py")
    # cwd 必须是引擎根：tts_backend.py 不在位即说明 cwd 不对（直接给中文诊断，
    # 不留一屏裸 traceback）
    if not os.path.isfile(backend):
        _fail(
            f"当前目录 {cwd} 下找不到 Inference/src/tts_backend.py。"
            "请以语音引擎根目录为工作目录运行本脚本"
            "（框架自动拉起路径会固定 cwd=[tts].gsvi_root，无需手动处理）"
        )
    # tts_backend import 时以 os.getcwd() 补 GPT_SoVITS 路径，但其自身目录要
    # 由我们提前塞进 sys.path 才能被 import
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    import tts_backend  # noqa: E402 —— 上游模块，import 即完成全部路由/加载逻辑

    port = int(getattr(tts_backend, "tts_port", 5000))
    # host 与上游 __main__ 补丁同源（tts_backend 模块级读 Inference/config.json
    # 的 tts_host，缺省 127.0.0.1）——单机部署语音引擎永不监听 0.0.0.0
    host = str(getattr(tts_backend, "tts_host", "127.0.0.1") or "127.0.0.1")
    tts_backend.app.run(host=host, port=port)


if __name__ == "__main__":
    main()
