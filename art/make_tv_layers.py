#!/usr/bin/env python3
"""The TV app's loading screen layers, from the icon SVG:
  loading_base.png   the emblem without its waves, joystick and heartbeat
  loading_stick.png  the joystick (stick + ball), drawn upright, to be rotated
The app draws the waves and the heartbeat itself and sways the stick.

  make_tv_layers.py <out dir> [size]
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
icon = open(os.path.join(HERE, "original", "batowatch_icon_transparent.svg")).read()
out = sys.argv[1]
SIZE = int(sys.argv[2]) if len(sys.argv) > 2 else 512

head = icon[:icon.index(">", icon.index("<g transform=")) + 1]
body = icon[len(head):icon.rindex("</g>")]
waves = re.compile(r'<path d="M[^"]*A(334|382|430),\1[^"]*"[^>]*/>')
heart = re.compile(r'<path d="M392,662[^>]*/>')
stick = re.compile(r'<line x1="512" y1="538" x2="578.0"[^>]*/>|<circle cx="578.0"[^>]*/>|'
                   r'<ellipse cx="562.0"[^>]*/>')
assert len(waves.findall(body)) == 9 and heart.search(body) and len(stick.findall(body)) == 4

base = heart.sub("", waves.sub("", stick.sub("", body)))
# the stick upright: rotate it about its pivot back to vertical
parts = "".join(stick.findall(body))
import math
lean = math.degrees(math.atan2(578.0 - 512, 538 - 419.7))     # degrees right of vertical
upright = f'<g transform="rotate({-lean:.3f} 512 538)">{parts}</g>'


def render(name, inner):
    svg = os.path.join(out, name + ".svg")
    with open(svg, "w") as fh:
        fh.write(head + inner + "</g></svg>")
    subprocess.run(["rsvg-convert", "-w", str(SIZE), "-h", str(SIZE), svg, "-o",
                    os.path.join(out, name + ".png")], check=True)
    os.unlink(svg)
    pass


os.makedirs(out, exist_ok=True)
render("loading_base", base)
render("loading_stick", upright)
print(f"lean {lean:.2f} deg; written to {out}")
