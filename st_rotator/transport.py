"""极简 HTTP 客户端 —— 纯标准库实现，零第三方依赖。

为什么不用 requests / httpx？
    这类工具经常要跑在内网、容器、离线机器上，"pip 装不上"是常态。
    所以这里只用 ``http.client`` + ``ssl`` + ``json``，自己实现连接池和流式读取。
    另外内置 ``handler`` 钩子，方便用假上游做离线测试。
"""

from __future__ import annotations

import contextlib
import http.client
import json
import ssl
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlsplit


class NetworkError(Exception):
    """网络层错误（连接失败、超时、对端断开）——可以换个 Key 重试。"""


@dataclass
class Request:
    """一次请求的不可变描述。"""

    method: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None


def _split_lines(payload: bytes) -> Iterator[str]:
    text = payload.decode("utf-8", "replace")
    for line in text.splitlines():
        yield line


class Response:
    """已完整读入内存的响应。"""

    __slots__ = ("status", "headers", "content")

    def __init__(self, status: int, headers: Mapping[str, str], content: bytes) -> None:
        self.status = int(status)
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.content = content or b""

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))

    def iter_lines(self) -> Iterator[str]:
        yield from _split_lines(self.content)

    def close(self) -> None:
        """兼容流式响应的接口，内存响应无需处理。"""

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Response {self.status} {len(self.content)}B>"


class StreamResponse:
    """流式响应：持有底层连接，读完或 close() 后自动归还连接。"""

    def __init__(
        self,
        raw: http.client.HTTPResponse,
        client: "HttpClient",
        conn: http.client.HTTPConnection,
        status: int,
        headers: Mapping[str, str],
    ) -> None:
        self.status = int(status)
        self.headers = {k.lower(): v for k, v in headers.items()}
        self._raw = raw
        self._client = client
        self._conn = conn
        self._buffer: bytes | None = None
        self._done = False

    # ------------------------------------------------------------ 读取

    def iter_lines(self) -> Iterator[str]:
        try:
            while True:
                line = self._raw.readline()
                if not line:
                    break
                yield line.decode("utf-8", "replace").rstrip("\r\n")
        finally:
            self.close()

    @property
    def text(self) -> str:
        """把剩余内容一次性读完（仅用于读取错误响应体）。"""
        if self._buffer is None:
            with contextlib.suppress(Exception):
                self._buffer = self._raw.read()
            self._buffer = self._buffer or b""
        self.close()
        return self._buffer.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def close(self) -> None:
        if self._done:
            return
        self._done = True
        with contextlib.suppress(Exception):
            self._raw.close()
        self._client._finish(self._conn)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<StreamResponse {self.status}>"


class HttpClient:
    """带连接池的同步 HTTP 客户端。

    Args:
        base_url: 例如 ``https://token.sensenova.cn/v1``。
        timeout: 读超时（秒）。
        connect_timeout: 建连超时（秒）。
        headers: 默认请求头。
        max_connections: 最大并发请求数。
        max_keepalive: 空闲连接池上限。
        verify: 是否校验 TLS 证书。
        handler: 测试钩子；提供后所有请求走它，不发起真实网络调用。
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 60.0,
        connect_timeout: float = 10.0,
        headers: Mapping[str, str] | None = None,
        max_connections: int = 100,
        max_keepalive: int = 20,
        verify: bool = True,
        handler: Callable[[Request], Response] | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"base_url 必须以 http(s):// 开头: {base_url!r}")
        if not parsed.hostname:
            raise ValueError(f"base_url 缺少主机名: {base_url!r}")

        self.base_url = base_url.rstrip("/")
        self._scheme = parsed.scheme
        self._host = parsed.hostname
        self._port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._base_path = parsed.path.rstrip("/")
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        self._headers = dict(headers or {})
        self._max_keepalive = max(1, max_keepalive)
        self._sem = threading.Semaphore(max(1, max_connections))
        self._lock = threading.Lock()
        self._idle: list[http.client.HTTPConnection] = []
        self._closed = False
        self._handler = handler
        self._context: ssl.SSLContext | None = None
        if self._scheme == "https":
            self._context = ssl.create_default_context()
            if not verify:
                self._context.check_hostname = False
                self._context.verify_mode = ssl.CERT_NONE

    # ------------------------------------------------------------ 便捷方法

    def get(self, path: str, *, headers: Mapping[str, str] | None = None, stream: bool = False):
        return self.request("GET", path, headers=headers, stream=stream)

    def post(
        self,
        path: str,
        *,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        stream: bool = False,
    ):
        return self.request("POST", path, json_body=json_body, headers=headers, stream=stream)

    # ------------------------------------------------------------ 主流程

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        stream: bool = False,
    ) -> Response | StreamResponse:
        merged = dict(self._headers)
        if headers:
            merged.update({k: v for k, v in headers.items() if v is not None})
        body: bytes | None = None
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            merged.setdefault("Content-Type", "application/json")

        request = Request(method.upper(), path, merged, body)

        if self._handler is not None:
            result = self._handler(request)
            if not isinstance(result, Response):
                raise TypeError(f"handler 必须返回 Response，实际为 {type(result).__name__}")
            return result

        return self._send(request, stream=stream)

    def _url(self, path: str) -> str:
        return f"{self._base_path}/{path.lstrip('/')}" if path else self._base_path or "/"

    def _new_connection(self) -> http.client.HTTPConnection:
        if self._scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                self._host, self._port, timeout=self._connect_timeout, context=self._context
            )
        else:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=self._connect_timeout)
        conn.connect()
        if conn.sock is not None:
            conn.sock.settimeout(self._timeout)  # 建连后切到读超时
        return conn

    def _checkout(self) -> tuple[http.client.HTTPConnection, bool]:
        with self._lock:
            if self._idle:
                return self._idle.pop(), True
        return self._new_connection(), False

    def _checkin(self, conn: http.client.HTTPConnection, *, healthy: bool = True) -> None:
        if healthy and conn.sock is not None:
            with self._lock:
                if len(self._idle) < self._max_keepalive:
                    self._idle.append(conn)
                    return
        with contextlib.suppress(Exception):
            conn.close()

    def _finish(self, conn: http.client.HTTPConnection, *, healthy: bool = True) -> None:
        """流式响应读完后调用：归还连接并释放并发额度。"""
        self._checkin(conn, healthy=healthy)
        self._sem.release()

    def _send(self, request: Request, *, stream: bool) -> Response | StreamResponse:
        if self._closed:
            raise NetworkError("HttpClient 已关闭")

        self._sem.acquire()
        handed_off = False
        conn: http.client.HTTPConnection | None = None
        try:
            conn, reused = self._checkout()
            try:
                conn.request(request.method, self._url(request.path), body=request.body, headers=request.headers)
                raw = conn.getresponse()
            except (http.client.HTTPException, OSError) as exc:
                # 连接池里的连接可能已被对端关闭，换一条新连接重试一次
                with contextlib.suppress(Exception):
                    conn.close()
                conn = None
                if not reused:
                    raise NetworkError(f"{type(exc).__name__}: {exc}") from exc
                conn = self._new_connection()
                conn.request(request.method, self._url(request.path), body=request.body, headers=request.headers)
                raw = conn.getresponse()

            status = raw.status
            headers = {k.lower(): v for k, v in raw.getheaders()}

            if stream:
                response = StreamResponse(raw, self, conn, status, headers)
                handed_off = True
                return response

            payload = raw.read()
            healthy = status < 500 and raw.getheader("Connection", "").lower() != "close"
            self._checkin(conn, healthy=healthy)
            conn = None
            return Response(status, headers, payload)
        except NetworkError:
            raise
        except (http.client.HTTPException, OSError, ssl.SSLError) as exc:
            raise NetworkError(f"{type(exc).__name__}: {exc}") from exc
        finally:
            if not handed_off:
                if conn is not None:
                    self._checkin(conn, healthy=False)
                self._sem.release()

    # ------------------------------------------------------------ 生命周期

    def close(self) -> None:
        self._closed = True
        with self._lock:
            idle, self._idle = self._idle, []
        for conn in idle:
            with contextlib.suppress(Exception):
                conn.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
