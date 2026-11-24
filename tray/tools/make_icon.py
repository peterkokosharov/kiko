# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""Generates tray/win/kiko-tray.ico: a bold white K on a rounded square in kiko's blue (0x2478E6, the
tray's "starting" colour). Stdlib only: anti-aliased shapes by hand, PNG via zlib, ICO packed here.
Usage: python tray/tools/make_icon.py [OUT.ico]"""
import math
import struct
import sys
import zlib
from pathlib import Path

SIZES = (16, 20, 24, 32, 48, 64, 256)
BLUE = (0x24, 0x78, 0xE6)
SS = 4  # supersampling per axis


def clamp01(v):
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = clamp01(((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy))
    return math.hypot(ax + t * dx - px, ay + t * dy - py)


def rrect_dist(px, py, half, radius):
    """Signed distance to a rounded square centred at 0.5,0.5 (negative inside)."""
    qx, qy = abs(px - 0.5) - (half - radius), abs(py - 0.5) - (half - radius)
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - radius


def render(size):
    """RGBA rows, top-down."""
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            acc = [0.0, 0.0, 0.0, 0.0]
            for sy in range(SS):
                for sx in range(SS):
                    px = (x + (sx + 0.5) / SS) / size
                    py = (y + (sy + 0.5) / SS) / size
                    a = 1.0 if rrect_dist(px, py, 0.5, 0.22) <= 0 else 0.0
                    d = seg_dist(px, py, 0.34, 0.24, 0.34, 0.76)
                    d = min(d, seg_dist(px, py, 0.34, 0.54, 0.68, 0.24))
                    d = min(d, seg_dist(px, py, 0.42, 0.47, 0.70, 0.76))
                    k = 1.0 if d <= 0.075 else 0.0
                    r, g, b = (255.0 if k else c for c in BLUE)
                    acc[0] += r * a
                    acc[1] += g * a
                    acc[2] += b * a
                    acc[3] += a
            n = SS * SS
            if acc[3] > 0:
                row += bytes((round(acc[0] / acc[3]), round(acc[1] / acc[3]), round(acc[2] / acc[3]),
                              round(255 * acc[3] / n)))
            else:
                row += b"\0\0\0\0"
        rows.append(bytes(row))
    return rows


def png(size, rows):
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\0" + r for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def build():
    images = [png(s, render(s)) for s in SIZES]
    out = struct.pack("<HHH", 0, 1, len(images))
    off = 6 + 16 * len(images)
    for s, img in zip(SIZES, images):
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(img), off)  # 256 is stored as 0
        off += len(img)
    return out + b"".join(images)


def main(argv):
    dest = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent / "win" / "kiko-tray.ico"
    dest.write_bytes(build())
    print(f"wrote {dest} ({dest.stat().st_size} bytes, sizes {', '.join(map(str, SIZES))})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
