#!/usr/bin/env python3
"""Build the BatoWatch logo variants (text as outlines) and the favicon.

Reads the user's original SVGs from original/, writes SVGs to out/.
Needs fontTools and the two fonts in fonts/ (Rajdhani Bold, Quicksand variable).
Render the SVGs to PNG with rsvg-convert (see render.sh).
"""
import os
import re
import sys

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

HERE = os.path.dirname(os.path.abspath(__file__))
ORIG = os.path.join(HERE, "original")
OUT = os.path.join(HERE, "out")


def load_fonts():
    rajdhani = TTFont(os.path.join(HERE, "fonts", "Rajdhani-Bold.ttf"))
    quick = instancer.instantiateVariableFont(
        TTFont(os.path.join(HERE, "fonts", "Quicksand-VF.ttf")), {"wght": 700})
    return rajdhani, quick


def kern_pairs(font):
    """Pair kerning from GPOS PairPos format 1 and 2, as {(left, right): xAdvance}."""
    pairs = {}
    if "GPOS" not in font:
        return pairs
    gpos = font["GPOS"].table
    for lookup in gpos.LookupList.Lookup:
        for sub in lookup.SubTable:
            if lookup.LookupType == 9:
                sub = sub.ExtSubTable
            if getattr(sub, "LookupType", lookup.LookupType) != 2:
                continue
            cov = sub.Coverage.glyphs
            if sub.Format == 1:
                for i, first in enumerate(cov):
                    for rec in sub.PairSet[i].PairValueRecord:
                        v = rec.Value1
                        if v is not None and getattr(v, "XAdvance", 0):
                            pairs.setdefault((first, rec.SecondGlyph), v.XAdvance)
            elif sub.Format == 2:
                c1 = sub.ClassDef1.classDefs
                c2 = sub.ClassDef2.classDefs
                for first in cov:
                    k1 = c1.get(first, 0)
                    for second, k2 in c2.items():
                        v = sub.Class1Record[k1].Class2Record[k2].Value1
                        if v is not None and getattr(v, "XAdvance", 0):
                            pairs.setdefault((first, second), v.XAdvance)
    return pairs


class Text:
    def __init__(self, font):
        self.font = font
        self.cmap = font.getBestCmap()
        self.gs = font.getGlyphSet()
        self.upm = font["head"].unitsPerEm
        self.kern = kern_pairs(font)

    def layout(self, s, size, spacing=0.0):
        """Glyph names with x offsets (font units scaled to px) and total width."""
        k = size / self.upm
        x, out, prev = 0.0, [], None
        for ch in s:
            g = self.cmap[ord(ch)]
            if prev is not None:
                x += self.kern.get((prev, g), 0) * k
            out.append((g, x))
            x += self.gs[g].width * k + spacing
            prev = g
        return out, x - spacing

    def width(self, s, size, spacing=0.0):
        return self.layout(s, size, spacing)[1]

    def path(self, s, size, x0, baseline, spacing=0.0):
        k = size / self.upm
        glyphs, _w = self.layout(s, size, spacing)
        pen = SVGPathPen(self.gs, ntos=lambda v: f"{v:.1f}".rstrip("0").rstrip("."))
        for g, x in glyphs:
            tp = TransformPen(pen, (k, 0, 0, -k, x0 + x, baseline))
            self.gs[g].draw(tp)
        return pen.getCommands()


def split_logo(svg):
    """(head up to the emblem, emblem group, name-gradient defs) from the original logo."""
    i = svg.index('<g transform="translate(512 440)')
    j = svg.index("</g>", i)
    # the emblem has no nested groups; its closing tag is the first </g>
    head = svg[:i]
    emblem_inner = svg[svg.index(">", i) + 1:j]
    m = re.search(r"<defs>\s*<linearGradient id=\"batoGrad\".*?</defs>", svg[j:], re.S)
    return head, emblem_inner, m.group(0)


# Emblem bounds in its own 1024 space (outer waves incl. stroke, disc incl. ring)
EMB_TOP, EMB_BOTTOM = 72.0, 805.0


def logo_svg(head, emblem, name_defs, raj, quick, *, glow=False, emb_scale, plate_w, plate_h,
             name_size, tag_size, tag_spacing, gap, transparent):
    plate_x = 512 - plate_w / 2
    emb_h = (EMB_BOTTOM - EMB_TOP) * emb_scale
    total = emb_h + gap + plate_h
    top = (1024 - total) / 2
    emb_cy = top + (512 - EMB_TOP) * emb_scale        # where the emblem's centre lands
    plate_y = top + emb_h + gap

    # Name: optically centred on the plate, tagline below it
    name_w = raj.width("BatoWatch", name_size)
    nx = 512 - name_w / 2
    bato_w = raj.width("Bato", name_size)
    name_base = plate_y + plate_h * 0.545
    tag_base = plate_y + plate_h * 0.875
    tag_w = quick.width("NETWORK · HARDWARE", tag_size, tag_spacing)

    # The original gradient runs from the top of the capitals (885 - 85) to just under the baseline
    grad = re.sub(r'y1="800"(.*?)y2="888"',
                  rf'y1="{name_base - name_size * 0.685:.1f}"\g<1>y2="{name_base + name_size * 0.024:.1f}"',
                  name_defs)
    if transparent:
        head = head.replace('<rect width="1024" height="1024" fill="url(#bg)"/>', "")
    parts = [
        head,
        f'<g transform="translate(512 {emb_cy:.1f}) scale({emb_scale}) translate(-512 -512)">',
        emblem, "</g>",
        grad,
        f'<rect x="{plate_x:.1f}" y="{plate_y:.1f}" width="{plate_w:.1f}" height="{plate_h:.1f}" '
        f'rx="{plate_h * 0.103:.2f}" fill="url(#plate)" stroke="#8996a1" stroke-width="5" filter="url(#shadow)"/>',
        f'<path d="{raj.path("Bato", name_size, nx, name_base)}" fill="url(#batoGrad)"/>',
        f'<path d="{raj.path("Watch", name_size, nx + bato_w + raj.kern.get((raj.cmap[ord("o")], raj.cmap[ord("W")]), 0) * name_size / raj.upm, name_base)}" '
        f'fill="#6fd0ff" filter="url(#glowS)"/>',
        f'<path d="{quick.path("NETWORK · HARDWARE", tag_size, 512 - tag_w / 2, tag_base, tag_spacing)}" fill="#7fe3c3"/>',
        "</svg>\n",
    ]
    # The approved PNGs were rendered without the SVG's filters (no glow, no plate
    # shadow); drop them so the SVG looks the same as those PNGs everywhere.
    svg = "\n".join(parts)
    return svg if glow else re.sub(r'\s*filter="url\(#\w+\)"', "", svg)


def favicon_svg(transparent=True):
    """The icon reduced to what survives at 16-32 px: disc, ring, meter, joystick."""
    bg = "" if transparent else '<rect x="196" y="196" width="632" height="632" rx="120" fill="#0c1418"/>'
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="196 196 632 632">
<defs>
  <radialGradient id="well" cx=".5" cy=".45" r=".55"><stop offset="0" stop-color="#22303a"/><stop offset="1" stop-color="#121b21"/></radialGradient>
  <linearGradient id="meter" gradientUnits="userSpaceOnUse" x1="290" y1="0" x2="734" y2="0">
    <stop offset="0" stop-color="#3ddc84"/><stop offset=".5" stop-color="#f2c94c"/><stop offset=".82" stop-color="#ff3b4e"/><stop offset="1" stop-color="#ff3b4e"/></linearGradient>
  <linearGradient id="sun" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#f2c94c"/><stop offset=".55" stop-color="#ff5a6e"/><stop offset="1" stop-color="#ff2d8a"/></linearGradient>
</defs>
{bg}
<circle cx="512" cy="512" r="286" fill="url(#well)"/>
<circle cx="512" cy="512" r="286" fill="none" stroke="#1e9bff" stroke-width="44"/>
<path d="M332.5,650 A222,222 0 1 1 691.5,650" fill="none" stroke="#33434e" stroke-width="64" stroke-linecap="round"/>
<path d="M332.5,650 A222,222 0 0 1 638,329" fill="none" stroke="url(#meter)" stroke-width="64" stroke-linecap="round"/>
<ellipse cx="500" cy="600" rx="96" ry="38" fill="#8996a1" stroke="#6fd0ff" stroke-width="12"/>
<line x1="500" y1="596" x2="576" y2="452" stroke="#f6efe4" stroke-width="40" stroke-linecap="round"/>
<circle cx="576" cy="440" r="72" fill="url(#sun)" stroke="#f6efe4" stroke-width="12"/>
</svg>
'''


def main():
    os.makedirs(OUT, exist_ok=True)
    raj_font, quick_font = load_fonts()
    raj, quick = Text(raj_font), Text(quick_font)
    with open(os.path.join(ORIG, "batowatch_logo.svg")) as fh:
        head, emblem, name_defs = split_logo(fh.read())

    variants = {
        # The original layout, only with the text outlined (to check the outlines)
        "orig": dict(emb_scale=0.72, plate_w=620, plate_h=217, name_size=124,
                     tag_size=33.48, tag_spacing=8.68, gap=116),
        # Same sizes, plate moved up
        "closer": dict(emb_scale=0.72, plate_w=620, plate_h=217, name_size=124,
                       tag_size=33.48, tag_spacing=8.68, gap=40),
        # Plate moved up, emblem, name and tagline bigger
        "bigger": dict(emb_scale=0.80, plate_w=720, plate_h=250, name_size=144,
                       tag_size=47, tag_spacing=9, gap=34),
    }
    for name, params in variants.items():
        for transparent in (False, True):
            suffix = "_transparent" if transparent else ""
            for glow in (False, True):
                svg = logo_svg(head, emblem, name_defs, raj, quick, transparent=transparent,
                               glow=glow, **params)
                g = "_glow" if glow else ""
                with open(os.path.join(OUT, f"batowatch_logo_{name}{g}{suffix}.svg"), "w") as fh:
                    fh.write(svg)

    with open(os.path.join(OUT, "batowatch_favicon.svg"), "w") as fh:
        fh.write(favicon_svg())
    print("wrote", len(os.listdir(OUT)), "files to", OUT)


if __name__ == "__main__":
    sys.exit(main())
