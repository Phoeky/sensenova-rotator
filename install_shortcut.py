"""创建「Key 轮换网关」快捷方式。

目标：让用户**双击就能启动**托盘程序，不用记命令行，也不会闪出黑色控制台窗口。

做法是生成一个指向 ``pythonw.exe`` 的 ``.lnk``（Windows 原生快捷方式）::

    pythonw.exe -m st_rotator tray -c config.json

**为什么不包一层 .bat / .vbs**

- ``.bat``：一定会闪一下黑色控制台窗口，观感很差。
- ``.vbs``：没有闪窗问题，但脚本文件的编码很坑——``wscript.exe`` 默认按 ANSI 解析，
  UTF-8 保存的中文会变成乱码甚至直接语法错误。
- ``.lnk``：Windows 原生，无闪窗、可带图标、可设工作目录，也没有编码问题。

用法::

    python install_shortcut.py                 # 装到项目目录
    python install_shortcut.py --desktop       # 同时装到桌面
    python install_shortcut.py --port 8899     # 指定端口
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SHORTCUT_NAME = "Key 轮换网关"
DEFAULT_PORT = 8899


def find_pythonw() -> Path:
    """找出 ``pythonw.exe``。

    优先读 ``python-path.txt``（记录着本项目的解释器），避免机器上装了多个 Python 时
    误用 PATH 里那一个——那个可能没装本项目需要的版本。
    """
    marker = ROOT / "python-path.txt"
    candidates: list[Path] = []
    if marker.exists():
        recorded = marker.read_text(encoding="utf-8").strip()
        if recorded:
            candidates.append(Path(recorded).with_name("pythonw.exe"))
    candidates.append(Path(sys.executable).with_name("pythonw.exe"))

    for candidate in candidates:
        if candidate.exists():
            return candidate
    # 都没有就退回 PATH（运行时由系统解析）
    return Path("pythonw.exe")


def ensure_python_marker() -> None:
    marker = ROOT / "python-path.txt"
    if not marker.exists():
        marker.write_text(sys.executable, encoding="utf-8")
        print(f"已记录解释器路径：{marker}")


def create_shortcut(lnk: Path, *, target: Path, arguments: str, icon: Path) -> None:
    """用 PowerShell 的 WScript.Shell 创建 .lnk（Python 标准库没有这个能力）。"""
    script = f'''
$ErrorActionPreference = "Stop"
$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut("{lnk}")
$sc.TargetPath = "{target}"
$sc.Arguments = "{arguments}"
$sc.WorkingDirectory = "{ROOT}"
$sc.IconLocation = "{icon},0"
$sc.Description = "{SHORTCUT_NAME} - 多 Key 轮换网关（系统托盘常驻）"
$sc.Save()
Write-Output "OK"
'''
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0 or "OK" not in (result.stdout or ""):
        raise RuntimeError(
            f"创建快捷方式失败：{result.stdout.strip()} {result.stderr.strip()}"
        )


def ensure_app_icon() -> Path:
    """确保 ``assets/app.ico`` 存在。

    图标是用 ``trayicons`` 现场画出来的，不需要把二进制文件塞进仓库。
    """
    icon = ROOT / "assets" / "app.ico"
    if icon.exists():
        return icon
    try:
        sys.path.insert(0, str(ROOT))
        from st_rotator.trayicons import (  # noqa: PLC0415
            COLOR_HEALTHY,
            ICON_SIZES,
            build_ico,
        )

        icon.parent.mkdir(parents=True, exist_ok=True)
        icon.write_bytes(build_ico(ICON_SIZES, COLOR_HEALTHY))
        print(f"已生成图标：{icon}")
    except Exception as exc:  # noqa: BLE001 - 图标只是锦上添花，不该挡住建快捷方式
        print(f"警告：生成图标失败（{exc}），快捷方式将使用默认图标。", file=sys.stderr)
    return icon


def desktop_dir() -> Path | None:
    """取桌面路径。注意 OneDrive 会把桌面重定向，所以问系统而不是拼 ~/Desktop。"""
    script = '[Environment]::GetFolderPath("Desktop")'
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    path = (result.stdout or "").strip()
    return Path(path) if path else None


def main() -> int:
    parser = argparse.ArgumentParser(description="创建Key 轮换网关的快捷启动入口")
    parser.add_argument("--desktop", action="store_true", help="同时在桌面创建一个")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"网关端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--token", default=None, help="本地口令；不传则由托盘自动生成并复用")
    args = parser.parse_args()

    if sys.platform != "win32":
        print("这个脚本只在 Windows 上有意义。", file=sys.stderr)
        return 1

    config = ROOT / "config.json"
    if not config.exists():
        print(f"找不到 {config}，请先复制 config.example.json 并填入 Key。", file=sys.stderr)
        return 1

    ensure_python_marker()
    pythonw = find_pythonw()
    icon = ensure_app_icon()

    arguments = f'-m st_rotator tray -c config.json --port {args.port}'
    if args.token:
        arguments += f" --token {args.token}"

    targets: list[Path] = [ROOT / f"{SHORTCUT_NAME}.lnk"]
    if args.desktop:
        desk = desktop_dir()
        if desk and desk.exists():
            targets.append(desk / f"{SHORTCUT_NAME}.lnk")
        else:
            print("警告：拿不到桌面路径，跳过桌面快捷方式。", file=sys.stderr)

    for lnk in targets:
        create_shortcut(lnk, target=pythonw, arguments=arguments, icon=icon)
        print(f"已创建：{lnk}")

    print()
    print(f"双击「{SHORTCUT_NAME}」即可启动。启动后不会弹窗口，")
    print("请到系统托盘（右下角，可能在「^」折叠区里）找图标，右键可操作。")
    print(f"控制台地址：http://127.0.0.1:{args.port}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
