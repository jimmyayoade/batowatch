import os, sys, tempfile, random, importlib.util
SRC = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].endswith(".py") else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "history.py")
spec = importlib.util.spec_from_file_location("history", SRC); H = importlib.util.module_from_spec(spec); spec.loader.exec_module(H)
d = tempfile.mkdtemp(); h = H.History(os.path.join(d, "hw.db"), {"raw": 86400, "minute": 7*86400, "hour": 365*86400})
h.db.execute("PRAGMA wal_autocheckpoint=0")       # count frames; checkpoint by hand
names = [f"m{i}" for i in range(39)]
t0 = 1_800_000_000
def frames():
    return h.db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()[1]
stats = {}
orig_roll, orig_prune = h._roll, h._prune
mode = sys.argv[-1] if sys.argv[-1] in ("all", "noroll", "noprune") else "all"
if mode == "noroll": h._roll = lambda now: None
if mode == "noprune": h._prune = lambda now: None
for s in range(0, 2 * 86400, 10):
    t = t0 + s
    h.add(t, {n: random.random() * 100 for n in names})
    if s % 300 == 290:
        h.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        h.flush(t + 1)
        if s >= 86400:
            stats.setdefault("f", []).append(frames())
f = stats["f"]
print(mode, f"frames per flush: avg {sum(f)/len(f):.1f} min {min(f)} max {max(f)}; page {h.db.execute('pragma page_size').fetchone()[0]}")
