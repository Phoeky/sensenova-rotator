"""Windows 托盘程序（纯 ctypes，零第三方依赖）。

**为什么不用 pystray**：本机没有 PyPI 通道，装不上；而且 pystray 在 Windows 上本来
也只是包了一层 ``Shell_NotifyIcon``。既然如此，直接调 Win32 API 反而少一层依赖，
也和项目「零第三方依赖」的定位一致。

**工作方式**

1. 建一个「消息专用窗口」（``HWND_MESSAGE``）——它不出现在屏幕上、也不进任务栏，
   只用来接收托盘图标的回调消息。
2. ``Shell_NotifyIconW`` 把图标挂到这个窗口上。
3. 跑标准 Win32 消息循环；托盘的鼠标事件以自定义消息投递到窗口过程。
4. ``SetTimer`` 每 2 秒刷新图标颜色与悬浮提示，池子一有异常马上能看出来。

**踩过的坑**

- 所有 ``user32`` 函数都必须显式声明 ``argtypes``/``restype``。不声明的话，
  ``LPARAM`` 这类指针宽度的参数会被当成 ``c_int`` 转换，遇到大值直接
  ``OverflowError: int too long to convert``——而且异常发生在 ctypes 回调里，
  只会打印一行 ``Exception ignored``，主流程看起来一切正常，极难排查。
- ``WNDPROC`` 回调对象必须一直持有引用，否则被 GC 回收后窗口过程就变成了野指针。
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable

from .trayicons import ensure_icons

# ---------------------------------------------------------------- Win32 常量

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_NULL = 0x0000
WM_TIMER = 0x0113
WM_APP = 0x8000
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
WM_CONTEXTMENU = 0x007B

HWND_MESSAGE = -3

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIM_SETVERSION = 0x00000004

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_STATE = 0x00000008
NIF_INFO = 0x00000010
NIF_SHOWTIP = 0x00000080

NIIF_INFO = 0x00000001
NIIF_WARNING = 0x00000002
NIIF_ERROR = 0x00000003

NOTIFYICON_VERSION_4 = 4

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
MF_CHECKED = 0x00000008
MF_GRAYED = 0x00000001
MF_DEFAULT = 0x00001000

TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
ERROR_ALREADY_EXISTS = 183

#: 托盘图标事件投递到窗口过程时用的消息号
_TRAY_CALLBACK_MESSAGE = WM_APP + 1
#: 定时刷新状态
_TIMER_ID = 0x5E01
_REFRESH_MS = 2000
#: 单实例互斥体名
MUTEX_NAME = "Local\\StRotatorTray"

# 菜单项 ID
_ID_OPEN = 1001
_ID_COPY_BASE = 1002
_ID_COPY_TOKEN = 1003
_ID_STATUS = 1004
_ID_PAUSE = 1005
_ID_LOG = 1006
_ID_QUIT = 1007

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    """完整版（V4）结构。``cbSize`` 填结构体自身大小即可，Win7+ 都认。"""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


def _bind_win32() -> tuple[object, object, object]:
    """绑定 Win32 函数并**声明全部参数类型**（见模块 docstring 里的坑）。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    user32.UnregisterClassW.restype = wintypes.BOOL
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = LRESULT
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.SetTimer.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
    user32.SetTimer.restype = wintypes.UINT
    user32.KillTimer.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR,
    ]
    user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, wintypes.HWND, ctypes.c_void_p,
    ]
    user32.TrackPopupMenu.restype = ctypes.c_int
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterWindowMessageW.restype = wintypes.UINT
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
    ]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.MessageBoxW.argtypes = [
        wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT,
    ]

    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]

    return user32, shell32, kernel32


user32, shell32, kernel32 = _bind_win32()

# 互斥体句柄必须留住：一旦被 GC 回收，单实例保护就失效了
_mutex_handles: list[int] = []


def acquire_single_instance(name: str = MUTEX_NAME) -> bool:
    """尝试取得单实例锁。返回 False 表示已经有实例在跑。"""
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        # 拿不到互斥体（权限等）就不拦着，让程序照常启动
        return True
    _mutex_handles.append(handle)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def copy_to_clipboard(text: str) -> bool:
    """把文本放进剪贴板（纯 ctypes，不依赖 pyperclip）。"""
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        size = (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return False
        ctypes.memmove(ptr, ctypes.create_unicode_buffer(text), size)
        kernel32.GlobalUnlock(handle)
        # 成功后所有权交给剪贴板，不能再自己释放
        return bool(user32.SetClipboardData(CF_UNICODETEXT, handle))
    finally:
        user32.CloseClipboard()


class TrayApp:
    """托盘应用：承载网关的运行、状态展示与退出。

    Args:
        rotator: 用于读取池状态。
        console: 控制台状态（读暂停标志）；没有控制台时传 None。
        url: 控制台地址（点「打开控制台」用）。
        open_url: 打开网页的实现，默认走 ``ui.open_console_window``。
        log: 日志回调。
    """

    def __init__(
        self,
        *,
        rotator: object,
        console: object | None,
        url: str,
        base_url: str,
        token: str | None = None,
        icon_dir: Path,
        log_file: Path | None = None,
        open_url: Callable[[str], bool] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.rotator = rotator
        self.console = console
        self.url = url
        self.base_url = base_url
        self.token = token
        self.icon_dir = icon_dir
        self.log_file = log_file
        self._open_url = open_url
        self._log = log or (lambda _m: None)

        self._icons = ensure_icons(icon_dir)
        self._hicons: dict[str, int] = {}
        self._hwnd: int | None = None
        self._class_name: str | None = None
        self._hinst: int | None = None
        self._nid: NOTIFYICONDATAW | None = None
        self._state: str | None = None
        self._ready = threading.Event()
        self._stopping = False
        self._wm_taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        # 必须持有引用，否则窗口过程被回收后就是野指针
        self._proc = WNDPROC(self._wndproc)

    # ------------------------------------------------------------ 状态计算

    def _pool_state(self) -> tuple[str, str]:
        """算出 ``(状态名, 悬浮提示)``。

        状态名对应 ``trayicons.STATE_COLORS`` 的键，决定图标颜色。
        """
        try:
            status = self.rotator.status()
        except Exception as exc:  # noqa: BLE001 - 状态读不到不能让托盘崩
            return "error", f"读取状态失败：{type(exc).__name__}"

        summary = status.get("summary") or {}
        total = int(summary.get("total") or 0)
        healthy = int(summary.get("healthy") or 0)
        cooldown = int(summary.get("cooldown") or 0)
        invalid = int(summary.get("invalid") or 0)
        rate = (status.get("rate_control") or {}).get("rate")

        paused = bool(self.console is not None and self.console.metrics.is_paused)
        if paused:
            state = "paused"
        elif healthy == 0 or invalid > 0:
            state = "error"
        elif cooldown > 0:
            state = "warn"
        else:
            state = "healthy"

        bits = [f"{healthy}/{total} 可用"]
        if cooldown:
            bits.append(f"{cooldown} 冷却")
        if invalid:
            bits.append(f"{invalid} 失效")
        if rate:
            bits.append(f"{rate:.2f} req/s")
        if paused:
            bits.append("已暂停")
        return state, "Key 轮换网关 · " + " · ".join(bits)

    # ------------------------------------------------------------ 图标

    def _hicon(self, state: str) -> int:
        if state not in self._hicons:
            path = self._icons.get(state) or next(iter(self._icons.values()))
            handle = user32.LoadImageW(
                None, str(path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE
            )
            self._hicons[state] = handle
        return self._hicons[state]

    def _refresh(self) -> None:
        """刷新图标与悬浮提示。状态没变就不折腾 Shell。"""
        state, tip = self._pool_state()
        if state == self._state:
            return
        self._state = state
        nid = self._nid
        if nid is None:
            return
        nid.uFlags = NIF_ICON | NIF_TIP
        nid.hIcon = self._hicon(state)
        nid.szTip = tip
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        self._log(f"[托盘] 状态变为 {state}：{tip}")

    def _notify(self, title: str, text: str, flags: int = NIIF_INFO) -> None:
        nid = self._nid
        if nid is None:
            return
        nid.uFlags = NIF_INFO
        nid.szInfoTitle = title
        nid.szInfo = text
        nid.dwInfoFlags = flags
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    # ------------------------------------------------------------ 菜单

    def _show_menu(self) -> None:
        paused = bool(self.console is not None and self.console.metrics.is_paused)
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            user32.AppendMenuW(menu, MF_STRING | MF_DEFAULT, _ID_OPEN, "打开控制台")
            user32.AppendMenuW(menu, MF_STRING, _ID_COPY_BASE, "复制网关地址")
            user32.AppendMenuW(menu, MF_STRING, _ID_COPY_TOKEN, "复制本地口令")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, _ID_STATUS, "查看状态")
            user32.AppendMenuW(
                menu,
                MF_STRING | (MF_CHECKED if paused else 0),
                _ID_PAUSE,
                "恢复接入" if paused else "暂停接入",
            )
            user32.AppendMenuW(
                menu,
                MF_STRING | (0 if self.log_file else MF_GRAYED),
                _ID_LOG,
                "打开日志文件",
            )
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, _ID_QUIT, "退出")

            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            # 必须先抢前台，否则菜单点外面不会消失
            user32.SetForegroundWindow(self._hwnd)
            cmd = user32.TrackPopupMenu(
                menu,
                TPM_RIGHTBUTTON | TPM_RETURNCMD,
                point.x,
                point.y,
                0,
                self._hwnd,
                None,
            )
            user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(menu)

        if cmd:
            self._handle_command(int(cmd))

    def _handle_command(self, cmd: int) -> None:
        if cmd == _ID_OPEN:
            self.open_console()
        elif cmd == _ID_COPY_BASE:
            self._copy(self.base_url, "网关地址")
        elif cmd == _ID_COPY_TOKEN:
            if self.token:
                self._copy(self.token, "本地口令")
            else:
                self._notify("未设置口令", "启动时没有指定 --token，任何本机进程都能调用。", NIIF_WARNING)
        elif cmd == _ID_STATUS:
            _state, tip = self._pool_state()
            self._notify("网关状态", tip)
        elif cmd == _ID_PAUSE:
            self.toggle_pause()
        elif cmd == _ID_LOG:
            self.open_log()
        elif cmd == _ID_QUIT:
            self.request_quit()

    def _copy(self, text: str, label: str) -> None:
        if copy_to_clipboard(text):
            self._notify("已复制", f"{label}已复制到剪贴板：\n{text}")
        else:
            self._notify("复制失败", "无法访问剪贴板。", NIIF_WARNING)

    # ------------------------------------------------------------ 动作

    def open_console(self) -> None:
        """打开控制台网页（带 token，前端会自动读取）。"""
        target = f"{self.url}#token={self.token}" if self.token else self.url
        ok = False
        if self._open_url is not None:
            ok = self._open_url(target)
        if not ok:
            try:
                os.startfile(self.url)  # noqa: S606 - Windows 上打开默认浏览器
                ok = True
            except OSError:
                ok = False
        if ok:
            self._log(f"[托盘] 已打开控制台：{self.url}")
        else:
            self._notify("打开失败", f"没能打开浏览器，请手动访问：\n{self.url}", NIIF_WARNING)

    def open_log(self) -> None:
        if not self.log_file:
            return
        if not self.log_file.exists():
            self._notify("日志不存在", f"还没生成日志文件：\n{self.log_file}", NIIF_WARNING)
            return
        try:
            os.startfile(str(self.log_file))  # noqa: S606
        except OSError as exc:
            self._notify("打开失败", f"{exc}", NIIF_WARNING)

    def toggle_pause(self) -> None:
        if self.console is None:
            return
        now = self.console.metrics.is_paused
        self.console.set_paused(not now)
        self._state = None  # 强制刷新图标
        self._refresh()
        self._notify(
            "已恢复接入" if now else "已暂停接入",
            "上层现在可以正常调用。" if now else "网关会向 /v1/* 返回 503，控制台仍可用。",
            NIIF_INFO if now else NIIF_WARNING,
        )

    def request_quit(self) -> None:
        """请求退出：向窗口投递关闭消息，由消息循环所在线程处理。

        这里**不能**直接 ``DestroyWindow``。Win32 要求销毁窗口的调用必须来自创建该窗口的
        线程，从别的线程（例如定时器线程、信号处理）调用会静默失败——表现为
        「点了退出但程序还在后台跑」。``PostMessageW`` 是线程安全的，消息会在拥有窗口的
        线程上被处理，从而走 ``WM_CLOSE`` → ``WM_DESTROY`` → ``PostQuitMessage`` 这条正常路径。
        """
        self._stopping = True
        hwnd = self._hwnd
        if hwnd:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)

    # ------------------------------------------------------------ 窗口过程

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == _TRAY_CALLBACK_MESSAGE:
                event = lparam & 0xFFFF
                if event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._show_menu()
                elif event == WM_LBUTTONDBLCLK:
                    self.open_console()
                elif event == WM_LBUTTONUP:
                    # 单击给个状态提示，避免用户以为没反应
                    _state, tip = self._pool_state()
                    self._notify("网关状态", tip)
                return 0
            if msg == WM_TIMER and wparam == _TIMER_ID:
                self._refresh()
                return 0
            if msg == WM_DESTROY:
                self._remove_icon()
                user32.PostQuitMessage(0)
                return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg and msg == self._wm_taskbar_created:
                # 资源管理器重启过，托盘区被清空，需要重新挂图标
                self._add_icon()
                return 0
        except Exception as exc:  # noqa: BLE001 - 窗口过程里绝不能把异常抛出去
            self._log(f"[托盘] 消息处理出错：{type(exc).__name__}: {exc}")
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ------------------------------------------------------------ 图标生命周期

    def _add_icon(self) -> None:
        if self._nid is None or self._hwnd is None:
            return
        state, tip = self._pool_state()
        self._state = state
        nid = self._nid
        nid.hWnd = self._hwnd
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
        nid.uCallbackMessage = _TRAY_CALLBACK_MESSAGE
        nid.hIcon = self._hicon(state)
        nid.szTip = tip
        if shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            # V4 让鼠标事件带上坐标，菜单定位更准
            nid.uVersion = NOTIFYICON_VERSION_4
            shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))
            self._log(f"[托盘] 图标已就位：{tip}")
        else:
            self._log("[托盘] 图标添加失败（可能托盘区被策略禁用）")

    def _remove_icon(self) -> None:
        if self._nid is not None:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
        for handle in self._hicons.values():
            if handle:
                user32.DestroyIcon(handle)
        self._hicons.clear()

    # ------------------------------------------------------------ 主循环

    def run(self) -> None:
        """创建窗口、挂上图标、进入消息循环。阻塞直到退出。"""
        hinst = kernel32.GetModuleHandleW(None)
        # 类名必须**每个实例都不同**：同一个类名第二次注册时，如果 WNDPROC 不是同一个
        # 函数指针，Windows 会拒绝（ERROR_CLASS_ALREADY_EXISTS）。用固定名字的话，
        # 同一进程里第二次创建托盘窗口就会直接失败。
        class_name = f"StRotatorTray_{os.getpid()}_{id(self):x}"
        self._class_name = class_name
        self._hinst = hinst

        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = class_name
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise OSError("RegisterClassW 失败，无法创建托盘窗口")

        hwnd = user32.CreateWindowExW(
            0, class_name, "st-rotator tray", 0, 0, 0, 0, 0,
            wintypes.HWND(HWND_MESSAGE), None, hinst, None,
        )
        if not hwnd:
            raise OSError("CreateWindowExW 失败，无法创建托盘窗口")
        self._hwnd = hwnd

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        self._nid = nid

        self._add_icon()
        user32.SetTimer(hwnd, _TIMER_ID, _REFRESH_MS, None)
        self._ready.set()

        message = wintypes.MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if ret <= 0:  # 0 = WM_QUIT，-1 = 出错
                break
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

        user32.KillTimer(hwnd, _TIMER_ID)
        # 注销窗口类，避免同一个进程里反复创建/销毁时把类注册表撑大
        user32.UnregisterClassW(class_name, hinst)
        self._hwnd = None
        self._log("[托盘] 已退出")

    def wait_ready(self, timeout: float = 10.0) -> bool:
        return self._ready.wait(timeout)


def run_tray(**kwargs) -> None:
    """便捷入口：``run_tray(rotator=..., console=..., ...)``。"""
    TrayApp(**kwargs).run()


def _self_test() -> int:  # pragma: no cover - 手工验证用
    """``python -m st_rotator.tray`` 手动看一眼托盘图标。"""
    import argparse

    parser = argparse.ArgumentParser(description="托盘程序自检")
    parser.add_argument("--seconds", type=float, default=8.0)
    args = parser.parse_args()

    class _FakePool:
        def status(self):
            return {
                "summary": {"total": 5, "healthy": 4, "cooldown": 1, "invalid": 0},
                "rate_control": {"rate": 0.31},
            }

    app = TrayApp(
        rotator=_FakePool(),
        console=None,
        url="http://127.0.0.1:8899/",
        base_url="http://127.0.0.1:8899/v1",
        token="selftest",
        icon_dir=Path(__file__).resolve().parent.parent / ".tray-icons",
        log=print,
    )
    threading.Timer(args.seconds, app.request_quit).start()
    print(f"托盘图标已出现，{args.seconds:.0f} 秒后自动退出。右键点它试试菜单。")
    app.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_self_test())
