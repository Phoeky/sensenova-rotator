"""命令行入口。

    python -m st_rotator ui                          # 图形控制台（推荐）
    python -m st_rotator demo                        # 离线演示，不需要真实 Key
    python -m st_rotator check -c config.json        # 逐把 Key 体检
    python -m st_rotator status -c config.json       # 看当前池状态
    python -m st_rotator chat  -c config.json "你好"
    python -m st_rotator serve -c config.json        # 只起网关，无界面
    python -m st_rotator bench -c config.json -n 200 -p 16
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import secrets
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from .client import StRotator
from .config import Config, ConfigStore, RateControlConfig
from .errors import ApiError, NoAvailableKey, RotationExhausted, RotatorError, StreamInterrupted
from .keypool import mask_key

# ---------------------------------------------------------------- 输出工具

_WIDTHS = {"account": 12, "key": 18, "status": 10}


def _print_table(rows: Sequence[dict[str, Any]], columns: Sequence[tuple[str, str]]) -> None:
    if not rows:
        print("(空)")
        return
    widths = []
    for field, title in columns:
        width = max(len(title), max(len(str(r.get(field, ""))) for r in rows))
        widths.append(min(width, 34))
    header = "  ".join(title.ljust(w) for (_, title), w in zip(columns, widths))
    print(header)
    print("-" * len(header))
    for row in rows:
        print("  ".join(str(row.get(field, ""))[:w].ljust(w) for (field, _), w in zip(columns, widths)))


def _print_pool(rotator: StRotator, title: str = "Key 池状态") -> None:
    data = rotator.status()
    summary = data["summary"]
    print(f"\n=== {title} ===")
    print(
        f"总计 {summary['total']} 把 | 可用 {summary['healthy']} | 冷却中 {summary['cooldown']} "
        f"| 失效 {summary['invalid']} | 在途 {summary['inflight']}"
    )
    rate = data.get("rate_control") or {}
    if rate.get("mode") == "adaptive":
        print(
            f"限速: 自适应 AIMD | 当前 {rate['rate']} req/s "
            f"(区间 {rate['min_rate']}~{rate['max_rate']}) "
            f"| 降速 {rate['penalties']} 次 / 提速 {rate['raises']} 次"
        )
    elif rate.get("mode") == "fixed":
        print(f"限速: 固定 {rate['rate']} req/s")
    else:
        print("限速: 关闭（不限速）")
    rows = []
    for item in data["keys"]:
        stats = item["stats"]
        rows.append({
            "account": item["account"],
            "key": item["key"],
            "status": item["status"],
            "cooldown": f"{item['cooldown_remaining']}s" if item["cooldown_remaining"] else "-",
            "ok/fail": f"{stats['successes']}/{stats['failures']}",
            "429": stats["rate_limited"],
            "avg_ms": stats["avg_latency_ms"],
            "note": item["last_error"][:40],
        })
    _print_table(rows, [
        ("account", "账号"), ("key", "Key"), ("status", "状态"), ("cooldown", "冷却"),
        ("ok/fail", "成功/失败"), ("429", "429次数"), ("avg_ms", "平均延迟(ms)"), ("note", "最近错误"),
    ])


def _percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(round((pct / 100.0) * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


# ---------------------------------------------------------------- 各子命令


def cmd_demo(args: argparse.Namespace) -> int:
    """离线演示：完全跑在模拟上游上，用于确认轮换逻辑生效。"""
    from .demo import build_demo_rotator

    logs: list[str] = []
    rotator, upstream = build_demo_rotator(logger=lambda m: logs.append(m))

    print("模拟上游已就绪：")
    print("  - sk-demo-A2  持续被限流（429 + Retry-After）")
    print("  - sk-demo-B2  鉴权失败但伪装成 429（检验失效识别）")
    print("  - sk-demo-C1  坏凭据（401）")
    print("  - 另有 12% 的随机 429 抖动")
    print("限速：自适应 AIMD，从 20 req/s 起步，撞 429 就降速，干净久了再提速")

    print("\n[1/3] 非流式对话…")
    resp = rotator.chat([{"role": "user", "content": "你好"}])
    print("  回复:", resp["choices"][0]["message"]["content"])

    print("\n[2/3] 流式对话…")
    print("  回复: ", end="", flush=True)
    for piece in rotator.chat_stream([{"role": "user", "content": "你好"}]):
        print(piece, end="", flush=True)
    print()

    print(f"\n[3/3] 并发压测 {args.requests} 次请求 / {args.parallel} 并发…")
    stats = _run_bench(rotator, args.requests, args.parallel, max_tokens=None)
    _print_bench(stats)

    print("\n轮换日志（节选）:")
    for line in logs[:8]:
        print("  " + line)
    if len(logs) > 8:
        print(f"  … 共 {len(logs)} 条")

    print(f"\n模拟上游收到的 429 总数: {upstream.rate_limited_total}")
    _print_pool(rotator, "演示结束后的 Key 池")
    rotator.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = Config.from_file(args.config)
    rc = config.rate_control
    print(
        f"网关: {config.base_url} | 默认模型: {config.default_model} | 策略: {config.strategy}\n"
        f"限速: {rc.mode}"
        + (f" (qps={rc.qps})" if rc.mode != "off" else "")
        + f" | 单请求预算: {config.max_total_wait or '不限'}s | 最大重试: {config.max_attempts}"
    )
    rotator = StRotator(config)
    try:
        _print_pool(rotator, "Key 池状态（尚未发过请求）")
        print("\n提示: status 只反映本地池状态，要验证凭据是否有效请用 check。")
    finally:
        rotator.close()
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """逐把 Key 单独体检，确认哪些是好的、哪些要换掉。"""
    config = Config.from_file(args.config)
    rotator = StRotator(config)
    rows = []
    try:
        for key in rotator.pool.keys:
            ok, detail = rotator.verify_key(key)
            rows.append({
                "account": key.account,
                "key": key.masked,
                "result": "OK" if ok else "FAIL",
                "detail": detail[:60],
            })
        _print_table(rows, [
            ("account", "账号"), ("key", "Key"), ("result", "结果"), ("detail", "详情"),
        ])
        good = sum(1 for r in rows if r["result"] == "OK")
        print(f"\n可用 {good} / {len(rows)} 把")
        return 0 if good == len(rows) else 1
    finally:
        rotator.close()


def cmd_chat(args: argparse.Namespace) -> int:
    config = Config.from_file(args.config)
    with StRotator(config, logger=lambda m: print("  " + m, file=sys.stderr)) as rotator:
        messages = [{"role": "user", "content": args.prompt}]
        if args.stream:
            for piece in rotator.chat_stream(messages, model=args.model):
                print(piece, end="", flush=True)
            print()
        else:
            resp = rotator.chat(messages, model=args.model)
            print(resp["choices"][0]["message"]["content"])
        if args.verbose:
            _print_pool(rotator)
    return 0


def _run_bench(
    rotator: StRotator,
    total: int,
    parallel: int,
    *,
    max_tokens: int | None = 64,
) -> dict[str, Any]:
    """并发打请求，统计成功率与延迟分布。

    注意：``max_tokens`` 默认给一个较小的值。推理模型（如 deepseek-v4-flash）会先写
    ``reasoning_content``，不设上限时单请求可能要跑十几秒，那样量到的是**模型生成速度**
    而不是**限流容量**。做容量评估必须把输出压到很短。
    """
    lock = threading.Lock()
    latencies: list[float] = []
    outcomes = {"success": 0, "rate_limited": 0, "invalid": 0, "failed": 0}
    extra: dict[str, Any] = {} if max_tokens is None else {"max_tokens": max_tokens}

    def one(index: int) -> None:
        started = time.perf_counter()
        try:
            rotator.chat([{"role": "user", "content": f"ping #{index}"}], **extra)
        except RotationExhausted as exc:
            with lock:
                outcomes["rate_limited" if exc.last_status == 429 else "failed"] += 1
            return
        except NoAvailableKey:
            with lock:
                outcomes["failed"] += 1
            return
        except ApiError:
            with lock:
                outcomes["invalid"] += 1
            return
        except StreamInterrupted:
            with lock:
                outcomes["failed"] += 1
            return
        with lock:
            outcomes["success"] += 1
            latencies.append(time.perf_counter() - started)

    wall_start = time.perf_counter()
    with futures.ThreadPoolExecutor(max_workers=parallel) as executor:
        list(executor.map(one, range(total)))
    wall = time.perf_counter() - wall_start

    return {
        "total": total,
        "parallel": parallel,
        "max_tokens": max_tokens,
        "wall": wall,
        "qps": total / wall if wall else 0.0,
        "latencies": latencies,
        **outcomes,
    }


def _print_bench(stats: dict[str, Any]) -> None:
    lat = stats["latencies"]
    print(f"\n=== 压测结果 ===")
    print(f"请求总数      : {stats['total']}  (并发 {stats['parallel']})")
    print(f"max_tokens    : {stats['max_tokens'] if stats['max_tokens'] else '未设限（延迟会含模型生成时间）'}")
    print(f"耗时          : {stats['wall']:.2f}s  →  实际吞吐 {stats['qps']:.2f} req/s")
    print(f"成功          : {stats['success']}  ({stats['success'] / stats['total']:.1%})")
    print(f"重试后仍 429  : {stats['rate_limited']}")
    print(f"其他失败      : {stats['failed']}")
    if lat:
        print(
            f"延迟 p50/p90/p99: {_percentile(lat, 50) * 1000:.0f}ms / "
            f"{_percentile(lat, 90) * 1000:.0f}ms / {_percentile(lat, 99) * 1000:.0f}ms"
        )


def _build_runtime(
    args: argparse.Namespace,
    *,
    default_log_file: str | None = None,
) -> tuple[Any, Any, Any, Any, Any]:
    """按 CLI 参数装配「配置 + 日志出口 + 轮换客户端」。

    返回 ``(store, config, sink, rotator, buffer)``。``store`` 只在需要落盘的场景
    （``ui``）才构造——命令行一次性任务不需要写回配置文件。
    """
    from .logs import LogBuffer, build_logger, make_log_sink

    store = None
    if getattr(args, "persist", False):
        store = ConfigStore.load(args.config)
        config = store.config
    else:
        config = Config.from_file(args.config)

    if getattr(args, "rate_mode", None):
        config.rate_control = replace(config.rate_control, mode=args.rate_mode)
    if getattr(args, "qps", 0.0):
        config.rate_control = replace(config.rate_control, qps=args.qps)

    log_file = getattr(args, "log_file", None)
    if log_file == "":
        log_file = default_log_file
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    buffer = LogBuffer(maxlen=getattr(args, "log_lines", 500))
    logger = build_logger(log_file) if log_file else None
    echo = None
    if getattr(args, "verbose", False):
        echo = lambda message: print("  " + message, file=sys.stderr)  # noqa: E731
    sink = make_log_sink(logger, buffer, echo)
    rotator = StRotator(config, logger=sink)
    return store, config, sink, rotator, buffer


def cmd_serve(args: argparse.Namespace) -> int:
    """起一个本地 OpenAI 兼容网关（无界面），供 WorkBuddy 等上层应用接入。"""
    from .proxy import serve

    _store, config, sink, rotator, _buffer = _build_runtime(args)
    sink(
        f"启动网关 host={args.host} port={args.port} keys={config.total_keys} "
        f"限速={config.rate_control.mode} 模型={config.default_model}"
    )
    try:
        serve(
            rotator,
            host=args.host,
            port=args.port,
            token=args.token,
            verbose=args.verbose,
            log_sink=sink,
        )
    finally:
        rotator.close()
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    """启动网关 + 图形控制台，并自动开一个无地址栏的应用窗口。"""
    from .proxy import serve
    from .ui import ConsoleState, open_console_window, open_in_default_browser

    default_log = str(Path(args.config).resolve().parent / "rotator.log")
    store, config, sink, rotator, buffer = _build_runtime(args, default_log_file=default_log)
    console = ConsoleState(
        store=store,
        rotator=rotator,
        host=args.host,
        port=args.port,
        token=args.token,
        buffer=buffer,
        log_file=args.log_file or default_log,
    )

    sink(
        f"启动控制台 host={args.host} port={args.port} keys={config.total_keys} "
        f"模型={config.default_model} 限速={config.rate_control.mode}"
    )
    if args.token:
        sink("[控制台] 已启用本地鉴权；窗口会自动带入 Token，无需手输")

    def on_ready(server: Any) -> None:
        host, port = server.server_address[:2]
        url = f"http://{host}:{port}/"
        if args.no_open:
            sink(f"[控制台] 已就绪（--no-open 未开窗）：{url}")
            return
        # Token 放 URL fragment：fragment 不会发给服务端，也不进 Referer
        target = f"{url}#token={args.token}" if args.token else url
        ok, note = open_console_window(target, browser=args.browser, size=args.window_size)
        if not ok:
            sink(f"[警告] {note}")
            if open_in_default_browser(url):
                sink(f"[控制台] 已改用默认浏览器打开：{url}")
        else:
            sink(f"[控制台] {note}：{url}")

    try:
        serve(
            rotator,
            host=args.host,
            port=args.port,
            token=args.token,
            verbose=args.verbose,
            log_sink=sink,
            console=console,
            on_ready=on_ready,
        )
    finally:
        rotator.close()
    return 0


def _load_or_create_token(directory: Path) -> str:
    """托盘模式自动准备一个本地口令。

    不设口令等于本机任何进程都能白嫖你的 Key，所以托盘模式**默认就带口令**；
    口令落盘复用，这样重启后书签里的控制台地址依然有效，不用每次重新找。
    """
    path = directory / ".tray-token"
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(18)
    try:
        path.write_text(token, encoding="utf-8")
    except OSError:
        pass  # 写不进去也要能跑，只是下次会换一个新口令
    return token


def cmd_tray(args: argparse.Namespace) -> int:
    """启动网关 + 控制台，并常驻系统托盘。

    与 ``ui`` 的区别：**不弹浏览器窗口**，而是在托盘放一个图标；点它才打开控制台。
    适合「开着就不管了」的日常使用方式。
    """
    from .proxy import create_server
    from .tray import TrayApp, acquire_single_instance
    from .ui import ConsoleState, open_console_window, open_in_default_browser

    config_path = Path(args.config).resolve()
    default_log = str(config_path.parent / "rotator.log")
    store, config, sink, rotator, buffer = _build_runtime(args, default_log_file=default_log)
    token = args.token or _load_or_create_token(config_path.parent)

    url_base = f"http://{args.host}:{args.port}/"

    if not acquire_single_instance():
        sink(f"[托盘] 已有实例在运行，改为打开它的控制台：{url_base}")
        target = f"{url_base}#token={token}" if token else url_base
        if not open_console_window(target, browser=args.browser, size=args.window_size)[0]:
            open_in_default_browser(url_base)
        rotator.close()
        return 0

    console = ConsoleState(
        store=store,
        rotator=rotator,
        host=args.host,
        port=args.port,
        token=token,
        buffer=buffer,
        log_file=args.log_file or default_log,
    )

    try:
        server = create_server(
            rotator, host=args.host, port=args.port, token=token,
            verbose=args.verbose, log_sink=sink, console=console,
        )
    except OSError as exc:
        sink(f"[错误] 无法监听 {args.host}:{args.port}：{exc}")
        rotator.close()
        return 1

    host, port = server.server_address[:2]
    console.host, console.port = host, port
    base_url = f"http://{host}:{port}/v1"
    url = f"http://{host}:{port}/"

    gateway = threading.Thread(target=server.serve_forever, name="rotator-gateway", daemon=True)
    gateway.start()
    sink(f"网关已启动: {base_url}")
    sink(f"[托盘] 控制台: {url}（右键托盘图标可操作）")

    app = TrayApp(
        rotator=rotator,
        console=console,
        url=url,
        base_url=base_url,
        token=token,
        icon_dir=config_path.parent / ".tray-icons",
        log_file=Path(args.log_file or default_log),
        open_url=lambda target: open_console_window(
            target, browser=args.browser, size=args.window_size
        )[0],
        log=sink,
    )
    try:
        app.run()
    except KeyboardInterrupt:
        sink("[托盘] 收到中断信号")
    finally:
        sink("[托盘] 正在停止网关…")
        server.shutdown()
        server.server_close()
        gateway.join(timeout=5)
        rotator.close()
        sink("[托盘] 已全部停止")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    config = Config.from_file(args.config)
    if args.rate_mode:
        config.rate_control = replace(config.rate_control, mode=args.rate_mode)
    if args.qps:
        config.rate_control = replace(config.rate_control, qps=args.qps)
    if args.max_wait is not None:
        config.max_total_wait = args.max_wait
    if args.attempts is not None:
        config.max_attempts = args.attempts
    RateControlConfig(
        mode=config.rate_control.mode,
        qps=config.rate_control.qps,
        min_qps=config.rate_control.min_qps,
        max_qps=config.rate_control.max_qps,
        decrease=config.rate_control.decrease,
        increase_step=config.rate_control.increase_step,
        recovery_seconds=config.rate_control.recovery_seconds,
    )  # 复用配置校验，防止 CLI 覆盖出非法组合
    rc = config.rate_control
    print(
        f"目标: {config.total_keys} 把 Key / {len(config.accounts)} 个账号 | 策略: {config.strategy}\n"
        f"限速: {rc.mode}"
        + (f" (起始 {rc.qps} req/s，区间 {rc.min_qps}~{rc.max_qps})" if rc.mode == "adaptive" else
           f" ({rc.qps} req/s)" if rc.mode == "fixed" else " (关闭)")
        + f" | 单请求预算: {config.max_total_wait or '不限'}s | 最大重试: {config.max_attempts}"
    )
    with StRotator(config, logger=(lambda m: print("  " + m, file=sys.stderr)) if args.verbose else None) as rotator:
        print(f"开始压测: {args.requests} 请求 / {args.parallel} 并发…")
        max_tokens = args.max_tokens or None
        stats = _run_bench(rotator, args.requests, args.parallel, max_tokens=max_tokens)
        _print_bench(stats)
        _print_pool(rotator, "压测结束后的 Key 池")
    return 0


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="st-rotator",
        description="多账户多 Key 轮换工具（限流自愈，面向 OpenAI 兼容端点）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_config(p: argparse.ArgumentParser) -> None:
        p.add_argument("-c", "--config", help="JSON 配置文件路径", required=True)

    def add_logging(p: argparse.ArgumentParser, *, file_log_default_on: bool) -> None:
        """挂上日志参数。

        ``file_log_default_on`` 决定「不传 ``--log-file``」时的行为：
        ``ui`` 默认落文件（事后要能翻日志），``serve`` 默认只打终端（前台调试够用）。

        这个参数**同时**决定 ``default`` 和 ``help`` 文案，必须传一致，否则会出现
        「帮助里写着会写 rotator.log、实际没写」这种自相矛盾——曾因为参数名叫
        ``default_off`` 而被两处当成相反含义用，导致 ``ui`` 的日志文件永远不生成。
        """
        p.add_argument(
            "--log-file",
            # "" 是哨兵值，表示"用调用方给的默认路径"（见 _build_runtime）。
            # None 表示"压根不写文件"。
            default="" if file_log_default_on else None,
            help="日志文件路径；不传则用配置文件同目录下的 rotator.log"
            if file_log_default_on
            else "日志文件路径；不传则只输出到终端",
        )
        p.add_argument("--log-lines", type=int, default=500, help="内存中保留的日志行数（控制台用）")

    p_ui = sub.add_parser("ui", help="启动图形控制台（推荐入口）")
    add_config(p_ui)
    p_ui.add_argument("--host", default="127.0.0.1", help="监听地址，默认只监听本机")
    p_ui.add_argument("--port", type=int, default=8080, help="监听端口")
    p_ui.add_argument("--token", default=None, help="本地鉴权 Token；设置后控制台与 API 都需要它")
    p_ui.add_argument("--no-open", action="store_true", help="只起服务，不自动打开窗口")
    p_ui.add_argument("--browser", default=None, help="指定浏览器可执行文件（默认自动找 Edge/Chrome）")
    p_ui.add_argument("--window-size", default="1380,900", help="窗口尺寸，形如 1380,900")
    p_ui.add_argument("--rate-mode", choices=RateControlConfig.MODES, default=None, help="覆盖限速模式")
    p_ui.add_argument("-q", "--qps", type=float, default=0.0, help="覆盖 rate_control.qps")
    add_logging(p_ui, file_log_default_on=True)
    p_ui.add_argument("-v", "--verbose", action="store_true", help="同时把日志打到终端")
    p_ui.set_defaults(func=cmd_ui, persist=True)

    p_tray = sub.add_parser(
        "tray",
        help="启动网关并常驻系统托盘（不弹窗口，点托盘图标开控制台）",
    )
    add_config(p_tray)
    p_tray.add_argument("--host", default="127.0.0.1", help="监听地址，默认只监听本机")
    p_tray.add_argument("--port", type=int, default=8080, help="监听端口")
    p_tray.add_argument(
        "--token", default=None,
        help="本地鉴权 Token；不传则自动生成并复用配置目录下的 .tray-token",
    )
    p_tray.add_argument("--browser", default=None, help="指定浏览器可执行文件（默认自动找 Edge/Chrome）")
    p_tray.add_argument("--window-size", default="1380,900", help="控制台窗口尺寸，形如 1380,900")
    p_tray.add_argument("--rate-mode", choices=RateControlConfig.MODES, default=None, help="覆盖限速模式")
    p_tray.add_argument("-q", "--qps", type=float, default=0.0, help="覆盖 rate_control.qps")
    add_logging(p_tray, file_log_default_on=True)
    p_tray.add_argument("-v", "--verbose", action="store_true", help="同时把日志打到终端")
    p_tray.set_defaults(func=cmd_tray, persist=True)

    p_demo = sub.add_parser("demo", help="离线演示：用模拟上游验证轮换逻辑，无需真实 Key")
    p_demo.add_argument("-n", "--requests", type=int, default=60, help="演示压测请求数")
    p_demo.add_argument("-p", "--parallel", type=int, default=12, help="并发数")
    p_demo.set_defaults(func=cmd_demo)

    p_status = sub.add_parser("status", help="查看 Key 池当前状态")
    add_config(p_status)
    p_status.set_defaults(func=cmd_status)

    p_check = sub.add_parser("check", help="逐把 Key 单独体检（调 /models）")
    add_config(p_check)
    p_check.set_defaults(func=cmd_check)

    p_chat = sub.add_parser("chat", help="发一次对话")
    add_config(p_chat)
    p_chat.add_argument("prompt", help="用户输入")
    p_chat.add_argument("-m", "--model", help="模型名，默认取配置")
    p_chat.add_argument("-s", "--stream", action="store_true", help="流式输出")
    p_chat.add_argument("-v", "--verbose", action="store_true", help="结束后打印 Key 池状态")
    p_chat.set_defaults(func=cmd_chat)

    p_serve = sub.add_parser("serve", help="启动本地 OpenAI 兼容网关（无界面）")
    add_config(p_serve)
    p_serve.add_argument("--host", default="127.0.0.1", help="监听地址，默认只监听本机")
    p_serve.add_argument("--port", type=int, default=8080, help="监听端口")
    p_serve.add_argument("--token", default=None, help="本地鉴权 Token；不设则任何本机进程都能调用")
    p_serve.add_argument("--rate-mode", choices=RateControlConfig.MODES, default=None, help="覆盖限速模式")
    p_serve.add_argument("-q", "--qps", type=float, default=0.0, help="覆盖 rate_control.qps")
    add_logging(p_serve, file_log_default_on=False)
    p_serve.add_argument("-v", "--verbose", action="store_true", help="打印每个请求与轮换日志")
    p_serve.set_defaults(func=cmd_serve)

    p_bench = sub.add_parser("bench", help="并发压测，验证轮换效果")
    add_config(p_bench)
    p_bench.add_argument("-n", "--requests", type=int, default=200, help="总请求数")
    p_bench.add_argument("-p", "--parallel", type=int, default=16, help="并发数")
    p_bench.add_argument("-q", "--qps", type=float, default=0.0, help="覆盖 rate_control.qps")
    p_bench.add_argument(
        "--rate-mode",
        choices=RateControlConfig.MODES,
        default=None,
        help="覆盖限速模式：off / fixed / adaptive",
    )
    p_bench.add_argument("--max-wait", type=float, default=None, help="覆盖单请求等待预算（秒）")
    p_bench.add_argument("--attempts", type=int, default=None, help="覆盖最大重试次数")
    p_bench.add_argument(
        "--max-tokens", type=int, default=64,
        help="压测请求的 max_tokens（默认 64）。推理模型不设限会让延迟被生成时间主导，量不到真实容量；传 0 表示不设限",
    )
    p_bench.add_argument("-v", "--verbose", action="store_true", help="打印每次轮换日志")
    p_bench.set_defaults(func=cmd_bench)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except RotatorError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"错误: 找不到文件 {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    # 少了这一段，`python -m st_rotator.cli ...` 会静默什么都不做：
    # 模块被导入但不执行 main()，退出码 0、无任何输出，排查起来非常费劲。
    # 文档推荐用 `python -m st_rotator ...`（走 __main__.py），
    # 这里补上是为了让两种写法行为一致。
    raise SystemExit(main())
