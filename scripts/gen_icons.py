#!/usr/bin/env python3
"""Generate LifeOS PWA icons (192px + 512px) as PNGs, no PIL required."""
import zlib, struct, math, os

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "app", "static", "icons")

BG = (15, 23, 42)          # dark-900 #0f172a
RING = (16, 185, 129)      # emerald-500 #10b981
ARROW = (226, 232, 240)    # slate-200 #e2e8f0


def rounded_rect(x, y, n, margin, r):
    """True if (x,y) inside a rounded rectangle."""
    x0, y0, x1, y1 = margin, margin, n - margin, n - margin
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hw, hh = (x1 - x0) / 2 - r, (y1 - y0) / 2 - r
    dx = max(abs(x - cx) - hw, 0.0)
    dy = max(abs(y - cy) - hh, 0.0)
    return dx * dx + dy * dy <= r * r


def seg_dist(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / (vx * vx + vy * vy)))
    dx, dy = px - (ax + t * vx), py - (ay + t * vy)
    return math.hypot(dx, dy)


def in_triangle(px, py, a, b, c):
    def sign(p1, p2, p3):
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])
    d1, d2, d3 = sign((px, py), a, b), sign((px, py), b, c), sign((px, py), c, a)
    has_neg = d1 < 0 or d2 < 0 or d3 < 0
    has_pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (has_neg and has_pos)


def pixel(n, x, y):
    # transparent outside the rounded square
    if not rounded_rect(x + 0.5, y + 0.5, n, n * 0.02, n * 0.225):
        return (0, 0, 0, 0)
    # ring (donut)
    cx, cy = n * 0.5, n * 0.5
    d = math.hypot(x + 0.5 - cx, y + 0.5 - cy)
    if n * 0.24 <= d <= n * 0.345:
        return RING + (255,)
    # upward arrow: shaft + head
    px, py = x + 0.5, y + 0.5
    if seg_dist(px, py, n * 0.5, n * 0.60, n * 0.5, n * 0.36) <= n * 0.05:
        return ARROW + (255,)
    if in_triangle(px, py, (n * 0.5, n * 0.15), (n * 0.5 - n * 0.13, n * 0.36), (n * 0.5 + n * 0.13, n * 0.36)):
        return ARROW + (255,)
    return BG + (255,)


def png(n):
    rows = bytearray()
    for y in range(n):
        rows.append(0)  # filter: none
        for x in range(n):
            rows.extend(pixel(n, x, y))
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        c += struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        return c
    ihdr = struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for size in (512, 192):
        path = os.path.join(OUT_DIR, f"icon-{size}.png")
        with open(path, "wb") as f:
            f.write(png(size))
        print("wrote", os.path.relpath(path))


if __name__ == "__main__":
    main()