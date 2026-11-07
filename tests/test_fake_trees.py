"""BatoWatch hardware tests with fake /sys and /proc trees, history aging with a
fake clock, a damaged database, and a fake nvidia-smi.

  python test_fake_trees.py <path to batowatch/>
"""
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import time

SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, SRC)
import hardware          # noqa: E402
from history import History   # noqa: E402

FAILS = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def w(root, path, text):
    p = os.path.join(root, path.lstrip("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(str(text) + "\n")


def proc_basics(root, cpus=4):
    lines = ["cpu  1000 0 500 8000 100 0 10 0 0 0"]
    lines += [f"cpu{i} 250 0 125 2000 25 0 2 0 0 0" for i in range(cpus)]
    w(root, "/proc/stat", "\n".join(lines))
    w(root, "/proc/meminfo", "MemTotal: 16000000 kB\nMemFree: 1000000 kB\nMemAvailable: 400000 kB\n"
                             "SwapTotal: 0 kB\nSwapFree: 0 kB")
    w(root, "/proc/loadavg", "0.50 0.40 0.30 1/100 123")
    w(root, "/proc/uptime", "1234.5 4000.0")
    w(root, "/proc/diskstats", "   8       0 sda 100 0 2000 0 50 0 4000 0 0 0 0")
    w(root, "/proc/net/dev", "Inter-|\n face |\n    lo: 1 0 0 0 0 0 0 0 1 0 0 0 0 0 0 0\n"
                             "  eth0: 1000 10 0 0 0 0 0 0 500 5 0 0 0 0 0 0")
    w(root, "/proc/mounts", "")
    os.makedirs(os.path.join(root, "sys/block/sda"), exist_ok=True)
    for i in range(cpus):
        w(root, f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq", 2000000 + i * 100000)


def sample_twice(root, skip=()):
    m = hardware.Monitor(root, set(skip) | {"smart", "nvidia", "game"}, 0)
    m.sample(time.time())
    # move counters so rates have something to show
    proc_stat = os.path.join(root, "proc/stat")
    txt = open(proc_stat).read().replace("1000 0 500 8000", "1500 0 700 8300").replace(
        "250 0 125 2000", "300 0 150 2100")
    open(proc_stat, "w").write(txt)
    time.sleep(0.05)
    return m, m.sample(time.time() + 10)


print("== AMD desktop (k10temp, amdgpu, fans, nct6775) ==")
root = tempfile.mkdtemp()
proc_basics(root, 8)
w(root, "/sys/class/hwmon/hwmon0/name", "k10temp")
w(root, "/sys/class/hwmon/hwmon0/temp1_input", 61500)
w(root, "/sys/class/hwmon/hwmon0/temp1_label", "Tctl")
w(root, "/sys/class/hwmon/hwmon0/temp3_input", 58000)
w(root, "/sys/class/hwmon/hwmon0/temp3_label", "Tdie")
w(root, "/sys/class/hwmon/hwmon1/name", "amdgpu")
w(root, "/sys/class/hwmon/hwmon1/temp1_input", 55000)
w(root, "/sys/class/hwmon/hwmon1/temp1_label", "edge")
w(root, "/sys/class/hwmon/hwmon1/temp1_crit", 100000)
w(root, "/sys/class/hwmon/hwmon1/fan1_input", 1200)
w(root, "/sys/class/hwmon/hwmon1/power1_average", 45000000)
w(root, "/sys/class/hwmon/hwmon2/name", "nct6775")
w(root, "/sys/class/hwmon/hwmon2/fan2_input", 65535)
w(root, "/sys/class/hwmon/hwmon2/fan1_input", 900)
w(root, "/sys/class/hwmon/hwmon2/in0_input", 1104)
w(root, "/sys/class/hwmon/hwmon2/temp1_input", 0)
w(root, "/sys/class/drm/card0/device/vendor", "0x1002")
w(root, "/sys/class/drm/card0/device/gpu_busy_percent", 37)
w(root, "/sys/class/drm/card0/device/mem_info_vram_used", 1073741824)
w(root, "/sys/class/drm/card0/device/mem_info_vram_total", 8589934592)
w(root, "/sys/class/drm/card0/device/pp_dpm_sclk", "0: 500Mhz\n1: 1800Mhz *")
m, r = sample_twice(root)
check("cpu.usage" in r and 0 < r["cpu.usage"].value <= 100, f"CPU usage {r.get('cpu.usage') and r['cpu.usage'].value}")
check(len([k for k in r if k.startswith("cpu.core")]) == 8, "8 cores")
check(hardware.main_cpu_temp(r).label.endswith("Tdie"), "main CPU temp is k10temp Tdie")
check(hardware.main_gpu_temp(r).value == 55.0, "main GPU temp is amdgpu edge")
check(r["temp.amdgpu.edge"].info.get("crit") == 100.0, "amdgpu crit limit read")
check("fan.nct6775.fan1" in r and "fan.nct6775.fan2" not in r, "fan 65535 dropped, 900 kept")
check("temp.nct6775.temp1" not in r, "0 °C sensor dropped")
check(r["gpu.card0.busy"].value == 37 and r["gpu.card0.vram_mb"].value == 1024, "amdgpu busy + VRAM")
check(r["gpu.card0.freq"].value == 1800, "amdgpu clock from pp_dpm_sclk")
check(r["power.amdgpu.power1"].value == 45.0, "amdgpu power from power1_average")
check(abs(r["volt.nct6775.in0"].value - 1.104) < 1e-6, "voltage in V")
check(r["mem.used_pct"].info["avail_pct"] == 2.5, "memory available 2.5%")
check(any(x["name"] == "rapl" for x in m.sources_report()["missing"]), "RAPL listed as missing")
shutil.rmtree(root)

print("== Handheld (battery, fan, thermal zones only for CPU) ==")
root = tempfile.mkdtemp()
proc_basics(root, 4)
w(root, "/sys/class/thermal/thermal_zone0/type", "cpu-thermal")
w(root, "/sys/class/thermal/thermal_zone0/temp", 47000)
w(root, "/sys/class/thermal/thermal_zone0/trip_point_0_type", "critical")
w(root, "/sys/class/thermal/thermal_zone0/trip_point_0_temp", 105000)
w(root, "/sys/class/hwmon/hwmon0/name", "steamdeck_hwmon")
w(root, "/sys/class/hwmon/hwmon0/fan1_input", 3100)
w(root, "/sys/class/power_supply/BAT1/type", "Battery")
w(root, "/sys/class/power_supply/BAT1/capacity", 64)
w(root, "/sys/class/power_supply/BAT1/status", "Discharging")
w(root, "/sys/class/power_supply/BAT1/power_now", 12500000)
w(root, "/sys/class/power_supply/ps-controller-battery-aa/type", "Battery")
w(root, "/sys/class/power_supply/ps-controller-battery-aa/scope", "Device")
w(root, "/sys/class/power_supply/ps-controller-battery-aa/capacity", 80)
m, r = sample_twice(root)
t = hardware.main_cpu_temp(r)
check(t is not None and t.value == 47.0 and t.info.get("crit") == 105.0, "CPU temp + crit from a thermal zone")
check(r["battery.bat1.capacity"].value == 64 and r["battery.bat1.power"].value == 12.5, "battery % and W")
check(r["battery.bat1.power"].label == "Battery draw", "discharging = draw")
c = r["battery.ps-controller-battery-aa.capacity"]
check(c.label == "Controller" and "battery.ps-controller-battery-aa.power" not in r,
      "controller battery labelled, no power reading")
w(root, "/sys/class/power_supply/ps-controller-battery-aa/device/uevent",
  "HID_ID=0005:0000054C:00000CE6\nHID_PHYS=libvirtualhid/uhid/054c:0ce6/9\n")
w(root, "/sys/class/power_supply/ps-controller-battery-bb/type", "Battery")
w(root, "/sys/class/power_supply/ps-controller-battery-bb/scope", "Device")
w(root, "/sys/class/power_supply/ps-controller-battery-bb/capacity", 40)
w(root, "/sys/class/power_supply/ps-controller-battery-bb/device/uevent",
  "HID_ID=0005:0000054C:00000CE6\nHID_PHYS=aa:bb:cc\nHID_NAME=Wireless Controller\n")
m, r = sample_twice(root)
check(r["battery.ps-controller-battery-aa.capacity"].label == "Moonlight controller" and
      r["battery.ps-controller-battery-bb.capacity"].label == "DualSense",
      "Sunshine pad named Moonlight controller, a real pad by its model")
check("fan.steamdeck_hwmon.fan1" in r, "handheld fan")
shutil.rmtree(root)

print("== Nothing but /proc ==")
root = tempfile.mkdtemp()
proc_basics(root, 2)
m, r = sample_twice(root)
miss = {x["name"]: x["reason"] for x in m.sources_report()["missing"]}
check({"hwmon", "thermal", "rapl", "gpu"} <= set(miss), f"missing: {sorted(miss)}")
check("battery" not in miss, "no battery isn't listed as missing (doesn't apply)")
check(hardware.main_cpu_temp(r) is None, "no CPU temperature, no crash")
check("cpu.usage" in r and "mem.used_pct" in r and "diskio.sda.write" in r and "net.eth0.rx" in r,
      "cpu, memory, disk activity, network still work")
shutil.rmtree(root)

print("== A source that breaks is switched off, others keep going ==")
root = tempfile.mkdtemp()
proc_basics(root, 2)
m = hardware.Monitor(root, {"smart", "nvidia", "game"}, 0)
bad = next(s for s in m.sources if s.name == "net")
bad.read = lambda now, dt: (_ for _ in ()).throw(OSError("boom"))
for i in range(3):
    with open(os.path.join(root, "proc/stat")) as fh:
        txt = fh.read()
    with open(os.path.join(root, "proc/stat"), "w") as fh:
        fh.write(txt.replace("cpu  ", "cpu  1").replace(" 8000 ", " 9000 ", 1))
    r = m.sample(time.time() + i)
check(all(s.name != "net" for s in m.sources) and "net" in m.failed, "net switched off after 3 errors")
check("cpu.usage" in r, "cpu still sampled")
shutil.rmtree(root)

print("== fake nvidia-smi ==")
bindir = tempfile.mkdtemp()
fake = os.path.join(bindir, "nvidia-smi")
with open(fake, "w") as fh:
    fh.write("#!/bin/sh\necho '0, NVIDIA GeForce RTX 3060, 42, 63, 88.50, 2048, 12288, 35, 1800'\n")
os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
os.environ["PATH"] = bindir + os.pathsep + os.environ["PATH"]
src = hardware.NvidiaSource("")
ok, why = src.probe()
check(ok, f"nvidia probe ({why})")
rr = {x.name: x for x in src.read(time.time(), None)}
check(rr["gpu.nv0.busy"].value == 42 and rr["gpu.nv0.temp"].value == 63 and rr["gpu.nv0.power"].value == 88.5,
      "nvidia busy/temp/power")
check(hardware.main_gpu_temp(rr).value == 63, "nvidia temp is the main GPU temp")
shutil.rmtree(bindir)

print("== history: tiers age out (fake clock) ==")
d = tempfile.mkdtemp()
keep = {"raw": 3600, "minute": 4 * 3600, "hour": 3 * 86400}
h = History(os.path.join(d, "h.db"), keep)
t0 = 1_800_000_000
for i in range(0, 5 * 3600, 10):
    h.add(t0 + i, {"x": i % 100})
    if i % 60 == 0:
        h.flush(t0 + i)
h.db.execute("DELETE FROM meta WHERE k='pruned'")
h.flush(t0 + 5 * 3600 + 700)
oldest = {t: h.db.execute(f"SELECT MIN(t) FROM {t}").fetchone()[0] for t in ("minute", "hour")}
oldest["raw"] = h.recent[0][0]
now = t0 + 5 * 3600 + 700
check(h.db.execute("SELECT COUNT(*) FROM raw").fetchone()[0] == 0, "full detail stays in memory (no raw writes)")
check(h.db.execute("PRAGMA page_size").fetchone()[0] == 1024, "small pages")
check(now - oldest["raw"] <= 3600 + 10, f"raw kept ≤ 1 h ({now - oldest['raw']} s)")
check(now - oldest["minute"] <= 4 * 3600 + 60, f"minute kept ≤ 4 h ({now - oldest['minute']} s)")
check(oldest["hour"] is not None, "hour tier has rows")
s1 = h.series(["x"], "1h", now)["x"]
s24 = h.series(["x"], "24h", now)["x"]
check(10 < len(s1) <= 520 and 0 < len(s24) <= 520, f"graph points: 1h={len(s1)}, 24h={len(s24)}")
ev = h.event_start("alert", now, "cpu_temp", "CPU too hot · 95 °C", 95)
h.close_open_events(now + 5)
check(h.events(["alert"], now - 10)[0]["t1"] == now + 5, "open events closed after a crash")
n_recent = len(h.recent)
h.close()
h = History(os.path.join(d, "h.db"), keep)
check(len(h.recent) == n_recent and len(h.series(["x"], "1h", now)["x"]) > 10,
      "full detail saved on a clean stop and read back")
check(h.events(["alert"], now - 10), "events saved")
h.close()
print("== history: an old 4 KB-page database is rebuilt with small pages ==")
p4 = os.path.join(d, "old.db")
c = sqlite3.connect(p4); c.execute("PRAGMA page_size=4096")
c.executescript(open(os.path.join(SRC, "history.py")).read().split('SCHEMA = """')[1].split('"""')[0])
c.execute("INSERT INTO metrics (name) VALUES ('x')"); c.execute("INSERT INTO layouts (ids) VALUES ('1')")
c.execute("INSERT INTO raw VALUES (?, 1, ?)", (int(time.time()) - 60, bytes(4))); c.commit(); c.close()
h4 = History(p4, keep)
check(h4.db.execute("PRAGMA page_size").fetchone()[0] == 1024 and len(h4.recent) == 1,
      "old database converted, its detail kept")
h4.close()

print("== history: damaged database is set aside ==")
p = os.path.join(d, "broken.db")
with open(p, "wb") as fh:
    fh.write(b"this is not a database" * 100)
h2 = History(p, keep)
h2.add(time.time(), {"y": 1})
h2.flush()
check(os.path.exists(os.path.join(d, "broken.broken")), "damaged file kept as .broken")
n2 = len(h2.recent)
h2.close()                       # BatoWatch holds its databases exclusively while open
check(n2 == 1 and
      sqlite3.connect(p).execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 1, "new database works")
h2.close()
shutil.rmtree(d)

print()

print("== history: a locked database is waited for, never set aside ==")
import threading as _th
d3 = tempfile.mkdtemp()
p3 = os.path.join(d3, "h.db")
ha = History(p3, keep)
ha.add(time.time(), {"z": 1})
ha.flush()
_th.Timer(3, ha.close).start()            # the old instance lets go after 3 s
t_start = time.time()
hb = History(p3, keep)                    # waits instead of starting over
check(not os.path.exists(os.path.join(d3, "h.broken")) and len(hb.recent) == 1
      and time.time() - t_start >= 2.5, "waited for the lock, history kept")
print("== history: a failed save keeps the readings and the ids ==")
real = hb._roll
def boom(now):
    raise sqlite3.OperationalError("database or disk is full")
hb._roll = boom
hb.add(time.time() + 10, {"z": 2, "new_metric": 5})
hb.flush()
hb._roll = real
hb.add(time.time() + 20, {"z": 3, "new_metric": 6})
hb.flush()
ids_in_file = {n for (n,) in hb.db.execute("SELECT name FROM metrics")}
check(not os.path.exists(os.path.join(d3, "h.broken")) and len(hb.recent) == 3
      and "new_metric" in ids_in_file, "disk error: nothing set aside, ids saved")
hb.close()

print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
