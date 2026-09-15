"""批量调用示例：多线程跑一批 prompt，统计成功率。

运行::

    python examples/batch_chat.py "总结这段话" "写一个正则" "解释 TCP 三次握手"
"""

from __future__ import annotations

import concurrent.futures as futures
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sensenova_rotator import Config, RotationExhausted, SenseNovaRotator  # noqa: E402

PROMPTS = [
    "用一句话解释什么是幂等性",
    "把 'Hello World' 翻译成日语",
    "写一个匹配邮箱的正则表达式",
    "解释一下 TCP 三次握手",
    "给一个 Python 快速排序的实现",
    "什么是 CAP 定理",
    "用 3 个要点说明为什么要做限流",
    "把这句话改写得更正式：这玩意儿不太好使",
]


def ask(rotator: SenseNovaRotator, prompt: str) -> tuple[str, str]:
    try:
        response = rotator.chat([{"role": "user", "content": prompt}], max_tokens=200)
        return prompt, response["choices"][0]["message"]["content"].strip()
    except RotationExhausted as exc:
        return prompt, f"[失败] {exc}"


def main(prompts: list[str]) -> int:
    config = Config.from_file(Path(__file__).resolve().parent.parent / "config.json")
    started = time.perf_counter()

    with SenseNovaRotator(config, logger=lambda m: print("  " + m, file=sys.stderr)) as rotator:
        with futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda p: ask(rotator, p), prompts))

        for prompt, answer in results:
            print(f"\nQ: {prompt}\nA: {answer}")

        summary = rotator.pool.summary()
        failed = sum(1 for _, answer in results if answer.startswith("[失败]"))
        print(
            f"\n完成 {len(results) - failed}/{len(results)} 条，耗时 {time.perf_counter() - started:.1f}s"
        )
        print(
            f"Key 池: 共 {summary['total']} 把 | 可用 {summary['healthy']} | "
            f"冷却中 {summary['cooldown']} | 失效 {summary['invalid']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or PROMPTS))
