"""轮换客户端：在 Key 池之上封装 OpenAI 兼容 HTTP 调用。

关键设计
--------
* **错误分类先于重试**。429 不一定是限流——商汤有实测案例把鉴权失败也返回成 429。
  所以 429 会先看响应体里有没有鉴权类关键词，有就按"失效 Key"处理，避免在坏 Key
  上无限轮换。
* **重试换 Key，退避不叠加**。每次重试都从池里取当前最优 Key（被限流的自然被跳过），
  同时在客户端侧叠加一层短退避，防止把池里的 Key 一起打爆。
* **流式不重复输出**。流式响应一旦开始吐字就视为成功，中断时直接抛错而不是重试。
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from dataclasses import replace
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterator, Mapping, Sequence

from .config import Config, RateControlConfig, STRATEGIES
from .errors import (
    AllKeysInvalid,
    ApiError,
    ConfigError,
    NoAvailableKey,
    RotationExhausted,
    RotatorError,
    StreamInterrupted,
)
from .keypool import ApiKey, KeyPool, mask_key
from .limiter import AdaptiveRateLimiter, RateLimiter
from .transport import HttpClient, NetworkError, Response, StreamResponse

# 值得换个 Key 重试的状态码
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 522, 524})
# 凭据类错误
AUTH_STATUS = frozenset({401, 403})
# 429 响应体里出现这些词，说明其实是鉴权/账号问题，而非限流
AUTH_HINTS = (
    "invalid api key", "invalid_api_key", "invalid apikey", "api key is invalid",
    "unauthorized", "authentication failed", "authentication_error",
    "invalid token", "token expired", "no permission", "permission denied",
    "account disabled", "account suspended", "arrears", "欠费", "鉴权失败", "密钥无效", "无权限",
)

_RETRY_AFTER_BODY = re.compile(r'"retry_?after"?\s*[:=]\s*"?(\d+(?:\.\d+)?)', re.I)
_RETRY_AFTER_CN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:秒|s)\s*(?:后|之后)", re.I)


def safe_text(response: Response | StreamResponse) -> str:
    """安全读取响应体文本（流式响应会顺带读完并归还连接）。"""
    try:
        return response.text
    except Exception:  # pragma: no cover - 极端编码/连接问题
        return ""


def parse_retry_after(response: Response | StreamResponse, body: str = "") -> float | None:
    """从响应头 / 响应体里解析服务端建议的等待秒数。"""
    raw = (
        response.headers.get("retry-after")
        or response.headers.get("x-ratelimit-reset-requests")
        or response.headers.get("x-ratelimit-reset")
    )
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            try:
                delta = parsedate_to_datetime(raw).timestamp() - time.time()
                return max(0.0, delta)
            except Exception:
                pass
    for pattern in (_RETRY_AFTER_BODY, _RETRY_AFTER_CN):
        match = pattern.search(body or "")
        if match:
            try:
                return max(0.0, float(match.group(1)))
            except ValueError:
                pass
    return None


def classify(response: Response | StreamResponse, body: str) -> tuple[str, float | None]:
    """把响应归类为 ok / retry / invalid / fatal。

    Returns:
        (动作, 服务端建议等待秒数)
    """
    status = response.status
    if status < 400:
        return "ok", None
    if status in AUTH_STATUS:
        return "invalid", None
    if status == 429:
        lowered = (body or "").lower()
        if any(hint in lowered for hint in AUTH_HINTS):
            # 商汤部分网关会用 429 表达鉴权失败，必须识别出来
            return "invalid", None
        return "retry", parse_retry_after(response, body)
    if status in RETRYABLE_STATUS:
        return "retry", parse_retry_after(response, body)
    return "fatal", None


def extract_error(body: str) -> str:
    """从响应体里抽出可读的错误信息。"""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return (body or "")[:300]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("msg") or err)[:300]
        if isinstance(err, str):
            return err[:300]
        for field in ("message", "msg", "error_msg", "detail"):
            if data.get(field):
                return str(data[field])[:300]
    return (body or "")[:300]


_extract_error = extract_error  # 兼容旧名字


def _parse_model_list(payload: Any) -> list[dict[str, Any]]:
    """把 ``/models`` 的返回规整成 UI 好用的结构。

    兼容两种形态：``{"data": [{"id": ...}]}``（OpenAI 风格，商汤走这个）和
    ``{"data": ["model-name", ...]}``（部分网关只给字符串数组）。
    """
    if not isinstance(payload, dict):
        return []
    raw = payload.get("data")
    if not isinstance(raw, list):
        return []
    models: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            models.append({"id": item})
            continue
        if not isinstance(item, dict):
            continue
        model_id = item.get("id") or item.get("name")
        if not model_id:
            continue
        models.append({
            "id": str(model_id),
            "name": str(item.get("name") or model_id),
            "context_length": item.get("context_length"),
            "max_output_length": item.get("max_output_length"),
            "input_modalities": item.get("input_modalities") or [],
            "output_modalities": item.get("output_modalities") or [],
            "features": item.get("supported_features") or [],
            "description": str(item.get("description") or "")[:400],
        })
    models.sort(key=lambda m: m["id"])
    return models


class StRotator:
    """带多 Key 轮换与限流自愈的 OpenAI 兼容客户端。

    用法::

        rotator = StRotator(Config.from_file("config.json"))
        resp = rotator.chat([{"role": "user", "content": "你好"}])
        print(resp["choices"][0]["message"]["content"])
    """

    def __init__(
        self,
        config: Config,
        *,
        pool: KeyPool | None = None,
        client: HttpClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.pool = pool or KeyPool(
            config.accounts, config.cooldown, strategy=config.strategy, clock=clock
        )
        self.limiter = self._build_limiter(config, clock, sleeper)
        self._clock = clock
        self._sleep = sleeper
        self._rng = random.Random()
        self._log = logger or (lambda _msg: None)
        self._owns_client = client is None
        self._client = client or HttpClient(
            config.base_url,
            timeout=config.timeout,
            connect_timeout=config.connect_timeout,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "st-rotator/1.0",
                **config.extra_headers,
            },
            max_connections=config.max_connections,
            max_keepalive=config.max_keepalive,
        )
        # 模型清单缓存：UI 的模型选择器用，避免每刷一次页面就打一次上游
        self._models_lock = threading.Lock()
        self._models_cache: list[dict[str, Any]] = []
        self._models_fetched_at = 0.0
        self._models_error = ""
        # 真正打到上游的请求次数（含重试）。和"客户端请求数"不是一回事：
        # 一个客户端请求可能因为 429 变成好几次上游尝试，这个比值就是轮换的成本。
        self._attempts_lock = threading.Lock()
        self._upstream_attempts = 0

    # ------------------------------------------------------------ 生命周期

    @staticmethod
    def _build_limiter(
        config: Config,
        clock: Callable[[], float],
        sleeper: Callable[[float], None],
    ) -> RateLimiter | AdaptiveRateLimiter:
        """按 rate_control.mode 选择限速器。"""
        rc = config.rate_control
        if rc.mode == "adaptive":
            return AdaptiveRateLimiter(
                rc.qps,
                min_rate=rc.min_qps,
                max_rate=rc.max_qps,
                decrease=rc.decrease,
                increase_step=rc.increase_step,
                recovery_seconds=rc.recovery_seconds,
                clock=clock,
                sleeper=sleeper,
            )
        if rc.mode == "fixed":
            return RateLimiter(rc.qps, clock=clock, sleeper=sleeper)
        return RateLimiter(0.0, clock=clock, sleeper=sleeper)  # off

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "StRotator":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------ 对外接口

    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str | None = None,
        attempts: int | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """非流式对话补全，自动轮换 Key 直到成功。"""
        payload = self._build_payload(messages, model, params)
        return self._post_with_rotation("chat/completions", payload, attempts=attempts)

    def chat_stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str | None = None,
        attempts: int | None = None,
        **params: Any,
    ) -> Iterator[str]:
        """流式对话补全，逐段 yield 文本增量（适合直接打印给人看）。"""
        payload = self._build_payload(messages, model, params)
        payload["stream"] = True
        yield from self._stream_with_rotation("chat/completions", payload, attempts=attempts)

    def chat_stream_raw(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str | None = None,
        attempts: int | None = None,
        **params: Any,
    ) -> Iterator[dict[str, Any]]:
        """流式对话补全，逐块透传**原始 chunk**。

        与 ``chat_stream`` 的区别：这里不做文本抽取，``tool_calls`` / ``finish_reason`` /
        ``usage`` 等字段原样保留。**做 OpenAI 兼容网关必须用这个**，否则 Agent 的工具
        调用会被吃掉。
        """
        payload = self._build_payload(messages, model, params)
        payload["stream"] = True
        yield from self._stream_with_rotation("chat/completions", payload, attempts=attempts, raw=True)

    def embeddings(
        self, inputs: Any, *, model: str, attempts: int | None = None, **params: Any
    ) -> dict[str, Any]:
        payload = {"model": model, "input": inputs, **params}
        return self._post_with_rotation("embeddings", payload, attempts=attempts)

    def request(
        self,
        path: str,
        *,
        method: str = "POST",
        json_body: Mapping[str, Any] | None = None,
        attempts: int | None = None,
    ) -> dict[str, Any]:
        """通用轮换请求，方便接未封装的端点。"""
        return self._post_with_rotation(
            path, dict(json_body) if json_body is not None else None, attempts=attempts, method=method
        )

    def models(self) -> dict[str, Any]:
        return self._post_with_rotation("models", None, method="GET")

    def verify_key(self, key: ApiKey) -> tuple[bool, str]:
        """单独校验一把 Key（用于 ``check`` 命令），不走池调度。

        只回答"能不能用"；分不清"Key 坏了"和"暂时被限流"的场景请用 ``probe_key``。
        """
        verdict, detail = self.probe_key(key.key)
        return verdict != "invalid", detail

    def probe_key(self, key: str) -> tuple[str, str]:
        """探测一把裸 Key，返回 ``(verdict, detail)``。

        ``verdict`` 有三种，这个区分很关键：

        * ``"ok"``      —— 凭据有效。
        * ``"invalid"`` —— 401/403，凭据确实坏了，应该拒收。
        * ``"unknown"`` —— 429 / 5xx / 网络错误。**不能据此判定 Key 有问题**：
          限流恰恰说明凭据是有效的，只是当下没额度。批量导入 Key 时如果把 429 当成
          失效，会把好 Key 一起拒掉。
        """
        headers = {"Authorization": f"Bearer {key}"}
        try:
            response = self._client.get("models", headers=headers)
            status = response.status
            if status < 400:
                return "ok", "ok"
            detail = f"{status} {extract_error(safe_text(response))}"
            if status in AUTH_STATUS:
                return "invalid", detail
            if status in RETRYABLE_STATUS:
                return "unknown", detail
            if status in (404, 405):
                # 网关不支持 /models，退回最小对话探测
                probe = self._client.post(
                    "chat/completions",
                    headers=headers,
                    json_body={
                        "model": self.config.default_model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                        "stream": False,
                    },
                )
                if probe.status < 400:
                    return "ok", "ok"
                probe_detail = f"{probe.status} {extract_error(safe_text(probe))}"
                if probe.status in AUTH_STATUS:
                    return "invalid", probe_detail
                if probe.status in RETRYABLE_STATUS:
                    return "unknown", probe_detail
                return "invalid", probe_detail
            return "invalid", detail
        except NetworkError as exc:
            return "unknown", f"网络错误: {exc}"

    def status(self) -> dict[str, Any]:
        """运行状态快照，便于接入监控。"""
        return {
            "summary": self.pool.summary(),
            "keys": self.pool.snapshot(),
            "rate_control": self.limiter.stats(),
            "upstream_attempts": self.upstream_attempts,
        }

    @property
    def upstream_attempts(self) -> int:
        """累计打到上游的请求次数（含重试）。"""
        with self._attempts_lock:
            return self._upstream_attempts

    def _note_upstream_attempt(self) -> None:
        with self._attempts_lock:
            self._upstream_attempts += 1

    # ------------------------------------------------------------ 运行时控制
    #
    # 下面这些方法给控制台（UI）用：不改进程、不重启网关就能调整运行参数。
    # 注意它们只改**内存中的 Config**，落盘由 UI 层的 ConfigStore 负责。

    def add_key(
        self,
        key: str,
        *,
        account: str | None = None,
        rpm_limit: int | None = None,
        max_concurrency: int = 4,
        weight: float = 1.0,
    ) -> ApiKey:
        """运行中加一把 Key，立刻参与轮换。"""
        return self.pool.add_key(
            key,
            account or f"账号{len(self.config.accounts) + 1}",
            rpm_limit=rpm_limit,
            max_concurrency=max_concurrency,
            weight=weight,
        )

    def remove_key(self, key: str) -> bool:
        """运行中移除一把 Key；返回是否真的移除了。"""
        return self.pool.remove_key(key) is not None

    def set_default_model(self, model: str) -> str:
        """切换默认模型。下一次请求即生效（``_build_payload`` 每次现读配置）。"""
        model = (model or "").strip()
        if not model:
            raise ConfigError("模型名不能为空")
        self.config.default_model = model
        self._log(f"[配置] 默认模型切换为 {model}")
        return model

    def set_strategy(self, strategy: str) -> str:
        """切换调度策略（round_robin / least_inflight / least_recent / weighted）。"""
        if strategy not in STRATEGIES:
            raise ConfigError(f"strategy 必须是 {STRATEGIES} 之一，当前为 {strategy!r}")
        self.config.strategy = strategy
        self.pool.strategy = strategy
        self._log(f"[配置] 调度策略切换为 {strategy}")
        return strategy

    def set_rate_control(self, **changes: Any) -> dict[str, Any]:
        """调整主动限速参数，并按新模式重建限速器。

        可改字段：``mode`` / ``qps`` / ``min_qps`` / ``max_qps`` / ``decrease`` /
        ``increase_step`` / ``recovery_seconds``。只传要改的字段。

        重建会**丢掉 AIMD 已经收敛到的速率**（回到 ``qps`` 起点）。这是刻意的：
        用户改了参数，就应该从新起点重新探测。
        """
        unknown = set(changes) - set(RateControlConfig.__dataclass_fields__)
        if unknown:
            raise ConfigError(f"未知的限速参数: {sorted(unknown)}")
        cleaned = {k: v for k, v in changes.items() if v is not None}
        if not cleaned:
            return self.limiter.stats()
        # 先构造新对象做校验，校验通过再赋值——避免把配置改坏后无法回滚
        updated = replace(self.config.rate_control, **cleaned)
        self.config.rate_control = updated
        self.limiter = self._build_limiter(self.config, self._clock, self._sleep)
        self._log(
            f"[配置] 限速重建: mode={updated.mode} qps={updated.qps} "
            f"区间={updated.min_qps}~{updated.max_qps}"
        )
        return self.limiter.stats()

    def available_models(self, *, refresh: bool = False, ttl: float = 300.0) -> dict[str, Any]:
        """取上游模型清单（带缓存），供 UI 的模型选择器使用。

        这里**故意吞掉所有异常**：模型清单是锦上添花的东西，而上游连不上/限流时，
        恰恰是用户最需要打开控制台排查的时刻。如果让网络错误冒出去，整个状态接口会
        502，界面直接白屏——那就本末倒置了。失败信息记在返回值的 ``error`` 里，
        由界面负责显示。

        Returns:
            ``{"models": [...], "fetched_at": 时间戳, "cached": bool, "error": str}``
        """
        with self._models_lock:
            fresh = self._models_cache and (time.monotonic() - self._models_fetched_at) < ttl
            if fresh and not refresh:
                return {
                    "models": list(self._models_cache),
                    "fetched_at": self._models_fetched_at,
                    "cached": True,
                    "error": self._models_error,
                }
            try:
                payload = self.models()
                self._models_cache = _parse_model_list(payload)
                self._models_error = ""
            except Exception as exc:  # noqa: BLE001 - 清单拉取失败绝不能影响主流程
                # 故意宽catch。上游不可达时 _post_with_rotation 会把 NetworkError 包装成
                # RotationExhausted 抛出，但真实环境里还可能冒出别的类型（JSON 解析异常、
                # 上游返回结构变化导致的 KeyError…）。模型清单只是锦上添花，
                # 绝不能让它把状态接口带崩——上游出故障时用户正需要打开控制台排查。
                self._models_error = f"{type(exc).__name__}: {exc}"
                self._log(f"[警告] 拉取模型清单失败: {self._models_error}")
            self._models_fetched_at = time.monotonic()
            return {
                "models": list(self._models_cache),
                "fetched_at": self._models_fetched_at,
                "cached": False,
                "error": self._models_error,
            }

    # ------------------------------------------------------------ 内部：载荷

    def _build_payload(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str | None,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not messages:
            raise ValueError("messages 不能为空")
        payload: dict[str, Any] = {
            "model": model or self.config.default_model,
            "messages": list(messages),
        }
        payload.update(params)
        return payload

    def _backoff(self, attempt: int) -> float:
        delay = min(self.config.retry_backoff * (2 ** (attempt - 1)), self.config.max_retry_backoff)
        return delay + self._rng.uniform(0.0, delay * 0.3)

    @staticmethod
    def _auth(key: ApiKey) -> dict[str, str]:
        """把当前租约对应的 Key 注入请求头。"""
        return {"Authorization": f"Bearer {key.key}"}

    def _remaining(self, started_at: float, budget: float | None) -> float | None:
        """单请求剩余等待预算；None 表示不设上限。"""
        if budget is None:
            return None
        return budget - (self._clock() - started_at)

    def _acquire_timeout(self, remaining: float | None) -> float:
        """等 Key 的超时不能超过剩余预算，否则会白白挂死。"""
        if remaining is None:
            return self.config.acquire_timeout
        return max(0.001, min(self.config.acquire_timeout, remaining))

    def _sleep_between(self, attempt: int, started_at: float, budget: float | None) -> None:
        delay = self._backoff(attempt)
        remaining = self._remaining(started_at, budget)
        if remaining is not None:
            if remaining <= 0:
                return
            delay = min(delay, remaining)
        self._sleep(delay)

    def _warn_if_reasoning_ate_budget(self, result: Any) -> None:
        """推理模型的坑：max_tokens 太小会被 reasoning_content 吃光，content 返回空串。

        ``deepseek-v4-flash`` 这类推理模型先输出 ``reasoning_content`` 再输出 ``content``，
        两者共用 ``max_tokens`` 预算。如果预算不够，就会拿到空回复 + ``finish_reason=length``。
        这不算错误，但排查起来很费时间，所以主动提示一句。
        """
        if not isinstance(result, dict):
            return
        choices = result.get("choices") or []
        if not choices:
            return
        first = choices[0] or {}
        message = first.get("message") or {}
        if message.get("content"):
            return
        if first.get("finish_reason") != "length":
            return
        details = (result.get("usage") or {}).get("completion_tokens_details") or {}
        reasoning = details.get("reasoning_tokens")
        if reasoning:
            self._log(
                f"[警告] content 为空：{reasoning} 个 token 全部被 reasoning_content 消耗，"
                f"请调大 max_tokens（推理模型需要额外预算）"
            )

    # ------------------------------------------------------------ 内部：非流式

    def _post_with_rotation(
        self,
        path: str,
        payload: dict[str, Any] | None,
        *,
        attempts: int | None = None,
        method: str = "POST",
    ) -> dict[str, Any]:
        max_attempts = attempts or self.config.max_attempts
        excluded: set[str] = set()
        last_status: int | None = None
        last_body = ""
        last_exc: Exception | None = None
        started_at = self._clock()
        budget = self.config.max_total_wait or None

        for attempt in range(1, max_attempts + 1):
            remaining = self._remaining(started_at, budget)
            if remaining is not None and remaining <= 0:
                last_exc = last_exc or TimeoutError(f"超出单请求等待预算 {budget:g}s")
                self._log(f"[放弃] 超出等待预算 {budget:g}s")
                break
            try:
                key = self.pool.acquire(
                    exclude=excluded,
                    timeout=self._acquire_timeout(remaining),
                )
            except AllKeysInvalid:
                raise
            except NoAvailableKey as exc:
                last_exc = exc
                self._log(f"[放弃] 等待可用 Key 超时: {exc}")
                break

            retryable = False
            try:
                self.limiter.acquire()
                started = self._clock()
                self._note_upstream_attempt()
                response = self._client.request(
                    method, path, json_body=payload, headers=self._auth(key)
                )
                body = safe_text(response) if response.status >= 400 else ""
                action, retry_after = classify(response, body)

                if action == "ok":
                    self.pool.report_success(key, latency=self._clock() - started)
                    self.limiter.on_success()
                    result = response.json()
                    self._warn_if_reasoning_ate_budget(result)
                    return result

                last_status, last_body = response.status, body
                if action == "invalid":
                    self.pool.report_invalid(key, extract_error(body))
                    excluded.add(key.key)
                    self._log(f"[失效] {key.masked}({key.account}) 凭据无效，已排除: {extract_error(body)}")
                    retryable = True
                elif action == "retry":
                    if response.status == 429:
                        delay = self.pool.report_rate_limit(key, retry_after)
                        self.limiter.on_rate_limited()
                        self._log(
                            f"[限流] {key.masked}({key.account}) 429，冷却 {delay:.1f}s "
                            f"(第 {key.consecutive_failures} 次)"
                        )
                    else:
                        delay = self.pool.report_server_error(key, f"HTTP {response.status}")
                        self._log(f"[异常] {key.masked}({key.account}) {response.status}，冷却 {delay:.1f}s")
                    retryable = True
                else:
                    self.pool.report_client_error(key, extract_error(body))
                    raise ApiError(response.status, body)
            except NetworkError as exc:
                delay = self.pool.report_server_error(key, f"{type(exc).__name__}")
                last_exc = exc
                self._log(f"[网络] {key.masked}({key.account}) {exc}，冷却 {delay:.1f}s")
                retryable = True
            finally:
                self.pool.release(key)

            if retryable and attempt < max_attempts:
                self._sleep_between(attempt, started_at, budget)

        raise self._exhausted(max_attempts, last_status, last_body, last_exc)

    # ------------------------------------------------------------ 内部：流式

    def _stream_with_rotation(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        attempts: int | None = None,
        raw: bool = False,
    ) -> Iterator[Any]:
        max_attempts = attempts or self.config.max_attempts
        excluded: set[str] = set()
        last_status: int | None = None
        last_body = ""
        last_exc: Exception | None = None
        started_at = self._clock()
        budget = self.config.max_total_wait or None

        for attempt in range(1, max_attempts + 1):
            remaining = self._remaining(started_at, budget)
            if remaining is not None and remaining <= 0:
                last_exc = last_exc or TimeoutError(f"超出单请求等待预算 {budget:g}s")
                break
            try:
                key = self.pool.acquire(
                    exclude=excluded,
                    timeout=self._acquire_timeout(remaining),
                )
            except AllKeysInvalid:
                raise
            except NoAvailableKey as exc:
                last_exc = exc
                break

            retryable = False
            try:
                self.limiter.acquire()
                started = self._clock()
                self._note_upstream_attempt()
                response = self._client.request(
                    "POST", path, json_body=payload, headers=self._auth(key), stream=True
                )
                body = safe_text(response) if response.status >= 400 else ""
                action, retry_after = classify(response, body)

                if action == "ok":
                    emitted = False
                    try:
                        for chunk in self._iter_sse_chunks(response):
                            if raw:
                                emitted = True
                                yield chunk
                                continue
                            piece = self._chunk_text(chunk)
                            if not piece:
                                continue
                            emitted = True
                            yield piece
                    except NetworkError as exc:
                        last_exc = exc
                        if emitted:
                            # 已经吐字了，重试会导致内容重复
                            raise StreamInterrupted(f"流式响应中途断开: {exc}") from exc
                        self.pool.report_server_error(key, str(exc))
                        retryable = True
                    else:
                        self.pool.report_success(key, latency=self._clock() - started)
                        self.limiter.on_success()
                        return
                elif action == "invalid":
                    self.pool.report_invalid(key, extract_error(body))
                    excluded.add(key.key)
                    self._log(f"[失效] {key.masked}({key.account}) 凭据无效: {extract_error(body)}")
                    retryable = True
                elif action == "retry":
                    last_status, last_body = response.status, body
                    if response.status == 429:
                        delay = self.pool.report_rate_limit(key, retry_after)
                        self.limiter.on_rate_limited()
                        self._log(f"[限流] {key.masked}({key.account}) 429，冷却 {delay:.1f}s")
                    else:
                        delay = self.pool.report_server_error(key, f"HTTP {response.status}")
                        self._log(f"[异常] {key.masked}({key.account}) {response.status}，冷却 {delay:.1f}s")
                    retryable = True
                else:
                    self.pool.report_client_error(key, extract_error(body))
                    raise ApiError(response.status, body)
            except NetworkError as exc:
                delay = self.pool.report_server_error(key, type(exc).__name__)
                last_exc = exc
                self._log(f"[网络] {key.masked}({key.account}) {exc}，冷却 {delay:.1f}s")
                retryable = True
            finally:
                self.pool.release(key)

            if retryable and attempt < max_attempts:
                self._sleep_between(attempt, started_at, budget)

        raise self._exhausted(max_attempts, last_status, last_body, last_exc)

    @staticmethod
    def _iter_sse_chunks(response: Response | StreamResponse) -> Iterator[dict[str, Any]]:
        """解析 OpenAI 风格 SSE，逐块 yield 原始 JSON 对象（保留全部字段）。"""
        for line in response.iter_lines():
            if not line:
                continue
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            if not data:
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(chunk, dict):
                yield chunk

    @staticmethod
    def _chunk_text(chunk: Mapping[str, Any]) -> str | None:
        """从 chunk 里抽出文本增量；没有文本（如纯 tool_calls 块）返回 None。"""
        choices = chunk.get("choices") or []
        if not choices:
            return None
        first = choices[0] or {}
        delta = first.get("delta") or {}
        return delta.get("content") or first.get("text") or None

    @classmethod
    def _iter_sse(cls, response: Response | StreamResponse) -> Iterator[str]:
        """解析 OpenAI 风格 SSE，只 yield 文本增量。"""
        for chunk in cls._iter_sse_chunks(response):
            piece = cls._chunk_text(chunk)
            if piece:
                yield piece

    # ------------------------------------------------------------ 内部：异常

    def _exhausted(
        self,
        attempts: int,
        last_status: int | None,
        last_body: str,
        last_exc: Exception | None,
    ) -> RotatorError:
        detail = extract_error(last_body) if last_body else (str(last_exc) if last_exc else "未知原因")
        return RotationExhausted(
            f"已尝试 {attempts} 次仍失败，最后一次状态 {last_status or 'N/A'}：{detail}",
            attempts=attempts,
            last_status=last_status,
            last_body=last_body,
        )
