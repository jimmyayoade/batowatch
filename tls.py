"""
HTTPS for the BatoWatch dashboard.

HTTPS=self (default): BatoWatch's own certificate, made with `openssl`,
covering every name and address the box can be opened by (hostname, LAN IPs,
Tailscale IPs and tailnet name). Browsers warn once per address. It's made
again when one of those names changes, and before it runs out (825 days,
the longest Apple devices accept).

HTTPS=auto adds a real Tailscale certificate for the tailnet name (asked
from tailscaled's local API), chosen per connection by the name the browser
asked for (SNI). HTTPS=tailscale uses only that. Both publish the tailnet
name in public Certificate Transparency logs.

The first byte of every connection says which it is (a TLS handshake starts
with 0x16), so plain http:// requests on the same port get a redirect.
"""
import json
import logging
import os
import socket
import ssl
import subprocess
import time

LOG = logging.getLogger("batowatch")
CERT_DAYS = 825
RENEW_BEFORE = 30 * 86400
TS_RENEW_AGE = 30 * 86400


def _run(cmd, timeout=30):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def cert_not_after(path):
    try:
        out = _run(["openssl", "x509", "-noout", "-enddate", "-in", path], 10).stdout
        return ssl.cert_time_to_seconds(out.strip().split("=", 1)[1])
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return 0


class Certs:
    def __init__(self, mode, cert_dir, ts_client):
        self.mode, self.dir, self.ts = mode, cert_dir, ts_client
        self.self_cert = os.path.join(cert_dir, "self.crt")
        self.self_key = os.path.join(cert_dir, "self.key")
        self.names_file = os.path.join(cert_dir, "self.names")
        self.ts_cert = os.path.join(cert_dir, "tailscale.crt")
        self.ts_key = os.path.join(cert_dir, "tailscale.key")
        self.ts_name = None
        self.ctx = None
        self.ts_ctx = None
        self.status = ""

    # -- names -----------------------------------------------------------------
    def wanted_names(self, local_ips):
        host = socket.gethostname()
        dns = {host, f"{host}.local", "localhost"}
        ips = {"127.0.0.1", "::1"}
        ips.update(ip for ip in local_ips if not ip.startswith("fe80"))
        try:
            st = self.ts.status()
            me = st.get("Self") or {}
            name = (me.get("DNSName") or "").rstrip(".")
            if name:
                dns.add(name)
                self.ts_name = name
            ips.update(me.get("TailscaleIPs") or [])
        except Exception:
            pass
        return sorted(dns), sorted(ips)

    # -- self-signed -----------------------------------------------------------
    def ensure_self(self, local_ips):
        dns, ips = self.wanted_names(local_ips)
        want = "\n".join(dns + ips)
        try:
            with open(self.names_file) as fh:
                have = fh.read()
        except OSError:
            have = None
        fresh = (os.path.exists(self.self_cert) and os.path.exists(self.self_key)
                 and have == want
                 and cert_not_after(self.self_cert) - time.time() > RENEW_BEFORE)
        if fresh:
            return False
        os.makedirs(self.dir, exist_ok=True)
        san = ",".join([f"DNS:{d}" for d in dns] + [f"IP:{i}" for i in ips])
        cmd = ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
               "ec_paramgen_curve:prime256v1", "-nodes", "-days", str(CERT_DAYS),
               "-subj", f"/CN={dns[0] if dns else 'batowatch'}/O=BatoWatch",
               "-addext", f"subjectAltName={san}",
               "-addext", "basicConstraints=critical,CA:FALSE",
               "-addext", "extendedKeyUsage=serverAuth",
               "-keyout", self.self_key + ".tmp", "-out", self.self_cert + ".tmp"]
        old_umask = os.umask(0o077)
        try:
            r = _run(cmd, 60)
        finally:
            os.umask(old_umask)
        if r.returncode != 0:
            raise OSError(f"openssl couldn't make a certificate: {r.stderr.strip()[-200:]}")
        os.replace(self.self_key + ".tmp", self.self_key)
        os.replace(self.self_cert + ".tmp", self.self_cert)
        with open(self.names_file, "w") as fh:
            fh.write(want)
        LOG.info(f"https: made a new certificate for {', '.join(dns + ips)} "
                 f"(browsers will warn once for it)")
        return True

    # -- tailscale ---------------------------------------------------------------
    def ensure_tailscale(self):
        if not self.ts_name:
            return False
        try:
            age = time.time() - os.stat(self.ts_cert).st_mtime
            if age < TS_RENEW_AGE:
                return False
        except OSError:
            pass
        try:
            pem = self.ts.cert_pair(self.ts_name)
        except Exception as e:
            self.status = f"Tailscale certificate unavailable: {e}"
            LOG.warning(f"https: {self.status}")
            return False
        key_start = pem.find("-----BEGIN")
        cert_start = pem.find("-----BEGIN CERTIFICATE")
        if key_start < 0 or cert_start < 0:
            return False
        key, cert = pem[:cert_start], pem[cert_start:]
        os.makedirs(self.dir, exist_ok=True)
        fd = os.open(self.ts_key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(key)
        with open(self.ts_cert, "w") as fh:
            fh.write(cert)
        LOG.info(f"https: got a Tailscale certificate for {self.ts_name}")
        return True

    # -- contexts ----------------------------------------------------------------
    def _context(self, cert, key):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
        return ctx

    def prepare(self, local_ips):
        """Make or refresh certificates; returns True when the contexts changed."""
        if self.mode == "off":
            return False
        changed = False
        if self.mode in ("self", "auto"):
            changed |= self.ensure_self(local_ips)
        else:
            self.wanted_names(local_ips)
        if self.mode in ("auto", "tailscale"):
            changed |= self.ensure_tailscale()
        if self.ctx is None or changed:
            self._build()
        return changed

    def _build(self):
        have_ts = os.path.exists(self.ts_cert) and os.path.exists(self.ts_key)
        ts_ctx = self._context(self.ts_cert, self.ts_key) if have_ts else None
        if self.mode == "tailscale":
            if ts_ctx is None:
                # No Tailscale certificate yet: fall back to our own rather than
                # leaving the dashboard unreachable.
                self.ensure_self([])
                ctx = self._context(self.self_cert, self.self_key)
            else:
                ctx = ts_ctx
        else:
            ctx = self._context(self.self_cert, self.self_key)
        if ts_ctx is not None and self.mode == "auto":
            ts_name = self.ts_name

            def pick(sock, server_name, _ctx):
                if server_name and ts_name and server_name.lower() == ts_name.lower():
                    sock.context = ts_ctx
            ctx.sni_callback = pick
        self.ctx, self.ts_ctx = ctx, ts_ctx


def is_tls(sock, timeout=5):
    """Peek at the first byte without consuming it."""
    sock.settimeout(timeout)
    try:
        b = sock.recv(1, socket.MSG_PEEK)
    finally:
        sock.settimeout(None)
    return b[:1] == b"\x16"


def tailscale_cert_pair(sock_path, domain):
    """PEM key + certificate from tailscaled's local API."""
    import http.client

    class Conn(http.client.HTTPConnection):
        def connect(conn):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(90)
            s.connect(sock_path)
            conn.sock = s

    c = Conn("local-tailscaled.sock", timeout=90)
    try:
        c.request("GET", f"/localapi/v0/cert/{domain}?type=pair",
                  headers={"Host": "local-tailscaled.sock", "Sec-Tailscale": "localapi"})
        r = c.getresponse()
        body = r.read().decode(errors="replace")
        if r.status != 200:
            try:
                body = json.loads(body).get("error", body)
            except ValueError:
                pass
            raise OSError(f"HTTP {r.status}: {body.strip()[:200]}")
        return body
    finally:
        c.close()
