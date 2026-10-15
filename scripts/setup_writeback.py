# -*- coding: utf-8 -*-
r"""setup.bat v2 调用的薄入口：主体在 virtuallover/tools/setup_writeback.py（可测）。

用法（setup.bat v2 第 5 步）：
    set VL_SETUP_API_KEY=<交互收集的 Key>
    set VL_SETUP_QQ=<交互收集的机器人 QQ 号>
    set VL_SETUP_OWNER_QQ=<可选：主人常用 QQ；缺省按宽松模式机器人号兼当主人>
    set VL_SETUP_ROOT=<运行根>
    .venv\Scripts\python.exe scripts\setup_writeback.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from virtuallover.tools.setup_writeback import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
