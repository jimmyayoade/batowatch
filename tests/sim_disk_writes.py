import os, sys, tempfile, random, importlib.util
SRC = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].endswith(".py") else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "history.py")
spec = importlib.util.spec_from_file_location("history", SRC); H = importlib.util.module_from_spec(spec); spec.loader.exec_module(H)
def io():
    d = {}
    for l in open("/proc/self/io"):
        k, v = l.split(":"); d[k] = int(v)
    return d
FL = int(os.environ.get("FL", "300"))
d = tempfile.mkdtemp(); h = H.History(os.path.join(d, "hw.db"), {"raw": 86400, "minute": 7*86400, "hour": 365*86400})
names = [f"m{i}" for i in range(39)]
t0 = 1_800_000_000; ev = None
for day in range(2):
    if day == 1:
        os.sync(); a = io()
    for s in range(day * 86400, (day + 1) * 86400, 10):
        t = t0 + s
        h.add(t, {n: random.random() * 100 for n in names})
        if s % 900 == 0:                       # a reconnect every 15 min
            if ev: h.event_end(ev, t)
            ev = h.event_start("connect", t, "1.2.3.4", "x")
        if s % FL == FL - 10: h.flush(t + 1)
os.sync(); b = io()
print(f"{os.path.basename(SRC)}: day 2 wchar {(b['wchar']-a['wchar'])/1e6:.1f} MB, write_bytes {(b['write_bytes']-a['write_bytes'])/1e6:.1f} MB, db {h.size()/1e6:.1f} MB")
