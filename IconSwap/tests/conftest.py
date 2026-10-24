"""测试全局配置。

项目采用 ``src/`` 布局：测试代码通过 ``sys.path`` 指向 ``src/`` 后
``import iconswap.*``。测试过程不写任何持久化图标目录（该功能已移除），
临时文件统一放在 ``tests/_tmp``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for entry in (str(SRC), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

# 让 tempfile / 测试临时目录落在仓库内，避免污染系统 %TEMP%
os.environ.setdefault("PYTEST_TMP_ROOT", str(ROOT / "tests" / "_tmp"))
