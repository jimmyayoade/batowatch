"""
BatoWatch settings: one KEY=value per line in batowatch.conf.

The conf is the only place settings are stored. The dashboard reads and
writes it through this module; people can also edit it by hand. Values use
shell syntax (the service script sources the same file for its log
settings), so a value with spaces is quoted.

Every setting is declared once in SETTINGS below: its type, default, the
values it accepts, whether it applies live or needs a restart, and the help
line shown on the dashboard and in the conf.
"""
import os
import re
import shlex
import tempfile

HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
DURATION_RE = re.compile(r"^(\d+)\s*([smhd]?)$")


class Setting:
    __slots__ = ("key", "kind", "default", "choices", "live", "help", "section",
                 "minimum", "maximum", "hidden")

    def __init__(self, key, kind, default, help, section, live=True, choices=None,
                 minimum=None, maximum=None, hidden=False):
        self.key, self.kind, self.default, self.help = key, kind, default, help
        self.section, self.live, self.choices = section, live, choices
        self.minimum, self.maximum, self.hidden = minimum, maximum, hidden


S = Setting
SETTINGS = [
    # -- dashboard ----------------------------------------------------------
    S("LOGIN", "login", "batocera", "Dashboard login: 'batocera' (user root, your Batocera "
      "password), 'user:password' for your own, or 'none'", "Dashboard"),
    S("HTTPS", "choice", "self", "self = BatoWatch's own certificate (browser warns once); "
      "auto = also a Tailscale certificate for the tailnet name (publishes that name in "
      "public certificate logs); tailscale = only that; off = plain http", "Dashboard",
      live=False, choices=("self", "auto", "tailscale", "off")),
    S("WEB_PORT", "int", 8686, "Dashboard port; 0 turns the dashboard off", "Dashboard",
      live=False, minimum=0, maximum=65535),
    S("WEB_BIND", "str", "0.0.0.0", "Address the dashboard listens on: 0.0.0.0 = every "
      "network; your Tailscale IP (100.x.y.z) keeps it off the LAN", "Dashboard", live=False),
    S("DEFAULT_TAB", "choice", "hardware", "Tab the dashboard opens on", "Dashboard",
      choices=("hardware", "network")),
    S("TEMP_UNIT", "choice", "TZ", "C, F, or TZ (picked from Batocera's time zone)",
      "Dashboard", choices=("TZ", "C", "F")),
    # -- parts --------------------------------------------------------------
    S("NETWORK", "bool", True, "Watch who connects to this box (the Network tab)", "Parts",
      live=False),
    S("HARDWARE", "bool", True, "Watch the hardware, with history (the Hardware tab)", "Parts",
      live=False),
    # -- banners ------------------------------------------------------------
    S("NOTIFY", "choice", "overlay", "How banners are shown: overlay (drawn over games), "
      "es (EmulationStation popup), both, or print (log only)", "Banners",
      choices=("overlay", "es", "both", "print")),
    S("OVERLAY_DURATION", "float", 5.0, "Seconds a connect banner stays on screen", "Banners",
      minimum=1, maximum=60),
    S("CONNECT_REPEAT", "int", 10, "Seconds after a stream ends before the same device "
      "gets a connect banner again (0 = always)", "Banners", minimum=0, maximum=3600),
    S("OVERLAY_STYLE", "choice", "outline", "outline = see-through outlined text; solid = "
      "dark box", "Banners", choices=("outline", "solid")),
    S("OVERLAY_SCALE", "float", 1.1, "Banner size (1.0 = 26 px text at 1440p)", "Banners",
      minimum=0.5, maximum=4),
    S("OVERLAY_POSITION", "choice", "top-right", "Where banners appear", "Banners",
      choices=("top-right", "top-left", "top-center")),
    S("OVERLAY_ACCENT", "color", "#6fd0ff", "Connect banner caption and accent colour (the logo's blue)",
      "Banners"),
    S("OVERLAY_FG", "color", "#ffffff", "Banner text colour", "Banners"),
    S("OVERLAY_BG", "color", "#161a22", "Banner box colour (solid style)", "Banners"),
    S("OVERLAY_OUTLINE", "color", "#000000", "Banner text edge colour (outline style)",
      "Banners"),
    # -- hardware -----------------------------------------------------------
    S("HW_INTERVAL", "int", 10, "Seconds between hardware samples", "Hardware", live=False,
      minimum=2, maximum=300),
    S("SMART_INTERVAL", "int", 600, "Seconds between disk health checks (smartctl); 0 turns "
      "them off", "Hardware", live=False, minimum=0, maximum=86400),
    S("HW_SKIP", "list", "", "Sources to turn off, comma-separated (e.g. smart,nvidia)",
      "Hardware", live=False),
    S("KEEP_RAW", "duration", "24h", "How long 10-second samples are kept", "History"),
    S("KEEP_MINUTE", "duration", "7d", "How long 1-minute averages are kept", "History"),
    S("KEEP_HOUR", "duration", "365d", "How long 1-hour averages are kept", "History"),
    S("HISTORY_DIR", "str", "", "Where the history is stored (empty = the add-on's "
      "history folder)", "History", live=False),
    # -- alerts -------------------------------------------------------------
    S("ALERTS", "bool", True, "Show a banner for critical hardware problems", "Alerts"),
    S("ALERT_DURATION", "float", 10.0, "Seconds an alert banner stays on screen", "Alerts",
      minimum=1, maximum=120),
    S("ALERT_ACCENT", "color", "#ef4444", "Alert banner caption and accent colour", "Alerts"),
    S("ALERT_CPU_TEMP", "limit", "auto", "CPU temperature limit in °C; auto = the sensor's "
      "own limit, else 90; 0 turns it off", "Alerts"),
    S("ALERT_GPU_TEMP", "limit", "auto", "GPU temperature limit in °C; auto = the sensor's "
      "own limit, else 90; 0 turns it off", "Alerts"),
    S("ALERT_DISK_TEMP", "limit", "auto", "Disk temperature limit in °C; auto = the "
      "drive's own limit, else 70; 0 turns it off", "Alerts"),
    S("ALERT_DISK_FREE_GB", "float", 5.0, "Alert when a drive has less than this many GB "
      "free; 0 turns it off", "Alerts", minimum=0, maximum=100000),
    S("ALERT_DISK_FREE_PCT", "float", 2.0, "Or less than this percent free, whichever comes "
      "first; 0 turns it off", "Alerts", minimum=0, maximum=100),
    S("ALERT_RAM", "float", 5.0, "Alert when less than this percent of RAM is available; 0 "
      "turns it off", "Alerts", minimum=0, maximum=100),
    S("ALERT_UNKNOWN", "bool", True, "Alert when a device that isn't known connects to "
      "this box", "Alerts"),
    S("ALERT_ATTEMPTS", "bool", False, "Also alert when an unknown device tries a port "
      "nothing is listening on (scans; phones and TVs do it too)", "Alerts"),
    S("KNOWN_DEVICES", "choice", "named", "Known devices: named = the ones in aliases.conf "
      "and your Tailscale machines; seen = also every device in the history", "Alerts",
      choices=("named", "seen")),
    # -- network details ------------------------------------------------------
    S("INTERVAL", "float", 2.0, "Seconds between connection scans", "Network", live=False,
      minimum=0.5, maximum=60),
    S("SUBNET_ROUTER", "list", "", "LAN IPs of subnet routers that auto-detection misses, "
      "comma-separated", "Network", live=False),
    S("WG_PORT", "int", 41641, "tailscaled's WireGuard port (hidden as Tailscale's own "
      "traffic)", "Network", live=False, minimum=1, maximum=65535),
    S("SHOW_GATEWAY", "bool", False, "Show your router's own DNS/UPnP traffic", "Network",
      live=False),
    S("SHOW_TRANSPORT", "bool", False, "Show Tailscale's own traffic (WireGuard, DERP, "
      "MagicDNS)", "Network", live=False),
    S("INBOUND_ONLY", "bool", False, "Only show connections made to this box", "Network",
      live=False),
    S("SUNSHINE_CONF", "str", "/userdata/system/add-ons/sunshine/.config/sunshine/sunshine.conf",
      "Sunshine's config file (for its ports)", "Network", live=False),
    S("TS_SOCKET", "str", "/var/run/tailscale/tailscaled.sock", "tailscaled's socket",
      "Network", live=False),
    S("DISPLAY", "str", ":0", "X display banners are drawn on", "Banners", live=False),
    # -- logs (also read by the service script) ------------------------------
    S("LOG", "str", "/userdata/system/logs/batowatch.log", "Where BatoWatch writes its log",
      "Logs", live=False),
    S("LOG_MAX_BYTES", "int", 1048576, "Rotate the log at this size; 0 never rotates",
      "Logs", live=False, minimum=0),
    S("LOG_KEEP", "int", 1, "Rotated logs to keep (at least 1)", "Logs", live=False,
      minimum=1, maximum=20),
    S("SERVICE_LOG", "str", "", "The service's own small log (empty = batowatch-service.log "
      "next to LOG)", "Logs", live=False),
    # -- test only (never on the settings page) ------------------------------
    S("HW_SYSFS_ROOT", "str", "/", "Tests only: read /sys and /proc from a fake tree",
      "Test", live=False, hidden=True),
    S("PASSWORD_CMD", "str", "batocera-config getRootPassword", "Tests only: command that "
      "prints the Batocera password", "Test", live=False, hidden=True),
]
BY_KEY = {s.key: s for s in SETTINGS}


def parse_duration(text):
    """'24h' -> 86400. Plain numbers are seconds."""
    m = DURATION_RE.match(str(text).strip().lower())
    if not m:
        raise ValueError(f"'{text}' isn't a duration like 30m, 24h or 7d")
    n, unit = int(m.group(1)), m.group(2) or "s"
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def coerce(setting, raw):
    """Turn the conf's text into the setting's value, or raise ValueError
    with a message a person can act on."""
    s, text = setting, str(raw).strip()
    if s.kind == "str":
        return text
    if s.kind == "bool":
        low = text.lower()
        if low in ("1", "on", "yes", "true"):
            return True
        if low in ("0", "off", "no", "false"):
            return False
        raise ValueError(f"{s.key}={text} isn't on or off")
    if s.kind in ("int", "float"):
        try:
            v = int(text) if s.kind == "int" else float(text)
        except ValueError:
            raise ValueError(f"{s.key}={text} isn't a number") from None
        if s.minimum is not None and v < s.minimum:
            raise ValueError(f"{s.key}={text} is below {s.minimum}")
        if s.maximum is not None and v > s.maximum:
            raise ValueError(f"{s.key}={text} is above {s.maximum}")
        return v
    if s.kind == "choice":
        for c in s.choices:
            if text.lower() == c.lower():
                return c
        raise ValueError(f"{s.key}={text} isn't one of {', '.join(s.choices)}")
    if s.kind == "color":
        if not HEX_RE.match(text):
            raise ValueError(f"{s.key}={text} isn't a colour like #7fe3c3")
        return "#" + text.lstrip("#").lower()
    if s.kind == "duration":
        parse_duration(text)
        return text.lower()
    if s.kind == "list":
        return [x.strip() for x in text.split(",") if x.strip()]
    if s.kind == "limit":
        if text.lower() == "auto":
            return "auto"
        try:
            v = float(text)
        except ValueError:
            raise ValueError(f"{s.key}={text} isn't auto, 0 or a temperature") from None
        if not 0 <= v <= 150:
            raise ValueError(f"{s.key}={text} isn't between 0 and 150 °C")
        return v
    if s.kind == "login":
        low = text.lower()
        if low in ("batocera", "none"):
            return low
        user, sep, pw = text.partition(":")
        if not sep or not user or not pw:
            raise ValueError("LOGIN must be batocera, none, or user:password")
        return text
    raise ValueError(f"unknown kind {s.kind}")


def to_text(setting, value):
    """The value as it's written in the conf (before shell quoting)."""
    if setting.kind == "bool":
        return "on" if value else "off"
    if setting.kind == "list":
        return ",".join(value) if isinstance(value, list) else str(value)
    if setting.kind == "float" and float(value).is_integer():
        return str(int(value))
    return str(value)


def quote(text):
    """Shell-safe, and left bare when it doesn't need quotes (easier to read)."""
    if text and re.match(r"^[A-Za-z0-9_.,:/#@%+=-]+$", text):
        return text
    return shlex.quote(text)


LINE_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)=(.*)$")


def read_conf_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()
    except FileNotFoundError:
        return []


def shell_words(text):
    """Split a value the way bash reads it: quotes group, a backslash escapes,
    and '#' starts a comment only at the start of a word (so #6fd0ff and
    pa#ss are values, and 'x # note' is x)."""
    words, cur, have, quote, i = [], [], False, None, 0
    while i < len(text):
        c = text[i]
        if quote == "'":
            if c == "'":
                quote = None
            else:
                cur.append(c)
        elif quote == '"':
            if c == '"':
                quote = None
            elif c == "\\" and i + 1 < len(text) and text[i + 1] in '"\\$`':
                i += 1
                cur.append(text[i])
            else:
                cur.append(c)
        elif c in " \t":
            if have:
                words.append("".join(cur))
                cur, have = [], False
        elif c == "#" and not have and words:
            break                                # a comment after the value
        elif c in "'\"":
            quote, have = c, True
        elif c == "\\" and i + 1 < len(text):
            i += 1
            cur.append(text[i])
            have = True
        else:
            cur.append(c)
            have = True
        i += 1
    if have:
        words.append("".join(cur))
    return words


def parse_lines(lines):
    """{KEY: raw text} for every KEY=value line. Later lines win, like in
    the shell."""
    raw = {}
    for line in lines:
        m = LINE_RE.match(line)
        if not m:
            continue
        key, rest = m.group(1), m.group(2)
        # KEY= with a space after it is empty in bash (whatever follows isn't the value)
        parts = [] if rest[:1] in (" ", "\t") else shell_words(rest.rstrip())
        raw[key] = " ".join(parts) if parts else ""
    return raw


class Settings:
    """Current values, the problems found in the conf, and the conf's
    modification time (to notice hand edits and refuse stale saves)."""

    def __init__(self, path, overrides=None):
        self.path = path
        self.overrides = overrides or {}
        self.values, self.errors, self.mtime, self.unknown = {}, {}, None, []
        self.load()

    def load(self):
        lines = read_conf_lines(self.path)
        try:
            self.mtime = os.stat(self.path).st_mtime_ns
        except OSError:
            self.mtime = None
        raw = parse_lines(lines)
        raw.update(self.overrides)
        values, errors = {}, {}
        for s in SETTINGS:
            if s.key in raw and raw[s.key] != "":
                try:
                    values[s.key] = coerce(s, raw[s.key])
                    continue
                except ValueError as e:
                    errors[s.key] = {"text": raw[s.key], "error": str(e)}
            values[s.key] = coerce(s, s.default) if s.kind != "str" else s.default
        self.unknown = sorted(k for k in raw if k not in BY_KEY)
        old = self.values
        self.values, self.errors = values, errors
        return [k for k in values if old and old.get(k) != values[k]]

    def changed_on_disk(self):
        try:
            return os.stat(self.path).st_mtime_ns != self.mtime
        except OSError:
            return self.mtime is not None

    def __getitem__(self, key):
        return self.values[key]

    def get(self, key, default=None):
        return self.values.get(key, default)

    def describe(self, hide_secret=True):
        """Everything the settings page needs, password left out."""
        out = []
        for s in SETTINGS:
            if s.hidden:
                continue
            v = self.values[s.key]
            if s.kind == "login" and hide_secret and v not in ("batocera", "none"):
                v = v.split(":", 1)[0] + ":"   # user name only
            out.append({"key": s.key, "kind": s.kind, "value": to_text(s, v) if
                        s.kind != "login" else v, "default": to_text(s, s.default),
                        "choices": list(s.choices) if s.choices else None,
                        "live": s.live, "help": s.help, "section": s.section,
                        "min": s.minimum, "max": s.maximum,
                        "error": self.errors.get(s.key)})
        return out

    def save(self, changes, expect_mtime):
        """Write the changed keys, keep everything else (comments, order,
        unknown lines). Returns (restart_needed, errors). Raises
        StaleConf when the file changed since the caller loaded it."""
        if expect_mtime is not None and str(expect_mtime) != str(self.mtime):
            raise StaleConf()
        errors, clean = {}, {}
        for key, text in changes.items():
            s = BY_KEY.get(key)
            if s is None or s.hidden:
                errors[key] = "not a setting that can be changed here"
                continue
            if s.kind == "login" and text.endswith(":") and text.count(":") == 1:
                # The page shows "user:" for an own login; unchanged means keep it
                continue
            try:
                clean[key] = to_text(s, coerce(s, text))
            except ValueError as e:
                errors[key] = str(e)
        if errors:
            return False, errors
        lines = read_conf_lines(self.path)
        done = set()
        for i, line in enumerate(lines):
            m = LINE_RE.match(line)
            if m and m.group(1) in clean:
                lines[i] = f"{m.group(1)}={quote(clean[m.group(1)])}"
                done.add(m.group(1))
        for key in clean:
            if key not in done:
                s = BY_KEY[key]
                lines += ["", f"# {s.help}", f"{key}={quote(clean[key])}"]
        write_atomic(self.path, "\n".join(lines) + "\n")
        restart = any(not BY_KEY[k].live for k in clean
                      if to_text(BY_KEY[k], self.values[k]) != clean[k])
        self.load()
        return restart, {}


class StaleConf(Exception):
    pass


def write_atomic(path, text):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".batowatch.conf.", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        except OSError:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# -- NetWatch conf -> BatoWatch keys (used by the installer) -----------------
NETWATCH_FLAGS = {
    "--overlay-scale": "OVERLAY_SCALE", "--overlay-style": "OVERLAY_STYLE",
    "--overlay-duration": "OVERLAY_DURATION", "--notify": "NOTIFY",
    "--web-port": "WEB_PORT", "--web-bind": "WEB_BIND", "--web-auth": "LOGIN",
    "--subnet-router": "SUBNET_ROUTER", "--wg-port": "WG_PORT",
    "--sunshine-conf": "SUNSHINE_CONF", "--ts-socket": "TS_SOCKET",
    "--interval": "INTERVAL", "--display": "DISPLAY",
}
NETWATCH_SWITCHES = {"--show-gateway": "SHOW_GATEWAY", "--show-transport": "SHOW_TRANSPORT",
                     "--inbound-only": "INBOUND_ONLY"}
NETWATCH_VARS = {"NETWATCH_LOG": "LOG", "NETWATCH_LOG_MAX_BYTES": "LOG_MAX_BYTES",
                 "NETWATCH_LOG_KEEP": "LOG_KEEP", "NETWATCH_SERVICE_LOG": "SERVICE_LOG"}
NETWATCH_DEFAULT_LOGS = ("", "/userdata/system/logs/netwatch.log",
                         "/userdata/system/add-ons/netwatch/netwatch.log")


def convert_netwatch(netwatch_conf):
    """{KEY: text} from a NetWatch conf, plus the flags BatoWatch doesn't know."""
    raw = parse_lines(read_conf_lines(netwatch_conf))
    out, unknown = {}, []
    args = shlex.split(raw.get("NETWATCH_ARGS", "")) if raw.get("NETWATCH_ARGS") else []
    i, routers = 0, []
    while i < len(args):
        a = args[i]
        flag, eq, val = a.partition("=")
        if flag in NETWATCH_SWITCHES:
            out[NETWATCH_SWITCHES[flag]] = "on"
        elif flag in NETWATCH_FLAGS:
            if not eq:
                i += 1
                val = args[i] if i < len(args) else ""
            key = NETWATCH_FLAGS[flag]
            if key == "SUBNET_ROUTER":
                routers.append(val)
            else:
                out[key] = val
        else:
            unknown.append(a)
        i += 1
    if routers:
        out["SUBNET_ROUTER"] = ",".join(routers)
    for old, new in NETWATCH_VARS.items():
        val = raw.get(old)
        if val is None:
            continue
        if new == "LOG":
            if val in NETWATCH_DEFAULT_LOGS:
                continue   # NetWatch's default moves to batowatch.log
        elif new == "SERVICE_LOG" and val == "":
            continue
        out[new] = val
    return out, unknown


def apply_to_conf(path, values):
    """Set KEY=value lines in an existing conf file (the installer's
    carry-over). Values are written as given; nothing is validated here, so
    a bad old value shows up on the settings page like a hand edit would."""
    lines = read_conf_lines(path)
    done = set()
    for i, line in enumerate(lines):
        m = LINE_RE.match(line)
        if m and m.group(1) in values:
            lines[i] = f"{m.group(1)}={quote(values[m.group(1)])}"
            done.add(m.group(1))
    for key, val in values.items():
        if key not in done:
            lines.append(f"{key}={quote(val)}")
    write_atomic(path, "\n".join(lines) + "\n")


def add_missing_keys(path, template):
    """Append settings a newer version introduced (with their comment block
    from the shipped template), so an existing conf documents them too."""
    have = set(parse_lines(read_conf_lines(path)))
    tmpl = read_conf_lines(template)
    blocks, comment = [], []
    for line in tmpl:
        m = LINE_RE.match(line)
        if m:
            if m.group(1) not in have:
                blocks.append(comment + [line])
            comment = []
        elif line.startswith("#"):
            comment.append(line)
        else:
            comment = []
    if not blocks:
        return []
    lines = read_conf_lines(path)
    lines += ["", "# --- Added by a newer BatoWatch (set to the defaults) ---"]
    for b in blocks:
        lines += [""] + b
    write_atomic(path, "\n".join(lines) + "\n")
    return [LINE_RE.match(b[-1]).group(1) for b in blocks]


if __name__ == "__main__":
    # Used by the installer:  settings.py convert <netwatch.conf> <batowatch.conf>
    #                         settings.py add-missing <batowatch.conf> <template>
    import json
    import sys
    if len(sys.argv) == 4 and sys.argv[1] == "convert":
        vals, unk = convert_netwatch(sys.argv[2])
        apply_to_conf(sys.argv[3], vals)
        print(json.dumps({"carried": sorted(vals), "unknown": unk}))
    elif len(sys.argv) == 4 and sys.argv[1] == "add-missing":
        print(json.dumps({"added": add_missing_keys(sys.argv[2], sys.argv[3])}))
    else:
        sys.exit("usage: settings.py convert <netwatch.conf> <batowatch.conf> | "
                 "add-missing <batowatch.conf> <template>")
