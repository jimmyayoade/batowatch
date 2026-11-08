#!/usr/bin/env python3
"""
BatoWatch on the TV: a controller-driven view of the hardware and network,
started from Ports. It has no logic of its own: everything comes from the
BatoWatch service's API on this box, which it logs in to with the token the
service writes to a root-only file.

Controls (after the BUA-style mapping on first start):
  D-pad / left stick  move      A  open      B  back
  LB / RB             tabs      X / Y  zoom graphs in / out
  Back                settings  Start  refresh      Hotkey+Start  exit (Batocera's own
                                                                  KEY_EXIT; no .keys file)
Keyboard: arrows, Enter, Esc, PageUp/PageDown, [ ], F1, F5.
"""
import json
import math
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.request

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame  # noqa: E402
import pygame.gfxdraw  # noqa: E402

HOME = os.environ.get("BATOWATCH_HOME", "/userdata/system/add-ons/batowatch")
sys.path.insert(0, HOME)
CONTROLS_FILE = os.path.join(HOME, "tv_controls.json")
PREFS_FILE = os.path.join(HOME, "tv_prefs.json")
SIZES = {"Small": 0.75, "Normal": 1.0, "Large": 1.25}
BUA_CONTROLS = "/userdata/system/add-ons/bua_controls.json"
LOGO = "/userdata/roms/ports/images/batowatch-logo.png"
ICON = os.path.join(HOME, "www", "icon-192.png")
TV_DIR = os.path.dirname(os.path.abspath(__file__))
RANGES = ["1h", "24h", "7d", "30d", "1y"]

# BatoWatch's own look (not the BUA menu's): the logo's slate-teal behind
# everything, borderless soft cards, mint for focus, gauges like the logo's
BG = (12, 20, 24)                 # the icon's #0c1418
BG_TOP = (26, 40, 48)             # the icon's #1a2830
PANEL = (22, 35, 42)
PANEL2 = (27, 44, 52)
SEL = (30, 58, 58)                # mint-tinted focus
AMBER = (226, 184, 104)           # gauges only: getting close to an alert limit
RULE = (38, 56, 64)
INK = (230, 235, 243)
MUTED = (135, 149, 171)
BLUE = (111, 208, 255)
BLUE_DEEP = (30, 155, 255)
MINT = (127, 227, 195)
BAD = (240, 96, 106)
WARM = (255, 122, 110)            # the logo's "Bato" only
# Graph lines are never red or orange: on a graph those read as "something is wrong"
LINE_COLORS = [BLUE, MINT, (167, 139, 250), (201, 214, 234)]
# The loading screen: the logo's waves, joystick and heartbeat, in the logo's
# own coordinates (a 1024 square), drawn live over loading_base.png
SPLASH_LOOP = 2.4                 # seconds; the pulse, the sway and the beat all loop in it
WAVE_DIM = (44, 106, 134)
WAVE_LIT = (200, 241, 255)
WAVE_RINGS = ((334, 24), (382, 22), (430, 20))     # radius, width
WAVE_ARCS = ((142, 218), (-38, 38), (60, 120))     # left, right, top (degrees)
HEART = ((392, 662), (457, 662), (482, 624), (512, 702), (540, 640), (560, 662), (632, 662))
STICK_PIVOT = (512, 538)
STICK_LEAN = 29.16                # degrees right of upright, as on the logo
# Button badges: a calm slate key with light text, like a keycap, visible without shouting
BADGE_BG = (37, 52, 76)
BADGE_EDGE = (88, 124, 166)
GAME_SHADE = (21, 38, 46)        # behind graphs while a game runs

# Logical buttons, as in the BUA menu; numbers come from the saved mapping
BTN = {"A": 0, "B": 1, "X": 2, "Y": 3, "LB": 4, "RB": 5, "BACK": 6, "START": 7}
STYLE_ICON = {"xbox": {"A": "A", "B": "B", "X": "X", "Y": "Y"},
              "playstation": {"A": "✕", "B": "○", "X": "□", "Y": "△"},
              "nintendo": {"A": "B", "B": "A", "X": "Y", "Y": "X"},
              "generic": {"A": "A", "B": "B", "X": "X", "Y": "Y"},
              "keyboard": {"A": "Enter", "B": "Backspace", "X": "[", "Y": "]", "LB": "Shift+Tab",
                           "RB": "Tab", "START": "Space", "BACK": "R", "EXIT": "Esc"}}
# Keyboard: arrows move, Enter opens, Backspace goes back, Tab / Shift+Tab
# switch tabs, [ ] zoom, Space is Start (Settings), R is Select (Refresh), and
# Esc exits, like Hotkey + Start on a pad. PageUp/PageDown, F1 and F5 still work.
KEYS = {pygame.K_UP: "up", pygame.K_DOWN: "down", pygame.K_LEFT: "left", pygame.K_RIGHT: "right",
        pygame.K_RETURN: "a", pygame.K_KP_ENTER: "a", pygame.K_BACKSPACE: "b", pygame.K_ESCAPE: "quit",
        pygame.K_TAB: "rb", pygame.K_PAGEUP: "lb", pygame.K_PAGEDOWN: "rb",
        pygame.K_LEFTBRACKET: "x", pygame.K_RIGHTBRACKET: "y",
        pygame.K_SPACE: "start", pygame.K_F1: "start", pygame.K_r: "back", pygame.K_F5: "back"}
DEADZONE = 0.5
REPEAT = 0.22


# ---------------------------------------------------------------- the API ---
class Api:
    """Polls the BatoWatch service in the background."""

    def __init__(self):
        self.state, self.history, self.error = None, {}, None
        self.range = "1h"
        self.want = []                  # metrics the screen wants graphed
        self.lock = threading.Lock()
        self.base, self.token = None, None
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE      # our own certificate, on this box
        self.wake = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _config(self):
        port, https = 8686, "self"
        try:
            import settings as s
            cfg = s.Settings(os.path.join(HOME, "batowatch.conf"))
            port, https = cfg["WEB_PORT"], cfg["HTTPS"]
        except Exception:
            pass
        if not port:
            raise OSError("the dashboard is turned off (WEB_PORT=0), and the TV app reads from it")
        self.base = f"{'http' if https == 'off' else 'https'}://127.0.0.1:{port}"
        with open(os.path.join(HOME, "tv.token")) as fh:
            self.token = fh.read().strip()

    def get(self, path, timeout=5):
        req = urllib.request.Request(self.base + path, headers={"X-BatoWatch-Token": self.token})
        with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as r:
            return json.loads(r.read())

    def _loop(self):
        hist_at = 0
        while True:
            try:
                self._config()
                st = self.get("/api/state")
                with self.lock:
                    self.state, self.error = st, None
                hw = st.get("hw")
                names = list(self.want)
                have = self.history.get("names") if self.history else None
                if hw and names and (time.time() - hist_at > 30 or self.wake.is_set() or
                                     have != names or self.history.get("range") != self.range):
                    rng = self.range
                    h = self.get(f"/api/history?range={rng}&metrics={','.join(names)}", 10)
                    with self.lock:
                        self.history = dict(h, range=rng, names=names)
                    hist_at = time.time()
            except Exception as e:
                with self.lock:
                    self.error = str(getattr(e, "reason", e))
            self.wake.clear()
            self.wake.wait(0.5 if self.want and (not self.history or
                                                 self.history.get("names") != self.want) else 2)

    def refresh(self):
        self.wake.set()


# ------------------------------------------------------------- controller ---
def load_map():
    for path in (CONTROLS_FILE, BUA_CONTROLS):
        try:
            with open(path) as fh:
                data = json.load(fh)
            if all(isinstance(data.get(k), int) for k in BTN):
                return {k: data[k] for k in BTN}
        except (OSError, ValueError):
            continue
    return None


def save_map(m):
    try:
        with open(CONTROLS_FILE, "w") as fh:
            json.dump(m, fh, indent=2)
    except OSError:
        pass


def load_prefs():
    try:
        with open(PREFS_FILE) as fh:
            p = json.load(fh)
        return p if isinstance(p, dict) else {}
    except (OSError, ValueError):
        return {}


def save_prefs(p):
    try:
        with open(PREFS_FILE, "w") as fh:
            json.dump(p, fh, indent=2)
    except OSError:
        pass


def detect_style(override):
    if override in ("xbox", "playstation", "nintendo", "generic"):
        return override
    if pygame.joystick.get_count() == 0:
        return "keyboard"
    names = " ".join((pygame.joystick.Joystick(i).get_name() or "").lower()
                     for i in range(pygame.joystick.get_count()))
    if any(k in names for k in ("sony", "playstation", "dualsense", "dualshock", "ps4", "ps5")):
        return "playstation"
    if any(k in names for k in ("nintendo", "switch", "pro controller")):
        return "nintendo"
    return "xbox"


# ------------------------------------------------------------------ the app ---
class App:
    def __init__(self):
        pygame.init()
        pygame.joystick.init()
        self.joys = [pygame.joystick.Joystick(i) for i in range(pygame.joystick.get_count())]
        for j in self.joys:
            j.init()
        if os.environ.get("BATOWATCH_TV_WINDOWED"):          # tests: e.g. 1920x1080
            w, _, h = os.environ["BATOWATCH_TV_WINDOWED"].partition("x")
            self.screen = pygame.display.set_mode((int(w), int(h)) if h else (1280, 720))
        else:
            info = pygame.display.Info()
            self.screen = pygame.display.set_mode((info.current_w, info.current_h))
        pygame.display.set_caption("BatoWatch")
        pygame.mouse.set_visible(False)
        self.W, self.H = self.screen.get_size()
        self.prefs = load_prefs()
        if self.prefs.get("size") not in SIZES:
            self.prefs["size"] = "Normal"
        self.set_size()
        self.api = Api()
        self.tab = 0                    # 0 hardware, 1 network
        self.sel = 0
        self.detail = None              # ("hw", section, sel) or ("dev", ip, sel)
        self.menu = None                # settings menu index, or None
        self.style_override = self.prefs.get("style")
        self.map = load_map()
        if self.map:
            BTN.update(self.map)
        self.clock = pygame.time.Clock()
        self.last_axis = {}
        self.flash = None               # (text, shown at): a short message, e.g. "Refreshed"
        self.running = True
        self.load_splash()
        self.splash_t0 = time.time()
        self.splash_until = self.splash_t0 + SPLASH_LOOP   # one full loop at least
        self.wait_until = 0.0           # after starting the service: keep loading until it answers

    def set_size(self):
        """Everything scales with the screen, times the Size setting."""
        self.k = max(1.0, min(self.W / 1280, self.H / 720)) * SIZES[self.prefs["size"]]
        # DejaVu Sans (Batocera ships it) has the ✕ ○ □ △ of PlayStation pads
        path = pygame.font.match_font("dejavusans,dejavu sans")

        bold = pygame.font.match_font("dejavusans,dejavu sans", bold=True) or path

        def f(size, p=path):
            return pygame.font.Font(p, int(size * 0.72)) if p else pygame.font.Font(None, size)
        self.f_huge = f(self.s(76), bold)
        self.f_bold = f(self.s(34), bold)
        self.f_big = f(self.s(64), bold)
        self.f_mid = f(self.s(40))
        self.f = f(self.s(32))
        self.f_small = f(self.s(26))
        cond = (pygame.font.match_font("dejavusanscondensed", bold=True) or
                pygame.font.match_font("dejavu sans condensed", bold=True) or bold)
        self.f_num = f(self.s(76), cond)            # readouts
        self.f_num_mid = f(self.s(44), cond)
        self.bg = pygame.Surface((self.W, self.H))
        for yy in range(self.H):
            t = yy / max(1, self.H - 1)
            c = tuple(int(BG_TOP[i] + (BG[i] - BG_TOP[i]) * min(1, t * 1.4)) for i in range(3))
            pygame.draw.line(self.bg, c, (0, yy), (self.W, yy))
        self.logo = None
        for p in (ICON, LOGO):
            try:
                img = pygame.image.load(p)
                try:
                    img = img.convert_alpha()
                except pygame.error:
                    pass
                self.logo = pygame.transform.smoothscale(img, (self.s(56), self.s(56)))
                break
            except Exception:
                continue

    def s(self, n):
        return int(round(n * self.k))

    def paint_bg(self):
        self.screen.blit(self.bg, (0, 0))

    def gauge(self, center, radius, frac, level="ok", width=None):
        """An arc like the logo's gauge: 240 degrees, filled to frac. Mint while
        normal, amber close to an alert limit, red once the alert fires."""
        width = width or max(3, self.s(9))
        start = math.radians(210)                 # bottom left, going clockwise
        sweep = math.radians(240)
        self.arc(center, radius, width, start, start - sweep, RULE)
        frac = max(0.0, min(1.0, frac))
        if frac > 0.01:
            color = {"bad": BAD, "warn": AMBER}.get(level, MINT)
            self.arc(center, radius, width, start, start - sweep * frac, color)

    def arc(self, center, radius, width, a0, a1, color, surf=None):
        """A smooth thick arc with round ends (pygame's own arc has holes)."""
        surf = surf or self.screen
        cx, cy = center
        steps = max(8, int(abs(a1 - a0) * radius / 3))
        ro, ri = radius, radius - width
        pts = [(cx + ro * math.cos(a0 + (a1 - a0) * i / steps),
                cy - ro * math.sin(a0 + (a1 - a0) * i / steps)) for i in range(steps + 1)]
        pts += [(cx + ri * math.cos(a0 + (a1 - a0) * i / steps),
                 cy - ri * math.sin(a0 + (a1 - a0) * i / steps)) for i in range(steps, -1, -1)]
        pygame.gfxdraw.aapolygon(surf, pts, color)
        pygame.gfxdraw.filled_polygon(surf, pts, color)
        rm = radius - width / 2
        for ang in (a0, a1):
            px, py = int(cx + rm * math.cos(ang)), int(cy - rm * math.sin(ang))
            pygame.gfxdraw.aacircle(surf, px, py, int(width / 2), color)
            pygame.gfxdraw.filled_circle(surf, px, py, int(width / 2), color)

    @staticmethod
    def thick_line(surf, a, b, width, color):
        """A smooth line with round ends."""
        (x0, y0), (x1, y1) = a, b
        d = math.hypot(x1 - x0, y1 - y0)
        r = width / 2
        if d > 0.5:
            nx, ny = -(y1 - y0) / d * r, (x1 - x0) / d * r
            pts = [(x0 + nx, y0 + ny), (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)]
            pygame.gfxdraw.aapolygon(surf, pts, color)
            pygame.gfxdraw.filled_polygon(surf, pts, color)
        for x, y in (a, b):
            pygame.gfxdraw.aacircle(surf, int(x), int(y), int(r), color)
            pygame.gfxdraw.filled_circle(surf, int(x), int(y), int(r), color)

    # -- the loading screen: the logo coming alive ----------------------------
    def load_splash(self):
        """The emblem without its moving parts, and the joystick upright. Both
        are optional: without them the app simply skips the animation."""
        self.splash = None
        try:
            base = pygame.image.load(os.path.join(TV_DIR, "loading_base.png")).convert_alpha()
            stick = pygame.image.load(os.path.join(TV_DIR, "loading_stick.png")).convert_alpha()
        except (pygame.error, OSError, FileNotFoundError):
            return
        D = int(min(self.W, self.H) * 0.6)
        k = D / base.get_width()
        base = pygame.transform.smoothscale(base, (D, D))
        stick = pygame.transform.smoothscale(stick, (int(stick.get_width() * k),
                                                     int(stick.get_height() * k)))
        box = stick.get_bounding_rect()
        stick = stick.subsurface(box).copy()
        ox, oy = (self.W - D) // 2, int(self.H * 0.44) - D // 2
        z = D / 1024

        def at(x, y):
            """A point in the logo's 1024 square, on screen (the logo's own
            transform: translate(512 584) scale(1.06) translate(-512 -512))."""
            return ox + (512 + 1.06 * (x - 512)) * z, oy + (584 + 1.06 * (y - 512)) * z
        px, py = at(*STICK_PIVOT)
        self.splash = {"base": base, "pos": (ox, oy), "D": D, "z": z * 1.06, "at": at,
                       "stick": stick, "pivot": (px, py),
                       "stick_off": pygame.math.Vector2(ox + box.centerx - px, oy + box.centery - py),
                       "glow": pygame.Surface((D // 6, D // 6), pygame.SRCALPHA)}

    @staticmethod
    def _mix(a, b, t):
        return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

    def draw_loading(self):
        self.paint_bg()
        sp = self.splash
        t = ((time.time() - self.splash_t0) % SPLASH_LOOP) / SPLASH_LOOP
        y_text = self.H * 0.44 + self.s(40)
        if sp:
            D, z, at = sp["D"], sp["z"], sp["at"]
            ox, oy = sp["pos"]
            # glow is drawn on a full-size layer, then blurred by shrinking it
            # and scaling it back up: cheap, and soft enough for a halo
            glow = pygame.Surface((D, D), pygame.SRCALPHA)
            cx, cy = at(512, 512)
            lit = []
            for ring, (rad, w) in enumerate(WAVE_RINGS):
                phase = (t - ring * 0.18) % 1.0
                g = max(0.0, 1.0 - phase / 0.42) ** 1.6     # bright as the pulse arrives
                lit.append(g)
                if g > 0.02:
                    for a0, a1 in WAVE_ARCS:
                        self.arc((cx - ox, cy - oy), (rad + w / 2 + 11) * z, (w + 22) * z,
                                 math.radians(a0), math.radians(a1),
                                 (*BLUE, int(215 * g)), glow)
            # the heartbeat's glowing head, on the same layer
            head = (t * 1.35) % 1.35                       # a sweep, then a short rest
            hx = hy = None
            if head <= 1.0:
                hx, hy = at(*self._along(HEART, head))
                pygame.gfxdraw.filled_circle(glow, int(hx - ox), int(hy - oy), int(26 * z), (*MINT, 150))
            small = pygame.transform.smoothscale(glow, sp["glow"].get_size())
            self.screen.blit(pygame.transform.smoothscale(small, (D, D)), (ox, oy))
            # the waves themselves, dim until the pulse lights them
            for (rad, w), g in zip(WAVE_RINGS, lit):
                for a0, a1 in WAVE_ARCS:
                    self.arc((cx, cy), (rad + w / 2) * z, w * z, math.radians(a0), math.radians(a1),
                             self._mix(WAVE_DIM, WAVE_LIT, g))
            self.screen.blit(sp["base"], (ox, oy))
            # the joystick sways about its base
            lean = STICK_LEAN + 9 * math.sin(2 * math.pi * t)
            img = pygame.transform.rotozoom(sp["stick"], -lean, 1.0)
            c = pygame.math.Vector2(sp["pivot"]) + sp["stick_off"].rotate(lean)
            self.screen.blit(img, img.get_rect(center=(round(c.x), round(c.y))))
            # the heartbeat: faint, with a bright pulse and a fading tail
            pts = [at(x, y) for x, y in HEART]
            faint = self._mix(BG, MINT, 0.4)
            for a, b in zip(pts, pts[1:]):
                self.thick_line(self.screen, a, b, 12 * z, faint)
            if hx is not None:
                steps, tail = 14, 0.38
                for i in range(steps):
                    f0, f1 = head - tail * (1 - i / steps), head - tail * (1 - (i + 1) / steps)
                    if f1 > 0:
                        op = ((i + 1) / steps) ** 1.8
                        self.thick_line(self.screen, at(*self._along(HEART, max(0, f0))),
                                        at(*self._along(HEART, f1)), 13 * z,
                                        self._mix(faint, (201, 251, 233), op))
                pygame.gfxdraw.aacircle(self.screen, int(hx), int(hy), max(2, int(9 * z)), (236, 255, 248))
                pygame.gfxdraw.filled_circle(self.screen, int(hx), int(hy), max(2, int(9 * z)), (236, 255, 248))
            y_text = oy + D + self.s(10)
        # LOADING, spaced like the BUA splash; the dots don't move the word
        dots = int(t * 4) % 4
        word = "LOADING"
        gap = self.s(6)
        widths = [self.f_bold.size(ch)[0] for ch in word + "..."]
        x = self.W // 2 - (sum(widths[:len(word)]) + gap * (len(word) - 1)) // 2
        for ch, w in zip(word + "." * dots, widths):
            self.text(ch, self.f_bold, BLUE, (x, int(y_text)))
            x += w + gap

    @staticmethod
    def _along(pts, f):
        """The point at fraction f of the way along a polyline."""
        segs = [(a, b, math.dist(a, b)) for a, b in zip(pts, pts[1:])]
        want = max(0.0, min(1.0, f)) * sum(d for _a, _b, d in segs)
        for a, b, d in segs:
            if want <= d:
                k = want / d if d else 0
                return a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k
            want -= d
        return pts[-1]

    # -- drawing helpers ------------------------------------------------------
    def text(self, txt, font, color, pos, anchor="topleft"):
        img = font.render(str(txt), True, color)
        r = img.get_rect(**{anchor: pos})
        self.screen.blit(img, r)
        return r

    def box(self, rect, color=PANEL, border=None, radius=14, top=None):
        radius = max(radius, 12)
        pygame.draw.rect(self.screen, color, rect, border_radius=radius)
        if border is not None and border != RULE:
            pygame.draw.rect(self.screen, border, rect, width=max(1, self.s(2)), border_radius=radius)
        if top:
            pygame.draw.rect(self.screen, top, (rect[0], rect[1], rect[2], self.s(4)),
                             border_top_left_radius=radius, border_top_right_radius=radius)

    def unit(self):
        st = self.api.state or {}
        return (st.get("hw") or {}).get("unit", "C")

    def fmt(self, r):
        if not r:
            return "–"
        v, u = r["value"], r["unit"]
        if r["name"].endswith(".health"):
            return "OK" if v else "Failing"
        if u == "°C":
            return f"{v * 9 / 5 + 32:.0f} °F" if self.unit() == "F" else f"{v:.0f} °C"
        if u == "%":
            return f"{v:.0f}%"
        if u == "W":
            return f"{v:.1f} W"
        if u == "MHz":
            return f"{v / 1000:.2f} GHz" if v >= 1000 else f"{v:.0f} MHz"
        if u == "MB":
            return f"{v / 1024:.1f} GB" if v >= 1024 else f"{v:.0f} MB"
        if u == "B/s":
            return human_bytes(v) + "/s"
        if u == "RPM":
            return f"{v:.0f} RPM"
        return str(v)

    @property
    def kbd(self):
        """No controller plugged in: the hints name keyboard keys."""
        return pygame.joystick.get_count() == 0

    def icons(self):
        """How buttons are named: the way the pad labels them (A, ✕, B, ○...),
        or the keyboard's keys when no controller is plugged in."""
        return STYLE_ICON["keyboard" if self.kbd else detect_style(self.style_override)]

    def badge(self, key, pos, anchor="midleft", font=None):
        """A button, drawn the way the pad or the keyboard labels it."""
        k = self.icons().get(key, key)
        font = font or self.f_bold
        img = font.render(k, True, INK)
        r = img.get_rect()
        box = pygame.Rect(0, 0, max(r.w + self.s(18), self.s(40)), r.h + self.s(10))
        setattr(box, anchor, pos)
        pygame.draw.rect(self.screen, BADGE_BG, box, border_radius=self.s(9))
        pygame.draw.rect(self.screen, BADGE_EDGE, box, width=max(1, self.s(2)), border_radius=self.s(9))
        self.screen.blit(img, img.get_rect(center=box.center))
        return box

    def hint(self, items):
        """The button bar at the bottom of every screen. On a keyboard, Esc
        exits (a pad has Hotkey + Start)."""
        if self.kbd:
            items = list(items) + [("EXIT", "Exit")]
        bar = pygame.Rect(0, self.H - self.s(64), self.W, self.s(64))
        pygame.draw.rect(self.screen, PANEL, bar)
        pygame.draw.line(self.screen, RULE, bar.topleft, bar.topright)
        x = self.s(30)
        for key, label in items:
            b = self.badge(key, (x, bar.centery))
            x = self.text(label, self.f, INK, (b.right + self.s(10), bar.centery), "midleft").right + self.s(36)

    def meter(self, rect, pct, bad=False):
        pygame.draw.rect(self.screen, PANEL2, rect, border_radius=self.s(4))
        w = int(rect[2] * max(0, min(100, pct)) / 100)
        if w:
            pygame.draw.rect(self.screen, BAD if bad else BLUE_DEEP, (rect[0], rect[1], w, rect[3]),
                             border_radius=self.s(4))

    # -- screens ------------------------------------------------------------
    def readings(self):
        hw = (self.api.state or {}).get("hw") or {}
        return {r["name"]: r for r in hw.get("readings", [])}, hw

    def sections(self):
        R, hw = self.readings()
        groups = [("CPU", lambda r: r["group"] in ("cpu", "cpu-core") or r["name"] ==
                   (hw.get("summary") or {}).get("cpu_temp")),
                  ("GPU", lambda r: r["group"] == "gpu" or (r["group"] == "temp" and
                                                              r["info"].get("kind") == "gpu")),
                  ("Memory", lambda r: r["group"] == "mem"),
                  ("Disks", lambda r: r["group"] in ("disk", "diskio", "smart")),
                  ("Power", lambda r: r["group"] in ("power", "battery")),
                  ("Temperatures", lambda r: r["group"] in ("temp", "zone")),
                  ("Fans", lambda r: r["group"] == "fan"),
                  ("Network", lambda r: r["group"] == "net"),
                  ("BatoWatch itself", lambda r: r["group"] == "self")]
        out = []
        for name, test in groups:
            rows = [r for r in R.values() if test(r) and not r["name"].endswith(".present")]
            if rows:
                out.append((name, sorted(rows, key=self._order)))
        return out

    @staticmethod
    def _order(r):
        """Most telling first: usage, temperature, clock, load, then the rest."""
        n = r["name"]
        rank = (0 if n in ("cpu.usage", "mem.used_pct") or n.endswith(("busy", "engine_busy")) else
                1 if r["unit"] == "°C" else 2 if r["unit"] == "MHz" else 3 if n == "cpu.load1" else
                5 if n == "cpu.throttled" else 4)
        num = int("".join(ch for ch in n if ch.isdigit()) or 0)
        return rank, n.rstrip("0123456789"), num

    def summary(self, name, rows):
        """One live line per section: what matters, not how many readings."""
        R, hw = self.readings()
        S, lim = hw.get("summary") or {}, hw.get("limits") or {}
        bad = any(self.is_bad(r, lim) for r in rows)
        if name == "CPU":
            parts = [self.fmt(R.get("cpu.usage"))]
            if S.get("cpu_temp") in R:
                parts.append(self.fmt(R[S["cpu_temp"]]))
            return " · ".join(parts), R.get("cpu.usage", {}).get("value"), bad
        if name == "GPU":
            loads = [r for r in rows if r["name"].endswith(("busy", "engine_busy"))]
            if len(loads) > 1:
                return f"{len(loads)} GPUs · " + " / ".join(self.fmt(r) for r in loads), None, bad
            busy = loads[0] if loads else None
            temp = next((r for r in rows if r["unit"] == "°C"), None)
            parts = [f"{self.fmt(busy)} load"] if busy else []
            if temp:
                parts.append(self.fmt(temp))
            if not parts:
                clock = next((r for r in rows if r["unit"] == "MHz"), None)
                parts = [self.fmt(clock)] if clock else ["found"]
            return " · ".join(parts), busy["value"] if busy else None, bad
        if name == "Memory":
            m = R.get("mem.used_pct")
            return (f"{self.fmt(m)} used" if m else "–"), m["value"] if m else None, bad
        if name == "Disks":
            space = [r for r in rows if r["group"] == "disk"]
            main = next((r for r in space if r["info"].get("mount") == "/userdata"),
                        space[0] if space else None)
            failing = any(r["name"].endswith(".health") and not r["value"] for r in rows)
            if main:
                gb = main["info"]["free_gb"]
                text = f"{gb:.0f} GB free" if gb >= 10 else f"{gb:.1f} GB free"
                return text + (" · failing" if failing else ""), main["value"], bad
            return ("failing" if failing else "healthy"), None, bad
        if name == "Power":
            p = R.get(S.get("power")) if S.get("power") else None
            if p:
                return self.fmt(p), None, bad
            bat = next((r for r in rows if r["unit"] == "%"), None)
            return (f"battery {self.fmt(bat)}" if bat else self.fmt(rows[0])), None, bad
        if name == "Temperatures":
            hot = max((r for r in rows if r["unit"] == "°C"), key=lambda r: r["value"], default=None)
            return (f"hottest {self.fmt(hot)}" if hot else "–"), None, bad
        if name == "Fans":
            return " · ".join(self.fmt(r) for r in rows[:2]), None, bad
        if name == "Network":
            rx = sum(r["value"] for r in rows if r["name"].endswith(".rx"))
            tx = sum(r["value"] for r in rows if r["name"].endswith(".tx"))
            return f"↓ {human_bytes(rx)}/s  ↑ {human_bytes(tx)}/s", None, bad
        if name == "BatoWatch itself":
            c, m = R.get("self.cpu"), R.get("self.rss_mb")
            return f"{self.fmt(c) if c else '–'} CPU · {self.fmt(m) if m else '–'}", None, bad
        return "", None, bad

    def draw_header(self):
        st = self.api.state or {}
        x = self.s(30)
        if self.logo:
            self.screen.blit(self.logo, (x, self.s(18)))
            x += self.s(70)
        r = self.text("Bato", self.f_big, WARM, (x, self.s(22)))
        r2 = self.text("Watch", self.f_big, BLUE, (r.right, self.s(22)))
        net = st.get("net") or {}
        host_left = self.W - self.s(30)
        if net.get("host"):
            ips = [i for i in net.get("local_ips", []) if ":" not in i]
            host_left = self.text(f"{net['host']}  ·  {', '.join(ips)}", self.f_small, MUTED,
                                  (self.W - self.s(30), r2.centery), "midright").left
        # the logo's heartbeat, faint, between the title and the host name
        py = r2.centery
        x0 = r2.right + self.s(24)
        x1 = host_left - self.s(24)
        mid = x0 + (x1 - x0) * 0.62
        pts = [(x0, py), (mid - self.s(40), py), (mid - self.s(24), py - self.s(14)),
               (mid - self.s(4), py + self.s(16)), (mid + self.s(14), py - self.s(6)),
               (mid + self.s(30), py), (x1, py)]
        if x1 - x0 > self.s(200):
            pygame.draw.lines(self.screen, (40, 74, 72), False, pts, max(2, self.s(3)))
        err = self.api.error
        if err:
            self.text(f"Can't reach BatoWatch: {err[:70]}", self.f_small, BAD,
                      (self.W - self.s(30), self.s(58)), "topright")
        alerts = st.get("alerts") or []
        y = self.s(92)
        rect = pygame.Rect(self.s(30), y, self.W - self.s(60), self.s(52))
        self.box(rect, PANEL, BAD if alerts else RULE, 8)
        pygame.draw.rect(self.screen, BAD if alerts else MINT, (rect.x, y, self.s(8), rect.h),
                         border_top_left_radius=8, border_bottom_left_radius=8)
        if alerts:
            msg = (f"{len(alerts)} problem{'s' if len(alerts) > 1 else ''}:  " +
                   "  ·  ".join(a["text"] for a in alerts))
        else:
            msg = "All good"
            if net:
                live = [d["name"] for d in net.get("devices", []) if d.get("streaming_since")]
                msg += f"     Streaming: {', '.join(live) if live else 'nobody'}"
        self.text(msg, self.f_bold if alerts else self.f, BAD if alerts else INK,
                  (rect.x + self.s(24), rect.centery), "midleft")
        # tabs, with the buttons that switch them
        ty = self.s(182)
        b = self.badge("LB", (self.s(30), ty))
        tx = b.right + self.s(22)
        for i, name in enumerate(("Hardware", "Network")):
            r = self.text(name, self.f_mid, INK if i == self.tab else MUTED, (tx, ty), "midleft")
            if i == self.tab:
                pygame.draw.rect(self.screen, BLUE, (r.x, r.bottom + self.s(6), r.w, self.s(5)),
                                 border_radius=3)
            tx = r.right + self.s(34)
        self.badge("RB", (tx - self.s(12), ty))

    def draw_tiles(self, y):
        R, hw = self.readings()
        S = hw.get("summary") or {}
        lim = hw.get("limits") or {}
        tiles = [("CPU", R.get("cpu.usage")), ("CPU temperature", R.get(S.get("cpu_temp"))),
                 ("Memory", R.get("mem.used_pct")), ("Power", R.get(S.get("power"))),
                 ("Hottest disk", R.get(S.get("disk_temp")))]
        tiles = [t for t in tiles if t[1]]
        if not tiles:
            return y
        h = self.s(128)
        w = (self.W - self.s(60) - self.s(14) * (len(tiles) - 1)) // len(tiles)
        for i, (label, r) in enumerate(tiles):
            x = self.s(30) + i * (w + self.s(14))
            bad = self.is_bad(r, lim)
            self.box((x, y, w, h), PANEL, BAD if bad else None, 16)
            self.text(label, self.f_small, MUTED, (x + self.s(18), y + self.s(16)))
            if bad is not None:
                word = lim[r["name"]]["word"] if bad else "OK"
                self.text(word.upper(), self.f_small, BAD if bad else MINT,
                          (x + self.s(18), y + h - self.s(14)), "bottomleft")
            tr = self.text(self.fmt(r), self.f_num, INK, (x + self.s(18), y + h // 2 + self.s(6)),
                           "midleft")
            # a gauge like the logo's, where there's something to measure against
            # how full, and how close to the alert: CPU load is never a warning
            # (games use the CPU), temperatures warn from 85% of their limit,
            # memory when less than twice the RAM alert level is free
            frac, level = None, "bad" if bad else "ok"
            if r["unit"] == "%":
                frac = r["value"] / 100
                ram = (lim.get(r["name"]) or {}).get("avail_pct")
                if not bad and ram and r["info"].get("avail_pct", 100) < ram * 2:
                    level = "warn"
            elif r["unit"] == "°C":
                limit = (lim.get(r["name"]) or {}).get("limit") or r["info"].get("crit") or 100
                frac = r["value"] / limit
                if not bad and frac >= 0.85:
                    level = "warn"
            if frac is not None:
                # as big as the space the readout leaves, never over it
                room = (x + w - self.s(18)) - (tr.right + self.s(16))
                rad = min(h // 2 - self.s(16), room // 2)
                if rad >= self.s(16):
                    self.gauge((x + w - rad - self.s(18), y + h // 2 + self.s(8)), rad, frac, level,
                               max(3, min(self.s(7), rad // 4)))
        return y + h + self.s(18)

    @staticmethod
    def is_bad(r, lim):
        lm = lim.get(r["name"]) if r else None
        if not lm:
            return None
        if lm.get("limit") is not None:
            return r["value"] >= lm["limit"]
        if lm.get("avail_pct") is not None:
            return r["info"].get("avail_pct", 100) < lm["avail_pct"]
        if lm.get("free_gb") is not None:
            return ((lm["free_gb"] > 0 and r["info"].get("free_gb", 1e9) < lm["free_gb"]) or
                    (lm["free_pct"] > 0 and r["info"].get("free_pct", 100) < lm["free_pct"]))
        return r["info"].get("passed") is False

    def graph_specs(self, name, rows):
        """Up to two graphs for a section, each with one unit."""
        R, hw = self.readings()
        S = hw.get("summary") or {}
        hist = [r for r in rows if not r["info"].get("no_history") and r["name"] != "cpu.throttled"
                and not r["name"].endswith(".health")]

        def pick(test, n=3, hottest=False):
            got = [r for r in hist if test(r)]
            if hottest:
                got.sort(key=lambda r: -r["value"])
            return [r["name"] for r in got[:n]]
        if name == "CPU":
            specs = [("CPU load", ["cpu.usage"]),
                     ("CPU temperature", [S["cpu_temp"]] if S.get("cpu_temp") in R else [])]
        elif name == "GPU":
            specs = [("GPU load", pick(lambda r: r["name"].endswith(("busy", "engine_busy")), 3)),
                     ("GPU temperature", pick(lambda r: r["unit"] == "°C", 2))]
            if not specs[0][1] and not specs[1][1]:
                specs = [("GPU clock", pick(lambda r: r["unit"] == "MHz", 1))]
        elif name == "Memory":
            specs = [("Memory used", ["mem.used_pct"])]
        elif name == "Disks":
            temps = [r["name"] for r in R.values() if r["unit"] == "°C" and r["info"].get("kind") == "disk"]
            specs = [("Disk temperatures", temps[:3]),
                     ("Space used", pick(lambda r: r["group"] == "disk"))]
        elif name == "Power":
            specs = [("Power", pick(lambda r: r["unit"] == "W")),
                     ("Battery", pick(lambda r: r["unit"] == "%", 2))]
        elif name == "Temperatures":
            specs = [("Hottest sensors", pick(lambda r: r["unit"] == "°C", 4, True))]
        elif name == "Fans":
            specs = [("Fans", pick(lambda r: r["unit"] in ("RPM", "%")))]
        elif name == "Network":
            specs = [("Download", pick(lambda r: r["name"].endswith(".rx"))),
                     ("Upload", pick(lambda r: r["name"].endswith(".tx")))]
        elif name == "BatoWatch itself":
            specs = [("BatoWatch CPU", ["self.cpu"]), ("BatoWatch memory", ["self.rss_mb"])]
        else:
            specs = []
        return [(t, n) for t, n in specs if n]

    @staticmethod
    def line_label(r):
        """A short name for one line in a graph with several."""
        info = r["info"]
        if r["group"] == "disk":
            return info.get("mount", "")
        if r["group"] == "net":
            return r["name"].split(".")[1]
        if info.get("kind") in ("disk", "gpu") or r["group"] in ("gpu", "diskio"):
            return info.get("model") or info.get("dev") or r["label"].split(" ")[0]
        return r["label"].replace(" temperature", "").split(" ")[-1]

    def axis_text(self, v, unit):
        if unit == "%":
            return f"{v:.0f}%"
        if unit == "°C":
            return f"{v:.0f}°"
        if unit == "B/s":
            return human_bytes(v)
        return f"{v:.0f}" if abs(v) >= 10 else f"{v:.1f}"

    def draw_graph(self, rect, names, title):
        hist = self.api.history
        series = (hist.get("series") or {}) if hist else {}
        R = self.readings()[0]
        x, y, w, h = rect
        self.box(rect)
        rng = hist.get("range", self.api.range) if hist else self.api.range
        self.text(title, self.f_bold, INK, (x + self.s(16), y + self.s(12)))
        colors = LINE_COLORS
        lx = x + w - self.s(16)
        for i, n in reversed(list(enumerate(names))):
            if n in R:
                label = self.line_label(R[n]) if len(names) > 1 else ""
                r = self.text(f"{label} {self.fmt(R[n])}".strip(), self.f, colors[i % 4],
                              (lx, y + self.s(12)), "topright")
                lx = r.x - self.s(18)
        pts_all = [p for n in names for p in series.get(n, [])]
        if len(pts_all) < 2:
            self.text("Collecting history…", self.f_small, MUTED, (x + w // 2, y + h // 2), "center")
            return
        unit = next((R[n]["unit"] for n in names if n in R), "")
        is_pct = unit == "%"
        f = (lambda c: c * 9 / 5 + 32) if (unit == "°C" and self.unit() == "F") else (lambda c: c)
        vals = [f(p[2]) for p in pts_all]
        if is_pct:
            vmin, vmax = 0, 100
        elif unit == "°C":
            vmin, vmax = min(vals) - 5, max(vals) + 5
        else:
            vmin, vmax = 0, max(max(vals) * 1.15, 1)
        t1 = time.time()
        t0 = t1 - {"1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000, "1y": 31536000}[rng]
        gx, gy, gw, gh = x + self.s(84), y + self.s(52), w - self.s(100), h - self.s(66)
        for k in range(3):
            yy = gy + gh * k // 2
            pygame.draw.line(self.screen, RULE, (gx, yy), (gx + gw, yy))
            v = vmax - (vmax - vmin) * k / 2
            self.text(self.axis_text(v, unit), self.f_small, MUTED, (gx - self.s(8), yy), "midright")
        shaded = False
        for g in (hist.get("games") or []):
            a = gx + (max(g["t0"], t0) - t0) / (t1 - t0) * gw
            b = gx + (min(g["t1"] or t1, t1) - t0) / (t1 - t0) * gw
            if b > a:
                pygame.draw.rect(self.screen, GAME_SHADE, (a, gy, max(2, b - a), gh))
                shaded = True
        if shaded:
            # say what the shading means, next to the title
            tw = self.f_bold.size(title)[0]
            kx, ky = x + self.s(16) + tw + self.s(22), y + self.s(12) + self.f_bold.get_height() // 2
            pygame.draw.rect(self.screen, GAME_SHADE, (kx, ky - self.s(8), self.s(22), self.s(16)),
                             border_radius=self.s(3))
            pygame.draw.rect(self.screen, BADGE_EDGE, (kx, ky - self.s(8), self.s(22), self.s(16)),
                             width=1, border_radius=self.s(3))
            self.text("game running", self.f_small, MUTED, (kx + self.s(30), ky), "midleft")
        for i, n in enumerate(names):
            pts = series.get(n, [])
            line = [(gx + (p[0] - t0) / (t1 - t0) * gw,
                     gy + gh - (f(p[2]) - vmin) / max(1e-6, vmax - vmin) * gh) for p in pts]
            if len(line) > 1:
                pygame.draw.lines(self.screen, colors[i % 4], False, line, max(2, self.s(3)))

    def draw_ranges(self, x, y, w):
        """Time range  X 1h 24h 7d 30d 1y Y, with the current range highlighted."""
        cur = self.api.range
        cap = self.text("Time range", self.f_small, MUTED, (x, y), "midleft")
        b = self.badge("X", (cap.right + self.s(14), y))
        cx = b.right + self.s(14)
        for rng in RANGES:
            img = self.f.render(rng, True, INK if rng == cur else MUTED)
            box = pygame.Rect(cx, 0, img.get_width() + self.s(22), img.get_height() + self.s(8))
            box.centery = y
            if rng == cur:
                pygame.draw.rect(self.screen, BLUE_DEEP, box, border_radius=self.s(8))
            self.screen.blit(img, img.get_rect(center=box.center))
            cx = box.right + self.s(6)
        self.badge("Y", (cx + self.s(8), y))

    def draw_hardware(self):
        y = self.draw_tiles(self.s(222))
        secs = self.sections()
        if not secs:
            self.text("Waiting for BatoWatch…", self.f_mid, MUTED, (self.W // 2, self.H // 2), "center")
            self.hint([("RB", "Network"), ("START", "Settings")])
            return
        self.sel = max(0, min(self.sel, len(secs) - 1))
        left_w = self.s(500)
        row_h = self.s(60)
        bottom = self.H - self.s(78)
        fit = max(1, (bottom - y) // row_h)
        first = max(0, min(self.sel - fit + 1, len(secs) - fit))
        x0 = self.s(30)
        for i, (name, rows) in enumerate(secs[first:first + fit], first):
            ry = y + (i - first) * row_h
            sel = i == self.sel
            rect = pygame.Rect(x0, ry, left_w, row_h - self.s(8))
            self.box(rect, SEL if sel else PANEL, None, 12)
            if sel:
                pygame.draw.rect(self.screen, MINT, (rect.x, rect.y, self.s(6), rect.h),
                                 border_top_left_radius=8, border_bottom_left_radius=8)
            self.text(name, self.f_bold if sel else self.f, INK, (rect.x + self.s(20), rect.centery),
                      "midleft")
            text, pct, bad = self.summary(name, rows)
            right = rect.right - self.s(16)
            if sel:
                right = self.badge("A", (right, rect.centery), "midright").x - self.s(12)
            self.text(text, self.f, BAD if bad else (INK if sel else MUTED), (right, rect.centery),
                      "midright")
        if first:
            self.text("▲ more", self.f_small, MUTED, (x0 + left_w // 2, y - self.s(4)), "midbottom")
        if first + fit < len(secs):
            self.text("▼ more", self.f_small, MUTED, (x0 + left_w // 2, y + fit * row_h - self.s(6)),
                      "midtop")
        gx = x0 + left_w + self.s(20)
        gw = self.W - gx - self.s(30)
        self.draw_ranges(gx, y + self.s(18), gw)
        gy = y + self.s(46)
        specs = self.graph_specs(*secs[self.sel])
        self.api.want = sorted({n for _t, names in specs for n in names})
        if not specs:
            self.box((gx, gy, gw, bottom - gy - self.s(10)))
            self.text("Nothing to graph here", self.f, MUTED, (gx + gw // 2, (gy + bottom) // 2), "center")
        gh = (bottom - gy) // max(1, len(specs))
        for i, (title, names) in enumerate(specs):
            self.draw_graph((gx, gy + i * gh, gw, gh - self.s(10)), names, title)
        self.hint([("A", "Open"), ("RB", "Network"), ("X", "Zoom in"), ("Y", "Zoom out"),
                   ("BACK", "Refresh"), ("START", "Settings")])

    # -- section pages: plain-language tables ----------------------------------
    SUBTITLES = {
        "CPU": "How hard the processor is working, and how hot it is.",
        "GPU": "The graphics card: how busy it is, how fast, and how hot.",
        "Memory": "RAM: what's in use and what's still free for games.",
        "Disks": "Each drive: free space, what it's reading and writing now, and its health.",
        "Power": "How much electricity each part uses right now.",
        "Temperatures": "Every temperature sensor, and the limit it shouldn't reach.",
        "Fans": "How fast each fan spins.",
        "Network": "Data coming in and going out on each connection.",
        "BatoWatch itself": "What BatoWatch costs this box.",
    }
    POWER_NAMES = {"CPU package": "Whole processor", "CPU cores": "Processor cores",
                   "Integrated GPU": "Built-in graphics", "RAM": "Memory",
                   "Whole system": "Whole system"}

    @staticmethod
    def friendly_temp(r):
        """Sensor names people understand: "Processor core 3", not "coretemp Core 3"."""
        info, label = r["info"], r["label"]
        chip = info.get("chip", "")
        if info.get("kind") == "disk":
            return label.replace(" temperature", "")
        if chip == "coretemp":
            m = re.search(r"Core (\d+)", label)
            return f"Processor core {m.group(1)}" if m else "Processor (whole chip)"
        if chip in ("k10temp", "zenpower"):
            return "Processor" + (" (die)" if label.endswith("Tdie") else "")
        if info.get("kind") == "gpu":
            return label
        if r["group"] == "zone":
            return {"x86_pkg_temp": "Processor", "cpu-thermal": "Processor", "acpitz": "Motherboard"
                    }.get(info.get("type", ""), label)
        return label

    @staticmethod
    def clean_cpu(model):
        model = re.sub(r"\((R|TM|tm|r)\)", "", model)
        model = re.sub(r"\s+CPU\s*@.*$|\s+@.*$|\s+\d+-Core Processor$", "", model)
        return re.sub(r"\s+", " ", model).strip()

    def status_cell(self, bad, word="OK"):
        if bad is None:
            return ("–", MUTED)
        return ((word if bad else "OK").upper(), BAD if bad else MINT)

    def section_tables(self, name, rows):
        """[(title or None, columns, rows)]; columns are (heading, share of width, align);
        a cell is text, (text, colour) or {"meter": percent, "text": ...}."""
        R, hw = self.readings()
        lim = hw.get("limits") or {}
        S = hw.get("summary") or {}
        T = []
        facts = [("", 0.55, "l"), ("", 0.45, "r")]
        if name == "CPU":
            model = (R.get("cpu.usage") or {}).get("info", {}).get("model") or "Processor"
            ct = R.get(S.get("cpu_temp")) if S.get("cpu_temp") else None
            thr = R.get("cpu.throttled")
            load = R.get("cpu.load1")
            cores = load["info"].get("cores") if load else None
            data = [["Processor", self.clean_cpu(model)], ["Load", self.fmt(R.get("cpu.usage"))]]
            if ct:
                data.append(["Temperature", (self.fmt(ct), BAD if self.is_bad(ct, lim) else INK)])
            if R.get("cpu.freq"):
                data.append(["Clock speed", self.fmt(R["cpu.freq"])])
            if load:
                data.append([f"Waiting work (load average{', ' + str(cores) + ' cores' if cores else ''})",
                             f"{load['value']:.2f}"])
            if thr:
                data.append(["Slowed down from heat (last sample)",
                             (f"{thr['value']:.0f} times", BAD if thr["value"] else INK)])
            T.append((None, facts, data))
            core_rows = sorted((r for r in rows if r["group"] == "cpu-core"), key=self._order)
            if core_rows:
                T.append(("Each core", [("Core", 0.3, "l"), ("Load", 0.7, "l")],
                          [[r["label"], {"meter": r["value"], "text": self.fmt(r)}] for r in core_rows]))
        elif name == "GPU":
            gpus = {}
            for r in R.values():
                if r["group"] == "gpu" or (r["info"].get("kind") == "gpu"):
                    k = r["info"].get("pdev") or r["info"].get("card") or r["info"].get("model")
                    gpus.setdefault(k, []).append(r)
            data = []
            for k, rs in gpus.items():
                model = next((r["info"]["model"] for r in rs if r["info"].get("model")), "Graphics card")
                get = lambda test: next((r for r in rs if test(r)), None)  # noqa: E731
                load = get(lambda r: r["name"].endswith(("busy", "engine_busy")))
                clock = get(lambda r: r["unit"] == "MHz")
                temp = get(lambda r: r["unit"] == "°C")
                mem = get(lambda r: r["name"].endswith("vram_mb"))
                memtxt = "–"
                if mem:
                    tot = mem["info"].get("total_mb")
                    memtxt = f"{self.fmt(mem)} of {tot / 1024:.0f} GB" if tot else self.fmt(mem)
                data.append([model, self.fmt(load) if load else "–", self.fmt(clock) if clock else "–",
                             (self.fmt(temp), BAD if self.is_bad(temp, lim) else INK) if temp else "–",
                             memtxt])
            T.append((None, [("Graphics card", 0.36, "l"), ("Load", 0.12, "r"), ("Clock", 0.16, "r"),
                             ("Temperature", 0.16, "r"), ("Memory", 0.2, "r")], data))
        elif name == "Memory":
            m = R.get("mem.used_pct")
            data = []
            if m:
                total = m["info"]["total_mb"] / 1024
                avail = m["info"]["avail_mb"] / 1024
                data += [["Total", f"{total:.1f} GB"],
                         ["In use", (f"{total - avail:.1f} GB ({m['value']:.0f}%)",
                                     BAD if self.is_bad(m, lim) else INK)],
                         ["Free for games and apps", f"{avail:.1f} GB"]]
            sw = R.get("mem.swap_used_pct")
            if sw:
                data.append(["Swap (overflow on disk) in use", self.fmt(sw)])
            T.append((None, facts, data))
        elif name == "Disks":
            devs = []
            for r in R.values():
                d = r["info"].get("dev") if (r["group"] in ("disk", "diskio", "smart") or
                                             r["info"].get("kind") == "disk") else None
                if d and d not in devs:
                    devs.append(d)
            data = []
            for d in sorted(devs):
                space = [r for r in R.values() if r["group"] == "disk" and r["info"].get("dev") == d]
                model = next((r["info"].get("model") for r in R.values()
                              if r["info"].get("dev") == d and r["info"].get("model")), d)
                if space:
                    free = sum(r["info"]["free_gb"] for r in space)
                    total = sum(r["info"]["total_gb"] for r in space)
                    full = any(self.is_bad(r, lim) for r in space)
                    sp = (f"{free:.0f} GB free of {total:.0f} GB", BAD if full else INK)
                else:
                    sp = ("not in use", MUTED)
                rd, wr = R.get(f"diskio.{d}.read"), R.get(f"diskio.{d}.write")
                temp = R.get(f"smart.{d}.temp") or next(
                    (r for r in R.values() if r["unit"] == "°C" and r["info"].get("dev") == d), None)
                health = R.get(f"smart.{d}.health")
                hc = ("–", MUTED) if not health else (("GOOD", MINT) if health["value"] else ("FAILING", BAD))
                data.append([model, sp, self.fmt(rd) if rd else "–", self.fmt(wr) if wr else "–",
                             (self.fmt(temp), BAD if self.is_bad(temp, lim) else INK) if temp else "–", hc])
            T.append((None, [("Drive", 0.28, "l"), ("Space", 0.24, "r"), ("Reading", 0.12, "r"),
                             ("Writing", 0.12, "r"), ("Temperature", 0.13, "r"), ("Health", 0.11, "r")],
                      data))
        elif name == "Power":
            parts = [r for r in rows if r["group"] == "power"]
            if parts:
                T.append((None, [("Part", 0.6, "l"), ("Power now", 0.4, "r")],
                          [[self.POWER_NAMES.get(r["label"], r["label"]), self.fmt(r)] for r in parts]))
            bats = [r for r in rows if r["group"] == "battery" and r["unit"] == "%"]
            if bats:
                T.append(("Batteries", [("Battery", 0.5, "l"), ("Charge", 0.2, "r"), ("Status", 0.3, "r")],
                          [[r["label"], self.fmt(r), r["info"].get("status", "–")] for r in bats]))
        elif name == "Temperatures":
            temps = sorted((r for r in rows if r["unit"] == "°C"), key=lambda r: -r["value"])
            data = []
            for r in temps:
                limit = (lim.get(r["name"]) or {}).get("limit") or r["info"].get("crit") or \
                    r["info"].get("max") or r["info"].get("temp_limit")
                bad = self.is_bad(r, lim)
                data.append([self.friendly_temp(r), (self.fmt(r), BAD if bad else INK),
                             self.fmt({"name": "", "value": limit, "unit": "°C"}) if limit else "–",
                             self.status_cell(bad, "Hot") if bad is not None else ("OK", MINT)])
            T.append((None, [("Sensor", 0.46, "l"), ("Now", 0.18, "r"), ("Safe limit", 0.18, "r"),
                             ("Status", 0.18, "r")], data))
        elif name == "Fans":
            T.append((None, [("Fan", 0.6, "l"), ("Speed", 0.4, "r")],
                      [[r["label"], self.fmt(r)] for r in rows]))
        elif name == "Network":
            ifs = sorted({r["name"].split(".")[1] for r in rows})

            def friendly(i):
                if i.startswith(("eth", "en")):
                    return f"Wired ({i})"
                if i.startswith(("wl", "wlan")):
                    return f"Wi-Fi ({i})"
                if i.startswith("tailscale"):
                    return "Tailscale"
                return i
            T.append((None, [("Connection", 0.5, "l"), ("Download", 0.25, "r"), ("Upload", 0.25, "r")],
                      [[friendly(i), self.fmt(R.get(f"net.{i}.rx")), self.fmt(R.get(f"net.{i}.tx"))]
                       for i in ifs]))
        elif name == "BatoWatch itself":
            g = lambda n: self.fmt(R.get(n)) if R.get(n) else "–"  # noqa: E731
            T.append((None, facts, [["Processor use (of one core)", g("self.cpu")],
                                    ["Memory use", g("self.rss_mb")],
                                    ["History on disk", g("self.db_mb")],
                                    ["Written to disk today", g("self.written_today_mb")],
                                    ["Dashboards open now", g("self.connections")]]))
        return T

    def draw_section(self, name):
        secs = dict(self.sections())
        rows = secs.get(name, [])
        text, _pct, bad = self.summary(name, rows)
        y0 = self.s(222)
        r = self.text(name, self.f_huge, INK, (self.s(30), y0))
        self.text(text, self.f_mid, BAD if bad else MINT, (r.right + self.s(24), r.centery), "midleft")
        self.text(self.SUBTITLES.get(name, ""), self.f, MUTED, (self.s(30), r.bottom + self.s(8)))
        top = r.bottom + self.s(56)
        bottom = self.H - self.s(80)
        x, w = self.s(30), self.W - self.s(60)
        line = self.s(50)
        # Lay every table out as blocks of lines, then show the page that fits
        blocks = []
        for title, cols, data in self.section_tables(name, rows):
            if title:
                blocks.append(("title", title, cols))
            if any(c[0] for c in cols):
                blocks.append(("head", None, cols))
            for row in data:
                blocks.append(("row", row, cols))
        per_page = max(1, (bottom - top) // line)
        pages = max(1, -(-len(blocks) // per_page))
        if self.sel >= pages:
            self.sel = 0
        y = top
        for kind, row, cols in blocks[self.sel * per_page:(self.sel + 1) * per_page]:
            if kind == "title":
                self.text(row, self.f_bold, INK, (x, y + line // 2), "midleft")
            elif kind == "head":
                cx = x
                for head, share, align in cols:
                    cw = int(w * share)
                    pos = (cx + self.s(14), y + line // 2) if align == "l" else (cx + cw - self.s(14), y + line // 2)
                    self.text(head, self.f_small, MUTED, pos, "midleft" if align == "l" else "midright")
                    cx += cw
                pygame.draw.line(self.screen, RULE, (x, y + line - self.s(4)), (x + w, y + line - self.s(4)))
            else:
                pygame.draw.rect(self.screen, PANEL, (x, y + self.s(3), w, line - self.s(6)), border_radius=6)
                cx = x
                for (head, share, align), cell in zip(cols, row):
                    cw = int(w * share)
                    color = INK
                    if isinstance(cell, dict):
                        mw = cw - self.s(150)
                        self.meter((cx + self.s(14), y + line // 2 - self.s(6), mw, self.s(12)), cell["meter"])
                        cell = cell["text"]
                        pos, anchor = (cx + cw - self.s(14), y + line // 2), "midright"
                    else:
                        if isinstance(cell, tuple):
                            cell, color = cell
                        pos = (cx + self.s(14), y + line // 2) if align == "l" else (cx + cw - self.s(14), y + line // 2)
                        anchor = "midleft" if align == "l" else "midright"
                    font = self.f_bold if (cx == x and head) else self.f
                    self.text(self.fit(str(cell), font, cw - self.s(28)), font, color, pos, anchor)
                    cx += cw
            y += line
        if pages > 1:
            self.text(f"Page {self.sel + 1} of {pages}", self.f_small, MUTED, (self.W - self.s(30), y0 + self.s(16)),
                      "topright")
        self.hint([("B", "Back")] + ([("A", "Next page")] if pages > 1 else []) + [("RB", "Network")])

    def fit(self, text, font, width):
        """Shorten text with … so it never runs into the next column."""
        if font.size(text)[0] <= width:
            return text
        while text and font.size(text + "…")[0] > width:
            text = text[:-1]
        return text + "…"

    def draw_network(self):
        net = (self.api.state or {}).get("net")
        if not net:
            self.text("The network part is turned off (NETWORK=off).", self.f_mid, MUTED,
                      (self.W // 2, self.H // 2), "center")
            self.hint([("LB", "Hardware"), ("START", "Settings")])
            return
        devs = [d for d in net["devices"] if d["route"] != "internet"]
        devs.sort(key=lambda d: (not d.get("streaming_since"), d["first_seen"]))
        y = self.s(222)
        live = [d for d in devs if d.get("streaming_since")]
        for d in live[:2]:
            rect = pygame.Rect(self.s(30), y, self.W - self.s(60), self.s(96))
            self.box(rect, (24, 54, 60), None, 14)
            pygame.draw.rect(self.screen, MINT, (rect.x, rect.y, self.s(6), rect.h),
                             border_top_left_radius=10, border_bottom_left_radius=10)
            self.text(d["name"], self.f_huge, INK, (rect.x + self.s(26), rect.centery), "midleft")
            self.text(f"streaming {human_dur(time.time() - d['streaming_since'])}  ·  "
                      f"{human_bits(d['rate_out'])}", self.f_mid, MINT,
                      (rect.right - self.s(24), rect.centery), "midright")
            y += self.s(110)
        self.text(f"Streaming ({len(live)})  ·  Connected ({len(devs)})", self.f_mid, INK,
                  (self.s(30), y))
        y += self.s(58)
        if not devs:
            self.text("No devices are connected.", self.f, MUTED, (self.s(30), y))
        self.sel = max(0, min(self.sel, max(0, len(devs) - 1)))
        row_h = self.s(64)
        bottom = self.H - self.s(78)
        first = max(0, self.sel - (bottom - y) // row_h + 1)
        for i, d in enumerate(devs[first:], first):
            ry = y + (i - first) * row_h
            if ry + row_h > bottom:
                break
            sel = i == self.sel
            rect = pygame.Rect(self.s(30), ry, self.W - self.s(60), row_h - self.s(8))
            self.box(rect, SEL if sel else PANEL, None, 12)
            if sel:
                pygame.draw.rect(self.screen, MINT, (rect.x, rect.y, self.s(6), rect.h),
                                 border_top_left_radius=8, border_bottom_left_radius=8)
            name = d["name"] + ("   ● streaming" if d.get("streaming_since") else "")
            self.text(name, self.f_bold if sel else self.f, INK, (rect.x + self.s(22), rect.centery),
                      "midleft")
            route = {"tailnet-direct": "Tailscale, direct", "tailnet-relay": "Tailscale, relayed",
                     "lan": "Home network", "subnet-router": "Subnet router"}.get(d["route"], d["route"])
            right = rect.right - self.s(16)
            if sel:
                right = self.badge("A", (right, rect.centery), "midright").x - self.s(12)
            self.text(f"{route}  ·  {human_dur(time.time() - d['first_seen'])}  ·  "
                      f"{human_bits(d['rate_in'] + d['rate_out'])}", self.f, MUTED,
                      (right, rect.centery), "midright")
        self.hint([("A", "Details"), ("LB", "Hardware"), ("BACK", "Refresh"), ("START", "Settings")])

    def draw_device(self, ip):
        net = (self.api.state or {}).get("net") or {}
        d = next((x for x in net.get("devices", []) if x["ip"] == ip), None)
        if not d:
            self.text("This device has disconnected.", self.f_mid, MUTED, (self.s(30), self.s(230)))
            self.hint([("B", "Back")])
            return
        self.text(d["name"], self.f_big, INK, (self.s(30), self.s(214)))
        self.text(", ".join(x for x in (d["ip"], d.get("os"), d.get("mac")) if x), self.f, MUTED,
                  (self.s(30), self.s(270)))
        y = self.s(320)
        for f in d["flows"][:12]:
            svc = f["service"] or "unlabelled"
            self.text(f"{svc} · {f['proto']} {f['port']} · {f['proc']}", self.f, INK, (self.s(30), y))
            self.text(f"in {human_bytes(f['in'])}  out {human_bytes(f['out'])}", self.f, MUTED,
                      (self.W - self.s(30), y), "topright")
            y += self.s(44)
        self.hint([("B", "Back")])

    MENU = ["Size", "Configure buttons", "Controller style", "Exit BatoWatch"]

    def draw_menu(self):
        pygame.draw.rect(self.screen, BG, (0, self.H - self.s(54), self.W, self.s(54)))
        w, h = self.s(680), self.s(400)
        x, y = (self.W - w) // 2, (self.H - h) // 2
        self.box((x, y, w, h), PANEL, BLUE, 14)
        self.text("Settings", self.f_mid, INK, (x + self.s(24), y + self.s(20)))
        ver = (self.api.state or {}).get("version")
        if ver:
            self.text(f"BatoWatch {ver}", self.f_small, MUTED, (x + w - self.s(24), y + self.s(28)), "topright")
        for i, item in enumerate(self.MENU):
            label = item
            if item == "Controller style":
                label = f"Controller style: {self.style_override or 'auto (' + detect_style(None) + ')'}"
            elif item == "Size":
                label = f"Size:   <  {self.prefs['size']}  >"
            sel = i == self.menu
            ry = y + self.s(90) + i * self.s(64)
            self.box((x + self.s(20), ry, w - self.s(40), self.s(54)), SEL if sel else PANEL, None, 12)
            self.text(label, self.f, INK, (x + self.s(40), ry + self.s(14)))
        self.hint([("A", "Choose"), ("B", "Close")] +
                  ([("< >", "Smaller / bigger")] if self.MENU[self.menu] == "Size" else []))

    # -- the BUA menu's hold-to-map wizard -------------------------------------
    def map_buttons(self):
        if not self.joys:
            return False
        order = [("A", "Confirm / open"), ("B", "Back"), ("X", "Zoom graphs in (less time)"),
                 ("Y", "Zoom graphs out (more time)"), ("LB", "Previous tab"), ("RB", "Next tab"),
                 ("BACK", "Refresh (Select / Back / Share)"), ("START", "Settings (Start / Menu)")]
        mapping, held, start, idx = {}, None, 0, 0
        pygame.event.get()
        while idx < len(order):
            label, desc = order[idx]
            prog = (pygame.time.get_ticks() - start) / 500 if held is not None else 0
            self.paint_bg()
            w, h = self.s(760), self.s(300)
            x, y = (self.W - w) // 2, (self.H - h) // 2
            self.box((x, y, w, h), PANEL, BLUE, 14)
            self.text("Controller setup", self.f_mid, INK, (x + self.s(24), y + self.s(20)))
            self.text(f"Hold the button for {label}", self.f, INK, (x + self.s(24), y + self.s(90)))
            self.text(desc, self.f_small, MUTED, (x + self.s(24), y + self.s(130)))
            bar = (x + self.s(24), y + h - self.s(70), w - self.s(48), self.s(22))
            pygame.draw.rect(self.screen, PANEL2, bar, border_radius=6)
            if prog > 0:
                pygame.draw.rect(self.screen, MINT, (bar[0], bar[1], int(bar[2] * min(1, prog)), bar[3]),
                                 border_radius=6)
            self.text("Esc cancels", self.f_small, MUTED, (x + w - self.s(24), y + self.s(26)), "topright")
            pygame.display.flip()
            for e in pygame.event.get():
                if e.type == pygame.QUIT or (e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE):
                    return False
                if e.type == pygame.JOYBUTTONDOWN and held != e.button:
                    held, start = e.button, pygame.time.get_ticks()
                if e.type == pygame.JOYBUTTONUP and e.button == held:
                    held = None
            if held is not None and pygame.time.get_ticks() - start >= 500:
                mapping[label] = held
                held, idx = None, idx + 1
                pygame.time.wait(200)
            self.clock.tick(60)
        BTN.update(mapping)
        save_map(mapping)
        self.map = mapping
        return True

    # -- input ----------------------------------------------------------------
    def actions(self, events):
        out = []
        now = time.time()
        for e in events:
            if e.type == pygame.QUIT:
                out.append("quit")
            elif e.type == pygame.KEYDOWN and e.key == pygame.K_F4 and e.mod & pygame.KMOD_ALT:
                out.append("quit")
            elif e.type == pygame.KEYDOWN:
                act = KEYS.get(e.key)
                if act == "rb" and e.key == pygame.K_TAB and e.mod & pygame.KMOD_SHIFT:
                    act = "lb"
                out.append(act)
            elif e.type == pygame.JOYBUTTONDOWN:
                for name, num in BTN.items():
                    if e.button == num:
                        out.append(name.lower())
                        break
            elif e.type == pygame.JOYHATMOTION:
                hx, hy = e.value
                out.append("up" if hy == 1 else "down" if hy == -1 else
                           "left" if hx == -1 else "right" if hx == 1 else None)
            elif e.type == pygame.JOYAXISMOTION and e.axis in (0, 1):
                d = 0 if abs(e.value) < DEADZONE else (1 if e.value > 0 else -1)
                key = (e.instance_id if hasattr(e, "instance_id") else 0, e.axis)
                last = self.last_axis.get(key, (0, 0))
                if d and (d != last[0] or now - last[1] > REPEAT):
                    out.append(("right" if d > 0 else "left") if e.axis == 0 else
                               ("down" if d > 0 else "up"))
                    self.last_axis[key] = (d, now)
                elif not d:
                    self.last_axis[key] = (0, now)
            elif e.type in (getattr(pygame, "JOYDEVICEADDED", -1),):
                self.joys = [pygame.joystick.Joystick(i) for i in range(pygame.joystick.get_count())]
                for j in self.joys:
                    j.init()
                if not self.map:
                    self.map_buttons()
        return [a for a in out if a]

    def handle(self, act):
        if act == "quit":
            self.running = False
        elif self.menu is not None:
            if act == "up":
                self.menu = (self.menu - 1) % len(self.MENU)
            elif act == "down":
                self.menu = (self.menu + 1) % len(self.MENU)
            elif act in ("b", "start"):
                self.menu = None
            elif self.MENU[self.menu] == "Size" and act in ("left", "right", "a"):
                names = list(SIZES)
                i = names.index(self.prefs["size"]) + (-1 if act == "left" else 1)
                self.prefs["size"] = names[i % len(names)] if act == "a" else names[max(0, min(len(names) - 1, i))]
                save_prefs(self.prefs)
                self.set_size()
            elif act == "a":
                item = self.MENU[self.menu]
                if item == "Configure buttons":
                    self.map_buttons()
                elif item == "Controller style":
                    styles = [None, "xbox", "playstation", "nintendo", "generic"]
                    self.style_override = styles[(styles.index(self.style_override) + 1) % len(styles)]
                    self.prefs["style"] = self.style_override
                    save_prefs(self.prefs)
                else:
                    self.running = False
        elif act in ("lb", "rb"):
            self.tab = 1 - self.tab
            self.sel, self.detail = 0, None
        elif act == "start":
            self.menu = 0
        elif act == "back":
            self.api.refresh()
            self.flash = ("Refreshed", time.time())
        elif act in ("x", "y"):
            i = RANGES.index(self.api.range) + (-1 if act == "x" else 1)
            self.api.range = RANGES[max(0, min(len(RANGES) - 1, i))]
            self.api.refresh()
        elif self.detail:
            if act == "b":
                self.detail, self.sel = None, self.detail[2]
            elif act == "a" and self.detail[0] == "hw":
                self.sel += 1          # draw_section wraps back to the first page
        elif act in ("up", "down"):
            self.sel += -1 if act == "up" else 1
        elif act == "a":
            if self.tab == 0:
                secs = self.sections()
                if secs:
                    self.detail = ("hw", secs[min(self.sel, len(secs) - 1)][0], self.sel)
                    self.sel = 0
            else:
                net = (self.api.state or {}).get("net") or {}
                devs = sorted([d for d in net.get("devices", []) if d["route"] != "internet"],
                              key=lambda d: (not d.get("streaming_since"), d["first_seen"]))
                if devs:
                    self.detail = ("dev", devs[min(self.sel, len(devs) - 1)]["ip"], self.sel)

    def offer_start(self):
        """BatoWatch isn't answering: say so, and offer to start it."""
        self.paint_bg()
        self.draw_header()
        err = self.api.error or ""
        if "Unauthorized" in err or "401" in err:
            title = "BatoWatch didn't accept the TV app's login."
        elif "WEB_PORT=0" in err:
            title = "BatoWatch's dashboard is turned off."
        else:
            title = "BatoWatch isn't running."
        self.text(title, self.f_big, INK, (self.W // 2, self.H // 2 - self.s(40)), "center")
        self.text(f"Press {self.icons()['A']} to start it, {self.icons()['B']} to leave.", self.f, MUTED,
                  (self.W // 2, self.H // 2 + self.s(30)), "center")

    def run(self):
        if self.joys and not self.map:
            self.map_buttons()
        shot = os.environ.get("BATOWATCH_TV_SHOT")      # tests: save a frame and exit
        shot_at = time.time() + float(os.environ.get("BATOWATCH_TV_SHOT_AFTER", "6"))
        keys = os.environ.get("BATOWATCH_TV_KEYS", "").split()
        while self.running:
            if keys and self.api.state:
                for a in keys:
                    self.handle(a)
                keys = []
            if shot and time.time() >= shot_at:
                pygame.image.save(self.screen, shot)
                break
            acts = self.actions(pygame.event.get())
            now = time.time()
            # loading: always for one loop at the start, then until BatoWatch
            # answers (or, after starting it from here, until it has had time to)
            loading = now < self.splash_until or (self.api.state is None and (
                self.api.error is None or now < self.wait_until))
            down = not loading and self.api.state is None and self.api.error is not None
            if loading:
                if any(a in ("b", "quit") for a in acts):
                    self.running = False
                self.draw_loading()
                pygame.display.flip()
                self.clock.tick(30)
                continue
            for a in acts:
                if down and a == "a":
                    subprocess.Popen(["batocera-services", "start", "batowatch"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self.api.refresh()
                    self.wait_until = time.time() + 20
                elif down and a == "b":
                    self.running = False
                else:
                    self.handle(a)
            if down:
                self.offer_start()
            else:
                self.paint_bg()
                self.draw_header()
                if self.detail and self.detail[0] == "hw":
                    self.draw_section(self.detail[1])
                elif self.detail:
                    self.draw_device(self.detail[1])
                elif self.tab == 0:
                    self.draw_hardware()
                else:
                    self.draw_network()
                if self.menu is not None:
                    self.draw_menu()
                if self.flash and time.time() - self.flash[1] < 1.5:
                    r = self.text(self.flash[0], self.f_mid, BG, (self.W // 2, self.s(36)), "midtop")
                    pygame.draw.rect(self.screen, MINT, r.inflate(self.s(30), self.s(14)), border_radius=8)
                    self.text(self.flash[0], self.f_mid, BG, (self.W // 2, self.s(36)), "midtop")
            pygame.display.flip()
            self.clock.tick(30)
        pygame.quit()


def human_bytes(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def human_bits(bps):
    b = bps * 8
    if b < 1e3:
        return f"{b:.0f} b/s"
    if b < 1e6:
        return f"{b / 1e3:.0f} kb/s"
    return f"{b / 1e6:.1f} Mb/s"


def human_dur(sec):
    sec = int(max(0, sec))
    h, m = sec // 3600, sec % 3600 // 60
    return f"{h}h {m:02d}m" if h else f"{m}m {sec % 60:02d}s"


if __name__ == "__main__":
    App().run()
