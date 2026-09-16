"""本地 OpenAI 兼容网关：把轮换池包装成一个标准 API 端点。

为什么需要它
------------
WorkBuddy / 各类 Agent 框架只认「OpenAI 兼容端点 + API Key」，没法直接调用 Python 类。
这个网关在本机起一个 HTTP 服务，把 ``/v1/chat/completions`` 转发到轮换池：

    WorkBuddy ──► http://127.0.0.1:8080/v1 ──► 轮换池 ──► 商汤网关（多 Key）

对上层完全透明：429、冷却、Key 轮换全部在网关内部消化，上层只看到一个稳定端点。

同一个端口还挂了一套本地控制台（``/`` 与 ``/api/*``），见 ``ui.py``：界面和 API 共用一个
进程，改配置就是改正在跑的那份内存配置，立刻生效，不需要重启任何东西。

设计要点
--------
* **流式必须透传原始 chunk**。Agent 靠 ``tool_calls`` 工作，如果只转发文本增量，
  工具调用会被吃掉。所以这里用 ``chat_stream_raw``，字段一个不改。
* **请求体里的 stream / model / messages 要拆出来**，其余参数（temperature、tools、
  response_format……）原样透传给上游。
* **错误要映射成 OpenAI 的错误结构**，上层框架才能正确识别和重试。
* **暂停开关**。控制台可以暂停对外服务（返回 503）而进程继续活着，方便临时止损。
"""

from __future__ import annotations

import hmac
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse

from .client import StRotator
from .errors import AllKeysInvalid, ApiError, NoAvailableKey, RotationExhausted, RotatorError
from . import ui

if TYPE_CHECKING:  # pragma: no cover
    from .ui import ConsoleState

# 需要特殊处理的路径（其余 /v1/* 一律原样透传）
CHAT_PATH = "/v1/chat/completions"
MODELS_PATH = "/v1/models"
HEALTH_PATHS = ("/healthz", "/health")
STATS_PATH = "/stats"

MAX_BODY_BYTES = 32 * 1024 * 1024


def error_payload(message: str, err_type: str = "upstream_error", code: str | None = None) -> dict:
    """OpenAI 风格的错误结构。"""
    return {"error": {"message": message, "type": err_type, "code": code, "param": None}}


def _passthrough_error(status: int, body: str) -> dict:
    """上游的错误体如果是 OpenAI 结构就原样转发，否则包一层。"""
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict) and "error" in parsed:
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return error_payload(body[:800] or f"upstream returned {status}", code=str(status))


class RotatorProxyHandler(BaseHTTPRequestHandler):
    """把请求转交给轮换池的 HTTP 处理器。"""

    protocol_version = "HTTP/1.1"
    server_version = "st-rotator-proxy"
    sys_version = ""

    # 由 create_server 注入
    rotator: StRotator
    token: str | None = None
    verbose: bool = False
    log_sink: Callable[[str], None] | None = None
    console: "ConsoleState | None" = None

    # ------------------------------------------------------------ 基础设施

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if not self.verbose:
            return
        line = "%s - %s" % (self.address_string(), fmt % args)
        # 注意：log_sink 必须以 staticmethod 注入（见 create_server）。如果直接当普通
        # 类属性放进去，self.log_sink 会变成绑定方法、悄悄把 self 当第一个参数传进去，
        # 调用时就是 "takes 1 positional argument but 2 were given"。
        # 这个坑只在 verbose=True 时暴露，因为 log_message 在此之前就 return 了。
        sink = type(self).log_sink
        if sink is not None:
            sink(line)
        else:
            sys.stderr.write("[proxy] " + line + "\n")

    def _read_body(self) -> bytes:
        """读取请求体，同时支持 Content-Length 与 chunked。"""
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            chunks: list[bytes] = []
            total = 0
            while True:
                size_line = self.rfile.readline().strip()
                if not size_line:
                    break
                try:
                    size = int(size_line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline()  # 吃掉结尾的 CRLF
                    break
                total += size
                if total > MAX_BODY_BYTES:
                    raise ValueError("请求体过大")
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)  # CRLF
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        if length > MAX_BODY_BYTES:
            raise ValueError("请求体过大")
        return self.rfile.read(length)

    def _authorized(self) -> bool:
        if not self.token:
            return True
        header = self.headers.get("Authorization") or ""
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else header.strip()
        return hmac.compare_digest(supplied, self.token)

    def _send_json(self, status: int, payload: Mapping[str, Any], *, retry_after: float | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if retry_after:
                self.send_header("Retry-After", str(int(retry_after) + 1))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _send_raw(self, status: int, text: str, content_type: str) -> None:
        body = text.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _forwardable_headers(self) -> dict[str, str]:
        """提取需要透传给上游的请求头（追踪 ID、租户、组织、自定义 x- 头等）。"""
        forward_headers: dict[str, str] = {}
        allowed_specific = {
            "x-request-id",
            "x-correlation-id",
            "traceparent",
            "tracestate",
            "openai-organization",
            "openai-project",
            "openai-beta",
        }
        for key, val in self.headers.items():
            k_lower = key.lower()
            if k_lower in allowed_specific or (
                k_lower.startswith("x-")
                and k_lower not in ("x-forwarded-for", "x-forwarded-proto", "x-forwarded-host")
            ):
                forward_headers[key] = val
        return forward_headers

    # ------------------------------------------------------------ 控制台

    @property
    def _paused(self) -> bool:
        return self.console is not None and self.console.metrics.is_paused

    def _dispatch_console(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, list[str]] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> None:
        """把请求交给控制台处理。控制台内部异常不能把网关带崩。"""
        try:
            result = self.console.handle(method, path, query=query, body=body)  # type: ignore[union-attr]
        except Exception as exc:  # pragma: no cover - 兜底
            result = ui.UiResponse.error(f"控制台内部错误：{type(exc).__name__}: {exc}", status=500)
        if result is None:
            self._send_json(404, error_payload(f"unknown path {path}", "invalid_request_error"))
            return
        if result.raw is not None:
            self._send_raw(result.status, result.raw, result.content_type)
            return
        self._send_json(result.status, result.payload, retry_after=result.retry_after)

    # ------------------------------------------------------------ 流式输出

    def _begin_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _write_chunk(self, data: bytes) -> None:
        """按 HTTP/1.1 chunked 编码写一段数据（SSE 必须边算边发，不能缓冲）。"""
        self.wfile.write(b"%X\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    def _end_stream(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _sse(self, obj: Mapping[str, Any]) -> bytes:
        return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")

    # ------------------------------------------------------------ 路由

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path in HEALTH_PATHS:
            summary = self.rotator.pool.summary()
            self._send_json(200, {"status": "ok", "paused": self._paused, **summary})
            return
        if path == STATS_PATH:
            self._send_json(200, self.rotator.status())
            return
        # 控制台：页面免鉴权（空壳，不含密钥），接口要鉴权
        if self.console is not None and self.console.is_console_path(path):
            if path.startswith(ui.API_PREFIX) and not self._authorized():
                self._send_json(401, error_payload("invalid console token", "authentication_error"))
                return
            self._dispatch_console("GET", path, query=parse_qs(parsed.query))
            return
        if not self._authorized():
            self._send_json(401, error_payload("invalid proxy token", "authentication_error"))
            return
        if self._paused:
            self._send_json(503, error_payload("gateway paused from console", "service_unavailable", "503"))
            return
        query_suffix = f"?{parsed.query}" if parsed.query else ""
        if path == MODELS_PATH:
            self._forward_simple("GET", f"models{query_suffix}")
            return
        if path.startswith("/v1/"):
            self._forward_simple("GET", f"{path[4:]}{query_suffix}")
            return
        self._send_json(404, error_payload(f"unknown path {path}", "invalid_request_error"))

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if not self._authorized():
            self._send_json(401, error_payload("invalid proxy token", "authentication_error"))
            return
        try:
            raw = self._read_body()
        except ValueError as exc:
            self._send_json(413, error_payload(str(exc), "invalid_request_error"))
            return

        if self.console is not None and path.startswith(ui.API_PREFIX):
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError as exc:
                self._send_json(400, error_payload(f"invalid JSON body: {exc}", "invalid_request_error"))
                return
            self._dispatch_console("POST", path, body=body if isinstance(body, dict) else {})
            return

        query_suffix = f"?{parsed.query}" if parsed.query else ""
        if path == CHAT_PATH:
            self._handle_chat(raw)
            return
        if path.startswith("/v1/"):
            self._forward_simple("POST", f"{path[4:]}{query_suffix}", raw)
            return
        self._send_json(404, error_payload(f"unknown path {path}", "invalid_request_error"))

    # ------------------------------------------------------------ 具体处理

    def _handle_chat(self, raw: bytes) -> None:
        if self._paused:
            self._send_json(503, error_payload("gateway paused from console", "service_unavailable", "503"))
            return
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            self._send_json(400, error_payload(f"invalid JSON body: {exc}", "invalid_request_error"))
            return
        if not isinstance(payload, dict):
            self._send_json(400, error_payload("body must be a JSON object", "invalid_request_error"))
            return

        params = dict(payload)
        stream = bool(params.pop("stream", False))
        model = params.pop("model", None)
        messages = params.pop("messages", None)
        if not messages:
            self._send_json(400, error_payload("'messages' is required", "invalid_request_error"))
            return

        if self.console is not None:
            self.console.metrics.note_request(stream=stream)

        headers = self._forwardable_headers()
        if stream:
            self._chat_stream(messages, model, params, headers=headers)
        else:
            self._chat_complete(messages, model, params, headers=headers)

    def _chat_complete(
        self, messages: Any, model: str | None, params: dict, headers: Mapping[str, str] | None = None
    ) -> None:
        try:
            result = self.rotator.chat(messages, model=model, headers=headers, **params)
        except ApiError as exc:
            self._send_json(exc.status, _passthrough_error(exc.status, exc.body))
            return
        except RotationExhausted as exc:
            status = exc.last_status if (exc.last_status or 0) >= 400 else 502
            self._send_json(status, error_payload(str(exc), "upstream_exhausted", str(status)))
            return
        except NoAvailableKey as exc:
            self._send_json(429, error_payload(str(exc), "rate_limit_exceeded", "429"),
                            retry_after=exc.retry_after)
            return
        except AllKeysInvalid as exc:
            self._send_json(503, error_payload(str(exc), "all_keys_invalid", "503"))
            return
        except RotatorError as exc:
            self._send_json(502, error_payload(str(exc), "upstream_error", "502"))
            return
        self._send_json(200, result)

    def _chat_stream(
        self, messages: Any, model: str | None, params: dict, headers: Mapping[str, str] | None = None
    ) -> None:
        stream = self.rotator.chat_stream_raw(messages, model=model, headers=headers, **params)
        first_chunk = None
        has_first = False
        try:
            first_chunk = next(stream)
            has_first = True
        except StopIteration:
            self._begin_stream()
            try:
                self._write_chunk(b"data: [DONE]\n\n")
                self._end_stream()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        except ApiError as exc:
            self._send_json(exc.status, _passthrough_error(exc.status, exc.body))
            return
        except RotationExhausted as exc:
            status = exc.last_status if (exc.last_status or 0) >= 400 else 502
            self._send_json(status, error_payload(str(exc), "upstream_exhausted", str(status)))
            return
        except NoAvailableKey as exc:
            self._send_json(429, error_payload(str(exc), "rate_limit_exceeded", "429"), retry_after=exc.retry_after)
            return
        except AllKeysInvalid as exc:
            self._send_json(503, error_payload(str(exc), "all_keys_invalid", "503"))
            return
        except RotatorError as exc:
            self._send_json(502, error_payload(str(exc), "upstream_error", "502"))
            return

        self._begin_stream()
        try:
            if has_first:
                self._write_chunk(self._sse(first_chunk))
            for chunk in stream:
                self._write_chunk(self._sse(chunk))
        except RotatorError as exc:
            # 已经发出 200 了，改不了状态码，只能补一条 error 事件收尾
            try:
                self._write_chunk(self._sse(error_payload(str(exc), "upstream_error", "502")))
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
        except (BrokenPipeError, ConnectionResetError, OSError):
            return  # 客户端主动断开，正常情况
        finally:
            try:
                self._write_chunk(b"data: [DONE]\n\n")
                self._end_stream()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    def _forward_simple(self, method: str, upstream_path: str, raw: bytes = b"") -> None:
        """其余端点（embeddings / images / models…）原样透传，同样享受轮换。"""
        if self._paused:
            self._send_json(503, error_payload("gateway paused from console", "service_unavailable", "503"))
            return
        body = None
        if raw:
            try:
                body = json.loads(raw)
            except json.JSONDecodeError as exc:
                self._send_json(400, error_payload(f"invalid JSON body: {exc}", "invalid_request_error"))
                return
        headers = self._forwardable_headers()
        try:
            result = self.rotator.request(upstream_path, method=method, json_body=body, headers=headers)
        except ApiError as exc:
            self._send_json(exc.status, _passthrough_error(exc.status, exc.body))
            return
        except RotationExhausted as exc:
            status = exc.last_status if (exc.last_status or 0) >= 400 else 502
            self._send_json(status, error_payload(str(exc), "upstream_exhausted", str(status)))
            return
        except NoAvailableKey as exc:
            self._send_json(429, error_payload(str(exc), "rate_limit_exceeded", "429"), retry_after=exc.retry_after)
            return
        except AllKeysInvalid as exc:
            self._send_json(503, error_payload(str(exc), "all_keys_invalid", "503"))
            return
        except RotatorError as exc:
            self._send_json(502, error_payload(str(exc), "upstream_error", "502"))
            return
        self._send_json(200, result)


def create_server(
    rotator: StRotator,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    token: str | None = None,
    verbose: bool = False,
    log_sink: Callable[[str], None] | None = None,
    console: "ConsoleState | None" = None,
) -> ThreadingHTTPServer:
    """创建网关服务实例（不启动）。"""
    handler = type(
        "BoundRotatorProxyHandler",
        (RotatorProxyHandler,),
        {
            "rotator": rotator,
            "token": token,
            "verbose": verbose,
            # 必须包一层 staticmethod：log_sink 是普通函数，直接当类属性会被描述符协议
            # 绑定成方法，调用时多传一个 self。
            "log_sink": staticmethod(log_sink),
            "console": console,
        },
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.allow_reuse_address = True
    return server


def serve(
    rotator: StRotator,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    token: str | None = None,
    verbose: bool = False,
    log_sink: Callable[[str], None] | None = None,
    ready: Callable[[str], None] | None = None,
    console: "ConsoleState | None" = None,
    on_ready: Callable[[ThreadingHTTPServer], None] | None = None,
) -> None:
    """阻塞式启动网关，Ctrl+C 退出。

    Args:
        console: 传入控制台状态则同时提供 Web 控制台。
        on_ready: 服务已绑定端口、进入循环前回调（用于自动开窗，避免开在服务就绪之前）。
    """
    server = create_server(
        rotator, host=host, port=port, token=token, verbose=verbose,
        log_sink=log_sink, console=console,
    )
    actual_host, actual_port = server.server_address[:2]
    base = f"http://{actual_host}:{actual_port}/v1"
    lines = [
        f"网关已启动: {base}",
        f"  - 健康检查 : http://{actual_host}:{actual_port}/healthz",
        f"  - 运行统计 : http://{actual_host}:{actual_port}/stats",
    ]
    if console is not None:
        # 端口可能是 0（自动分配），回填真实端口，否则控制台里显示的地址是错的
        console.host, console.port = actual_host, actual_port
        lines.append(f"  - 控制台   : http://{actual_host}:{actual_port}/")
    lines.append(
        f"  - 本地鉴权 : {'已开启' if token else '未开启（仅监听本机，建议加 --token）'}"
    )
    lines.append(f"  把上层应用的 base_url 指到 {base} 即可。")
    message = "\n".join(lines)
    if ready:
        ready(message)
    else:
        print(message)
    if log_sink is not None:
        log_sink(message)
    if on_ready is not None:
        try:
            on_ready(server)
        except Exception as exc:  # pragma: no cover - 开窗失败不该影响服务
            if log_sink is not None:
                log_sink(f"[警告] 控制台开窗失败：{exc}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if log_sink is not None:
            log_sink("网关已停止")
        server.shutdown()
        server.server_close()
