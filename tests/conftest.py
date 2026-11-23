"""pytest 路径配置：把仓库根的 src 目录插入 sys.path。

仓库采用 src 布局，且不要求先安装包；这里只做路径准备，不引入第三方插件。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
