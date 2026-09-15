"""异常定义。

分层原则：
- ``RotatorError``      本工具所有异常的基类，方便调用方一把兜住
- ``NoAvailableKey``    "暂时没有可用 Key"——可重试，通常是全部冷却中
- ``AllKeysInvalid``    "所有 Key 都废了"——不可重试，必须换凭据
- ``RotationExhausted`` "轮换用尽仍失败"——重试次数打完还没成功
- ``ApiError``          "上游明确拒绝"——4xx 业务错误，换 Key 也没用
"""

from __future__ import annotations


class RotatorError(RuntimeError):
    """本工具所有异常的基类。"""


class ConfigError(RotatorError):
    """配置文件缺失、格式非法或字段取值不合法。"""


class NoAvailableKey(RotatorError):
    """当前没有任何可用 Key（全部冷却 / 并发打满），或等待可用 Key 超时。

    ``retry_after`` 是预估的最短等待秒数，便于上层做调度决策。
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class AllKeysInvalid(RotatorError):
    """所有 Key 均被判定为失效（401/403 或鉴权类 429），轮换无法自救。"""


class RotationExhausted(RotatorError):
    """已用尽重试次数仍未拿到成功响应。"""

    def __init__(
        self,
        message: str,
        *,
        attempts: int,
        last_status: int | None = None,
        last_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_status = last_status
        self.last_body = last_body


class ApiError(RotatorError):
    """上游返回了不可通过换 Key 解决的错误（如 400 参数错、404 模型不存在）。"""

    def __init__(self, status: int, body: str, message: str | None = None) -> None:
        super().__init__(message or f"上游返回 {status}: {body[:500]}")
        self.status = status
        self.body = body


class StreamInterrupted(RotatorError):
    """流式响应已经开始吐字后中断，无法安全重试（避免重复输出）。"""
