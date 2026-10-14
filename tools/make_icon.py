"""Generate Perun's application icon (a gold lightning bolt).

Pure standard library: no Pillow required. Writes a multi-resolution PNG-based
``.ico`` that Windows uses for the desktop shortcut, plus a ``.png`` favicon the
dashboard serves.

Usage::

    python tools/make_icon.py [output.ico]

The icon is deterministic, so it can be regenerated at any time and the result
committed. It mirrors the dashboard's visual language: a gold bolt
(``--gold-light`` -> ``--gold``) on the near-black ``--bg-secondary`` card with
a slim ``--gold-dark`` rim.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

# --- brand colours (kept in sync with web/css/perun.css) -------------------- #
BG = (12, 12, 14)            # --bg-secondary
GOLD = (201, 162, 39)        # --gold
GOLD_LIGHT = (228, 196, 106)  # --gold-light
GOLD_DARK = (143, 116, 25)   # --gold-dark

# Lightning bolt outline in a normalised 0..1 box (y grows downward).
BOLT = [
    (0.55, 0.03),
    (0.18, 0.58),
    (0.42, 0.58),
    (0.34, 0.97),
    (0.82, 0.40),
    (0.56, 0.40),
    (0.72, 0.03),
]

SIZES = [16, 32, 48, 64, 128, 256]
SS = 4  # supersampling factor per axis (SS*SS samples per pixel)


def _inside_poly(px: float, py: float, poly) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _inside_rounded(px: float, py: float, size: float, radius: float, inset: float) -> bool:
    x0 = y0 = inset
    x1 = y1 = size - inset
    if px < x0 or px > x1 or py < y0 or py > y1:
        return False
    cx = min(max(px, x0 + radius), x1 - radius)
    cy = min(max(py, y0 + radius), y1 - radius)
    dx, dy = px - cx, py - cy
    return dx * dx + dy * dy <= radius * radius


def _lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _render(size: int) -> bytes:
    """Return the RGBA bytes for one ``size``-sized icon."""
    radius = size * 0.22
    rim = max(1.0, size * 0.016)
    inset = size * 0.17  # keep the bolt clear of the rounded edge
    bolt = [(inset + x * (size - 2 * inset), inset + y * (size - 2 * inset)) for x, y in BOLT]

    rows = []
    step = 1.0 / SS
    samples = SS * SS
    for y in range(size):
        row = bytearray()
        for x in range(size):
            ra = ga = ba = aa = 0
            for sy in range(SS):
                for sx in range(SS):
                    px = x + (sx + 0.5) * step
                    py = y + (sy + 0.5) * step
                    if _inside_rounded(px, py, size, radius, rim):
                        if _inside_poly(px, py, bolt):
                            col = _lerp(GOLD_LIGHT, GOLD, py / size)
                        else:
                            col = BG
                    elif _inside_rounded(px, py, size, radius, 0.0):
                        col = GOLD_DARK
                    else:
                        continue  # transparent
                    ra += col[0]
                    ga += col[1]
                    ba += col[2]
                    aa += 255
            if aa:
                # Weight colour by coverage so the transparent edge never picks
                # up a dark fringe.
                cov = max(1, aa // 255)
                row += bytes((ra // cov, ga // cov, ba // cov, aa // samples))
            else:
                row += b"\x00\x00\x00\x00"
        rows.append(bytes(row))
    return _png(size, size, rows)


def _png(width: int, height: int, rows) -> bytes:
    def chunk(typ: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + typ
            + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + r for r in rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _ico(images) -> bytes:
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = b""
    offset = 6 + 16 * len(images)
    blob = b""
    for size, png in images:
        dim = 0 if size >= 256 else size  # 0 means 256 in the ICO directory
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        blob += png
    return header + entries + blob


def build_ico() -> bytes:
    return _ico([(s, _render(s)) for s in SIZES])


def main(argv) -> int:
    here = Path(__file__).resolve().parent.parent
    ico_path = Path(argv[1]) if len(argv) > 1 else here / "web" / "assets" / "perun.ico"
    ico_path.parent.mkdir(parents=True, exist_ok=True)
    ico_path.write_bytes(build_ico())
    print(f"wrote {ico_path} ({ico_path.stat().st_size} bytes, sizes={SIZES})")

    png_path = ico_path.with_suffix(".png")
    png_path.write_bytes(_render(256))
    print(f"wrote {png_path} ({png_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
