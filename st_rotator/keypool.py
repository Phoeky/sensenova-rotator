"""Key 池：多账户 / 多 Key 的轮换调度、冷却与统计。

设计要点
--------
1. 每把 Key 独立维护状态：健康 / 冷却中 / 已失效，外加最近 60s 的请求时间窗，
   用于本地 RPM 预限流（主动避开而不是被动挨打）。
2. ``acquire()`` 返回一把被"占用"的 Key，必须在 ``finally`` 里 ``release()``；
   所有等待都挂在 ``Condition`` 上，Key 一恢复可用立刻唤醒，不做无谓轮询。
3. 429 走指数退避 + 抖动冷却；401/403 直接标记失效并从轮换集合中排除，避免在
   坏 Key 上反复空转；5xx / 网络超时只做短冷却（不是 Key 的错，也不累计退避）。
4. 全程持锁时间极短，多线程下安全。异步场景可用 ``asyncio.to_thread`` 包裹。
"""

from __future__ import annotations

import contextlib
import hashlib
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Iterator, Sequence

from .config import AccountConfig, CooldownConfig
from .errors import AllKeysInvalid, ConfigError, NoAvailableKey


class KeyStatus(str, Enum):
    """Key 的可用性状态。"""

    HEALTHY = "healthy"      # 可用
    COOLDOWN = "cooldown"    # 临时冷却（429 / 5xx）
    INVALID = "invalid"      # 凭据失效（401/403）


def mask_key(key: str) -> str:
    """脱敏展示，日志里永远不要出现完整 Key，但要能区分是哪一把。"""
    if not key:
        return "***"
    length = len(key)
    if length <= 6:
        return key[0] + "*" * (length - 1)
    if length <= 14:
        # 短 Key：保留首尾各 2 位，中间打码
        return f"{key[:2]}{'*' * (length - 4)}{key[-2:]}"
    return f"{key[:6]}...{key[-4:]}"


def key_id(key: str) -> str:
    """给一把 Key 生成稳定短标识（sha256 前 12 位）。

    为什么需要它：控制台只拿到**脱敏后**的 Key，而脱敏值可能撞车（两把 Key 恰好首尾
    相同）。要精确地"测试/删除某一把"，就需要一个既能唯一定位、又不泄漏原文的标识。
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


@dataclass
class KeyStats:
    """单把 Key 的累计统计。"""

    requests: int = 0
    successes: int = 0
    failures: int = 0
    rate_limited: int = 0
    server_errors: int = 0
    client_errors: int = 0
    total_latency: float = 0.0

    @property
    def avg_latency(self) -> float:
        return self.total_latency / self.successes if self.successes else 0.0

    @property
    def success_rate(self) -> float:
        return self.successes / self.requests if self.requests else 1.0

    def to_dict(self) -> dict[str, float | int]:
        return {
            "requests": self.requests,
            "successes": self.successes,
            "failures": self.failures,
            "rate_limited": self.rate_limited,
            "server_errors": self.server_errors,
            "client_errors": self.client_errors,
            "success_rate": round(self.success_rate, 4),
            "avg_latency_ms": round(self.avg_latency * 1000, 1),
        }


class AccountState:
    """账号的运行时状态，聚合该账号下所有 Key 的配额与冷却联动。"""

    __slots__ = (
        "name", "rpm_limit", "max_concurrency", "weight",
        "inflight", "cooldown_until", "consecutive_failures",
        "last_error", "_window",
    )

    def __init__(
        self,
        name: str,
        *,
        rpm_limit: int | None = None,
        max_concurrency: int = 4,
        weight: float = 1.0,
    ) -> None:
        self.name = name
        self.rpm_limit = rpm_limit
        self.max_concurrency = max_concurrency
        self.weight = weight
        self.inflight = 0
        self.cooldown_until = 0.0
        self.consecutive_failures = 0
        self.last_error = ""
        self._window: deque[float] = deque()

    def _prune(self, now: float) -> None:
        cutoff = now - 60.0
        window = self._window
        while window and window[0] < cutoff:
            window.popleft()

    def rpm_blocked_until(self, now: float) -> float:
        if not self.rpm_limit:
            return 0.0
        self._prune(now)
        if len(self._window) < self.rpm_limit:
            return 0.0
        return self._window[len(self._window) - self.rpm_limit] + 60.0

    def available_at(self, now: float) -> float:
        return max(self.cooldown_until, self.rpm_blocked_until(now))

    def is_usable(self, now: float) -> bool:
        if self.inflight >= self.max_concurrency:
            return False
        return self.available_at(now) <= now


class ApiKey:
    """一把 Key 的运行时状态。"""

    __slots__ = (
        "key", "account", "rpm_limit", "max_concurrency", "weight", "tags",
        "status", "cooldown_until", "consecutive_failures", "inflight",
        "last_used", "last_error", "stats", "_window", "account_state",
    )

    def __init__(
        self,
        key: str,
        account: str,
        *,
        rpm_limit: int | None = None,
        max_concurrency: int = 4,
        weight: float = 1.0,
        tags: Sequence[str] = (),
        account_state: AccountState | None = None,
    ) -> None:
        self.key = key
        self.account = account
        self.rpm_limit = rpm_limit
        self.max_concurrency = max_concurrency
        self.weight = weight
        self.tags = list(tags)
        self.status = KeyStatus.HEALTHY
        self.cooldown_until = 0.0
        self.consecutive_failures = 0
        self.inflight = 0
        self.last_used = 0.0
        self.last_error = ""
        self.stats = KeyStats()
        self._window: deque[float] = deque()  # 最近 60s 内的发起时间
        self.account_state = account_state

    # ------------------------------------------------------------ 内部工具

    @property
    def masked(self) -> str:
        return mask_key(self.key)

    @property
    def key_id(self) -> str:
        return key_id(self.key)

    def _prune(self, now: float) -> None:
        cutoff = now - 60.0
        window = self._window
        while window and window[0] < cutoff:
            window.popleft()

    def rpm_blocked_until(self, now: float) -> float:
        """本地 RPM 窗口何时才会腾出额度（0 表示当前就有额度）。"""
        if self.account_state is not None:
            return self.account_state.rpm_blocked_until(now)
        if not self.rpm_limit:
            return 0.0
        self._prune(now)
        if len(self._window) < self.rpm_limit:
            return 0.0
        # 需要最早的那批请求滑出窗口，才能回到 limit-1
        return self._window[len(self._window) - self.rpm_limit] + 60.0

    def available_at(self, now: float) -> float:
        """最早可能恢复可用的时刻。"""
        at = max(self.cooldown_until, self.rpm_blocked_until(now))
        if self.account_state is not None:
            at = max(at, self.account_state.available_at(now))
        return at

    def is_usable(self, now: float, exclude: frozenset[str] = frozenset()) -> bool:
        if self.key in exclude:
            return False
        if self.status is KeyStatus.INVALID:
            return False
        if self.inflight >= self.max_concurrency:
            return False
        if self.account_state is not None and not self.account_state.is_usable(now):
            return False
        return self.available_at(now) <= now

    def to_dict(self, now: float) -> dict[str, object]:
        self._prune(now)
        cooldown = max(0.0, self.cooldown_until - now)
        if self.account_state is not None:
            cooldown = max(cooldown, self.account_state.cooldown_until - now)
            window_len = len(self.account_state._window)
            rpm_limit = self.account_state.rpm_limit or self.rpm_limit
        else:
            window_len = len(self._window)
            rpm_limit = self.rpm_limit
        return {
            "id": self.key_id,
            "account": self.account,
            "key": self.masked,
            "status": self.status.value,
            "cooldown_remaining": round(max(0.0, cooldown), 1),
            "inflight": self.inflight,
            "consecutive_failures": self.consecutive_failures,
            "rpm_window": f"{window_len}/{rpm_limit or '-'}",
            "last_error": self.last_error,
            "stats": self.stats.to_dict(),
        }


class KeyPool:
    """多账户 / 多 Key 的调度池。线程安全。"""

    def __init__(
        self,
        accounts: Sequence[AccountConfig],
        cooldown: CooldownConfig | None = None,
        *,
        strategy: str = "round_robin",
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self.cooldown = cooldown or CooldownConfig()
        self.strategy = strategy
        self._clock = clock
        self._rng = rng or random.Random()
        self._accounts: dict[str, AccountState] = {}
        self._keys: list[ApiKey] = []
        for account in accounts:
            acct_state = self._accounts.get(account.name)
            if acct_state is None:
                acct_state = AccountState(
                    account.name,
                    rpm_limit=account.rpm_limit,
                    max_concurrency=account.max_concurrency,
                    weight=account.weight,
                )
                self._accounts[account.name] = acct_state
            for raw in account.api_keys:
                self._keys.append(
                    ApiKey(
                        raw,
                        account.name,
                        rpm_limit=account.rpm_limit,
                        max_concurrency=account.max_concurrency,
                        weight=account.weight,
                        tags=account.tags,
                        account_state=acct_state,
                    )
                )
        if not self._keys:
            raise ConfigError("Key 池为空：请至少配置一个账号和一把 Key")
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._cursor = 0

    # ------------------------------------------------------------ 只读访问

    @property
    def keys(self) -> tuple[ApiKey, ...]:
        return tuple(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def find_key(self, key: str) -> ApiKey | None:
        """按明文查找（大小写敏感，Key 就是这样的）。"""
        for item in self._keys:
            if item.key == key:
                return item
        return None

    def find_by_id(self, identifier: str) -> ApiKey | None:
        """按明文或 ``key_id`` 查找，方便控制台用脱敏标识操作。"""
        found = self.find_key(identifier)
        if found is not None:
            return found
        for item in self._keys:
            if item.key_id == identifier:
                return item
        return None

    # ------------------------------------------------------------ 运行时增删

    def add_key(
        self,
        key: str,
        account: str = "default",
        *,
        rpm_limit: int | None = None,
        max_concurrency: int = 4,
        weight: float = 1.0,
        tags: Sequence[str] = (),
    ) -> ApiKey:
        """运行中加一把 Key，立刻参与轮换。

        Raises:
            ConfigError: Key 为空或已在池中。
        """
        key = (key or "").strip()
        if not key:
            raise ConfigError("api_key 不能为空")
        with self._cond:
            if any(item.key == key for item in self._keys):
                raise ConfigError("该 Key 已在池中，无需重复添加")
            acct_name = account or "default"
            acct_state = self._accounts.get(acct_name)
            if acct_state is None:
                acct_state = AccountState(
                    acct_name,
                    rpm_limit=rpm_limit,
                    max_concurrency=max_concurrency,
                    weight=weight,
                )
                self._accounts[acct_name] = acct_state
            item = ApiKey(
                key,
                acct_name,
                rpm_limit=rpm_limit,
                max_concurrency=max_concurrency,
                weight=weight,
                tags=tags,
                account_state=acct_state,
            )
            self._keys.append(item)
            self._cond.notify_all()
            return item

    def remove_key(self, key: str) -> ApiKey | None:
        """运行中移除一把 Key，返回被移除的对象（不存在则返回 None）。

        参数可以是 Key 明文，也可以是 ``key_id``（控制台只有脱敏值）。

        允许移除"正在处理请求"的 Key：在途请求持有的是它自己的 ``ApiKey`` 对象，
        释放时照常 ``release``，只是不再参与后续调度。所以这里不需要等它跑完。
        """
        with self._cond:
            for index, item in enumerate(self._keys):
                if item.key == key or item.key_id == key:
                    del self._keys[index]
                    if self._cursor >= len(self._keys):
                        self._cursor = 0
                    if item.account_state and not any(k.account_state is item.account_state for k in self._keys):
                        self._accounts.pop(item.account_state.name, None)
                    self._cond.notify_all()
                    return item
        return None

    # ------------------------------------------------------------ 获取 / 归还

    def acquire(
        self,
        *,
        exclude: Iterable[str] = (),
        timeout: float | None = None,
    ) -> ApiKey:
        """取一把可用 Key（已计入 inflight 与 RPM 窗口）。

        Args:
            exclude: 本轮不要使用的 Key 明文集合（例如刚被判失效的）。
            timeout: 最长等待秒数；None 表示一直等到有 Key 可用。

        Raises:
            AllKeysInvalid: 池内所有 Key 均已失效，等待没有意义。
            NoAvailableKey: 池为空，或等待超时仍无可用 Key。
        """
        excluded = frozenset(exclude)
        deadline = None if timeout is None else self._clock() + timeout

        with self._cond:
            while True:
                now = self._clock()
                self._refresh(now)

                if not self._keys:
                    # 空池要单独报错：走下面的 all(...) 分支会得到"全部 0 把 Key 失效"这种误导信息
                    raise NoAvailableKey("Key 池为空，请先添加至少一把 Key")

                if all(k.status is KeyStatus.INVALID for k in self._keys):
                    raise AllKeysInvalid(
                        f"全部 {len(self._keys)} 把 Key 均已被判定失效（401/403），请更换凭据"
                    )

                candidates = [k for k in self._keys if k.is_usable(now, excluded)]
                if candidates:
                    return self._reserve(self._pick(candidates), now)

                wait = self._next_wait(now, excluded)
                sleep_for = wait if wait > 0 else 0.05
                if deadline is not None:
                    remain = deadline - now
                    if remain <= 0:
                        raise NoAvailableKey(
                            f"等待可用 Key 超时（{timeout:g}s）：{self._describe(now, excluded)}",
                            retry_after=wait,
                        )
                    sleep_for = min(sleep_for, remain)
                self._cond.wait(timeout=sleep_for)

    @contextlib.contextmanager
    def lease(
        self,
        *,
        exclude: Iterable[str] = (),
        timeout: float | None = None,
    ) -> Iterator[ApiKey]:
        """``acquire`` + 自动 ``release`` 的上下文管理器。"""
        key = self.acquire(exclude=exclude, timeout=timeout)
        try:
            yield key
        finally:
            self.release(key)

    def release(self, key: ApiKey) -> None:
        """归还 Key（只减 inflight，不改变健康状态）。"""
        with self._cond:
            if key.inflight > 0:
                key.inflight -= 1
            if key.account_state is not None and key.account_state.inflight > 0:
                key.account_state.inflight -= 1
            self._cond.notify_all()

    def _reserve(self, key: ApiKey, now: float) -> ApiKey:
        key.inflight += 1
        key.last_used = now
        key._window.append(now)
        key.stats.requests += 1
        if key.account_state is not None:
            key.account_state.inflight += 1
            key.account_state._window.append(now)
        return key

    # ------------------------------------------------------------ 选择策略

    def _pick(self, candidates: list[ApiKey]) -> ApiKey:
        strategy = self.strategy
        if strategy == "least_inflight":
            return min(candidates, key=lambda k: (k.inflight, k.last_used))
        if strategy == "least_recent":
            return min(candidates, key=lambda k: k.last_used)
        if strategy == "weighted":
            total = sum(k.weight for k in candidates)
            point = self._rng.uniform(0.0, total)
            acc = 0.0
            for key in candidates:
                acc += key.weight
                if point <= acc:
                    return key
            return candidates[-1]
        # round_robin：按池内顺序轮转，配额均摊最均匀
        size = len(self._keys)
        for offset in range(size):
            key = self._keys[(self._cursor + offset) % size]
            if any(key is c for c in candidates):
                self._cursor = (self._cursor + offset + 1) % size
                return key
        return candidates[0]

    def _next_wait(self, now: float, excluded: frozenset[str]) -> float:
        """还需要等多久才可能有 Key 可用（0 表示只是并发打满，等 release 通知）。"""
        soonest: float | None = None
        for key in self._keys:
            if key.key in excluded or key.status is KeyStatus.INVALID:
                continue
            moment = key.available_at(now)
            if moment <= now:
                return 0.0
            soonest = moment if soonest is None else min(soonest, moment)
        return 0.0 if soonest is None else max(soonest - now, 0.0)

    def _describe(self, now: float, excluded: frozenset[str] = frozenset()) -> str:
        parts = []
        for key in self._keys:
            remain = key.available_at(now) - now
            if key.key in excluded:
                parts.append(f"{key.masked}({key.account})=本轮跳过")
            elif key.status is KeyStatus.INVALID:
                parts.append(f"{key.masked}({key.account})=失效")
            elif remain > 0:
                parts.append(f"{key.masked}({key.account})={remain:.1f}s后可用")
            else:
                parts.append(f"{key.masked}({key.account})=并发已满")
        return "; ".join(parts)

    # ------------------------------------------------------------ 状态刷新

    def _refresh(self, now: float) -> None:
        """把冷却到期的 Key 和账号复活（调用方需持锁）。"""
        for acct in self._accounts.values():
            if acct.cooldown_until and acct.cooldown_until <= now:
                acct.cooldown_until = 0.0
                acct.consecutive_failures = 0
                acct.last_error = ""
        for key in self._keys:
            if key.cooldown_until and key.cooldown_until <= now:
                key.cooldown_until = 0.0
                key.status = KeyStatus.HEALTHY
                key.consecutive_failures = 0
                key.last_error = ""

    # ------------------------------------------------------------ 结果上报

    def report_success(self, key: ApiKey, latency: float | None = None) -> None:
        with self._cond:
            key.stats.successes += 1
            if latency is not None:
                key.stats.total_latency += latency
            key.consecutive_failures = 0
            key.cooldown_until = 0.0
            key.status = KeyStatus.HEALTHY
            key.last_error = ""
            if key.account_state is not None:
                key.account_state.consecutive_failures = 0
                key.account_state.cooldown_until = 0.0
                key.account_state.last_error = ""
            self._cond.notify_all()

    def report_rate_limit(self, key: ApiKey, retry_after: float | None = None) -> float:
        """记录一次 429，返回实际冷却秒数。"""
        with self._cond:
            key.stats.rate_limited += 1
            key.stats.failures += 1
            key.consecutive_failures += 1
            delay = self.cooldown.for_attempt(key.consecutive_failures, self._rng)
            if retry_after and retry_after > delay:
                delay = min(retry_after, self.cooldown.max * 4)  # 尊重服务端建议，但别被拖死
            key.status = KeyStatus.COOLDOWN
            key.cooldown_until = self._clock() + delay
            key.last_error = f"429 rate limited (#{key.consecutive_failures})"

            # 同步冷却同一账号下的所有 Key，防止连环送死！
            if key.account_state is not None:
                key.account_state.consecutive_failures += 1
                key.account_state.cooldown_until = max(key.account_state.cooldown_until, self._clock() + delay)
                key.account_state.last_error = key.last_error
                for sibling in self._keys:
                    if sibling.account_state is key.account_state and sibling.status is not KeyStatus.INVALID:
                        sibling.status = KeyStatus.COOLDOWN
                        sibling.cooldown_until = max(sibling.cooldown_until, key.account_state.cooldown_until)
                        sibling.last_error = key.last_error

            self._cond.notify_all()
            return delay

    def report_server_error(self, key: ApiKey, detail: str = "") -> float:
        """记录一次 5xx / 网络超时：短冷却，不累计退避次数。"""
        with self._cond:
            key.stats.server_errors += 1
            key.stats.failures += 1
            delay = self.cooldown.server_error
            key.status = KeyStatus.COOLDOWN
            key.cooldown_until = max(key.cooldown_until, self._clock() + delay)
            key.last_error = detail or "server/network error"
            self._cond.notify_all()
            return delay

    def report_invalid(self, key: ApiKey, detail: str = "") -> None:
        """记录一次凭据失效（401/403）。"""
        with self._cond:
            key.stats.failures += 1
            key.status = KeyStatus.INVALID
            ttl = self.cooldown.invalid_ttl
            key.cooldown_until = self._clock() + ttl if ttl > 0 else float("inf")
            key.last_error = detail or "invalid credential"
            self._cond.notify_all()

    def report_client_error(self, key: ApiKey, detail: str = "") -> None:
        """记录一次业务错误（400/404 等）——不是 Key 的问题，只计数。"""
        with self._cond:
            key.stats.client_errors += 1
            key.stats.failures += 1
            key.last_error = detail or "client error"

    # ------------------------------------------------------------ 快照

    def snapshot(self) -> list[dict[str, object]]:
        now = self._clock()
        with self._cond:
            self._refresh(now)
            return [k.to_dict(now) for k in self._keys]

    def summary(self) -> dict[str, int]:
        now = self._clock()
        with self._cond:
            self._refresh(now)
            total = len(self._keys)
            healthy = sum(1 for k in self._keys if k.status is KeyStatus.HEALTHY)
            cooling = sum(1 for k in self._keys if k.status is KeyStatus.COOLDOWN)
            invalid = sum(1 for k in self._keys if k.status is KeyStatus.INVALID)
            return {
                "total": total,
                "healthy": healthy,
                "cooldown": cooling,
                "invalid": invalid,
                "inflight": sum(k.inflight for k in self._keys),
            }
