#!/usr/bin/env python3
"""BatoWatch loading animation: the logo with its signal waves lighting up from
the inside out, a gently swaying joystick, and "LOADING" with dots.

  make_loading.py <out dir> [width height fps seconds]

Writes frame PNGs (rendered from the user's icon SVG with rsvg-convert); the
caller assembles them into a GIF or MP4.
"""
import math
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(HERE, "original", "batowatch_icon_transparent.svg")

out = sys.argv[1]
W, H = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) > 3 else (1280, 720)
FPS = int(sys.argv[4]) if len(sys.argv) > 4 else 25
SECONDS = float(sys.argv[5]) if len(sys.argv) > 5 else 2.4
FRAMES = int(FPS * SECONDS)
os.makedirs(out, exist_ok=True)

icon = open(ICON).read()
head_end = icon.index("<g transform=")
defs = icon[icon.index("<defs>"):icon.index("</defs>") + 7]
body = icon[icon.index(">", head_end) + 1:icon.rindex("</g>")]
# The icon's own extra defs (meter gradient) live inside the body; keep them.

wave_re = re.compile(r'<path d="(M[^"]*A(334|382|430),\2[^"]*)"[^>]*/>')
waves = wave_re.findall(body)
body_no_waves = wave_re.sub("", body)
RINGS = {"334": 0, "382": 1, "430": 2}

HEART_RE = re.compile(r'<path d="M392,662[^>]*/>')
HEART = [(392, 662), (457, 662), (482, 624), (512, 702), (540, 640), (560, 662), (632, 662)]
body_no_waves = HEART_RE.sub("", body_no_waves)

TIP0 = (578.0, 419.7)
BASE = (512.0, 538.0)


def mix(a, b, t):
    a = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def along(pts, f):
    """The point at fraction f (0..1) of the way along a polyline."""
    segs = [(a, b, math.dist(a, b)) for a, b in zip(pts, pts[1:])]
    total = sum(d for _a, _b, d in segs)
    want = max(0.0, min(1.0, f)) * total
    for a, b, d in segs:
        if want <= d:
            k = want / d if d else 0
            return (a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k)
        want -= d
    return pts[-1]


def heartbeat(t):
    """The line, faint, with a bright pulse sweeping along it: a glowing head
    and a tail that fades, like a heart monitor."""
    base = " ".join(f"{x},{y}" for x, y in HEART)
    out = [f'<polyline points="{base}" fill="none" stroke="#7fe3c3" stroke-width="12" '
           f'stroke-linejoin="round" stroke-linecap="round" opacity="0.35"/>']
    head = (t * 1.35) % 1.35                     # sweep, then a short rest before the next beat
    if head <= 1.0:
        tail = 0.38
        steps = 18
        for k in range(steps):
            f0 = head - tail * (1 - k / steps)
            f1 = head - tail * (1 - (k + 1) / steps)
            if f1 <= 0:
                continue
            a, b = along(HEART, max(0, f0)), along(HEART, f1)
            op = ((k + 1) / steps) ** 1.8
            out.append(f'<line x1="{a[0]:.1f}" y1="{a[1]:.1f}" x2="{b[0]:.1f}" y2="{b[1]:.1f}" '
                       f'stroke="#c9fbe9" stroke-width="13" stroke-linecap="round" opacity="{op:.3f}"/>')
        hx, hy = along(HEART, head)
        out.append(f'<circle cx="{hx:.1f}" cy="{hy:.1f}" r="26" fill="#7fe3c3" opacity="0.55" '
                   f'filter="url(#halo)"/>')
        out.append(f'<circle cx="{hx:.1f}" cy="{hy:.1f}" r="9" fill="#ecfff8"/>')
    return "\n".join(out)


def frame_svg(i):
    t = i / FRAMES                                   # 0..1, loops
    parts = []
    # a pulse travels outward: each ring brightens as it passes, then fades
    for d, rad in waves:
        ring = RINGS[rad]
        phase = (t * 1.0 - ring * 0.18) % 1.0
        glow = max(0.0, 1.0 - phase / 0.42) ** 1.6   # bright when the pulse arrives
        color = mix("#2c6a86", "#c8f1ff", glow)
        width = {0: 24, 1: 22, 2: 20}[ring]
        if glow > 0.02:
            parts.append(f'<path d="{d}" fill="none" stroke="#6fd0ff" stroke-width="{width + 22}" '
                         f'stroke-linecap="round" opacity="{0.85 * glow:.3f}" filter="url(#halo)"/>')
        parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}" '
                     f'stroke-linecap="round"/>')
    # the joystick sways (a full swing per loop, so it loops seamlessly)
    ang = math.radians(9) * math.sin(2 * math.pi * t)
    dx, dy = TIP0[0] - BASE[0], TIP0[1] - BASE[1]
    tx = BASE[0] + dx * math.cos(ang) - dy * math.sin(ang)
    ty = BASE[1] + dx * math.sin(ang) + dy * math.cos(ang)
    b = body_no_waves
    b = b.replace('x2="578.0" y2="419.7"', f'x2="{tx:.1f}" y2="{ty:.1f}"')
    b = b.replace('cx="578.0" cy="419.7"', f'cx="{tx:.1f}" cy="{ty:.1f}"')
    hx, hy = tx - 16, ty - 17
    b = b.replace('cx="562.0" cy="402.7"', f'cx="{hx:.1f}" cy="{hy:.1f}"')
    b = b.replace('rotate(-35 562.0 402.7)', f'rotate(-35 {hx:.1f} {hy:.1f})')
    emblem = "\n".join(parts) + b + heartbeat(t)

    size = min(W, H) * 0.70
    ix, iy = (W - size) / 2, H * 0.43 - size / 2
    dots = int(t * 4) % 4
    ty_text = iy + size + H * 0.035
    lead = H * 0.05
    text = (f'<text x="{W / 2}" y="{ty_text + lead}" text-anchor="middle" font-family="DejaVu Sans" '
            f'font-weight="bold" font-size="{H * 0.042:.1f}" letter-spacing="{H * 0.008:.1f}" '
            f'fill="#6fd0ff" filter="url(#txt)">LOADING{"." * dots}{" " * (3 - dots)}</text>')
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
<defs>
  <radialGradient id="page" cx=".5" cy=".42" r=".75">
    <stop offset="0" stop-color="#1a2830"/><stop offset="1" stop-color="#0a1115"/></radialGradient>
  <filter id="txt" x="-20%" y="-50%" width="140%" height="200%">
    <feGaussianBlur stdDeviation="{H * 0.006:.1f}" result="b"/>
    <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
</defs>
<rect width="{W}" height="{H}" fill="url(#page)"/>
<svg x="{ix:.1f}" y="{iy:.1f}" width="{size:.1f}" height="{size:.1f}" viewBox="0 0 1024 1024">
{defs.replace("</defs>", '<filter id="halo" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="16"/></filter></defs>')}
<g transform="translate(512 584) scale(1.06) translate(-512 -512)">
{emblem}
</g>
</svg>
{text}
</svg>'''


for i in range(FRAMES):
    svg = os.path.join(out, f"f{i:03d}.svg")
    with open(svg, "w") as fh:
        fh.write(frame_svg(i))
    subprocess.run(["rsvg-convert", "-w", str(W), "-h", str(H), svg, "-o",
                    os.path.join(out, f"f{i:03d}.png")], check=True)
    os.unlink(svg)
print(f"{FRAMES} frames, {W}x{H}, {FPS} fps -> {out}")
