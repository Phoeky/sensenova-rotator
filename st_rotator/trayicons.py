"""托盘图标生成（纯标准库，不依赖 Pillow）。

Windows 托盘需要一个 ``HICON``。本机没有 Pillow，也不想让用户为此额外装依赖，
所以这里直接把 ``.ico`` 文件的字节拼出来：手工构造 ``BITMAPINFOHEADER`` +
32 位 BGRA 像素 + AND 掩码，再交给 ``LoadImageW`` 从文件加载。

图标语义设计成「一眼看出池子健康度」，不用打开控制台：

    绿 = 全部可用    黄 = 有 Key 在冷却    红 = 有 Key 失效 / 无可用    灰 = 已暂停

为了让边缘不毛刺，渲染时先放大 ``_SUPERSAMPLE`` 倍再降采样（相当于 16 倍超采样）。
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path

# ---------------------------------------------------------------- 状态配色

#: 全部 Key 可用
COLOR_HEALTHY = (0x22, 0xC5, 0x5E)
#: 有 Key 正在 429 冷却
COLOR_WARN = (0xF5, 0x9E, 0x0B)
#: 有 Key 失效，或一把可用的都没有
COLOR_ERROR = (0xEF, 0x44, 0x44)
#: 对外接入被暂停
COLOR_PAUSED = (0x94, 0xA3, 0xB8)
#: 启动中 / 状态未知
COLOR_STARTING = (0x3B, 0x82, 0xF6)

#: 一个 .ico 里塞多个尺寸，Windows 会按场景挑（托盘 16，Alt+Tab 32，大图标 48）
ICON_SIZES = (16, 20, 24, 32, 48)

_SUPERSAMPLE = 4


def _darken(rgb: tuple[int, int, int], factor: float = 0.62) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(c * factor))) for c in rgb)  # type: ignore[return-value]


def _render_bgra(
    size: int,
    color: tuple[int, int, int],
    ring: tuple[int, int, int],
) -> bytes:
    """画一个带描边的实心圆，返回自底向上的 BGRA 像素。

    用「直通 alpha」（straight alpha）：被覆盖的像素 RGB 保持纯色，只有 A 随覆盖率变化。
    这是 ICO 里 32bpp 位图的预期格式（不是预乘 alpha）。
    """
    n = size * _SUPERSAMPLE
    center = (n - 1) / 2.0
    r_outer = n * 0.46
    r_inner = n * 0.32
    samples = _SUPERSAMPLE * _SUPERSAMPLE

    # 先在超采样分辨率上算覆盖率，再逐像素平均
    rows: list[bytes] = []
    for y in range(size - 1, -1, -1):  # BMP 是自底向上存的
        row = bytearray()
        for x in range(size):
            inner_hits = 0
            ring_hits = 0
            for sy in range(_SUPERSAMPLE):
                py = y * _SUPERSAMPLE + sy + 0.5
                for sx in range(_SUPERSAMPLE):
                    px = x * _SUPERSAMPLE + sx + 0.5
                    d = ((px - center) ** 2 + (py - center) ** 2) ** 0.5
                    if d <= r_inner:
                        inner_hits += 1
                    elif d <= r_outer:
                        ring_hits += 1
            covered = inner_hits + ring_hits
            if covered == 0:
                row += b"\x00\x00\x00\x00"
                continue
            # 直通 alpha：RGB 按内圆/描边的占比混合，A 就是覆盖率
            b = (color[2] * inner_hits + ring[2] * ring_hits) / covered
            g = (color[1] * inner_hits + ring[1] * ring_hits) / covered
            r = (color[0] * inner_hits + ring[0] * ring_hits) / covered
            a = round(covered / samples * 255)
            row += bytes((int(b), int(g), int(r), a))
        rows.append(bytes(row))
    return b"".join(rows)


def _bmp_payload(size: int, bgra: bytes) -> bytes:
    """把 BGRA 像素包成 ICO 内部使用的 BMP（BITMAPINFOHEADER + XOR + AND）。"""
    header = struct.pack(
        "<IiiHHIIiiII",
        40,            # biSize
        size,          # biWidth
        size * 2,      # biHeight：ICO 里是 XOR 高度 + AND 高度，所以乘 2
        1,             # biPlanes
        32,            # biBitCount
        0,             # biCompression = BI_RGB
        len(bgra),     # biSizeImage
        0, 0, 0, 0,
    )
    # AND 掩码：32bpp 下由 alpha 通道决定透明度，掩码全 0 即可。
    # 每行按 4 字节对齐，1bpp → 每行 ceil(w/32)*4 字节。
    mask_row = ((size + 31) // 32) * 4
    return header + bgra + b"\x00" * (mask_row * size)


def build_ico(sizes: tuple[int, ...], color: tuple[int, int, int]) -> bytes:
    """把多个尺寸打包成一个 .ico 文件的字节。"""
    ring = _darken(color)
    images = [_bmp_payload(s, _render_bgra(s, color, ring)) for s in sizes]

    count = len(images)
    out = bytearray(struct.pack("<HHH", 0, 1, count))  # reserved, type=icon, count
    offset = 6 + 16 * count
    for size, blob in zip(sizes, images):
        out += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,   # 0 表示 256
            0 if size >= 256 else size,
            0,                            # 调色板色数（真彩为 0）
            0,                            # reserved
            1,                            # 色彩平面
            32,                           # 位深
            len(blob),
            offset,
        )
        offset += len(blob)
    for blob in images:
        out += blob
    return bytes(out)


#: 状态名 → 颜色，供托盘按健康度换图标
STATE_COLORS: dict[str, tuple[int, int, int]] = {
    "healthy": COLOR_HEALTHY,
    "warn": COLOR_WARN,
    "error": COLOR_ERROR,
    "paused": COLOR_PAUSED,
    "starting": COLOR_STARTING,
}


def ensure_icons(directory: Path) -> dict[str, Path]:
    """确保每个状态都有一份 .ico 文件，返回 ``{状态: 路径}``。

    文件名里带上「渲染参数」的哈希：参数没变就直接复用已有文件，**不做重复渲染**
    （渲染要跑 16 倍超采样，虽然不慢但没必要每次启动都白算一遍）；
    配色或尺寸改了，哈希就变，自动生成新文件并清掉旧的。
    """
    directory.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for state, color in STATE_COLORS.items():
        key = f"{state}|{color}|{ICON_SIZES}|{_SUPERSAMPLE}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
        path = directory / f"tray-{state}-{digest}.ico"
        if not path.exists():
            path.write_bytes(build_ico(ICON_SIZES, color))
        paths[state] = path

    # 清掉历史版本的图标，别让目录越积越多
    keep = {p.name for p in paths.values()}
    for stale in directory.glob("tray-*.ico"):
        if stale.name not in keep:
            try:
                stale.unlink()
            except OSError:  # pragma: no cover - 清不掉就算了，不该影响启动
                pass
    return paths
