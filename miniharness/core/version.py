"""单一版本来源：`miniharness.__version__` 由此导入。

pyproject 的 dynamic version 读 `miniharness.__version__`，本模块是其唯一落点
（web `host.describe` 也经此读版本，避免导入顶层教学面）。
"""

__version__ = "0.2.0"

__all__ = ["__version__"]