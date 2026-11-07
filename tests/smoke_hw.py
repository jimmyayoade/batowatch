import sys, time, tempfile, os, json
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, SRC)
import hardware, alerts, settings
from history import History
m = hardware.Monitor("/", set(), 0)
r1 = m.sample(time.time()); time.sleep(2); r = m.sample(time.time())
print("available:", [s.name for s in m.sources])
for x in m.sources_report()["missing"]: print("missing:", x["name"], "-", x["reason"])
for k in sorted(r)[:200]:
    v = r[k]; print(f"  {k:45} {v.value} {v.unit}  [{v.group}] {v.label}")
print("cpu temp:", (hardware.main_cpu_temp(r) or None) and hardware.main_cpu_temp(r).name)
d = tempfile.mkdtemp(); h = History(os.path.join(d, "hw.db"), {"raw":86400,"minute":7*86400,"hour":365*86400})
now=int(time.time())
for i in range(400):
    h.add(now-4000+i*10, {"cpu.usage": i%100})
h.flush(now)
s = h.series(["cpu.usage"], "1h", now); print("1h points:", len(s["cpu.usage"]), s["cpu.usage"][:2])
s = h.series(["cpu.usage"], "24h", now); print("24h points:", len(s["cpu.usage"]))
cfg = settings.Settings(os.path.join(d,"none.conf"))
a = alerts.Alerts(h, lambda t,k: print("NOTIFY", k, t)); a.started -= 120
cfg.values["ALERT_RAM"]=99.9
for i in range(5): a.check(r, cfg, "F", time.time()+i*20)
print("active:", a.active()); print("limits:", json.dumps(a.limits)[:300])
