"""
BatoWatch hardware alerts: a banner for critical problems only.

Rules so it doesn't nag:
  - a condition has to last 30 s (disk space and disk health: right away),
  - the same alert repeats at most every 30 min while it lasts,
  - it's re-armed only after the value has been back to normal for a minute,
  - nothing in the first minute after start, while values settle.
Every alert is also kept in the history and listed on the dashboard.
"""
import time

from hardware import main_cpu_temp, main_gpu_temp

HOLD = 30
REPEAT = 1800
CLEAR = 60
SETTLE = 60
TEMP_MARGIN = 3.0     # °C below the limit before a temperature counts as normal again


def fmt_temp(c, unit):
    if unit == "F":
        return f"{c * 9 / 5 + 32:.0f} °F"
    return f"{c:.0f} °C"


def temp_limit(setting, reading, fallback, extra_keys=()):
    if setting == 0:
        return None
    if setting != "auto":
        return float(setting)
    for k in ("crit",) + tuple(extra_keys) + ("max",):
        v = reading.info.get(k) if reading else None
        if v:
            return float(v)
    return fallback


class Alert:
    __slots__ = ("key", "kind", "text", "value", "limit", "since", "active", "shown_at",
                 "normal_since", "event_id", "immediate")

    def __init__(self, key, kind, immediate):
        self.key, self.kind, self.immediate = key, kind, immediate
        self.text, self.value, self.limit = "", None, None
        self.since = self.shown_at = self.normal_since = None
        self.active = False
        self.event_id = None


class Alerts:
    def __init__(self, history=None, notify=None):
        self.started = time.time()
        self.state = {}
        self.history = history
        self.notify = notify or (lambda text, kind: None)
        self.baseline = {}         # SMART counters when first seen
        self.limits = {}           # what the dashboard colours cards by

    def _cond(self, key, kind, bad, normal, text, value, limit, now, immediate=False):
        a = self.state.get(key)
        if a is None:
            a = self.state[key] = Alert(key, kind, immediate)
        a.value, a.limit = value, limit
        if bad:
            a.text = text
            a.normal_since = None
            if a.since is None:
                a.since = now
            held = now - a.since >= (0 if immediate else HOLD)
            if held and now - self.started >= SETTLE:
                if not a.active:
                    a.active = True
                    if self.history:
                        a.event_id = self.history.event_start("alert", now, key, text, value)
                if a.shown_at is None or now - a.shown_at >= REPEAT:
                    a.shown_at = now
                    self.notify(text, "alert")
        else:
            a.since = None
            if normal:
                if a.normal_since is None:
                    a.normal_since = now
                if a.active and now - a.normal_since >= CLEAR:
                    a.active = False
                    a.shown_at = None
                    if self.history and a.event_id:
                        self.history.event_end(a.event_id, now)
                    a.event_id = None

    def check(self, readings, cfg, unit, now=None):
        now = now or time.time()
        if not cfg["ALERTS"]:
            for a in self.state.values():
                if a.active and self.history and a.event_id:
                    self.history.event_end(a.event_id, now)
                a.active = False
            self.limits = {}
            return
        limits = {}

        # CPU / GPU temperature
        for key, finder, setting, label in (("cpu_temp", main_cpu_temp, "ALERT_CPU_TEMP", "CPU"),
                                            ("gpu_temp", main_gpu_temp, "ALERT_GPU_TEMP", "GPU")):
            r = finder(readings)
            lim = temp_limit(cfg[setting], r, 90.0)
            if r is None or lim is None:
                continue
            limits[r.name] = {"limit": lim, "word": "Hot"}
            self._cond(key, "temp", r.value >= lim, r.value < lim - TEMP_MARGIN,
                       f"{label} too hot · {fmt_temp(r.value, unit)}", r.value, lim, now)

        # Disk temperature (SMART, or the nvme/drivetemp sensor)
        disk_temps = {}
        for r in readings.values():
            if r.group == "temp" and r.info.get("kind") == "disk":
                dev = r.info.get("dev") or r.info.get("chip")
                if dev not in disk_temps or r.name.startswith("smart."):
                    disk_temps[dev] = r
        for dev, r in disk_temps.items():
            lim = temp_limit(cfg["ALERT_DISK_TEMP"], r, 70.0, ("temp_limit",))
            if lim is None:
                continue
            limits[r.name] = {"limit": lim, "word": "Hot"}
            self._cond(f"disk_temp:{dev}", "temp", r.value >= lim, r.value < lim - TEMP_MARGIN,
                       f"Disk too hot · {r.info.get('model') or dev} {fmt_temp(r.value, unit)}",
                       r.value, lim, now)

        # Disk almost full: less than N GB or N % free, whichever comes first
        gb, pct = cfg["ALERT_DISK_FREE_GB"], cfg["ALERT_DISK_FREE_PCT"]
        for r in readings.values():
            if r.group != "disk":
                continue
            free_gb, free_pct = r.info.get("free_gb", 1e9), r.info.get("free_pct", 100)
            low = (gb > 0 and free_gb < gb) or (pct > 0 and free_pct < pct)
            if gb > 0 or pct > 0:
                limits[r.name] = {"free_gb": gb, "free_pct": pct, "word": "Almost full"}
            left = f"{free_gb:.1f} GB left" if free_gb < 10 else f"{free_gb:.0f} GB left"
            self._cond(f"disk_full:{r.info.get('mount')}", "disk_full", low,
                       not low, f"Disk almost full · {r.info.get('mount')} {left}", free_gb,
                       gb, now, immediate=True)

        # Disk health
        for r in readings.values():
            if r.group != "smart" or not r.name.endswith(".health"):
                continue
            info, dev = r.info, r.info.get("dev")
            base = self.baseline.setdefault(dev, {k: info.get(k) for k in ("reallocated",
                                                                         "pending")})
            why = None
            if info.get("passed") is False:
                why = "SMART says failing"
            elif info.get("critical_warning"):
                why = "critical warning"
            elif any(info.get(k) is not None and base.get(k) is not None and
                     info[k] > base[k] for k in ("reallocated", "pending")):
                why = "bad sectors went up"
            elif (info.get("wear_pct") or 0) >= 90:
                why = f"worn out ({info['wear_pct']}%)"
            limits[r.name] = {"word": "Failing"}
            self._cond(f"disk_health:{dev}", "disk_health", why is not None, why is None,
                       f"Disk failing · {info.get('model') or dev}{' (' + why + ')' if why else ''}",
                       None, None,
                       now, immediate=True)

        # RAM almost full (and swap too, if there is any)
        r = readings.get("mem.used_pct")
        lim = cfg["ALERT_RAM"]
        if r is not None and lim > 0:
            avail = r.info.get("avail_pct", 100)
            swap = readings.get("mem.swap_used_pct")
            swap_full = swap is None or swap.value >= 90
            limits[r.name] = {"avail_pct": lim, "word": "Almost full"}
            self._cond("ram", "ram", avail < lim and swap_full, avail >= lim + 2,
                       f"RAM almost full · {avail:.0f}% left", avail, lim, now)
        self.limits = limits

    def active(self):
        return [{"key": a.key, "kind": a.kind, "text": a.text, "since": a.since,
                 "value": a.value} for a in self.state.values() if a.active]
