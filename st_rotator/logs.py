"""日志：文件落盘（按大小轮转）+ 内存环形缓冲（给 UI 实时展示）。

UI 需要"最近 N 条日志"，但不能读文件（要反复 seek、还要处理轮转）。
所以这里维护一个线程安全的环形缓冲，所有日志同时写文件和缓冲。
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Sequence

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUPS = 5
DEFAULT_BUFFER_LINES = 500


class LogBuffer:
    """线程安全的日志环形缓冲。

    每条日志带一个自增游标，前端只要传上次拿到的游标就能增量拉取，
    不用每次把全部日志重传一遍。
    """

    def __init__(self, maxlen: int = DEFAULT_BUFFER_LINES) -> None:
        self._lines: deque[tuple[int, str]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._cursor = 0

    def append(self, message: str) -> None:
        message = message.rstrip("\n")
        if not message:
            return
        with self._lock:
            self._cursor += 1
            self._lines.append((self._cursor, message))

    def since(self, cursor: int = 0) -> tuple[int, list[dict[str, Any]]]:
        """返回 ``(最新游标, 游标之后的新日志)``。"""
        with self._lock:
            if cursor >= self._cursor:
                return self._cursor, []
            items = [
                {"seq": seq, "text": text}
                for seq, text in self._lines
                if seq > cursor
            ]
            return self._cursor, items

    def tail(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._lines)[-limit:]
        return [{"seq": seq, "text": text} for seq, text in items]

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()


def build_logger(
    log_file: str | Path | None,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backups: int = DEFAULT_BACKUPS,
    level: int = logging.INFO,
) -> logging.Logger:
    """构造按大小轮转的文件 logger；``log_file`` 为 None 时只建内存 logger。"""
    logger = logging.getLogger("st_rotator")
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8", delay=False
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(handler)
    return logger


def fanout_sink(*sinks: Callable[[str], None] | None) -> Callable[[str], None]:
    """把多个日志出口合并成一个（文件 + 内存缓冲 + 控制台）。"""
    active = [sink for sink in sinks if sink is not None]

    def sink(message: str) -> None:
        for target in active:
            try:
                target(message)
            except Exception:  # pragma: no cover - 日志出口不该影响主流程
                pass

    return sink


def make_log_sink(
    logger: logging.Logger | None = None,
    buffer: LogBuffer | None = None,
    echo: Callable[[str], None] | None = None,
) -> Callable[[str], None]:
    """构造一个同时写文件 / 内存 / 控制台的日志出口。"""
    def sink(message: str) -> None:
        if logger is not None:
            logger.info(message)
        if buffer is not None:
            buffer.append(message)
        if echo is not None:
            echo(message)

    return sink


def read_log_tail(log_file: str | Path, lines: int = 200) -> Sequence[str]:
    """从文件尾部读若干行（UI 兜底用；正常走 LogBuffer 更好）。"""
    path = Path(log_file)
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return list(deque(handle, maxlen=lines))
