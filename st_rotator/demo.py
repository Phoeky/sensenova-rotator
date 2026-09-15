"""内置模拟上游：不需要真实 Key 就能验证轮换 / 冷却 / 失效识别是否生效。

模拟了三种真实世界里会遇到的坑：
1. 某把 Key 被持续限流（429 + Retry-After）
2. 某把 Key 是坏凭据（401）
3. **最阴的**：某把 Key 鉴权失败但网关返回 429 —— 如果只按状态码判断，就会在这把
   坏 Key 上无限轮换。本工具靠响应体关键词把它识别为"失效"。
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Any

from .config import AccountConfig, Config, CooldownConfig, RateControlConfig
from .transport import HttpClient, Request, Response

# 模拟上游用的占位模型名。这里刻意不写任何真实服务商的模型名——
# 演示完全离线（走 FakeUpstream），写真实模型名只会让人误以为它可用。
DEMO_MODEL = "demo-model"


@dataclass
class FakeUpstream:
    """可配置的假上游网关。"""

    invalid_keys: set[str] = field(default_factory=set)        # 返回 401
    always_limited: set[str] = field(default_factory=set)      # 持续 429
    auth_as_429: set[str] = field(default_factory=set)         # 429 但其实是鉴权失败
    flaky_rate: float = 0.12                                   # 随机 429 概率
    latency: tuple[float, float] = (0.01, 0.04)
    hits: dict[str, int] = field(default_factory=dict)
    rate_limited_total: int = 0

    def handle(self, request: Request) -> Response:
        key = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        self.hits[key] = self.hits.get(key, 0) + 1
        time.sleep(random.uniform(*self.latency))

        path = request.path
        if path.rstrip("/").endswith("models"):
            if key in self.invalid_keys:
                return Response(401, {}, b'{"error":{"message":"invalid api key"}}')
            return Response(200, {}, json.dumps({"data": [{"id": DEMO_MODEL, "object": "model"}]}).encode())

        if key in self.invalid_keys:
            return Response(401, {}, b'{"error":{"message":"invalid api key"}}')
        if key in self.auth_as_429:
            self.rate_limited_total += 1
            return Response(
                429,
                {"retry-after": "5"},
                json.dumps({"error": {"message": "authentication failed: api key is invalid", "code": 429}}).encode(),
            )
        if key in self.always_limited:
            self.rate_limited_total += 1
            return Response(
                429,
                {"retry-after": "2"},
                json.dumps({"error": {"message": "Requests rate limit exceeded, retry_after: 2"}}).encode(),
            )
        if random.random() < self.flaky_rate:
            self.rate_limited_total += 1
            return Response(429, {}, b'{"error":{"message":"Too Many Requests"}}')

        try:
            payload = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            payload = {}
        reply = "你好，我是模拟上游返回的回复。"

        if payload.get("stream"):
            chunks = [
                "data: " + json.dumps({"choices": [{"delta": {"content": char}}]}, ensure_ascii=False) + "\n\n"
                for char in reply
            ]
            chunks.append("data: [DONE]\n\n")
            return Response(
                200,
                {"content-type": "text/event-stream"},
                "".join(chunks).encode("utf-8"),
            )

        return Response(200, {}, json.dumps({
            "id": "chatcmpl-demo",
            "object": "chat.completion",
            "model": payload.get("model", DEMO_MODEL),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 14, "total_tokens": 26},
        }, ensure_ascii=False).encode("utf-8"))


def demo_config() -> Config:
    """一份带"问题 Key"的演示配置。"""
    return Config(
        base_url="https://demo.local/v1",
        default_model=DEMO_MODEL,
        accounts=[
            AccountConfig(name="acct-A", api_keys=["sk-demo-A1", "sk-demo-A2"], max_concurrency=3, rpm_limit=60),
            AccountConfig(name="acct-B", api_keys=["sk-demo-B1", "sk-demo-B2"], max_concurrency=3, rpm_limit=60),
            AccountConfig(name="acct-C", api_keys=["sk-demo-C1"], max_concurrency=2, rpm_limit=60),
        ],
        max_attempts=6,
        retry_backoff=0.1,
        max_retry_backoff=0.5,
        acquire_timeout=15.0,
        cooldown=CooldownConfig(base=0.4, factor=1.6, max=3.0, jitter=0.2, invalid_ttl=60.0, server_error=0.2),
        # 演示自适应限速：从 20 QPS 起步，撞 429 就降，干净久了再慢慢升
        rate_control=RateControlConfig(
            mode="adaptive",
            qps=20.0,
            min_qps=2.0,
            max_qps=60.0,
            decrease=0.6,
            increase_step=2.0,
            recovery_seconds=1.0,
        ),
    )


def demo_upstream() -> FakeUpstream:
    """让 A2 持续被限流、B2 是"429 伪装"的坏 Key、C1 是 401 坏 Key。"""
    return FakeUpstream(
        always_limited={"sk-demo-A2"},
        auth_as_429={"sk-demo-B2"},
        invalid_keys={"sk-demo-C1"},
    )


def build_demo_rotator(**kwargs: Any):
    """构造一个完全离线、跑在模拟上游上的 rotator。"""
    from .client import StRotator

    upstream = demo_upstream()
    config = demo_config()
    client = HttpClient(
        config.base_url,
        timeout=config.timeout,
        connect_timeout=config.connect_timeout,
        handler=upstream.handle,
    )
    rotator = StRotator(config, client=client, **kwargs)
    return rotator, upstream
