#!/usr/bin/env python3
"""
BatoWatch: who connects to this Batocera box, and what its hardware is doing.

Pure Python stdlib. Talks to the kernel's conntrack over netlink directly
(no conntrack-tools needed), reads the hardware from /proc and /sys, keeps
history in SQLite, and serves a dashboard over HTTPS with a Digest login.
Must run as root.

  python3 batowatch.py                # live terminal view + dashboard
  python3 batowatch.py --no-tui       # how the service runs it
  python3 batowatch.py --once         # one network snapshot and exit

Settings live in $BATOWATCH_HOME/batowatch.conf (one KEY=value per line).
BATOWATCH_HOME (env var) sets the install directory; default below.
"""
import argparse
import collections
import errno
import http.client
import ipaddress
import json
import logging
import logging.handlers
import os
import queue
import re
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

import alerts as alerts_mod            # noqa: E402  (after the path is set)
import hardware                       # noqa: E402
import settings as settings_mod       # noqa: E402
import tls                            # noqa: E402
import webauth                        # noqa: E402
from history import History           # noqa: E402

# Bump with every change that ships: 1.0.1 a fix, 1.1.0 a new feature, 2.0.0 a
# change that breaks something (like the settings format). The installer
# reads it from this line to say what it installs or updates.
VERSION = "1.1.0"

BATOWATCH_HOME = os.environ.get("BATOWATCH_HOME", "/userdata/system/add-ons/batowatch")
OVERLAY_SCRIPT = os.path.join(APP_DIR, "overlay.py")
WWW_DIR = os.path.join(APP_DIR, "www")
RESTART_CODE = 75        # the service loop restarts BatoWatch at once on this exit code
FLUSH = 300              # s between history writes (fewer writes are kinder to SD cards)
LOG = logging.getLogger("batowatch")

# ---------------------------------------------------------------- netlink ---
NETLINK_NETFILTER = 12
NLMSG_ERROR, NLMSG_DONE = 2, 3
NLM_F_REQUEST, NLM_F_DUMP = 0x1, 0x300
NFNL_SUBSYS_CTNETLINK = 1
IPCTNL_MSG_CT_NEW, IPCTNL_MSG_CT_GET, IPCTNL_MSG_CT_DELETE = 0, 1, 2
NF_NETLINK_CONNTRACK_NEW, NF_NETLINK_CONNTRACK_DESTROY = 0x1, 0x4

CTA_TUPLE_ORIG, CTA_TUPLE_REPLY = 1, 2
CTA_COUNTERS_ORIG, CTA_COUNTERS_REPLY = 9, 10
CTA_ID, CTA_TIMESTAMP = 12, 20
CTA_TUPLE_IP, CTA_TUPLE_PROTO = 1, 2
CTA_IP_V4_SRC, CTA_IP_V4_DST, CTA_IP_V6_SRC, CTA_IP_V6_DST = 1, 2, 3, 4
CTA_PROTO_NUM, CTA_PROTO_SRC_PORT, CTA_PROTO_DST_PORT = 1, 2, 3
CTA_COUNTERS_PACKETS, CTA_COUNTERS_BYTES = 1, 2
CTA_TIMESTAMP_START = 1
CTA_PROTOINFO, CTA_PROTOINFO_TCP, CTA_PROTOINFO_TCP_STATE = 4, 1, 1
TCP_CLOSED_STATES = {6, 7, 8}   # LAST_ACK, TIME_WAIT, CLOSE
NLA_TYPE_MASK = 0x3FFF

PROTO_NAMES = {1: "icmp", 6: "tcp", 17: "udp", 58: "icmp6", 132: "sctp"}


def parse_attrs(buf):
    attrs, off = {}, 0
    while off + 4 <= len(buf):
        ln, typ = struct.unpack_from("=HH", buf, off)
        if ln < 4:
            break
        attrs[typ & NLA_TYPE_MASK] = buf[off + 4:off + ln]
        off += (ln + 3) & ~3
    return attrs


def parse_tuple(buf):
    a = parse_attrs(buf)
    ip = parse_attrs(a.get(CTA_TUPLE_IP, b""))
    pr = parse_attrs(a.get(CTA_TUPLE_PROTO, b""))
    if CTA_IP_V4_SRC in ip:
        fam, s, d = socket.AF_INET, ip[CTA_IP_V4_SRC], ip.get(CTA_IP_V4_DST)
    elif CTA_IP_V6_SRC in ip:
        fam, s, d = socket.AF_INET6, ip[CTA_IP_V6_SRC], ip.get(CTA_IP_V6_DST)
    else:
        return None
    if d is None:
        return None
    proto = pr[CTA_PROTO_NUM][0] if CTA_PROTO_NUM in pr else 0
    sport = struct.unpack(">H", pr[CTA_PROTO_SRC_PORT])[0] if CTA_PROTO_SRC_PORT in pr else 0
    dport = struct.unpack(">H", pr[CTA_PROTO_DST_PORT])[0] if CTA_PROTO_DST_PORT in pr else 0
    return proto, socket.inet_ntop(fam, s), socket.inet_ntop(fam, d), sport, dport


def parse_counters(buf):
    if not buf:
        return 0, 0
    a = parse_attrs(buf)
    pk = struct.unpack(">Q", a[CTA_COUNTERS_PACKETS])[0] if CTA_COUNTERS_PACKETS in a else 0
    by = struct.unpack(">Q", a[CTA_COUNTERS_BYTES])[0] if CTA_COUNTERS_BYTES in a else 0
    return pk, by


class Flow:
    __slots__ = ("key", "proto", "osrc", "odst", "osport", "odport",
                 "pkts_o", "pkts_r", "bytes_o", "bytes_r", "has_counters",
                 "start", "seen", "direction", "remote", "lport", "rport", "proc",
                 "state")

    def ingress_bytes(self):
        return self.bytes_o if self.direction == "in" else self.bytes_r

    def egress_bytes(self):
        return self.bytes_r if self.direction == "in" else self.bytes_o

    def packets(self):
        return self.pkts_o + self.pkts_r


def parse_ct(payload):
    if len(payload) < 4:
        return None
    a = parse_attrs(payload[4:])  # skip nfgenmsg
    if CTA_TUPLE_ORIG not in a:
        return None
    t = parse_tuple(a[CTA_TUPLE_ORIG])
    if not t:
        return None
    f = Flow()
    f.proto, f.osrc, f.odst, f.osport, f.odport = t
    f.pkts_o, f.bytes_o = parse_counters(a.get(CTA_COUNTERS_ORIG))
    f.pkts_r, f.bytes_r = parse_counters(a.get(CTA_COUNTERS_REPLY))
    f.has_counters = CTA_COUNTERS_ORIG in a
    f.start = None
    if CTA_TIMESTAMP in a:
        ts = parse_attrs(a[CTA_TIMESTAMP])
        if CTA_TIMESTAMP_START in ts:
            f.start = struct.unpack(">Q", ts[CTA_TIMESTAMP_START])[0] / 1e9
    f.state = None
    if CTA_PROTOINFO in a:
        tcp = parse_attrs(parse_attrs(a[CTA_PROTOINFO]).get(CTA_PROTOINFO_TCP, b""))
        if CTA_PROTOINFO_TCP_STATE in tcp:
            f.state = tcp[CTA_PROTOINFO_TCP_STATE][0]
    if CTA_ID in a:
        f.key = struct.unpack(">I", a[CTA_ID])[0]
    else:
        f.key = (f.proto, f.osrc, f.osport, f.odst, f.odport)
    f.seen = 0.0
    f.direction = f.remote = None
    f.lport = f.rport = 0
    f.proc = None
    return f


def iter_nlmsgs(data):
    off = 0
    while off + 16 <= len(data):
        ln, typ, flags, _seq, _pid = struct.unpack_from("=IHHII", data, off)
        if ln < 16:
            break
        yield typ, flags, data[off + 16:off + ln]
        off += (ln + 3) & ~3


def ct_dump():
    """Full conntrack table (IPv4 + IPv6) as {key: Flow}."""
    s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_NETFILTER)
    try:
        s.bind((0, 0))
        hdr = struct.pack("=IHHII", 20, (NFNL_SUBSYS_CTNETLINK << 8) | IPCTNL_MSG_CT_GET,
                          NLM_F_REQUEST | NLM_F_DUMP, int(time.time()) & 0xFFFFFFFF, 0)
        s.sendto(hdr + struct.pack("=BBH", socket.AF_UNSPEC, 0, 0), (0, 0))
        flows = {}
        while True:
            data = s.recv(1 << 18)
            for typ, _flags, payload in iter_nlmsgs(data):
                if typ == NLMSG_DONE:
                    return flows
                if typ == NLMSG_ERROR:
                    err = struct.unpack_from("=i", payload)[0]
                    if err:
                        raise OSError(-err, os.strerror(-err))
                    continue
                f = parse_ct(payload)
                if f:
                    flows[f.key] = f
    finally:
        s.close()


def ct_events(handler):
    """Blocking loop: calls handler(kind, flow) for NEW / DESTROY events."""
    s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_NETFILTER)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    except OSError:
        pass
    s.bind((0, NF_NETLINK_CONNTRACK_NEW | NF_NETLINK_CONNTRACK_DESTROY))
    while True:
        try:
            data = s.recv(1 << 18)
        except OSError as e:
            if e.errno == errno.ENOBUFS:
                handler("overrun", None)
                continue
            raise
        for typ, _flags, payload in iter_nlmsgs(data):
            if typ >> 8 != NFNL_SUBSYS_CTNETLINK:
                continue
            f = parse_ct(payload)
            if not f:
                continue
            msg = typ & 0xFF
            if msg == IPCTNL_MSG_CT_NEW:
                handler("new", f)
            elif msg == IPCTNL_MSG_CT_DELETE:
                handler("destroy", f)


# ------------------------------------------------- packet-counting source ---
# Batocera 43's kernel can't switch connection tracking on (no usable
# netfilter), so the table above stays empty. bw-flows (flows/bw-flows.c)
# counts packets per connection instead and prints one JSON line a second.
FLOWS_HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bw-flows")
PROTO_NUMS = {v: k for k, v in PROTO_NAMES.items()}


def server_ports():
    """{(proto, port)} of local sockets that wait for others: listening TCP
    sockets and unconnected UDP sockets. Tells which end of a flow is the
    server, which packet counts alone can't."""
    ports = set()
    for proto, names in ((6, ("tcp", "tcp6")), (17, ("udp", "udp6"))):
        for name in names:
            try:
                fh = open(f"/proc/net/{name}")
            except OSError:
                continue
            with fh:
                next(fh, None)
                for line in fh:
                    p = line.split()
                    if len(p) < 4:
                        continue
                    if proto == 6 and p[3] != "0A":          # TCP_LISTEN
                        continue
                    if proto == 17 and not p[2].endswith(":0000"):
                        continue
                    try:
                        ports.add((proto, int(p[1].rsplit(":", 1)[1], 16)))
                    except (IndexError, ValueError):
                        pass
    return ports


class PacketFlows:
    """Runs bw-flows and turns its one-way counts into Flows, so dump() can
    stand in for ct_dump(). A conversation with no packets for TCP_IDLE /
    OTHER_IDLE seconds counts as closed."""
    TCP_IDLE, OTHER_IDLE = 120, 30

    def __init__(self, path, env):
        self.path, self.env = path, env
        self.lock = threading.Lock()
        self.latest = None
        self.proc = None
        self.stopping = False
        self.ready = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="bw-flows").start()

    def _run(self):
        while not self.stopping:
            err = ""
            try:
                self.proc = subprocess.Popen([self.path, "-i", "1"], stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, text=True)
                for line in self.proc.stdout:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    with self.lock:
                        self.latest = d
                    self.ready.set()
                err = self.proc.stderr.read().strip()
                rc = self.proc.wait()
                err = f"bw-flows exited with status {rc}" + (f": {err[-300:]}" if err else "")
            except OSError as e:
                err = f"bw-flows could not start: {e}"
            if not self.stopping:
                LOG.warning(f"{err}; restarting in 5s")
                time.sleep(5)

    def stop(self):
        self.stopping = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _inbound(self, proto, lport, rport, servers):
        """True when the remote end started the conversation."""
        if lport in self.env.sun.labels:
            return True
        local_srv = (proto, lport) in servers
        remote_srv = rport in COMMON_PORTS or rport < 1024
        if local_srv != remote_srv:
            return local_srv
        return lport < rport      # otherwise the lower port is the server's

    def dump(self):
        with self.lock:
            d = self.latest
        if not d:
            return {}
        now = d["time"]
        convs = {}
        for _iface, direction, proto, src, sport, dst, dport, pkts, nbytes, last in d["flows"]:
            pnum = PROTO_NUMS.get(proto)
            if pnum is None:
                try:
                    pnum = int(proto)
                except ValueError:
                    continue
            if direction == "in":
                key = (pnum, dst, dport, src, sport)      # proto, local, lport, remote, rport
            else:
                key = (pnum, src, sport, dst, dport)
            c = convs.setdefault(key, [0, 0, 0, 0, 0])     # pkts in, bytes in, pkts out, bytes out, last
            i = 0 if direction == "in" else 2
            c[i] += pkts
            c[i + 1] += nbytes
            c[4] = max(c[4], last)
        servers = server_ports()
        flows = {}
        for (pnum, loc, lport, rem, rport), (pi, bi, po, bo, last) in convs.items():
            if now - last > (self.TCP_IDLE if pnum == 6 else self.OTHER_IDLE):
                continue
            f = Flow()
            f.proto = pnum
            if self._inbound(pnum, lport, rport, servers):
                f.osrc, f.osport, f.odst, f.odport = rem, rport, loc, lport
                f.pkts_o, f.bytes_o, f.pkts_r, f.bytes_r = pi, bi, po, bo
            else:
                f.osrc, f.osport, f.odst, f.odport = loc, lport, rem, rport
                f.pkts_o, f.bytes_o, f.pkts_r, f.bytes_r = po, bo, pi, bi
            f.has_counters = True
            f.start = f.state = None
            f.key = ("pkt", pnum, loc, lport, rem, rport)
            f.seen = 0.0
            f.direction = f.remote = None
            f.lport = f.rport = 0
            f.proc = None
            flows[f.key] = f
        return flows


# ---------------------------------------------------------------- helpers ---
TS_V4 = ipaddress.ip_network("100.64.0.0/10")
TS_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def fmt_bytes(n):
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


def fmt_dur(sec):
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def read_gateways():
    """Default-route next hops from the kernel's main routing table."""
    gws = set()
    try:
        with open("/proc/net/route") as fh:
            next(fh, None)
            for line in fh:
                p = line.split()
                # Iface Destination Gateway Flags ...; RTF_GATEWAY = 0x2
                if len(p) > 3 and p[1] == "00000000" and int(p[3], 16) & 0x2:
                    gws.add(socket.inet_ntoa(struct.pack("<I", int(p[2], 16))))
    except (OSError, ValueError):
        pass
    try:
        with open("/proc/net/ipv6_route") as fh:
            for line in fh:
                p = line.split()
                # dest dest_len src src_len next_hop metric refcnt use flags iface
                if len(p) >= 10 and p[0] == "0" * 32 and p[1] == "00" and p[4] != "0" * 32:
                    gws.add(str(ipaddress.IPv6Address(bytes.fromhex(p[4]))))
    except (OSError, ValueError):
        pass
    return gws


def split_hostport(s):
    host, _, port = s.rpartition(":")
    return host.strip("[]"), int(port) if port.isdigit() else 0


class SunshinePorts:
    """Sunshine derives every port from one base port (default 47989)."""

    def __init__(self, conf_path):
        self.conf_path, self.mtime = conf_path, None
        self.set_base(47989)

    def set_base(self, base):
        self.base = base
        self.https, self.http, self.web = base - 5, base, base + 1
        self.video, self.control, self.audio = base + 9, base + 10, base + 11
        self.rtsp = base + 21
        self.stream = {self.video, self.control, self.audio, self.rtsp}
        self.labels = {self.https: "sunshine-https", self.http: "sunshine-http",
                       self.web: "sunshine-webui", self.rtsp: "sunshine-rtsp",
                       self.video: "sunshine-video", self.control: "sunshine-control",
                       self.audio: "sunshine-audio"}

    def refresh(self):
        try:
            mt = os.stat(self.conf_path).st_mtime
        except OSError:
            return
        if mt == self.mtime:
            return
        self.mtime = mt
        base = 47989
        with open(self.conf_path, errors="replace") as fh:
            for line in fh:
                m = re.match(r"\s*port\s*=\s*(\d+)", line)
                if m:
                    base = int(m.group(1))
        self.set_base(base)


COMMON_PORTS = {22: "ssh", 53: "dns", 67: "dhcp", 68: "dhcp", 123: "ntp",
                137: "netbios-ns", 138: "netbios-dgm", 139: "netbios", 443: "https",
                80: "http", 445: "smb", 1234: "es-api", 1900: "ssdp", 3478: "stun",
                5351: "nat-pmp", 5353: "mdns", 8080: "http-alt", 41641: "tailscale-wg"}

TS_SERVICE_IP = "100.100.100.100"   # MagicDNS / Tailscale's in-node services
BRIDGE_PREFIXES = ("docker", "br-", "virbr", "cni", "podman", "veth", "lxc",
                   "flannel", "cali")


def socket_owners():
    """{(proto, local_port): process name} for every TCP/UDP socket on the host."""
    inode_comm = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as fh:
                comm = fh.read().strip()
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if link.startswith("socket:["):
                inode_comm[link[8:-1]] = comm
    owners = {}
    for proto, names in ((6, ("tcp", "tcp6")), (17, ("udp", "udp6"))):
        for name in names:
            try:
                fh = open(f"/proc/net/{name}")
            except OSError:
                continue
            with fh:
                next(fh, None)
                for line in fh:
                    p = line.split()
                    if len(p) < 10:
                        continue
                    comm = inode_comm.get(p[9])
                    if comm:
                        owners.setdefault((proto, int(p[1].rsplit(":", 1)[1], 16)), comm)
    return owners


# ----------------------------------------------------------- environment ---
class TailscaleClient:
    def __init__(self, sock_path):
        self.sock_path = sock_path

    def status(self):
        class Conn(http.client.HTTPConnection):
            def connect(conn):
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(3)
                s.connect(self.sock_path)
                conn.sock = s

        c = Conn("local-tailscaled.sock", timeout=3)
        try:
            c.request("GET", "/localapi/v0/status",
                      headers={"Host": "local-tailscaled.sock", "Sec-Tailscale": "localapi"})
            r = c.getresponse()
            if r.status != 200:
                raise OSError(f"tailscale localapi HTTP {r.status}")
            return json.loads(r.read())
        finally:
            c.close()

    def cert_pair(self, domain):
        return tls.tailscale_cert_pair(self.sock_path, domain)


class Env:
    """Everything about the host and its neighbours, refreshed periodically."""

    def __init__(self, args):
        self.args = args
        self.local_ips = {}          # ip -> ifname
        self.local_nets = []         # (network, ifname), excluding tailscale0
        self.ts_peers = {}           # tailnet ip -> peer info
        self.ts_endpoints = set()    # (ip, port) of peer WireGuard endpoints
        self.router_ips = set(args.subnet_router)
        self.gateways = set()        # default-route next hops (IPv4 and IPv6)
        self.ts_error = None
        self.arp = {}                # ip -> mac
        self.aliases = {}            # mac or ip -> name
        self.aliases_mtime = None
        self.sun = SunshinePorts(args.sunshine_conf)
        self.ts = TailscaleClient(args.ts_socket)
        self.rdns = {}               # ip -> (name or None, fetched_at)
        self.rdns_q = queue.Queue(maxsize=256)
        self.owners = {}             # (proto, local port) -> process name
        self.owners_at = 0.0
        self.last_refresh = 0.0
        threading.Thread(target=self._rdns_worker, daemon=True).start()

    # -- refresh -----------------------------------------------------------
    def refresh(self, force=False):
        now = time.time()
        if force or now - self.owners_at >= 5:
            self._refresh_owners(now)
        if not force and now - self.last_refresh < 30:
            self._read_arp()
            return
        self.last_refresh = now
        self._read_addrs()
        self._read_arp()
        self._read_aliases()
        self.sun.refresh()
        self._read_tailscale()

    def _read_addrs(self):
        try:
            out = subprocess.run(["ip", "-o", "addr", "show"], capture_output=True,
                                 text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return
        ips, nets = {}, []
        for line in out.splitlines():
            p = line.split()
            if len(p) < 4 or p[2] not in ("inet", "inet6"):
                continue
            ifname = p[1].rstrip(":").split("@")[0]
            try:
                iface = ipaddress.ip_interface(p[3])
            except ValueError:
                continue
            ips[str(iface.ip)] = ifname
            if ifname != "tailscale0" and not iface.ip.is_loopback:
                nets.append((iface.network, ifname))
        if ips:
            self.local_ips, self.local_nets = ips, nets
        self.gateways = read_gateways()

    def _read_arp(self):
        arp = {}
        try:
            with open("/proc/net/arp") as fh:
                next(fh)
                for line in fh:
                    p = line.split()
                    if len(p) >= 4 and p[3] != "00:00:00:00:00:00":
                        arp[p[0]] = p[3].lower()
        except OSError:
            return
        self.arp = arp

    def _read_aliases(self):
        path = self.args.aliases
        try:
            mt = os.stat(path).st_mtime
        except OSError:
            self.aliases = {}
            return
        if mt == self.aliases_mtime:
            return
        self.aliases_mtime, al = mt, {}
        with open(path, errors="replace") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if "=" in line:
                    k, v = (x.strip() for x in line.split("=", 1))
                    if k and v:
                        al[k.lower()] = v
        self.aliases = al

    def _read_tailscale(self):
        try:
            st = self.ts.status()
        except (OSError, ValueError, http.client.HTTPException) as e:
            self.ts_error = str(e)
            return
        self.ts_error = None
        peers, endpoints = {}, set()
        routers = set(self.args.subnet_router)
        for p in (st.get("Peer") or {}).values():
            name = (p.get("DNSName") or "").split(".")[0] or p.get("HostName") or "?"
            info = {"name": name, "host": p.get("HostName"), "os": p.get("OS"),
                    "direct": bool(p.get("CurAddr")), "relay": p.get("Relay")}
            for ip in p.get("TailscaleIPs") or []:
                peers[ip] = info
            addrs = list(p.get("Addrs") or [])
            if p.get("CurAddr"):
                addrs.append(p["CurAddr"])
            parsed = [split_hostport(a) for a in addrs]
            endpoints.update(parsed)
            # A peer whose advertised routes cover one of our LANs is a subnet
            # router; its LAN-side addresses are where SNAT'd traffic comes from.
            routes = []
            for r in p.get("PrimaryRoutes") or []:
                try:
                    routes.append(ipaddress.ip_network(r, strict=False))
                except ValueError:
                    pass
            if any(rt.overlaps(net) for rt in routes for net, _ in self.local_nets
                   if rt.version == net.version and rt.prefixlen > 0):
                for host, _port in parsed:
                    if self._in_lan(host):
                        routers.add(host)
        self.ts_peers, self.ts_endpoints, self.router_ips = peers, endpoints, routers

    # -- lookups -----------------------------------------------------------
    def _iface_for(self, ip):
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return None
        for net, ifname in self.local_nets:
            if a in net:
                return ifname
        return None

    def _in_lan(self, ip):
        ifname = self._iface_for(ip)
        return ifname is not None and not ifname.startswith(BRIDGE_PREFIXES)

    def is_broadcast(self, a):
        return a.version == 4 and any(
            a == net.broadcast_address for net, _ in self.local_nets
            if net.version == 4 and net.prefixlen < 31)

    def classify(self, ip):
        a = ipaddress.ip_address(ip)
        if a in TS_V4 or a in TS_V6:
            peer = self.ts_peers.get(ip)
            if not peer:
                return "tailnet"
            return "tailnet-direct" if peer["direct"] else "tailnet-relay"
        if ip in self.gateways:
            return "gateway"
        if ip in self.router_ips:
            return "subnet-router"
        ifname = self._iface_for(ip)
        if ifname:
            return "container" if ifname.startswith(BRIDGE_PREFIXES) else "lan"
        if a.is_link_local:
            return "link-local"
        if a.is_private:
            return "private"
        return "internet"

    def _refresh_owners(self, now):
        self.owners_at = now
        try:
            self.owners = socket_owners()
        except OSError:
            pass

    def owner(self, f):
        """Owning process of a flow's local socket. A brand-new socket may not
        be in the map yet, so a miss triggers a rescan (at most every 2s)."""
        key = (f.proto, f.lport)
        proc = self.owners.get(key)
        if proc is None and time.time() - self.owners_at > 2:
            self._refresh_owners(time.time())
            proc = self.owners.get(key)
        return proc

    def is_transport(self, f):
        """Tailscale's own plumbing: the WireGuard tunnel, STUN, DERP, control
        plane, and MagicDNS. Identified by the owning process, not the port."""
        if f.proc == "tailscaled" or f.remote == TS_SERVICE_IP:
            return True
        return f.proto == 17 and (f.lport == self.args.wg_port
                                  or (f.remote, f.rport) in self.ts_endpoints)

    def name_for(self, ip):
        # An alias is an explicit choice, so it beats every other name source.
        mac = self.arp.get(ip)
        for k in (mac, ip.lower()):
            if k and k in self.aliases:
                return self.aliases[k], "alias"
        peer = self.ts_peers.get(ip)
        if peer:
            return peer["name"], "tailscale"
        cached = self.rdns.get(ip)
        if cached is None or time.time() - cached[1] > 600:
            if cached is None:
                self.rdns[ip] = (None, time.time())  # placeholder, avoid requeue
            try:
                self.rdns_q.put_nowait(ip)
            except queue.Full:
                pass
        elif cached[0]:
            return cached[0], "rdns"
        return ip, "ip"

    def _rdns_worker(self):
        while True:
            ip = self.rdns_q.get()
            try:
                name = socket.gethostbyaddr(ip)[0]
            except (OSError, UnicodeError):
                name = None
            self.rdns[ip] = (name, time.time())

    def port_label(self, port):
        return self.sun.labels.get(port) or COMMON_PORTS.get(port, "")


# ---------------------------------------------------------------- tracker ---
class Device:
    def __init__(self, ip, since):
        self.ip = ip
        self.first_seen = since
        self.exact = True
        self.flows = {}
        self.empty_since = None
        self.closed_in = self.closed_out = 0
        self.stream_since = None
        self.stream_pkts = 0
        self.stream_change = 0.0
        self.rate_in = self.rate_out = 0.0     # bytes/s over the last interval
        self.rate_t = None
        self.rate_prev = (0, 0)

    def bytes_in(self):
        return self.closed_in + sum(f.ingress_bytes() for f in self.flows.values())

    def bytes_out(self):
        return self.closed_out + sum(f.egress_bytes() for f in self.flows.values())


class Tracker:
    DEVICE_GRACE = 30      # s with zero flows before a device counts as gone
    STREAM_IDLE = 15       # s of no stream packets before a Sunshine session ends
    STREAM_MIN = 20        # s after start before we're allowed to call it ended
    NOTIFY_COOLDOWN = 10   # s; a reconnect inside this window doesn't re-notify (CONNECT_REPEAT)

    def __init__(self, env, args, notifier):
        self.env, self.args, self.notify = env, args, notifier
        self.lock = threading.Lock()
        self.devices = {}
        self.flow_dev = {}
        self.history = collections.deque(maxlen=30)
        self.events = collections.deque(maxlen=60)
        self.last_stream_end = {}
        self.tombstones = set()   # conntrack keys we've retired but the kernel still lists
        self.bootstrapping = True
        self.hook = None          # hook(kind, phase, ip, name) for the network history
        self.source = "conntrack" # or "packets" (bw-flows)

    def counters_on(self):
        return self.source == "packets" or read_sysctl("nf_conntrack_acct") == "1"

    def log(self, text):
        now = time.time()
        self.events.append((now, text))
        # With the terminal screen up and no log file, a stderr line would
        # scribble over the screen, so events only go to the screen then.
        if self.args.no_tui or self.args.log_file:
            LOG.info(text)

    def _orient(self, f):
        local = self.env.local_ips
        s_local, d_local = f.osrc in local, f.odst in local
        if s_local and not d_local:
            f.direction, f.remote, f.lport, f.rport = "out", f.odst, f.osport, f.odport
        elif d_local and not s_local:
            f.direction, f.remote, f.lport, f.rport = "in", f.osrc, f.odport, f.osport
        else:
            return False
        a = ipaddress.ip_address(f.remote)
        if (a.is_multicast or a.is_loopback or f.remote == "255.255.255.255"
                or self.env.is_broadcast(a)):
            return False
        if (f.remote in self.env.gateways and not self.args.show_gateway
                and not (f.direction == "in" and f.lport in self.env.sun.labels)):
            # The router's own DNS / NAT-PMP / UPnP chatter isn't a device
            # connecting. Sunshine traffic from it is kept: with a port forward
            # and hairpin NAT, a real stream can arrive from the router's IP.
            return False
        f.proc = self.env.owner(f)
        if self.args.inbound_only and f.direction == "out":
            return False
        if not self.args.show_transport and self.env.is_transport(f):
            return False
        return True

    def upsert(self, f, now):
        if f.key in self.tombstones:
            return
        dev = self.flow_dev.get(f.key)
        if dev is not None:
            cur = dev.flows[f.key]
            if f.has_counters:
                cur.pkts_o, cur.pkts_r = f.pkts_o, f.pkts_r
                cur.bytes_o, cur.bytes_r = f.bytes_o, f.bytes_r
                cur.has_counters = True
            if f.start and not cur.start:
                cur.start = f.start
            if f.state is not None:
                cur.state = f.state
            if cur.proc is None:
                cur.proc = self.env.owner(cur)
                # Owner just became known; it may turn out to be Tailscale plumbing.
                if cur.proc and not self.args.show_transport and self.env.is_transport(cur):
                    self._retire(f.key, now, silent=True)
                    return
            if cur.proto == 6 and cur.state in TCP_CLOSED_STATES:
                self._retire(f.key, now)   # bytes roll into the device totals
                return
            cur.seen = now
            return
        if not self._orient(f):
            return
        if f.proto == 6 and f.state in TCP_CLOSED_STATES:
            self.tombstones.add(f.key)     # already dead when first seen
            return
        f.seen = now
        dev = self.devices.get(f.remote)
        if dev is None:
            dev = Device(f.remote, f.start or now)
            # Without a kernel timestamp, a flow found at startup only tells us
            # "connected at least since BatoWatch started".
            dev.exact = bool(f.start) or not self.bootstrapping
            self.devices[f.remote] = dev
            path = self.env.classify(f.remote)
            if path != "internet":
                if not self.bootstrapping:
                    self.log(f"+ {self.env.name_for(f.remote)[0]} ({f.remote}, {path})")
                self._hook("device", "start", f.remote)
        elif f.start and f.start < dev.first_seen:
            dev.first_seen = f.start
            dev.exact = True
        dev.flows[f.key] = f
        dev.empty_since = None
        self.flow_dev[f.key] = dev
        sun = self.env.sun
        if f.direction == "in" and dev.stream_since is None and (
                (f.proto == 6 and f.lport == sun.rtsp)
                or (f.proto == 17 and f.lport == sun.video)):
            self._stream_start(dev, now)

    def remove(self, key, now, final=None):
        dev = self.flow_dev.pop(key, None)
        if dev is None:
            return
        f = dev.flows.pop(key)
        if final is not None and final.has_counters:
            f.pkts_o, f.pkts_r, f.bytes_o, f.bytes_r = (final.pkts_o, final.pkts_r,
                                                        final.bytes_o, final.bytes_r)
        dev.closed_in += f.ingress_bytes()
        dev.closed_out += f.egress_bytes()
        if not dev.flows:
            dev.empty_since = now

    def _retire(self, key, now, silent=False):
        """Stop tracking a flow the kernel still lists. silent=True means it
        should never have been shown: discard it without counting its bytes."""
        self.tombstones.add(key)
        dev = self.flow_dev.get(key)
        if dev is None:
            return
        if not silent:
            self.remove(key, now)
            return
        del self.flow_dev[key]
        dev.flows.pop(key, None)
        if not dev.flows:
            if dev.closed_in == 0 and dev.closed_out == 0 and dev.stream_since is None:
                del self.devices[dev.ip]
            else:
                dev.empty_since = now

    def reconcile(self, dump, now):
        self.tombstones &= dump.keys()
        for f in dump.values():
            self.upsert(f, now)
        for key, dev in list(self.flow_dev.items()):
            if key not in dump and now - dev.flows[key].seen > 3:
                self.remove(key, now)

    def on_event(self, kind, f):
        now = time.time()
        with self.lock:
            if kind == "new":
                self.upsert(f, now)
            elif kind == "destroy":
                self.remove(f.key, now, f)
                self.tombstones.discard(f.key)
            elif kind == "overrun":
                self.log("! event buffer overrun; next dump will reconcile")

    def _hook(self, kind, phase, ip):
        if self.hook:
            try:
                self.hook(kind, phase, ip, self.env.name_for(ip)[0])
            except Exception as e:      # history trouble must not stop the tracker
                LOG.warning(f"network history: {e}")

    # -- sunshine sessions -------------------------------------------------
    def _stream_flows(self, dev):
        return [f for f in dev.flows.values()
                if f.direction == "in" and f.lport in self.env.sun.stream]

    def _stream_start(self, dev, now):
        dev.stream_since = now
        dev.stream_change = now
        dev.stream_pkts = sum(f.packets() for f in self._stream_flows(dev))
        name = self.env.name_for(dev.ip)[0]
        if self.bootstrapping:
            self.log(f"~ Sunshine stream already active: {name}")
            self._hook("stream", "start", dev.ip)
            return
        self.log(f"> Sunshine stream started: {name} ({dev.ip})")
        self._hook("stream", "start", dev.ip)
        if now - self.last_stream_end.get(dev.ip, 0) >= self.NOTIFY_COOLDOWN:
            self.notify(f"{name} has connected")

    def tick(self, now):
        for ip, dev in list(self.devices.items()):
            bi, bo = dev.bytes_in(), dev.bytes_out()
            if dev.rate_t is not None and now > dev.rate_t:
                dt = now - dev.rate_t
                dev.rate_in = max(0.0, (bi - dev.rate_prev[0]) / dt)
                dev.rate_out = max(0.0, (bo - dev.rate_prev[1]) / dt)
            dev.rate_t, dev.rate_prev = now, (bi, bo)
            if dev.stream_since is not None:
                flows = self._stream_flows(dev)
                pk = sum(f.packets() for f in flows)
                if pk != dev.stream_pkts:
                    dev.stream_pkts, dev.stream_change = pk, now
                counted = bool(flows) and all(f.has_counters for f in flows)
                idle = now - dev.stream_change
                if now - dev.stream_since > self.STREAM_MIN and (
                        (not flows and idle > self.STREAM_IDLE)
                        or (counted and idle > self.STREAM_IDLE)):
                    dur = dev.stream_change - dev.stream_since
                    self.log(f"< Sunshine stream ended: {self.env.name_for(ip)[0]}"
                             f" after {fmt_dur(dur)}")
                    self.last_stream_end[ip] = now
                    dev.stream_since = None
                    self._hook("stream", "end", ip)
            if not dev.flows and dev.empty_since and now - dev.empty_since > self.DEVICE_GRACE:
                name, _ = self.env.name_for(ip)
                path = self.env.classify(ip)
                self.history.appendleft((now, name, ip, path, dev.empty_since - dev.first_seen,
                                         dev.bytes_in(), dev.bytes_out()))
                if path != "internet":
                    self.log(f"- {name} ({ip}) gone after {fmt_dur(dev.empty_since - dev.first_seen)}")
                    self._hook("device", "end", ip)
                del self.devices[ip]

    # -- view --------------------------------------------------------------
    def flow_groups(self, d):
        """Flows grouped by (protocol, direction, service port, process), with a
        connection count and the range of peer-side ephemeral ports."""
        groups = {}
        for f in d.flows.values():
            port = f.lport if f.direction == "in" else f.rport
            peer = f.rport if f.direction == "in" else f.lport
            g = groups.setdefault((f.proto, f.direction, port, f.proc or "-"),
                                  {"n": 0, "in": 0, "out": 0, "peers": []})
            g["n"] += 1
            g["in"] += f.ingress_bytes()
            g["out"] += f.egress_bytes()
            g["peers"].append(peer)
        result = []
        for (proto, direction, port, proc), g in sorted(groups.items(),
                                                        key=lambda kv: (kv[0][1], kv[0][2])):
            peers = g["peers"]
            result.append({
                "proto": PROTO_NAMES.get(proto, str(proto)), "dir": direction,
                "side": "local" if direction == "in" else "remote", "port": port,
                "service": self.env.port_label(port), "proc": proc, "count": g["n"],
                "peers": str(peers[0]) if len(peers) == 1 else f"{min(peers)}-{max(peers)}",
                "in": g["in"], "out": g["out"]})
        return result

    def _render_groups(self, d):
        lines = []
        for g in self.flow_groups(d):
            count = f"x{g['count']}" if g["count"] > 1 else ""
            lines.append(f"    {g['proto']:5} {g['dir']:3} {g['side']}:{g['port']:<5} "
                         f"{g['service']:16} {g['proc'][:12]:12} {count:5} "
                         f"peer:{g['peers']:12} in {fmt_bytes(g['in']):>7}  "
                         f"out {fmt_bytes(g['out']):>7}")
        return lines

    def snapshot(self, now):
        """Everything the web dashboard shows, as plain JSON-able data."""
        env = self.env
        devices = []
        for d in self.devices.values():
            name, src = env.name_for(d.ip)
            peer = env.ts_peers.get(d.ip) or {}
            devices.append({
                "ip": d.ip, "name": name, "name_source": src, "route": env.classify(d.ip),
                "mac": env.arp.get(d.ip), "os": peer.get("os"),
                "first_seen": d.first_seen, "exact": d.exact, "idle": not d.flows,
                "bytes_in": d.bytes_in(), "bytes_out": d.bytes_out(),
                "rate_in": round(d.rate_in), "rate_out": round(d.rate_out),
                "streaming_since": d.stream_since if d.stream_since is not None else None,
                "flows": self.flow_groups(d)})
        return {
            "now": now, "host": socket.gethostname(),
            "local_ips": sorted(ip for ip in env.local_ips if not ip.startswith(("127.", "::1"))),
            "sunshine_base": env.sun.base, "tailscale_error": env.ts_error,
            "counters": self.counters_on(), "source": self.source,
            "devices": devices,
            "history": [{"t": t, "name": n, "ip": ip, "route": r, "duration": dur,
                         "bytes_in": bi, "bytes_out": bo}
                        for t, n, ip, r, dur, bi, bo in self.history],
            "events": [{"t": t, "text": text} for t, text in reversed(self.events)]}

    def render(self, now):
        env = self.env
        order = {"tailnet-direct": 0, "tailnet-relay": 0, "tailnet": 0, "subnet-router": 1,
                 "lan": 2, "gateway": 2, "container": 3, "private": 3, "link-local": 3, "internet": 4}
        devs = sorted(self.devices.values(),
                      key=lambda d: (d.stream_since is None, order.get(env.classify(d.ip), 5),
                                     d.first_seen))
        out = [f"BatoWatch  {time.strftime('%H:%M:%S')}  devices={len(devs)} "
               f"flows={len(self.flow_dev)}  sunshine-base={env.sun.base}  "
               f"tailscale={'ok' if not env.ts_error else 'ERR ' + env.ts_error}  "
               f"source={self.source}  acct={'on' if self.counters_on() else 'OFF'}", ""]
        out.append(f"{'DEVICE':34} {'PATH':15} {'SINCE':9} {'DURATION':9} "
                   f"{'IN':>8} {'OUT':>8}  NOTE")
        for d in devs:
            name, _src = env.name_for(d.ip)
            if name == d.ip:
                label = d.ip
            else:
                room = max(8, 34 - len(d.ip) - 3)
                label = f"{name[:room]} ({d.ip})"
            note = f"STREAMING {fmt_dur(now - d.stream_since)}" if d.stream_since else ""
            if not d.flows:
                note = (note + " idle").strip()
            mark = "" if d.exact else ">"
            since = mark + time.strftime('%H:%M:%S', time.localtime(d.first_seen))
            out.append(f"{label[:34]:34} {env.classify(d.ip):15} {since:9} "
                       f"{mark + fmt_dur(now - d.first_seen):9} {fmt_bytes(d.bytes_in()):>8} "
                       f"{fmt_bytes(d.bytes_out()):>8}  {note}")
            if self.args.flows and not self.args.raw_flows:
                out += self._render_groups(d)
            elif self.args.flows:
                for f in sorted(d.flows.values(), key=lambda x: (x.direction, x.lport)):
                    arrow = "<-" if f.direction == "in" else "->"
                    svc = env.port_label(f.lport if f.direction == "in" else f.rport)
                    out.append(f"    {PROTO_NAMES.get(f.proto, f.proto):5} {f.direction:3} "
                               f"local:{f.lport:<5} {arrow} remote:{f.rport:<5} {svc:16} "
                               f"{(f.proc or '-')[:12]:12} "
                               f"in {fmt_bytes(f.ingress_bytes()):>7}  "
                               f"out {fmt_bytes(f.egress_bytes()):>7}")
        if any(not d.exact for d in devs):
            out += ["", "> = connected before BatoWatch started (true start unknown)"]
        if self.history:
            out += ["", "RECENTLY DISCONNECTED"]
            for t, name, ip, path, dur, bi, bo in list(self.history)[:5]:
                out.append(f"  {time.strftime('%H:%M:%S', time.localtime(t))}  "
                           f"{name[:30]:30} {path:15} {fmt_dur(dur)}  "
                           f"in {fmt_bytes(bi)} out {fmt_bytes(bo)}")
        if self.events:
            out += ["", "EVENTS"]
            for t, text in list(self.events)[-12:]:
                out.append(f"  {time.strftime('%H:%M:%S', time.localtime(t))}  {text}")
        return "\n".join(out)


# --------------------------------------------------------------- the hub ---
class Hub:
    """The latest network snapshot, hardware readings and alerts, served as
    one JSON state and pushed to every open dashboard (Server-Sent Events)."""

    def __init__(self):
        self.cond = threading.Condition()
        self.parts = {"net": None, "hw": None, "alerts": [], "restart_needed": False,
                      "version": VERSION}
        self.body = b"{}"
        self.version = 0
        self.clients = 0

    def update(self, **parts):
        with self.cond:
            self.parts.update(parts)
            self.parts["now"] = time.time()
            self.body = json.dumps(self.parts, separators=(",", ":"), default=str).encode()
            self.version += 1
            self.cond.notify_all()


# ------------------------------------------------------------------- web ---
STATIC = {"favicon.svg": "image/svg+xml", "favicon.ico": "image/x-icon",
          "icon-64.png": "image/png", "icon-192.png": "image/png", "icon-512.png": "image/png"}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, certs):
        self.certs = certs
        super().__init__(addr, handler)

    def finish_request(self, request, client_address):
        """Decide per connection: TLS handshake, or plain http (redirected
        to https when HTTPS is on)."""
        ctx = self.certs.ctx if self.certs else None
        if ctx is None:
            return self.RequestHandlerClass(request, client_address, self)
        try:
            is_tls = tls.is_tls(request)
        except OSError:
            return
        if not is_tls:
            return self.RequestHandlerClass(request, client_address, self)
        try:
            wrapped = ctx.wrap_socket(request, server_side=True)
        except (OSError, ValueError):
            return          # a browser refusing our certificate lands here; that's normal
        try:
            self.RequestHandlerClass(wrapped, client_address, self)
        finally:
            try:
                wrapped.close()
            except OSError:
                pass


class WebServer:
    def __init__(self, app):
        self.app = app

    def start(self):
        app = self.app
        hub = app.hub

        class Handler(BaseHTTPRequestHandler):
            server_version = "BatoWatch"
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def _secure(self):
                return isinstance(self.connection, ssl.SSLSocket)

            def _send(self, code, ctype, body, headers=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, code, obj):
                self._send(code, "application/json",
                           json.dumps(obj, separators=(",", ":"), default=str).encode())

            def _redirect_https(self):
                host = self.headers.get("Host") or (app.env.local_ips and
                                                    next(iter(app.env.local_ips))) or "localhost"
                if ":" not in host.rsplit("]", 1)[-1]:
                    host = f"{host}:{app.cfg['WEB_PORT']}"
                self._send(301, "text/plain", b"BatoWatch uses https\n",
                           {"Location": f"https://{host}{self.path}"})

            def _authorized(self):
                auth = app.auth
                state = auth.check(self.command, self.path, self.headers, self.client_address[0])
                if state == "ok":
                    return True
                locked = auth.locked_reason()
                if locked and state == "no":
                    self._locked(locked)
                    return False
                self.send_response(401)
                for c in auth.challenges(stale=state == "stale"):
                    self.send_header("WWW-Authenticate", c)
                body = b"Login needed: user root and your Batocera password.\n"
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return False

            def _locked(self, why):
                text = ("BatoWatch's dashboard is locked: the Batocera password couldn't be "
                        f"read ({why}).\nSet LOGIN in {app.conf_path} (user:password, or none) "
                        "and restart BatoWatch.\n")
                if self.path.startswith("/api/"):
                    self._json(503, {"error": "locked", "reason": text})
                else:
                    self._send(503, "text/plain; charset=utf-8", text.encode())

            def _gate(self):
                if app.certs and app.certs.ctx and not self._secure():
                    self._redirect_https()
                    return False
                return self._authorized()

            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                path, _, query = self.path.partition("?")
                if path == "/healthz":
                    return self._send(200, "text/plain", b"ok\n")
                if path.startswith("/www/") and path[5:] in STATIC:
                    if app.certs and app.certs.ctx and not self._secure():
                        return self._redirect_https()
                    try:
                        with open(os.path.join(WWW_DIR, path[5:]), "rb") as fh:
                            return self._send(200, STATIC[path[5:]], fh.read(),
                                              {"Cache-Control": "max-age=86400"})
                    except OSError:
                        return self._send(404, "text/plain", b"not found\n")
                if not self._gate():
                    return
                q = urllib.parse.parse_qs(query)
                if path in ("/", "/index.html"):
                    try:
                        with open(os.path.join(WWW_DIR, "dashboard.html"), "rb") as fh:
                            return self._send(200, "text/html; charset=utf-8", fh.read())
                    except OSError:
                        return self._send(500, "text/plain",
                                          b"dashboard.html is missing from BatoWatch's www folder\n")
                if path == "/api/state":
                    with hub.cond:
                        body = hub.body
                    return self._send(200, "application/json", body)
                if path == "/api/events":
                    return self._stream()
                if path == "/api/hw/sources":
                    return self._json(200, app.monitor.sources_report() if app.monitor else
                                      {"available": [], "missing": []})
                if path == "/api/history":
                    db = app.net_hist if q.get("db", ["hardware"])[0] == "network" else app.hw_hist
                    if db is None:
                        return self._json(404, {"error": "that history is turned off"})
                    names = [n for n in (q.get("metrics", [""])[0]).split(",") if n][:24]
                    rng = q.get("range", ["24h"])[0]
                    games = db.events(["game"], time.time() - history_mod_ranges(rng)) \
                        if db is app.hw_hist else []
                    return self._json(200, {"range": rng, "series": db.series(names, rng),
                                            "games": games})
                if path == "/api/alerts":
                    past = app.hw_hist.events(["alert"], time.time() - 30 * 86400) \
                        if app.hw_hist else []
                    return self._json(200, {"active": app.alerts.active() if app.alerts else [],
                                            "past": past})
                if path == "/api/settings":
                    return self._json(200, app.settings_view())
                self._send(404, "text/plain", b"not found\n")

            def do_POST(self):
                path = self.path.partition("?")[0]
                if not self._gate():
                    return
                # Only the dashboard itself may change things: another web page
                # can't send JSON here without the browser asking first (and we
                # never say yes), and a browser names the page a request is from
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                origin = self.headers.get("Origin")
                if ctype != "application/json" or (
                        origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host")):
                    return self._json(403, {"error": "changes are only accepted from the dashboard"})
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    data = json.loads(self.rfile.read(min(n, 65536)) or b"{}") if n else {}
                except (ValueError, OSError):
                    return self._json(400, {"error": "bad request"})
                if not app.auth.required:
                    return self._json(403, {"error": "Settings can only be changed here while "
                                            "the dashboard login is on. Use batowatch.conf."})
                if path == "/api/settings":
                    return self._json(*app.save_settings(data))
                if path == "/api/restart":
                    self._json(200, {"ok": True})
                    app.restart_soon("asked from the dashboard")
                    return
                self._send(404, "text/plain", b"not found\n")

            def _stream(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                seen = -1
                with hub.cond:
                    hub.clients += 1
                try:
                    while not app.stopping:
                        with hub.cond:
                            if hub.version == seen:
                                hub.cond.wait(timeout=15)
                            version, body = hub.version, hub.body
                        if version == seen:
                            self.wfile.write(b": keepalive\n\n")
                        else:
                            seen = version
                            self.wfile.write(b"data: " + body + b"\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                finally:
                    with hub.cond:
                        hub.clients -= 1

        httpd = Server((app.cfg["WEB_BIND"], app.cfg["WEB_PORT"]), Handler, app.certs)
        threading.Thread(target=httpd.serve_forever, daemon=True, name="web").start()
        return httpd


def history_mod_ranges(rng):
    from history import RANGES
    return RANGES.get(rng, 86400)


# -------------------------------------------------------------- notifier ---
class Notifier:
    """overlay: X11 banner (overlay.py, own process), falling back to the ES
    popup if it can't start. es: ES popup only. print: log line only.
    Colours, position and durations are read from the settings each time, so
    a change applies to the next banner."""

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log

    def __call__(self, text, kind="connect"):
        self.log(f"* NOTIFY: {text}")
        mode = self.cfg["NOTIFY"]
        if mode == "es":
            threading.Thread(target=self._es, args=(text,), daemon=True).start()
        elif mode == "overlay":
            threading.Thread(target=self._overlay, args=(text, kind), daemon=True).start()

    def _overlay(self, text, kind):
        c = self.cfg
        alert = kind == "alert"
        duration = c["ALERT_DURATION"] if alert else c["OVERLAY_DURATION"]
        if not os.path.exists(OVERLAY_SCRIPT):
            self.log("! overlay.py not found next to batowatch.py; using ES popup")
            return self._es(text)
        env = dict(os.environ, DISPLAY=c["DISPLAY"])
        cmd = [sys.executable, OVERLAY_SCRIPT, "--duration", str(duration),
               "--style", c["OVERLAY_STYLE"], "--scale", str(c["OVERLAY_SCALE"]),
               "--position", c["OVERLAY_POSITION"],
               "--caption", "NOTICE" if alert else "SUNSHINE",
               "--accent", c["ALERT_ACCENT"] if alert else c["OVERLAY_ACCENT"],
               "--fg", c["OVERLAY_FG"], "--bg", c["OVERLAY_BG"],
               "--outline", c["OVERLAY_OUTLINE"], text]
        try:
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE)
        except OSError as e:
            self.log(f"! overlay failed to start: {e}; using ES popup")
            return self._es(text)
        try:
            _out, err = proc.communicate(timeout=duration + 10)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.log("! overlay hung and was killed")
            return
        if proc.returncode != 0:
            msg = (err or b"").decode(errors="replace").strip().splitlines()
            self.log(f"! overlay exit {proc.returncode} ({msg[-1] if msg else 'no output'})"
                     "; using ES popup")
            self._es(text)

    def _es(self, text):
        try:
            c = http.client.HTTPConnection("127.0.0.1", 1234, timeout=3)
            c.request("POST", "/notify", body=text.encode(),
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
            c.getresponse().read()
            c.close()
        except (OSError, http.client.HTTPException) as e:
            self.log(f"! ES notify failed: {e}")


# ------------------------------------------------------------------ setup ---
def read_sysctl(name):
    try:
        with open(f"/proc/sys/net/netfilter/{name}") as fh:
            return fh.read().strip()
    except OSError:
        return None


def flow_source(env):
    """PacketFlows when bw-flows is installed, else None (conntrack).
    bw-flows is used whenever it's there, so a box behaves the same whether
    or not something else happens to have switched connection tracking on
    (on Batocera 42 that was Tailscale's firewall rules, added after boot).
    BATOWATCH_FLOWS=conntrack forces the old source."""
    if os.environ.get("BATOWATCH_FLOWS") == "conntrack":
        LOG.info("network: connection tracking (BATOWATCH_FLOWS=conntrack)")
        return None
    if not os.access(FLOWS_HELPER, os.X_OK):
        LOG.warning(f"network: {FLOWS_HELPER} is missing, so using connection tracking; "
                    "on kernels where nothing switches it on, the Network tab stays empty")
        return None
    LOG.info(f"network: counting packets with {FLOWS_HELPER}")
    return PacketFlows(FLOWS_HELPER, env)


def prepare_kernel():
    if not os.path.isdir("/sys/module/nf_conntrack_netlink"):
        try:
            subprocess.run(["modprobe", "nf_conntrack_netlink"], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as e:
            LOG.warning(f"could not load nf_conntrack_netlink: {e}")
    for name in ("nf_conntrack_acct", "nf_conntrack_timestamp", "nf_conntrack_events"):
        if read_sysctl(name) == "1":
            continue
        try:
            with open(f"/proc/sys/net/netfilter/{name}", "w") as fh:
                fh.write("1")
            LOG.info(f"enabled {name}")
        except OSError as e:
            LOG.warning(f"could not enable {name}: {e}")


def setup_logging(log_file, max_bytes, keep):
    """Log to a size-rotated file, or to stderr when log_file is None.
    Uncaught exceptions in any thread are logged too, so a crash leaves its
    traceback in the rotated log rather than an unbounded service log."""
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        # maxBytes=0 means never rotate; backupCount is >= 1 (checked in settings)
        handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=keep, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    LOG.addHandler(handler)

    def crash(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return sys.__excepthook__(exc_type, exc, tb)
        LOG.critical("BatoWatch crashed", exc_info=(exc_type, exc, tb))

    sys.excepthook = crash
    threading.excepthook = lambda a: LOG.error(
        f"thread {a.thread.name if a.thread else '?'} crashed",
        exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


def parse_args():
    ap = argparse.ArgumentParser(description="BatoWatch: network and hardware monitor for "
                                 "Batocera")
    ap.add_argument("--conf", default=os.path.join(BATOWATCH_HOME, "batowatch.conf"),
                    help="settings file (one KEY=value per line)")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="override a setting for this run (repeatable; for tests)")
    ap.add_argument("--no-tui", action="store_true",
                    help="no terminal screen; events go to the log (for the service)")
    ap.add_argument("--log-file", default=None,
                    help="log here instead of the LOG setting ('-' = stderr)")
    ap.add_argument("--no-flows", dest="flows", action="store_false",
                    help="terminal view: hide per-flow port detail")
    ap.add_argument("--raw-flows", action="store_true",
                    help="terminal view: one line per connection instead of grouping by service")
    ap.add_argument("--once", action="store_true", help="one network snapshot and exit")
    args = ap.parse_args()
    overrides = {}
    for item in args.set:
        k, sep, v = item.partition("=")
        if not sep or k not in settings_mod.BY_KEY:
            ap.error(f"--set {item}: not KEY=VALUE with a known KEY")
        overrides[k] = v
    args.overrides = overrides
    return args


class App:
    def __init__(self, args):
        self.args = args
        self.conf_path = args.conf
        self.cfg = settings_mod.Settings(args.conf, args.overrides)
        self.start_values = dict(self.cfg.values)
        self.hub = Hub()
        self.stopping = False
        self.stop_hooks = []      # called on shutdown (e.g. stop bw-flows)
        self.env = self.tracker = self.monitor = self.alerts = None
        self.hw_hist = self.net_hist = None
        self.certs = None
        self.auth = None
        self.restart_reason = None
        hist_dir = self.cfg["HISTORY_DIR"] or os.path.join(BATOWATCH_HOME, "history")
        self.hist_dir = hist_dir
        # The tracker and Env still read their options from an argparse-like
        # object, as in NetWatch; these are all restart settings.
        c = self.cfg
        self.nargs = argparse.Namespace(
            subnet_router=list(c["SUBNET_ROUTER"]), sunshine_conf=c["SUNSHINE_CONF"],
            ts_socket=c["TS_SOCKET"], aliases=os.path.join(BATOWATCH_HOME, "aliases.conf"),
            wg_port=c["WG_PORT"], show_gateway=c["SHOW_GATEWAY"],
            show_transport=c["SHOW_TRANSPORT"], inbound_only=c["INBOUND_ONLY"],
            no_tui=args.no_tui, log_file=self.log_file(), flows=args.flows,
            raw_flows=args.raw_flows, interval=c["INTERVAL"])

    def log_file(self):
        if self.args.log_file == "-":
            return None
        return self.args.log_file or self.cfg["LOG"]

    def keep(self):
        c = self.cfg
        return {"raw": settings_mod.parse_duration(c["KEEP_RAW"]),
                "minute": settings_mod.parse_duration(c["KEEP_MINUTE"]),
                "hour": settings_mod.parse_duration(c["KEEP_HOUR"])}

    def unit(self):
        return hardware.unit_for(self.cfg["TEMP_UNIT"])

    # -- settings ------------------------------------------------------------
    def settings_view(self):
        login = self.cfg["LOGIN"]
        return {"version": VERSION, "mtime": str(self.cfg.mtime), "conf": self.conf_path,
                "settings": self.cfg.describe(), "unknown": self.cfg.unknown,
                "editable": self.auth.required if self.auth else False,
                "login": "batocera" if login == "batocera" else "none" if login == "none"
                else "own", "restart_needed": self.restart_needed(),
                "unit": self.unit(), "timezone": hardware.timezone_name(),
                "certs": self.certs.status if self.certs else ""}

    def restart_needed(self):
        return [k for k, v in self.cfg.values.items()
                if not settings_mod.BY_KEY[k].live and self.start_values.get(k) != v]

    def save_settings(self, data):
        changes = data.get("changes") or {}
        if not isinstance(changes, dict) or not all(isinstance(v, (str, int, float, bool))
                                                    for v in changes.values()):
            return 400, {"error": "changes must be {KEY: value}"}
        changes = {k: settings_mod.to_text(settings_mod.BY_KEY[k], v)
                   if isinstance(v, bool) and k in settings_mod.BY_KEY else str(v)
                   for k, v in changes.items()}
        if not changes:                          # nothing to write
            return 200, self.settings_view()
        try:
            restart, errors = self.cfg.save(changes, data.get("mtime"))
        except settings_mod.StaleConf:
            return 409, {"error": "The settings file changed; reload to see the new values."}
        except OSError as e:
            return 500, {"error": f"couldn't write {self.conf_path}: {e}"}
        if errors:
            return 400, {"error": "Some values aren't valid.", "fields": errors}
        self.apply_live()
        LOG.info(f"settings changed from the dashboard: {', '.join(sorted(changes))}")
        if restart:
            self.restart_soon("a setting that needs a restart was changed")
        return 200, {"ok": True, "restart": restart, "mtime": str(self.cfg.mtime)}

    def apply_live(self):
        if self.tracker:
            self.tracker.NOTIFY_COOLDOWN = self.cfg["CONNECT_REPEAT"]
        if self.auth and self.auth.mode != self.cfg["LOGIN"]:
            self.auth.set_mode(self.cfg["LOGIN"])
        for h in (self.hw_hist, self.net_hist):
            if h:
                h.keep = self.keep()
        self.hub.update(restart_needed=self.restart_needed())

    def watch_conf(self):
        while not self.stopping:
            time.sleep(10)
            try:
                if self.cfg.changed_on_disk():
                    changed = self.cfg.load()
                    if changed:
                        LOG.info(f"settings file changed: {', '.join(changed)}")
                    for k, e in self.cfg.errors.items():
                        LOG.warning(f"settings: {e['error']}; using the default")
                    self.apply_live()
            except Exception as e:
                LOG.warning(f"settings: couldn't reload {self.conf_path}: {e}")

    def restart_soon(self, why):
        if self.restart_reason:
            return
        self.restart_reason = why

        def go():
            time.sleep(1)          # let the reply reach the browser
            LOG.info(f"restarting: {why}")
            self.shutdown()
            os._exit(RESTART_CODE)
        threading.Thread(target=go, daemon=True).start()

    def shutdown(self):
        self.stopping = True
        for stop in self.stop_hooks:
            try:
                stop()
            except Exception:
                pass
        for h in (self.hw_hist, self.net_hist):
            if h:
                try:
                    h.flush()
                    h.close()
                except Exception:
                    pass

    # -- hardware ----------------------------------------------------------------
    def self_extra(self):
        size = sum(h.size() for h in (self.hw_hist, self.net_hist) if h)
        return {"db_bytes": size, "connections": self.hub.clients}

    def hardware_loop(self):
        c = self.cfg
        interval = c["HW_INTERVAL"]
        game_event, last_flush = None, time.time()
        while not self.stopping:
            t0 = time.time()
            try:
                readings = self.monitor.sample(t0)
                values, units, labels = {}, {}, {}
                for r in readings.values():
                    if r.info.get("no_history") or not isinstance(r.value, (int, float)):
                        continue
                    values[r.name], units[r.name], labels[r.name] = r.value, r.unit, r.label
                self.hw_hist.add(t0, values, units, labels)
                g = readings.get("game.running")
                running = bool(g and g.value)
                if running and game_event is None:
                    game_event = self.hw_hist.event_start(
                        "game", t0, g.info.get("system"),
                        f"{g.info.get('name')} ({g.info.get('system')})")
                elif not running and game_event is not None:
                    self.hw_hist.event_end(game_event, t0)
                    game_event = None
                unit = self.unit()
                self.alerts.check(readings, c, unit, t0)
                if t0 - last_flush >= FLUSH:
                    self.hw_hist.flush(t0)
                    last_flush = t0
                self.hub.update(hw=self.hw_state(readings, unit, t0),
                                alerts=self.alerts.active())
            except Exception as e:
                LOG.error(f"hardware loop: {e}", exc_info=True)
            time.sleep(max(0.5, interval - (time.time() - t0)))

    def hw_state(self, readings, unit, now):
        rs = [{"name": r.name, "value": r.value, "unit": r.unit, "label": r.label,
               "group": r.group, "info": r.info} for r in readings.values()]
        cpu_t = hardware.main_cpu_temp(readings)
        gpu_t = hardware.main_gpu_temp(readings)
        busy = [r.value for r in readings.values() if r.name.endswith((".busy", ".engine_busy"))]
        power = readings.get("power.rapl.intel-rapl_0") or next(
            (r for r in readings.values() if r.group == "power"), None)
        disk_t = [r for r in readings.values() if r.group == "temp" and
                  r.info.get("kind") == "disk"]
        summary = {
            "cpu": readings["cpu.usage"].value if "cpu.usage" in readings else None,
            "cpu_temp": cpu_t.name if cpu_t else None,
            "gpu_temp": gpu_t.name if gpu_t else None,
            "mem": readings["mem.used_pct"].value if "mem.used_pct" in readings else None,
            "power": power.name if power else None,
            "gpu": max(busy) if busy else None,
            "disk_temp": max(disk_t, key=lambda r: r.value).name if disk_t else None,
        }
        return {"t": now, "unit": unit, "readings": rs, "summary": summary,
                "limits": self.alerts.limits, "sources": self.monitor.sources_report(),
                "interval": self.cfg["HW_INTERVAL"]}

    # -- network history ------------------------------------------------------------
    def net_hook(self, open_events):
        def hook(kind, phase, ip, name):
            key = f"{kind}:{ip}"
            if phase == "start" and key not in open_events:
                open_events[key] = self.net_hist.event_start(kind, time.time(), ip, name)
            elif phase == "end" and key in open_events:
                self.net_hist.event_end(open_events.pop(key), time.time())
        return hook

    def record_traffic(self, now):
        values, labels, units = {}, {}, {}
        for d in self.tracker.devices.values():
            route = self.env.classify(d.ip)
            if route == "internet":
                continue
            name = self.env.name_for(d.ip)[0]
            for k, v in (("in", d.rate_in), ("out", d.rate_out)):
                m = f"dev:{d.ip}:{k}"
                values[m], units[m] = round(v), "B/s"
                labels[m] = f"{name} {'download' if k == 'in' else 'upload'}"
        self.net_hist.add(now, values, units, labels)


def main():
    args = parse_args()
    app = App(args)
    c = app.cfg
    setup_logging(app.log_file(), c["LOG_MAX_BYTES"], c["LOG_KEEP"])
    LOG.info(f"BatoWatch {VERSION} starting")
    for k, e in c.errors.items():
        LOG.warning(f"settings: {e['error']}; using the default")
    if os.geteuid() != 0:
        LOG.critical("BatoWatch must run as root (conntrack over netlink needs CAP_NET_ADMIN, "
                     "and the password and power counters are root-only)")
        sys.exit(1)
    os.makedirs(BATOWATCH_HOME, exist_ok=True)
    if not c["NETWORK"] and not c["HARDWARE"]:
        LOG.warning("both NETWORK and HARDWARE are off; only the dashboard runs")

    app.env = Env(app.nargs)
    app.env.refresh(force=True)
    log = lambda text: (app.tracker.log(text) if app.tracker else LOG.info(text))  # noqa: E731
    notifier = Notifier(c, log)

    dump_flows, packets = ct_dump, None
    if c["NETWORK"]:
        app.tracker = Tracker(app.env, app.nargs, notifier)
        app.tracker.NOTIFY_COOLDOWN = c["CONNECT_REPEAT"]
        packets = flow_source(app.env)
        if packets:
            dump_flows = packets.dump
            app.tracker.source = "packets"
            app.stop_hooks.append(packets.stop)
            packets.ready.wait(3)
        else:
            prepare_kernel()
        app.net_hist = History(os.path.join(app.hist_dir, "network.db"), app.keep())
        app.net_hist.close_open_events(time.time())
        app.tracker.hook = app.net_hook({})
        try:
            dump = dump_flows()
        except OSError as e:
            LOG.critical(f"conntrack dump failed: {e}")
            sys.exit(1)
        with app.tracker.lock:
            app.tracker.reconcile(dump, time.time())
            app.tracker.bootstrapping = False
        if args.once:
            time.sleep(0.5)  # give reverse DNS a moment
            print(app.tracker.render(time.time()))
            return

    if c["HARDWARE"]:
        app.hw_hist = History(os.path.join(app.hist_dir, "hardware.db"), app.keep())
        app.hw_hist.close_open_events(time.time())
        app.alerts = alerts_mod.Alerts(app.hw_hist, notifier)
        app.monitor = hardware.Monitor(c["HW_SYSFS_ROOT"], set(c["HW_SKIP"]),
                                       c["SMART_INTERVAL"], app.self_extra)
        for m in app.monitor.sources_report()["missing"]:
            LOG.info(f"hardware: {m['title']}: not available ({m['reason']})")
        threading.Thread(target=app.hardware_loop, daemon=True, name="hardware").start()

    def stop(_sig, _frame):
        app.shutdown()
        os._exit(0)
    signal.signal(signal.SIGTERM, stop)

    app.auth = webauth.Auth(c["LOGIN"], c["PASSWORD_CMD"],
                            os.path.join(BATOWATCH_HOME, "tv.token"))
    if c["WEB_PORT"]:
        if c["HTTPS"] != "off":
            app.certs = tls.Certs(c["HTTPS"], os.path.join(BATOWATCH_HOME, "certs"), app.env.ts)
            try:
                app.certs.prepare(list(app.env.local_ips))
            except (OSError, ssl.SSLError, subprocess.SubprocessError) as e:
                LOG.error(f"https: {e}; the dashboard uses plain http for now")
                app.certs = None
        try:
            WebServer(app).start()
            scheme = "https" if app.certs else "http"
            log(f"~ dashboard on {scheme}://{c['WEB_BIND']}:{c['WEB_PORT']}/")
        except OSError as e:
            log(f"! dashboard could not start on port {c['WEB_PORT']}: {e}")
    threading.Thread(target=app.watch_conf, daemon=True, name="settings").start()

    def cert_check():
        # New LAN IP or tailnet name, or a certificate about to run out: make
        # a new one (checked hourly)
        while app.certs and not app.stopping:
            time.sleep(3600)
            try:
                app.env.refresh(force=True)
                app.certs.prepare(list(app.env.local_ips))
            except Exception as e:
                LOG.warning(f"https: certificate check failed: {e}")
    threading.Thread(target=cert_check, daemon=True, name="certs").start()

    if not c["NETWORK"]:
        while True:
            time.sleep(3600)

    def event_loop():
        while True:
            try:
                ct_events(app.tracker.on_event)
            except OSError as e:
                with app.tracker.lock:
                    app.tracker.log(f"! event socket error: {e}; retrying")
                time.sleep(5)

    if not packets:
        threading.Thread(target=event_loop, daemon=True, name="conntrack").start()
    tty = sys.stdout.isatty()
    tracker, env = app.tracker, app.env
    last_traffic = last_flush = time.time()
    while True:
        time.sleep(app.nargs.interval)
        env.refresh()
        try:
            dump = dump_flows()
        except OSError as e:
            with tracker.lock:
                tracker.log(f"! dump failed: {e}")
            continue
        now = time.time()
        with tracker.lock:
            tracker.reconcile(dump, now)
            tracker.tick(now)
            snap = tracker.snapshot(now)
            if now - last_traffic >= 60:
                app.record_traffic(now)
                last_traffic = now
            screen = None if args.no_tui else tracker.render(now)
        if now - last_flush >= FLUSH:
            app.net_hist.flush(now)
            last_flush = now
        app.hub.update(net=snap)
        if screen is not None:
            print(("\x1b[H\x1b[2J" if tty else "\n") + screen, flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
