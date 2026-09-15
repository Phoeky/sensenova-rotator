"""主动节流器：在本地把请求"摊平"，减少撞 429 的概率。

比"撞到 429 再退避"更划算——429 本身也要消耗配额和时间。

两个实现：

* ``RateLimiter``          —— 固定速率，适合已经摸清配额上限的场景。
* ``AdaptiveRateLimiter``  —— AIMD 自适应，适合"隐藏动态限流"（商汤就是这种）。
"""

from __future__ import annotations

import threading
import time
from typing import Callable


class RateLimiter:
    """匀速放行器：任意相邻两次放行间隔 >= 1/rate 秒。

    采用"预订下一个时间片"的实现，多线程下不会出现惊群或超发。

    ``rate <= 0`` 表示不限速。
    """

    __slots__ = ("rate", "_interval", "_lock", "_next", "_clock", "_sleeper")

    def __init__(
        self,
        rate: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rate = float(rate)
        self._interval = 1.0 / self.rate if self.rate > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0
        self._clock = clock
        self._sleeper = sleeper

    @property
    def enabled(self) -> bool:
        return self._interval > 0

    def acquire(self) -> float:
        """阻塞到可以发请求为止，返回实际等待的秒数。"""
        if self._interval <= 0:
            return 0.0
        with self._lock:
            now = self._clock()
            wait = max(0.0, self._next - now)
            self._next = max(now, self._next) + self._interval
        if wait > 0:
            self._sleeper(wait)
        return wait

    # 固定限速器没有反馈回路，提供空实现让调用方可以统一调用
    def on_success(self) -> None:
        """记录一次成功（固定限速器不需要）。"""

    def on_rate_limited(self) -> None:
        """记录一次 429（固定限速器不需要）。"""

    def stats(self) -> dict[str, float | int | str]:
        return {"mode": "fixed", "rate": round(self.rate, 3)}


class AdaptiveRateLimiter:
    """AIMD 自适应限速器：成功时缓慢提速，撞 429 时快速降速。

    为什么需要它：商汤的限流是"隐藏的动态策略"，人工试出来的固定值会在服务端
    策略变化后失效（白天和半夜的阈值可能都不一样）。与其反复手调，不如让工具
    自己找平衡点——思路直接借用 TCP 拥塞控制：

    * **撞到 429** → ``rate *= decrease``（快速退让，别把配额打爆）
    * **持续 ``recovery_seconds`` 没有 429** → ``rate += increase_step``（缓慢试探上限）

    这样稳态会收敛到"刚好不出 429"的速率附近，而且能自动跟着服务端策略漂移。

    **参数调平很重要**（实测教训）：默认 `decrease=0.6` + `recovery_seconds=20` 在商汤这个
    环境下会失衡——12 次降速只对应 6 次提速，速率被一路砸到下限，吞吐反而比固定限速更差。
    原因是 429 密集时几乎没有连续 20 秒的"干净窗口"，提速永远触发不了。
    所以默认值调成了 `decrease=0.85` + `recovery_seconds=8`：降得更温和、恢复更及时。
    调参口诀：**降速要温和，恢复要及时，下限别太低**。

    Args:
        rate: 初始速率（每秒请求数）。
        min_rate: 速率下限，避免降到几乎不可用。别设太低，否则一旦触底就爬不回来。
        max_rate: 速率上限，避免探过头把 Key 打到冷却。
        decrease: 撞 429 时的乘性衰减系数，取值 (0, 1)。越接近 1 越温和。
        increase_step: 恢复期的加性增量。
        recovery_seconds: 距离上次 429 多久才允许提速。太长会导致触底爬不回来。
    """

    def __init__(
        self,
        rate: float,
        *,
        min_rate: float = 0.15,
        max_rate: float = 5.0,
        decrease: float = 0.85,
        increase_step: float = 0.05,
        recovery_seconds: float = 8.0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._min = max(float(min_rate), 1e-6)
        self._max = max(float(max_rate), self._min)
        self._rate = min(max(float(rate), self._min), self._max)
        self._decrease = min(max(float(decrease), 0.1), 0.99)
        self._increase_step = max(float(increase_step), 0.0)
        self._recovery = max(float(recovery_seconds), 0.0)
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._next = 0.0
        self._last_penalty = float("-inf")  # 从未被限流过
        self._last_raise = float("-inf")
        self._penalties = 0
        self._raises = 0
        self._successes = 0
        self._history: list[tuple[float, float]] = []  # (时刻, 速率) 便于排查

    # ------------------------------------------------------------ 只读

    @property
    def rate(self) -> float:
        with self._lock:
            return self._rate

    @property
    def enabled(self) -> bool:
        return True

    # ------------------------------------------------------------ 放行

    def acquire(self) -> float:
        """按当前速率阻塞放行，返回实际等待的秒数。"""
        with self._lock:
            interval = 1.0 / self._rate
            now = self._clock()
            wait = max(0.0, self._next - now)
            self._next = max(now, self._next) + interval
        if wait > 0:
            self._sleeper(wait)
        return wait

    # ------------------------------------------------------------ 反馈回路

    def on_success(self) -> None:
        """一次成功：若已"干净"足够久，小幅提速试探上限。"""
        with self._lock:
            self._successes += 1
            if self._rate >= self._max:
                return
            now = self._clock()
            if now - self._last_penalty < self._recovery:
                return  # 刚被限流过，先稳住
            if now - self._last_raise < self._recovery:
                return  # 提速别太频繁
            self._rate = min(self._max, self._rate + self._increase_step)
            self._last_raise = now
            self._raises += 1
            self._record(now)

    def on_rate_limited(self) -> float:
        """一次 429：乘性降速，返回降速后的速率。"""
        with self._lock:
            self._penalties += 1
            now = self._clock()
            self._last_penalty = now
            self._rate = max(self._min, self._rate * self._decrease)
            self._record(now)
            return self._rate

    def _record(self, now: float) -> None:
        # 只保留最近 200 次调整，避免长期运行吃内存
        self._history.append((round(now, 3), round(self._rate, 4)))
        if len(self._history) > 200:
            del self._history[:100]

    # ------------------------------------------------------------ 观测

    def stats(self) -> dict[str, float | int | str | list]:
        with self._lock:
            return {
                "mode": "adaptive",
                "rate": round(self._rate, 3),
                "min_rate": self._min,
                "max_rate": self._max,
                "successes": self._successes,
                "penalties": self._penalties,
                "raises": self._raises,
                "history": list(self._history),
            }
