"""商汤日日新（SenseNova）多账户多 Key 轮换工具包。

零第三方依赖，只用 Python 标准库。

快速开始::

    from sensenova_rotator import Config, SenseNovaRotator

    config = Config.from_file("config.json")
    with SenseNovaRotator(config) as rotator:
        resp = rotator.chat([{"role": "user", "content": "你好"}])
        print(resp["choices"][0]["message"]["content"])

需要图形界面就用命令行入口::

    python -m sensenova_rotator ui
"""

from .client import SenseNovaRotator, classify, extract_error, parse_retry_after
from .config import AccountConfig, Config, ConfigStore, CooldownConfig, RateControlConfig
from .errors import (
    AllKeysInvalid,
    ApiError,
    ConfigError,
    NoAvailableKey,
    RotationExhausted,
    RotatorError,
    StreamInterrupted,
)
from .keypool import ApiKey, KeyPool, KeyStatus, key_id, mask_key
from .limiter import AdaptiveRateLimiter, RateLimiter
from .logs import LogBuffer, build_logger, make_log_sink, read_log_tail
from .proxy import create_server, serve
from .transport import HttpClient, NetworkError, Request, Response, StreamResponse
from .ui import ConsoleState, GatewayMetrics, UiResponse, open_console_window
from .version import __version__

__all__ = [
    "AccountConfig",
    "AdaptiveRateLimiter",
    "AllKeysInvalid",
    "ApiError",
    "ApiKey",
    "Config",
    "ConfigError",
    "ConfigStore",
    "ConsoleState",
    "CooldownConfig",
    "GatewayMetrics",
    "HttpClient",
    "KeyPool",
    "KeyStatus",
    "LogBuffer",
    "NetworkError",
    "NoAvailableKey",
    "RateControlConfig",
    "RateLimiter",
    "Request",
    "Response",
    "RotationExhausted",
    "RotatorError",
    "SenseNovaRotator",
    "StreamInterrupted",
    "StreamResponse",
    "UiResponse",
    "__version__",
    "build_logger",
    "classify",
    "create_server",
    "extract_error",
    "key_id",
    "make_log_sink",
    "mask_key",
    "open_console_window",
    "parse_retry_after",
    "read_log_tail",
    "serve",
]
