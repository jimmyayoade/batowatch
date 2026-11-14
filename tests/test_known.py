#!/usr/bin/env python3
"""Known devices: aliases.conf edits, and when the unknown-device alert fires.
Runs anywhere (no Batocera, no network): python3 tests/test_known.py"""
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.argv = [sys.argv[0]]

import known  # noqa: E402
import batowatch as bw  # noqa: E402

failures = 0


def check(name, cond, detail=""):
    global failures
    print(f"{name}: {'ok' if cond else 'FAIL ' + detail}")
    if not cond:
        failures += 1


# -- aliases.conf edits ------------------------------------------------------
d = tempfile.mkdtemp()
path = os.path.join(d, "aliases.conf")
original = "# header comment\n#\n# 3c:22:fb:91:0a:7e = Example (commented out)\n\n" \
           "AA:BB:CC:DD:EE:01 = Living room  # trailing comment\n10.0.0.5 = Printer\n"
with open(path, "w") as fh:
    fh.write(original)

check("read keeps file order, lowercases keys, skips comments",
      known.read(path) == [("aa:bb:cc:dd:ee:01", "Living room"), ("10.0.0.5", "Printer")],
      repr(known.read(path)))

known.set_name(path, "10.0.0.5", "Office printer")
text = open(path).read()
check("rename changes only that line", "10.0.0.5 = Office printer" in text
      and "# header comment" in text and "# 3c:22:fb:91:0a:7e" in text
      and text.count("10.0.0.5") == 1, text)

known.set_name(path, "AA:BB:CC:DD:EE:02", "Agent box # not a comment")
check("add appends; '#' can't sneak a comment in",
      ("aa:bb:cc:dd:ee:02", "Agent box not a comment") in known.read(path), repr(known.read(path)))

for bad_key, bad_name in (("not-an-ip", "x"), ("10.0.0.9", "   "), ("10.0.0.9\n1.2.3.4", "x")):
    try:
        known.set_name(path, bad_key, bad_name)
        check(f"rejects {bad_key!r}/{bad_name!r}", False, "accepted")
    except ValueError:
        check(f"rejects {bad_key!r}/{bad_name!r}", True)

known.set_name(path, "FE80::0001", "Link-local thing")
check("IPv6 keys are normalised", ("fe80::1", "Link-local thing") in known.read(path))
check("forget matches the normalised key", known.forget(path, ["fe80:0:0::1"]) == 1)

n = known.forget(path, ["aa:bb:cc:dd:ee:01", None])
text = open(path).read()
check("forget removes the line, keeps comments", n == 1 and "Living room" not in text
      and "# header comment" in text, text)

n = known.add_many(path, [("10.0.0.5", "dup"), ("10.0.0.7", "Phone"), ("bad", "x")],
                   "Found in the history")
check("add_many skips keys already there and bad ones", n == 1
      and ("10.0.0.7", "Phone") in known.read(path)
      and ("10.0.0.5", "Office printer") in known.read(path), repr(known.read(path)))
check("add_many writes its heading", "# Found in the history" in open(path).read())

missing = os.path.join(d, "none", "aliases.conf")
check("reading a missing file gives nothing", known.read(missing) == [])


# -- the alert ---------------------------------------------------------------
class FakeEnv:
    def __init__(self):
        self.aliases, self.arp, self.ts_peers, self.router_ips = {}, {}, {}, set()
        self.seen = set()
        self.sun = types.SimpleNamespace(labels={}, stream=set(), rtsp=48010, video=47998)

    known_by = bw.Env.known_by
    alias_key = bw.Env.alias_key

    def classify(self, ip):
        return "tailnet" if ip.startswith("100.") else "lan"

    def _read_tailscale(self):
        pass

    def name_for(self, ip):
        return self.aliases.get(ip, ip), "ip"

    def port_label(self, port):
        return {22: "ssh"}.get(port, "")


def flow(remote, lport, direction="in", proc="sshd", proto=6):
    f = bw.Flow.__new__(bw.Flow)
    f.key, f.remote, f.lport, f.rport = ("t", remote, lport), remote, lport, 50000
    f.direction, f.proc, f.proto = direction, proc, proto
    return f


def tracker(**cfg):
    sent = []
    t = bw.Tracker(FakeEnv(), types.SimpleNamespace(no_tui=False, log_file=None),
                   lambda text, kind="connect": sent.append((kind, text)),
                   dict({"ALERT_UNKNOWN": True, "ALERT_ATTEMPTS": False,
                         "KNOWN_DEVICES": "named"}, **cfg))
    t.bootstrapping = False
    return t, sent


def fire(t, f, now=1000.0):
    dev = bw.Device(f.remote, now)
    t.devices[f.remote] = dev
    t._check_unknown(dev, f, now)
    return dev


t, sent = tracker()
fire(t, flow("192.168.1.50", 22))
check("unknown device reaching SSH alerts", sent == [
    ("unknown", "192.168.1.50 on SSH")], repr(sent))
check("...and logs it as a warning", any(e[1].startswith("! Unknown device") for e in t.events))

t, sent = tracker()
t.env.aliases["192.168.1.50"] = "Mine"
fire(t, flow("192.168.1.50", 22))
check("a device in aliases.conf never alerts", sent == [], repr(sent))

t, sent = tracker()
t.env.arp["192.168.1.51"] = "aa:bb:cc:dd:ee:03"
t.env.aliases["aa:bb:cc:dd:ee:03"] = "Mine by MAC"
fire(t, flow("192.168.1.51", 22))
check("...also when it's listed by MAC", sent == [], repr(sent))

t, sent = tracker()
t.env.ts_peers["100.64.0.9"] = {"name": "agent-batocera"}
fire(t, flow("100.64.0.9", 22))
check("your Tailscale machines never alert", sent == [], repr(sent))

t, sent = tracker()
fire(t, flow("192.168.1.50", 23, proc=None))
check("an attempt on a closed port doesn't alert (ALERT_ATTEMPTS=off)", sent == [], repr(sent))

t, sent = tracker(ALERT_ATTEMPTS=True)
fire(t, flow("192.168.1.50", 23, proc=None))
check("...it does with ALERT_ATTEMPTS=on", sent == [
    ("unknown", "192.168.1.50 tried port 23")], repr(sent))

t, sent = tracker(ALERT_UNKNOWN=False)
fire(t, flow("192.168.1.50", 22))
check("ALERT_UNKNOWN=off: no alert", sent == [], repr(sent))

t, sent = tracker(KNOWN_DEVICES="seen")
t.env.seen.add("192.168.1.50")
fire(t, flow("192.168.1.50", 22))
check("KNOWN_DEVICES=seen: a device in the history doesn't alert", sent == [], repr(sent))

t, sent = tracker()
t.env.seen.add("192.168.1.50")
fire(t, flow("192.168.1.50", 22))
check("KNOWN_DEVICES=named: being in the history isn't enough", len(sent) == 1, repr(sent))

t, sent = tracker()
t.quiet_until = 2000.0
fire(t, flow("192.168.1.50", 22), now=1000.0)
check("no alerts during the quiet start", sent == [], repr(sent))

t, sent = tracker()
fire(t, flow("192.168.1.50", 22), now=1000.0)
fire(t, flow("192.168.1.50", 22), now=1000.0 + 600)
check("the same device doesn't alert again within the hour", len(sent) == 1, repr(sent))
fire(t, flow("192.168.1.50", 22), now=1000.0 + 3700)
check("...but does after it", len(sent) == 2, repr(sent))

env = FakeEnv()
env.arp["192.168.1.52"] = "aa:bb:cc:dd:ee:04"
check("Mark as known saves a LAN device by MAC",
      env.alias_key("192.168.1.52") == "aa:bb:cc:dd:ee:04")
check("...and a device without a MAC by IP", env.alias_key("100.64.0.10") == "100.64.0.10")

print()
print("all passed" if not failures else f"{failures} FAILED")
sys.exit(1 if failures else 0)
