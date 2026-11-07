"""
BatoWatch hardware monitor: what the box is doing, from /proc and /sys.

Every data source is a small class with probe() and read(). probe() says
"available" or "missing" with a reason a person can understand; read()
returns readings. A source that fails three times in a row is switched off,
listed with its error, and tried again every 10 minutes. One bad sensor
never stops the others.

The generic hwmon source reads every temperature, fan, voltage and power
sensor the kernel exposes, named after its chip and label, so hardware
nobody has seen before still shows up without new code.
"""
import http.client
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time

LOG = logging.getLogger("batowatch")

CPU_CHIPS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "soc_thermal",
             "x86_pkg_temp", "fam15h_power", "via_cputemp")
GPU_CHIPS = ("amdgpu", "radeon", "nouveau", "i915", "xe")
DISK_CHIPS = ("nvme", "drivetemp")
FS_TYPES = ("ext2", "ext3", "ext4", "btrfs", "xfs", "f2fs", "vfat", "exfat", "ntfs",
            "ntfs3", "fuseblk", "jfs", "reiserfs")
GOOD_TEMP = (-40.0, 150.0)

# Countries that use Fahrenheit (TEMP_UNIT=TZ): US states and territories,
# Bahamas, Belize, Cayman Islands, Liberia, Palau, Marshall Islands, Micronesia
F_ZONES = {
    "America/New_York", "America/Detroit", "America/Chicago", "America/Denver",
    "America/Phoenix", "America/Los_Angeles", "America/Anchorage", "America/Juneau",
    "America/Sitka", "America/Metlakatla", "America/Yakutat", "America/Nome", "America/Adak",
    "America/Boise", "America/Menominee", "America/Puerto_Rico", "America/St_Thomas",
    "America/Nassau", "America/Belize", "America/Cayman", "Africa/Monrovia", "Pacific/Palau",
    "Pacific/Majuro", "Pacific/Kwajalein", "Pacific/Chuuk", "Pacific/Pohnpei",
    "Pacific/Kosrae", "Pacific/Honolulu", "Pacific/Guam", "Pacific/Saipan",
    "Pacific/Pago_Pago", "Pacific/Midway", "Pacific/Wake", "Pacific/Johnston", "US/Eastern",
    "US/Central", "US/Mountain", "US/Pacific", "US/Alaska", "US/Hawaii", "US/Arizona",
    "US/Michigan", "US/East-Indiana", "US/Indiana-Starke", "US/Aleutian", "US/Samoa",
    "EST5EDT", "CST6CDT", "MST7MDT", "PST8PDT", "EST", "MST", "HST",
}
F_PREFIXES = ("America/Indiana/", "America/Kentucky/", "America/North_Dakota/")


def timezone_name():
    """Batocera's system.timezone, else /etc/localtime's target."""
    try:
        with open("/userdata/system/batocera.conf", errors="replace") as fh:
            for line in fh:
                m = re.match(r"\s*system\.timezone\s*=\s*(\S+)", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    try:
        target = os.readlink("/etc/localtime")
        return target.split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return None


def unit_for(setting):
    if setting in ("C", "F"):
        return setting
    tz = timezone_name() or ""
    return "F" if tz in F_ZONES or tz.startswith(F_PREFIXES) else "C"


class Reading:
    __slots__ = ("name", "value", "unit", "label", "group", "info")

    def __init__(self, name, value, unit, label, group, info=None):
        self.name, self.value, self.unit = name, value, unit
        self.label, self.group, self.info = label, group, info or {}


def _read(path, default=None):
    try:
        with open(path, errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return default


def _num(path, scale=1.0):
    t = _read(path)
    if t is None:
        return None
    try:
        return float(t) / scale
    except ValueError:
        return None


def _safe(text):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_").lower() or "x"


DISK_MODELS = {}      # "sda" -> "Patriot P210 1024GB"; smartctl's full name wins
PCI_NAMES = {}        # "0000:00:02.0" -> "Intel HD Graphics 530"
VENDORS = {"intel": "Intel", "advanced micro devices": "AMD", "amd": "AMD", "ati": "AMD",
           "nvidia": "NVIDIA", "qualcomm": "Qualcomm", "broadcom": "Broadcom"}


def disk_model(root, dev):
    """A drive's model name, for people (sda means nothing to most)."""
    if dev not in DISK_MODELS:
        model = (_read(f"{root}/sys/block/{dev}/device/model") or "").strip()
        DISK_MODELS[dev] = re.sub(r"\s+", " ", model) or dev
    return DISK_MODELS[dev]


def parent_disk(root, devpath):
    """/dev/sda1 -> sda, /dev/nvme0n1p2 -> nvme0n1, /dev/sda -> sda."""
    name = os.path.basename(devpath)
    link = f"{root}/sys/class/block/{name}"
    if os.path.exists(f"{link}/partition"):
        return os.path.basename(os.path.dirname(os.path.realpath(link)))
    return name


def pci_name(slot):
    """A graphics card's name from lspci: "Intel HD Graphics 530",
    "AMD Radeon RX 6600", "NVIDIA GeForce RTX 3060"."""
    if not slot:
        return None
    if slot not in PCI_NAMES:
        name = None
        try:
            out = subprocess.run(["lspci", "-mm", "-s", slot], capture_output=True, text=True,
                                 timeout=5).stdout
            import shlex
            f = shlex.split(out)
            if len(f) >= 4:
                vendor, device = f[2], f[3]
                m = re.search(r"\[([^\]]+)\]", device)
                device = (m.group(1) if m else device).split("/")[0].strip()
                short = next((v for k, v in VENDORS.items() if vendor.lower().startswith(k)),
                             vendor.split(" ")[0])
                name = device if device.lower().startswith(short.lower()) else f"{short} {device}"
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        PCI_NAMES[slot] = name
    return PCI_NAMES[slot]


class Source:
    name = "?"
    title = "?"

    def __init__(self, root):
        self.root = root.rstrip("/") or ""

    def p(self, path):
        return self.root + path

    def probe(self):
        return True, None

    def read(self, now, dt):
        return []


# -- CPU ------------------------------------------------------------------------
class CpuSource(Source):
    name, title = "cpu", "CPU usage and clock"

    def __init__(self, root):
        super().__init__(root)
        self.prev = None
        self.prev_throttle = None

    def probe(self):
        if _read(self.p("/proc/stat")) is None:
            return False, "/proc/stat can't be read"
        return True, None

    def _stat(self):
        out = {}
        for line in (_read(self.p("/proc/stat")) or "").splitlines():
            if line.startswith("cpu"):
                p = line.split()
                vals = [int(x) for x in p[1:9]]
                idle = vals[3] + vals[4]
                out[p[0]] = (sum(vals), idle)
        return out

    def read(self, now, dt):
        cur = self._stat()
        res = []
        if self.prev:
            for cpu, (tot, idle) in cur.items():
                pt, pi = self.prev.get(cpu, (tot, idle))
                d = tot - pt
                if d <= 0:
                    continue
                usage = 100.0 * (1 - (idle - pi) / d)
                if cpu == "cpu":
                    res.append(Reading("cpu.usage", round(usage, 1), "%", "CPU", "cpu"))
                else:
                    n = cpu[3:]
                    res.append(Reading(f"cpu.core{n}.usage", round(usage, 1), "%",
                                       f"Core {n}", "cpu-core"))
        self.prev = cur
        freqs = []
        base = self.p("/sys/devices/system/cpu")
        try:
            cpus = [d for d in os.listdir(base) if re.match(r"cpu\d+$", d)]
        except OSError:
            cpus = []
        throttle = 0
        for c in cpus:
            f = _num(f"{base}/{c}/cpufreq/scaling_cur_freq", 1000.0)
            if f:
                freqs.append(f)
            for kind in ("core", "package"):
                v = _num(f"{base}/{c}/thermal_throttle/{kind}_throttle_count")
                if v:
                    throttle += v
        if freqs:
            res.append(Reading("cpu.freq", round(sum(freqs) / len(freqs)), "MHz",
                               "CPU clock (average)", "cpu", {"max": round(max(freqs))}))
        if self.prev_throttle is not None and throttle >= self.prev_throttle:
            res.append(Reading("cpu.throttled", throttle - self.prev_throttle, "events",
                               "Throttled (too hot)", "cpu"))
        self.prev_throttle = throttle
        load = (_read(self.p("/proc/loadavg")) or "").split()
        if load:
            res.append(Reading("cpu.load1", float(load[0]), "", "Load (1 min)", "cpu",
                               {"cores": len(cpus) or None}))
        up = (_read(self.p("/proc/uptime")) or "").split()
        if up:
            res.append(Reading("uptime", float(up[0]), "s", "Uptime", "system",
                               {"no_history": True}))
        model = None
        for line in (_read(self.p("/proc/cpuinfo")) or "").splitlines():
            if line.lower().startswith(("model name", "hardware")):
                model = line.split(":", 1)[1].strip()
                break
        if model and res:
            res[0].info["model"] = model
        return res


# -- memory -----------------------------------------------------------------------
class MemSource(Source):
    name, title = "mem", "Memory"

    def probe(self):
        if _read(self.p("/proc/meminfo")) is None:
            return False, "/proc/meminfo can't be read"
        return True, None

    def read(self, now, dt):
        mi = {}
        for line in (_read(self.p("/proc/meminfo")) or "").splitlines():
            k, _, v = line.partition(":")
            try:
                mi[k] = int(v.split()[0])
            except (ValueError, IndexError):
                pass
        total, avail = mi.get("MemTotal"), mi.get("MemAvailable")
        res = []
        if total and avail is not None:
            res.append(Reading("mem.used_pct", round(100 * (1 - avail / total), 1), "%",
                               "Memory used", "mem",
                               {"total_mb": round(total / 1024), "avail_mb": round(avail / 1024),
                                "avail_pct": round(100 * avail / total, 1)}))
            res.append(Reading("mem.avail_mb", round(avail / 1024), "MB", "Memory available",
                               "mem"))
        st, sf = mi.get("SwapTotal"), mi.get("SwapFree")
        if st:
            res.append(Reading("mem.swap_used_pct", round(100 * (1 - sf / st), 1), "%",
                               "Swap used", "mem", {"total_mb": round(st / 1024)}))
        return res


# -- hwmon (every sensor the kernel exposes) -----------------------------------
class HwmonSource(Source):
    name, title = "hwmon", "Temperature, fan, voltage and power sensors"
    KINDS = {"temp": ("temp", "°C", 1000.0), "fan": ("fan", "RPM", 1.0),
             "in": ("volt", "V", 1000.0), "power": ("power", "W", 1e6),
             "curr": ("curr", "A", 1000.0)}

    def chips(self):
        base = self.p("/sys/class/hwmon")
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return []
        out = []
        for h in names:
            d = f"{base}/{h}"
            chip = _read(f"{d}/name") or h
            dev = os.path.realpath(f"{d}/device") if os.path.exists(f"{d}/device") else ""
            out.append((h, d, chip, dev))
        return out

    def probe(self):
        chips = self.chips()
        if not chips:
            return False, "no hardware sensors exposed by this system (/sys/class/hwmon is empty)"
        return True, None

    def owner(self, chip, kind, dev):
        """Model and device of the GPU or drive a sensor chip belongs to."""
        base = os.path.basename(dev)
        if kind == "gpu" and re.match(r"[0-9a-f]{4}:", base):
            return {"model": pci_name(base) or chip, "pdev": base}
        if chip == "nvme" and base.startswith("nvme"):
            ns = sorted(n for n in os.listdir(dev) if re.match(rf"{base}n\d+$", n)) \
                if os.path.isdir(dev) else []
            disk = ns[0] if ns else f"{base}n1"
            model = (_read(f"{dev}/model") or "").strip() or disk_model(self.root, disk)
            return {"model": re.sub(r"\s+", " ", model), "dev": disk}
        if chip == "drivetemp":
            blocks = os.path.join(dev, "block")
            disk = sorted(os.listdir(blocks))[0] if os.path.isdir(blocks) else None
            if disk:
                return {"model": disk_model(self.root, disk), "dev": disk}
        return None

    @staticmethod
    def chip_kind(chip, dev):
        if chip in CPU_CHIPS:
            return "cpu"
        if chip in GPU_CHIPS or "/drm/" in dev:
            return "gpu"
        if chip in DISK_CHIPS:
            return "disk"
        if chip.startswith(("BAT", "battery")) or "power_supply" in dev:
            return "battery"
        return "board"

    def read(self, now, dt):
        res = []
        seen = {}
        for h, d, chip, dev in self.chips():
            kind = self.chip_kind(chip, dev)
            n = seen[chip] = seen.get(chip, -1) + 1
            cid = _safe(chip) + (str(n) if n else "")
            try:
                files = os.listdir(d)
            except OSError:
                continue
            for f in sorted(files):
                m = re.match(r"(temp|fan|in|power|curr)(\d+)_(input|average)$", f)
                if not m:
                    continue
                pre, idx, _ = m.groups()
                if pre == "power" and m.group(3) == "input" and f"{pre}{idx}_average" in files:
                    continue
                group, unit, scale = self.KINDS[pre]
                v = _num(f"{d}/{f}", scale)
                if v is None:
                    continue
                if group == "temp" and (v == 0 or not GOOD_TEMP[0] <= v <= GOOD_TEMP[1]):
                    continue        # 0 °C is what an unconnected sensor reports
                if group == "fan" and not 0 <= v < 20000:
                    continue
                label = _read(f"{d}/{pre}{idx}_label") or f"{pre}{idx}"
                info = {"chip": chip, "kind": kind, "hwmon": h}
                owner = self.owner(chip, kind, dev)
                if owner:
                    info.update(owner)
                if group == "temp":
                    for lim in ("crit", "max"):
                        lv = _num(f"{d}/temp{idx}_{lim}", 1000.0)
                        if lv and 30 < lv < GOOD_TEMP[1]:
                            info[lim] = lv
                    if dev:
                        info["device"] = dev
                res.append(Reading(f"{group}.{cid}.{_safe(label)}",
                                   round(v, 3 if group in ("volt", "curr") else 2), unit,
                                   f"{info.get('model', chip)} {label}", group, info))
        return res


class ThermalSource(Source):
    name, title = "thermal", "Thermal zones"

    def zones(self):
        base = self.p("/sys/class/thermal")
        try:
            return sorted(z for z in os.listdir(base) if z.startswith("thermal_zone"))
        except OSError:
            return []

    def probe(self):
        if not self.zones():
            return False, "no thermal zones exposed"
        return True, None

    def read(self, now, dt):
        res = []
        for z in self.zones():
            d = self.p(f"/sys/class/thermal/{z}")
            t = _num(f"{d}/temp", 1000.0)
            typ = _read(f"{d}/type") or z
            if t is None or t == 0 or not GOOD_TEMP[0] <= t <= GOOD_TEMP[1]:
                continue
            info = {"zone": z, "type": typ}
            for i in range(8):
                if (_read(f"{d}/trip_point_{i}_type") or "") == "critical":
                    c = _num(f"{d}/trip_point_{i}_temp", 1000.0)
                    if c and 30 < c < GOOD_TEMP[1]:
                        info["crit"] = c
            res.append(Reading(f"zone.{_safe(typ)}.{z[12:]}", round(t, 2), "°C", typ, "zone",
                               info))
        return res


# -- power (RAPL) -------------------------------------------------------------------
class RaplSource(Source):
    name, title = "rapl", "CPU power use (RAPL)"

    def __init__(self, root):
        super().__init__(root)
        self.prev = {}

    def domains(self):
        base = self.p("/sys/class/powercap")
        try:
            names = sorted(n for n in os.listdir(base) if re.match(r"(intel|amd)-rapl:\d", n))
        except OSError:
            return []
        return [(n, f"{base}/{n}") for n in names]

    def probe(self):
        doms = self.domains()
        if not doms:
            return False, "no RAPL power counters (Intel, or AMD on newer kernels)"
        if _read(f"{doms[0][1]}/energy_uj") is None:
            return False, "RAPL counters can't be read (needs root)"
        return True, None

    def read(self, now, dt):
        res = []
        for n, d in self.domains():
            e = _num(f"{d}/energy_uj")
            if e is None:
                continue
            name = _read(f"{d}/name") or n
            prev = self.prev.get(n)
            self.prev[n] = (now, e)
            if prev is None or now <= prev[0]:
                continue
            de = e - prev[1]
            if de < 0:
                de += _num(f"{d}/max_energy_range_uj") or 0
            if de < 0:
                continue
            watts = de / 1e6 / (now - prev[0])
            label = {"package-0": "CPU package", "core": "CPU cores", "uncore": "Integrated GPU",
                     "dram": "RAM", "psys": "Whole system"}.get(name, name)
            res.append(Reading(f"power.rapl.{_safe(n)}", round(watts, 2), "W", label, "power",
                               {"domain": name}))
        return res


# -- GPUs -----------------------------------------------------------------------------
class DrmSource(Source):
    """amdgpu busy %/VRAM, Intel clocks, and every card's identity."""
    name, title = "gpu", "Graphics card (clock, load, memory)"

    def cards(self):
        base = self.p("/sys/class/drm")
        try:
            names = sorted(c for c in os.listdir(base) if re.match(r"card\d+$", c))
        except OSError:
            return []
        out = []
        for c in names:
            d = f"{base}/{c}"
            dev = f"{d}/device"
            vendor = _read(f"{dev}/vendor") or ""
            driver = os.path.basename(os.path.realpath(f"{dev}/driver")) if os.path.exists(
                f"{dev}/driver") else ""
            out.append((c, d, dev, vendor, driver))
        return out

    def probe(self):
        if not self.cards():
            return False, "no graphics card found in /sys/class/drm"
        return True, None

    def read(self, now, dt):
        res = []
        for c, d, dev, vendor, driver in self.cards():
            real = os.path.realpath(dev)
            model = pci_name(os.path.basename(real)) or {"0x8086": "Intel GPU", "0x1002": "AMD GPU",
                                                         "0x10de": "NVIDIA GPU"}.get(vendor, "GPU")
            info = {"card": c, "driver": driver, "vendor": vendor, "device": real, "model": model,
                    "pdev": os.path.basename(real)}
            busy = _num(f"{dev}/gpu_busy_percent")
            if busy is not None:
                res.append(Reading(f"gpu.{c}.busy", busy, "%", f"{model} load", "gpu", dict(info)))
            used = _num(f"{dev}/mem_info_vram_used", 1048576.0)
            total = _num(f"{dev}/mem_info_vram_total", 1048576.0)
            if used is not None and total:
                res.append(Reading(f"gpu.{c}.vram_mb", round(used), "MB", f"{model} memory used",
                                   "gpu", dict(info, total_mb=round(total))))
            for f in ("gt_act_freq_mhz", "gt_cur_freq_mhz"):
                v = _num(f"{d}/{f}")
                if v is not None:
                    mx = _num(f"{d}/gt_max_freq_mhz") or _num(f"{d}/gt_RP0_freq_mhz")
                    res.append(Reading(f"gpu.{c}.freq", v, "MHz", f"{model} clock", "gpu",
                                       dict(info, max=mx)))
                    break
            else:
                sclk = _read(f"{dev}/pp_dpm_sclk")
                if sclk:
                    m = re.search(r"(\d+)Mhz \*", sclk)
                    if m:
                        res.append(Reading(f"gpu.{c}.freq", float(m.group(1)), "MHz",
                                           f"{model} clock", "gpu", dict(info)))
            if not res or res[-1].name.split(".")[1] != c:
                # Nothing to graph, but the card is still worth naming
                res.append(Reading(f"gpu.{c}.present", 1, "", model, "gpu",
                                   dict(info, no_history=True)))
        return res


class FdinfoSource(Source):
    """GPU busy % for any driver that reports engine time per client
    (kernel 5.19+: i915, xe, amdgpu, msm, panfrost, v3d...): the method
    MangoHud uses. Only processes holding /dev/dri are scanned, and that
    list is refreshed once a minute."""
    name, title = "fdinfo", "GPU load (per-process engine time)"

    def __init__(self, root):
        super().__init__(root)
        self.holders = []
        self.holders_at = 0.0
        self.prev = None

    def _find_holders(self):
        out = []
        base = self.p("/proc")
        for pid in os.listdir(base):
            if not pid.isdigit():
                continue
            fd_dir = f"{base}/{pid}/fd"
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                continue
            for fd in fds:
                try:
                    if os.readlink(f"{fd_dir}/{fd}").startswith("/dev/dri/"):
                        out.append((pid, fd))
                except OSError:
                    continue
        self.holders, self.holders_at = out, time.time()

    def _engines(self):
        """{(pdev, client id): {engine: ns}}"""
        clients = {}
        for pid, fd in self.holders:
            text = _read(self.p(f"/proc/{pid}/fdinfo/{fd}"))
            if not text or "drm-client-id" not in text:
                continue
            kv = dict(line.split(":", 1) for line in text.splitlines() if ":" in line)
            cid = (kv.get("drm-pdev", "").strip(), kv.get("drm-client-id", "").strip())
            eng = {}
            for k, v in kv.items():
                if k.startswith("drm-engine-") and not k.startswith("drm-engine-capacity"):
                    parts = v.split()
                    if parts and parts[-1] == "ns":
                        try:
                            eng[k[11:]] = int(parts[0])
                        except ValueError:
                            pass
            if eng:
                clients[cid] = eng
        return clients

    def probe(self):
        self._find_holders()
        if not self._engines():
            return False, ("no GPU engine times in /proc/*/fdinfo (kernel older than 5.19, or "
                           "a driver that doesn't report them)")
        return True, None

    def read(self, now, dt):
        if now - self.holders_at > 60:
            self._find_holders()
        cur = self._engines()
        res = []
        if self.prev and now > self.prev[0]:
            span = (now - self.prev[0]) * 1e9
            per_dev = {}
            for cid, eng in cur.items():
                old = self.prev[1].get(cid, {})
                for e, ns in eng.items():
                    d = ns - old.get(e, ns)
                    if d > 0:
                        per_dev.setdefault(cid[0], {}).setdefault(e, 0)
                        per_dev[cid[0]][e] += d
            for pdev in sorted({c[0] for c in cur}):
                engines = per_dev.get(pdev, {})
                busy = min(100.0, max(engines.values(), default=0) / span * 100)
                key = _safe(pdev) if pdev else "gpu"
                model = pci_name(pdev) or "GPU"
                res.append(Reading(f"gpu.{key}.engine_busy", round(busy, 1), "%",
                                   f"{model} load", "gpu", {"pdev": pdev, "model": model,
                                                            "engines": sorted(engines)}))
        self.prev = (now, cur)
        return res


class NvidiaSource(Source):
    name, title = "nvidia", "NVIDIA graphics (nvidia-smi)"
    QUERY = ("index,name,utilization.gpu,temperature.gpu,power.draw,memory.used,memory.total,"
             "fan.speed,clocks.gr")

    def probe(self):
        if self.root not in ("", "/"):
            return False, "not available in a test tree"
        if not shutil.which("nvidia-smi"):
            return False, "no NVIDIA card or driver (nvidia-smi not found)"
        try:
            self._query()
        except Exception as e:
            return False, f"nvidia-smi didn't work: {e}"
        return True, None

    def _query(self):
        r = subprocess.run(["nvidia-smi", f"--query-gpu={self.QUERY}",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True,
                           timeout=8)
        if r.returncode != 0:
            raise OSError(r.stderr.strip()[:200] or f"exit {r.returncode}")
        return [[x.strip() for x in line.split(",")] for line in r.stdout.splitlines() if line]

    def read(self, now, dt):
        res = []
        for row in self._query():
            idx, name = row[0], row[1]
            info = {"model": name, "vendor": "0x10de"}

            def num(i):
                try:
                    return float(row[i])
                except (ValueError, IndexError):
                    return None
            short = name.replace("NVIDIA ", "")
            for i, key, unit, label in ((2, "busy", "%", f"{short} load"),
                                        (3, "temp", "°C", f"{short} temperature"),
                                        (4, "power", "W", f"{short} power"),
                                        (5, "vram_mb", "MB", f"{short} memory used"),
                                        (7, "fan", "%", f"{short} fan"),
                                        (8, "freq", "MHz", f"{short} clock")):
                v = num(i)
                if v is not None:
                    extra = {"total_mb": num(6)} if key == "vram_mb" else {}
                    res.append(Reading(f"gpu.nv{idx}.{key}", v, unit, label,
                                       "gpu" if key != "temp" else "temp",
                                       dict(info, kind="gpu", **extra)))
        return res


# -- battery -----------------------------------------------------------------------------
class BatterySource(Source):
    name, title = "battery", "Battery"

    def batteries(self):
        base = self.p("/sys/class/power_supply")
        try:
            return [(b, f"{base}/{b}") for b in sorted(os.listdir(base))
                    if _read(f"{base}/{b}/type") == "Battery"]
        except OSError:
            return []

    def probe(self):
        if not self.batteries():
            return False, "no battery (not a handheld or laptop)"
        return True, None

    # Controllers by USB/Bluetooth id (vendor:product); the kernel's own names
    # are often generic ("Wireless Controller" for every PlayStation pad)
    PAD_NAMES = {"054c:05c4": "DualShock 4", "054c:09cc": "DualShock 4", "054c:0ba0": "DualShock 4",
                 "054c:0ce6": "DualSense", "054c:0df2": "DualSense Edge",
                 "057e:2009": "Switch Pro Controller", "057e:2006": "Joy-Con (L)",
                 "057e:2007": "Joy-Con (R)", "28de:1102": "Steam Controller",
                 "28de:1205": "Steam Deck"}

    def pad_name(self, d):
        """Who a controller battery belongs to: a pad streamed in through
        Sunshine (Moonlight), or the pad's own name."""
        ue = {}
        for line in (_read(f"{d}/device/uevent") or "").splitlines():
            k, _, v = line.partition("=")
            ue[k] = v
        # Sunshine makes its pads with libvirtualhid (older versions: inputtino)
        phys = ue.get("HID_PHYS", "").lower()
        if any(x in phys for x in ("libvirtualhid", "inputtino", "sunshine")):
            return "Moonlight controller", True
        m = re.match(r"[0-9a-f]+:0*([0-9a-f]{4}):0*([0-9a-f]{4})", ue.get("HID_ID", "").lower())
        if m and f"{m[1]}:{m[2]}" in self.PAD_NAMES:
            return self.PAD_NAMES[f"{m[1]}:{m[2]}"], False
        if m and m[1] == "045e":
            return "Xbox controller", False
        name = _read(f"{d}/model_name") or ue.get("HID_NAME") or ""
        return (name if name and name != "Wireless Controller" else "Controller"), False

    def read(self, now, dt):
        res = []
        names = {}
        for b, d in self.batteries():
            # scope=Device: a controller's or other gadget's own battery, not the box's
            if _read(f"{d}/scope") == "Device":
                names[b] = self.pad_name(d)
        # two pads with the same name are numbered: Moonlight controller 1, 2
        count, seen = {}, {}
        for n, _ in names.values():
            count[n] = count.get(n, 0) + 1
        for b, d in self.batteries():
            status = _read(f"{d}/status") or "Unknown"
            cap = _num(f"{d}/capacity")
            device = b in names
            label = "Battery"
            if device:
                n, streamed = names[b]
                seen[n] = seen.get(n, 0) + 1
                label = f"{n} {seen[n]}" if count[n] > 1 else n
            if cap is not None:
                res.append(Reading(f"battery.{_safe(b)}.capacity", cap, "%", label, "battery",
                                   {"status": status, "device": device,
                                    "streamed": device and names[b][1]}))
            p = _num(f"{d}/power_now", 1e6)
            if p is None:
                i, v = _num(f"{d}/current_now", 1e6), _num(f"{d}/voltage_now", 1e6)
                p = i * v if i is not None and v is not None else None
            if p is not None and not device:
                res.append(Reading(f"battery.{_safe(b)}.power", round(abs(p), 2), "W",
                                   "Battery draw" if status == "Discharging" else "Battery charge",
                                   "battery", {"status": status}))
        return res


# -- disks ----------------------------------------------------------------------------------
def whole_disks(root):
    base = root + "/sys/block"
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    return [n for n in names if not n.startswith(("loop", "ram", "zram", "dm-", "md", "sr",
                                                  "fd", "nbd", "mtdblock"))]


class DiskSpaceSource(Source):
    name, title = "disk", "Disk space"

    def mounts(self):
        """One entry per writable drive. A drive mounted twice (Batocera
        mounts the data partition at /var/batocerafs and /userdata) is named
        by /userdata when it's one of them."""
        by_dev = {}
        for line in (_read(self.p("/proc/mounts")) or "").splitlines():
            p = line.split()
            if len(p) < 4:
                continue
            dev, mnt, fs, opts = p[0], p[1].replace("\\040", " "), p[2], p[3].split(",")
            if fs not in FS_TYPES or not dev.startswith("/dev/") or "ro" in opts:
                continue
            if mnt.startswith("/boot"):
                continue
            if dev not in by_dev or mnt == "/userdata":
                by_dev[dev] = (dev, mnt, fs)
        return list(by_dev.values())

    def probe(self):
        if not self.mounts():
            return False, "no writable disks mounted"
        return True, None

    def read(self, now, dt):
        res = []
        for dev, mnt, fs in self.mounts():
            try:
                st = os.statvfs(self.p(mnt) if self.root else mnt)
            except OSError:
                continue
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            if total <= 0:
                continue
            key = {"/userdata": "userdata", "/": "root"}.get(mnt) or _safe(mnt)
            disk = parent_disk(self.root, dev)
            info = {"mount": mnt, "device": dev, "fs": fs, "total_gb": round(total / 1e9, 1),
                    "dev": disk, "model": disk_model(self.root, disk),
                    "free_gb": round(free / 1e9, 2), "free_pct": round(100 * free / total, 1)}
            res.append(Reading(f"disk.{key}.used_pct", round(100 * (1 - free / total), 1), "%",
                               f"{mnt} used", "disk", info))
        return res


class DiskIoSource(Source):
    name, title = "diskio", "Disk activity"

    def __init__(self, root):
        super().__init__(root)
        self.prev = None

    def probe(self):
        if _read(self.p("/proc/diskstats")) is None:
            return False, "/proc/diskstats can't be read"
        return True, None

    def read(self, now, dt):
        disks = set(whole_disks(self.root))
        cur = {}
        for line in (_read(self.p("/proc/diskstats")) or "").splitlines():
            p = line.split()
            if len(p) >= 10 and p[2] in disks:
                cur[p[2]] = (int(p[5]) * 512, int(p[9]) * 512)
        res = []
        if self.prev and now > self.prev[0]:
            span = now - self.prev[0]
            for d, (rd, wr) in cur.items():
                prd, pwr = self.prev[1].get(d, (rd, wr))
                model = disk_model(self.root, d)
                info = {"dev": d, "model": model}
                res.append(Reading(f"diskio.{d}.read", round(max(0, rd - prd) / span), "B/s",
                                   f"{model} read", "diskio", dict(info)))
                res.append(Reading(f"diskio.{d}.write", round(max(0, wr - pwr) / span), "B/s",
                                   f"{model} write", "diskio", dict(info)))
        self.prev = (now, cur)
        return res


class SmartSource(Source):
    """Disk health with smartctl, every SMART_INTERVAL seconds, in its own
    thread so a slow drive never delays the other readings. Batocera has
    shipped smartctl since version 33 (2022)."""
    name, title = "smart", "Disk health (smartctl)"

    def __init__(self, root, interval):
        super().__init__(root)
        self.interval = interval
        self.results = {}
        self.ran_at = 0.0
        self.running = False

    def probe(self):
        if self.interval <= 0:
            return False, "turned off (SMART_INTERVAL=0)"
        if not shutil.which("smartctl"):
            return False, "Disk health needs Batocera 33 or newer (smartctl not found)"
        if self.root not in ("", "/"):
            return False, "not available in a test tree"
        return True, None

    def _check(self, dev):
        r = subprocess.run(["smartctl", "-j", "-H", "-A", "-i", f"/dev/{dev}"],
                           capture_output=True, text=True, timeout=30)
        try:
            j = json.loads(r.stdout)
        except ValueError:
            return None
        if not j.get("smart_status") and not j.get("temperature") and \
                not j.get("nvme_smart_health_information_log"):
            return None            # USB bridge or card reader without SMART
        out = {"model": j.get("model_name") or j.get("model_family") or dev,
               "passed": (j.get("smart_status") or {}).get("passed"),
               "temp": (j.get("temperature") or {}).get("current"),
               "poh": (j.get("power_on_time") or {}).get("hours")}
        tl = j.get("temperature") or {}
        for k in ("op_limit_max", "limit_max", "drive_trip"):
            if tl.get(k):
                out["temp_limit"] = tl[k]
                break
        nv = j.get("nvme_smart_health_information_log")
        if nv:
            out["wear_pct"] = nv.get("percentage_used")
            out["written_gb"] = round((nv.get("data_units_written") or 0) * 512000 / 1e9, 1)
            out["critical_warning"] = nv.get("critical_warning") or 0
            wct = j.get("nvme_warning_temperature") or (j.get("temperature") or {}).get("op_limit_max")
            if wct and "temp_limit" not in out:
                out["temp_limit"] = wct
        attrs = {a.get("id"): a for a in (j.get("ata_smart_attributes") or {}).get("table", [])}
        if attrs:
            def raw(i):
                return ((attrs.get(i) or {}).get("raw") or {}).get("value")
            out["reallocated"] = raw(5)
            out["pending"] = raw(197)
            for i in (231, 233, 177, 169, 202):
                a = attrs.get(i)
                if a and a.get("value") is not None:
                    out["wear_pct"] = max(0, 100 - a["value"])
                    break
        return out

    def _run(self):
        res = {}
        for dev in whole_disks(""):
            if dev.startswith("mmcblk"):
                continue          # SD cards have no SMART
            try:
                r = self._check(dev)
            except (OSError, subprocess.SubprocessError) as e:
                LOG.warning(f"smartctl /dev/{dev}: {e}")
                continue
            if r:
                res[dev] = r
                if r.get("model") and r["model"] != dev:
                    DISK_MODELS[dev] = r["model"]
        self.results, self.running = res, False

    def read(self, now, dt):
        if not self.running and now - self.ran_at >= self.interval:
            self.ran_at, self.running = now, True
            threading.Thread(target=self._run, daemon=True, name="smartctl").start()
        out = []
        for dev, r in self.results.items():
            info = dict(r, dev=dev)
            model = r.get("model") or dev
            if r.get("temp") is not None:
                out.append(Reading(f"smart.{dev}.temp", r["temp"], "°C", f"{model} temperature",
                                   "temp", dict(info, kind="disk")))
            if r.get("wear_pct") is not None:
                out.append(Reading(f"smart.{dev}.wear", r["wear_pct"], "%", f"{model} wear",
                                   "smart", info))
            out.append(Reading(f"smart.{dev}.health", 1 if r.get("passed") is not False else 0,
                               "", f"{model} health", "smart", dict(info, no_history=True)))
        return out


# -- network ------------------------------------------------------------------------------
class NetSource(Source):
    name, title = "net", "Network traffic"

    def __init__(self, root):
        super().__init__(root)
        self.prev = None

    def probe(self):
        if _read(self.p("/proc/net/dev")) is None:
            return False, "/proc/net/dev can't be read"
        return True, None

    def read(self, now, dt):
        cur = {}
        for line in (_read(self.p("/proc/net/dev")) or "").splitlines()[2:]:
            name, _, rest = line.partition(":")
            name, p = name.strip(), rest.split()
            if name == "lo" or name.startswith(("veth", "docker", "br-")) or len(p) < 9:
                continue
            cur[name] = (int(p[0]), int(p[8]))
        res = []
        if self.prev and now > self.prev[0]:
            span = now - self.prev[0]
            for n, (rx, tx) in cur.items():
                prx, ptx = self.prev[1].get(n, (rx, tx))
                res.append(Reading(f"net.{_safe(n)}.rx", round(max(0, rx - prx) / span), "B/s",
                                   f"{n} download", "net"))
                res.append(Reading(f"net.{_safe(n)}.tx", round(max(0, tx - ptx) / span), "B/s",
                                   f"{n} upload", "net"))
        self.prev = (now, cur)
        return res


# -- what's running in EmulationStation -------------------------------------------------
class GameSource(Source):
    name, title = "game", "Running game (EmulationStation)"

    def probe(self):
        if self.root not in ("", "/"):
            return False, "not available in a test tree"
        try:
            self._query()
        except OSError as e:
            return False, f"EmulationStation's API isn't answering ({e})"
        return True, None

    def _query(self):
        c = http.client.HTTPConnection("127.0.0.1", 1234, timeout=2)
        try:
            c.request("GET", "/runningGame")
            r = c.getresponse()
            body = r.read()
        except (http.client.HTTPException, ValueError) as e:
            raise OSError(str(e)) from None
        finally:
            c.close()
        if r.status != 200:
            return None
        try:
            j = json.loads(body)
        except ValueError:
            return None
        if not isinstance(j, dict) or not j.get("name"):
            return None
        return {"name": j.get("name"), "system": j.get("systemName") or "",
                "path": j.get("path") or ""}

    def read(self, now, dt):
        try:
            g = self._query()
        except OSError:
            g = None
        return [Reading("game.running", 1 if g else 0, "", "Game running", "game",
                        dict(g or {}, no_history=True))]


# -- BatoWatch itself ---------------------------------------------------------------------
class SelfSource(Source):
    name, title = "self", "BatoWatch itself"

    def __init__(self, root, extra=None):
        super().__init__(root)
        self.prev = None
        self.day = None
        self.day_written = 0
        self.extra = extra or (lambda: {})

    def read(self, now, dt):
        st = (_read("/proc/self/stat") or "").rsplit(")", 1)[-1].split()
        ticks = int(st[11]) + int(st[12]) if len(st) > 12 else 0
        hz = os.sysconf("SC_CLK_TCK")
        rss = 0
        for line in (_read("/proc/self/status") or "").splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]) / 1024
        wb = 0
        for line in (_read("/proc/self/io") or "").splitlines():
            if line.startswith("write_bytes:"):
                wb = int(line.split()[1])
        res = [Reading("self.rss_mb", round(rss, 1), "MB", "BatoWatch memory", "self")]
        if self.prev and now > self.prev[0]:
            cpu = (ticks - self.prev[1]) / hz / (now - self.prev[0]) * 100
            res.append(Reading("self.cpu", round(cpu, 2), "%", "BatoWatch CPU (one core)",
                               "self"))
            today = time.strftime("%Y-%m-%d")
            if today != self.day:
                self.day, self.day_written = today, 0
            self.day_written += max(0, wb - self.prev[2])
        self.prev = (now, ticks, wb)
        extra = self.extra()
        res.append(Reading("self.written_today_mb", round(self.day_written / 1048576, 2), "MB",
                           "BatoWatch disk writes today", "self", {"no_history": True}))
        if extra.get("db_bytes") is not None:
            res.append(Reading("self.db_mb", round(extra["db_bytes"] / 1048576, 2), "MB",
                               "History size", "self"))
        if extra.get("connections") is not None:
            res.append(Reading("self.connections", extra["connections"], "",
                               "Dashboard connections", "self", {"no_history": True}))
        return res


# -- the sampler ----------------------------------------------------------------------------
class Monitor:
    FAIL_LIMIT = 3
    RETRY = 600

    def __init__(self, root, skip, smart_interval, self_extra=None):
        root = "" if root in ("", "/") else root
        self.root = root
        all_sources = [CpuSource(root), MemSource(root), HwmonSource(root), ThermalSource(root),
                       RaplSource(root), DrmSource(root), FdinfoSource(root), NvidiaSource(root),
                       BatterySource(root), DiskSpaceSource(root), DiskIoSource(root),
                       SmartSource(root, smart_interval), NetSource(root), GameSource(root),
                       SelfSource(root, self_extra)]
        self.sources, self.missing, self.failed = [], {}, {}
        self.fails, self.retry_at = {}, {}
        for s in all_sources:
            if s.name in skip:
                self.missing[s.name] = {"title": s.title, "reason": "turned off (HW_SKIP)"}
                continue
            self._probe(s)
        self.all = all_sources
        self.skip = skip
        self.last = {}
        self.last_t = None

    def _probe(self, s):
        try:
            ok, why = s.probe()
        except Exception as e:      # a probe must never stop BatoWatch
            ok, why = False, f"check failed: {e}"
        if ok:
            self.sources.append(s)
            self.missing.pop(s.name, None)
        else:
            self.missing[s.name] = {"title": s.title, "reason": why}
        return ok

    def sample(self, now):
        dt = now - self.last_t if self.last_t else None
        self.last_t = now
        readings = {}
        for s in list(self.sources):
            try:
                for r in s.read(now, dt):
                    readings[r.name] = r
                self.fails[s.name] = 0
            except Exception as e:
                n = self.fails.get(s.name, 0) + 1
                self.fails[s.name] = n
                if n == 1:
                    LOG.warning(f"hardware: {s.title}: {e}")
                if n >= self.FAIL_LIMIT:
                    self.sources.remove(s)
                    self.failed[s.name] = {"title": s.title, "reason": f"stopped after errors: {e}"}
                    self.retry_at[s.name] = now + self.RETRY
                    LOG.warning(f"hardware: {s.title} switched off after {n} errors; "
                                f"trying again in 10 minutes")
        for name, at in list(self.retry_at.items()):
            if now >= at:
                s = next(x for x in self.all if x.name == name)
                del self.retry_at[name]
                self.fails[name] = 0
                if self._probe(s):
                    self.failed.pop(name, None)
                else:
                    self.retry_at[name] = now + self.RETRY
        # Disks plugged in later, and smartctl/ES coming up after BatoWatch
        if int(now) % 300 < (dt or 10):
            for s in self.all:
                if s.name in self.missing and s.name not in self.skip and s.name not in \
                        self.failed and s.name in ("disk", "smart", "game", "battery", "fdinfo"):
                    self._probe(s)
        self._dedupe(readings)
        self.last = readings
        return readings

    @staticmethod
    def _dedupe(readings):
        """Thermal zones that repeat an hwmon chip (acpitz, iwlwifi...), and
        fdinfo GPU load for a card that reports its own busy %, are dropped."""
        chips = {r.info.get("chip") for r in readings.values() if r.group == "temp"}
        cpu_hwmon = any(r.info.get("kind") == "cpu" for r in readings.values() if r.group == "temp")
        cards_busy = {os.path.basename(r.info.get("device", "")) for r in readings.values()
                      if r.name.endswith(".busy") and r.info.get("device")}
        for name, r in list(readings.items()):
            if r.group == "zone" and (r.info.get("type") in chips or (cpu_hwmon and any(
                    k in r.info.get("type", "").lower() for k in ("x86_pkg", "cpu", "soc", "package")))):
                del readings[name]
            elif name.endswith(".engine_busy") and r.info.get("pdev") in cards_busy:
                del readings[name]

    def not_applicable(self):
        """Missing sources that just don't apply here, so listing them would
        only confuse: NVIDIA's tool without an NVIDIA card, a battery on a
        desktop. (AMD and Intel graphics are read by the general GPU source.)"""
        out = {"battery"}
        vendors = {(_read(f"{self.root}/sys/class/drm/{c}/device/vendor") or "")
                   for c in (os.listdir(f"{self.root}/sys/class/drm")
                             if os.path.isdir(f"{self.root}/sys/class/drm") else [])}
        if "0x10de" not in vendors:
            out.add("nvidia")
        if any(s.name == "gpu" for s in self.sources):
            out.add("fdinfo")      # the card reports its own load already, or has none to give
        return out

    def sources_report(self):
        skip = self.not_applicable()
        return {"available": [{"name": s.name, "title": s.title} for s in self.sources],
                "missing": [dict(v, name=k) for k, v in sorted(self.missing.items())
                            if k not in skip or v.get("reason", "").startswith("turned off")] +
                           [dict(v, name=k) for k, v in sorted(self.failed.items())]}


def main_cpu_temp(readings):
    """The one CPU temperature for the summary: coretemp's package,
    k10temp's Tdie/Tctl, else the hottest CPU sensor, else a CPU thermal zone."""
    cands = [r for r in readings.values() if r.group == "temp" and r.info.get("kind") == "cpu"]
    for want in ("package id 0", "tdie", "tctl"):
        for r in cands:
            if r.label.lower().endswith(want):
                return r
    if cands:
        return max(cands, key=lambda r: r.value)
    zones = [r for r in readings.values() if r.group == "zone" and
             any(k in r.label.lower() for k in ("cpu", "x86_pkg", "soc", "package"))]
    return max(zones, key=lambda r: r.value) if zones else None


def main_gpu_temp(readings):
    cands = [r for r in readings.values() if r.group == "temp" and r.info.get("kind") == "gpu"]
    for want in ("edge", "junction"):
        for r in cands:
            if r.label.lower().endswith(want):
                return r
    return max(cands, key=lambda r: r.value) if cands else None
