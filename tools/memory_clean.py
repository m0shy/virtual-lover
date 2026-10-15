# -*- coding: utf-8 -*-
"""memory_clean.py —— 记忆存量性别漂移修正（五轮修缮 R3）·仓库根启动入口。

实现本体在包内（可导入可测试，与 config_migrate/history_migrate 同居）：
    virtuallover/tools/memory_clean.py

两种等价用法（仓库根执行）：
    python tools/memory_clean.py                        # 演练：只打印 diff
    python tools/memory_clean.py --apply                # 备份后写库
    python -m virtuallover.tools.memory_clean --apply   # 同上（包入口）

本文件只是薄启动器，零业务逻辑；只依赖标准库。
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from virtuallover.tools.memory_clean import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
