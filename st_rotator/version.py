"""版本号单独放一个模块。

为什么不写在 ``__init__.py``：``ui.py`` 需要版本号，而 ``ui.py`` 会被 ``proxy.py``
导入、``proxy.py`` 又被 ``__init__.py`` 导入。如果版本号定义在 ``__init__.py`` 里，
就得依赖"赋值语句必须排在 import 之前"这种隐式顺序，很容易被后人改坏。单独一个模块
最省心。
"""

from __future__ import annotations

__version__ = "1.3.0"
